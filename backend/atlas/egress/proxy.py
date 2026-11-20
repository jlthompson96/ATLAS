"""atlas-egress: HTTP CONNECT proxy, one loopback listener per caller.

Security properties (each is covered by a test in tests/test_egress_proxy.py):

1. The listening port identifies the caller; nftables lets only that caller's Unix user reach it.
2. Only ``CONNECT host:443``; every other method, port or malformed head is refused.
3. The host must be on that caller's exact-match allowlist. IP literals are refused.
4. The host is resolved once, here. Every answer must be a public unicast address, otherwise the
   whole request is refused (DNS rebinding). The tunnel connects to one of the addresses that
   were checked, by IP, never by name.
5. After our ``200``, the first TLS record must be a well-formed ClientHello whose SNI equals the
   CONNECT host. Nothing is sent upstream before that check passes.
6. Every decision is written to atlas-audit before any upstream byte is sent. If the audit
   service cannot take the record, the tunnel is closed (fail closed). Denials are audited too.
7. Bounded everything: head size and count, one total deadline per phase, per-caller and global
   connection limits, idle timeout while tunnelling, and a per-caller rate of tunnel opens (429) kept
   well below the egress audit budget, so one caller cannot starve the others (known issue N3).

Nothing in a log or an audit record contains request headers, bodies or payload bytes. Only the
caller, the (sanitized) target host, port, decision, a stable reason/code and byte counts.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from atlas.audit.client import AuditError
from atlas.egress.addresses import IPAddress, IPNetwork, is_public_address, normalize
from atlas.egress.config import HTTPS_PORT, CallerPolicy, EgressConfig, looks_like_ip_literal
from atlas.egress.http import ConnectRequest, RequestError, parse_connect, read_head
from atlas.egress.tls import TlsError, parse_client_hello_sni, read_client_hello

log = logging.getLogger("atlas.egress")

STATUS_TEXT: Final = {
    400: "Bad Request",
    403: "Forbidden",
    405: "Method Not Allowed",
    408: "Request Timeout",
    429: "Too Many Requests",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
    503: "Service Unavailable",
}
MAX_ANSWERS: Final = 64  # resolver answers examined
MAX_ADDRESSES: Final = 8  # addresses we would try to connect to
CHUNK: Final = 16 * 1024


class AuditSink(Protocol):
    async def append(self, record: dict[str, Any]) -> object: ...


Resolver = Callable[[str], Awaitable[list[str]]]
Connector = Callable[[IPAddress, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


@dataclass(frozen=True)
class Limits:
    head_timeout_s: float = 10.0
    hello_timeout_s: float = 10.0
    resolve_timeout_s: float = 5.0
    connect_timeout_s: float = 10.0
    idle_timeout_s: float = 600.0  # Discord's gateway heartbeats every ~41 s, so 10 minutes is generous
    max_total: int = 64
    max_per_caller: int = 16
    # CONNECT requests per caller (token bucket), refused with 429 beyond it (known issue N3). Each allowed tunnel costs
    # two audit records, so a caller's sustained maximum is ~5 records/s (with denials) against atlas-egress's audit
    # budget of 30/s (audit/server.py ACTOR_BUDGETS). Honest peaks (a SearXNG fan-out, a Gmail sync) fit in the burst.
    open_burst: float = 30.0
    open_rate_per_s: float = 2.0
    # Denial audit records are rate limited per caller (token bucket); what is suppressed is counted and written
    # as one aggregate record per caller and reason every ``suppress_flush_interval_s`` (review F5).
    denial_burst: float = 10.0
    denial_rate_per_s: float = 1.0
    suppress_flush_interval_s: float = 60.0


async def system_resolver(host: str) -> list[str]:
    """Resolve through the system resolver (the LAN resolver, PLAN 5.7). Called once per request."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, HTTPS_PORT, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    seen: list[str] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        address = str(sockaddr[0])
        if address not in seen:
            seen.append(address)
    return seen


async def tcp_connector(ip: IPAddress, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(str(ip), port)  # an address literal: no second DNS lookup


class Denied(Exception):
    """A request was refused. ``code`` is the audit ``result.code``; ``reason`` the schema ``block_reason``."""

    def __init__(self, code: str, *, reason: str | None, status: int = 403, result: str = "denied") -> None:
        super().__init__(code)
        self.code = code
        self.reason = reason
        self.status = status
        self.result = result


REQUEST_REASONS: Final = {
    "METHOD_NOT_ALLOWED": "method",
    "HEADER_TIMEOUT": "limit",
}  # every other request-shape refusal (bad line, oversized head, too many headers, bad target) is malformed_request
_TLS_REASONS: Final = {
    "SNI_MISSING": "sni_mismatch",
    "SNI_INVALID": "sni_mismatch",
    "TLS_INVALID": "sni_mismatch",
    "TLS_UNSUPPORTED": "sni_mismatch",
    "TLS_TIMEOUT": "limit",
    "ECH_REFUSED": "sni_mismatch",  # the real SNI is hidden from us
    "DUPLICATE_EXTENSION": "sni_mismatch",
    "CLIENT_RESET": "sni_mismatch",
}


def audit_host(host: str | None) -> str:
    """The target as it may appear in an audit record: a plain hostname/IP string or ``-``."""
    if host is None or not 0 < len(host) <= 253 or not all(c.isascii() and (c.isalnum() or c in ".-:") for c in host):
        return "-"
    return host


def egress_record(
    caller: str,
    host: str | None,
    port: int | None,
    *,
    decision: str,
    status: str,
    code: str | None = None,
    reason: str | None = None,
    bytes_in: int | None = None,
    bytes_out: int | None = None,
) -> dict[str, Any]:
    egress: dict[str, Any] = {"caller": caller, "mode": "connect", "host": audit_host(host), "decision": decision}
    if port is not None and 1 <= port <= 65535:
        egress["port"] = port
    if reason is not None:
        egress["block_reason"] = reason
    if bytes_in is not None:
        egress["bytes_in"] = bytes_in
    if bytes_out is not None:
        egress["bytes_out"] = bytes_out
    result: dict[str, Any] = {"status": status}
    if code is not None:
        result["code"] = code
    return {"kind": "egress", "context": "system", "egress": egress, "result": result}


class EgressProxy:
    def __init__(
        self,
        config: EgressConfig,
        audit: AuditSink,
        *,
        resolver: Resolver = system_resolver,
        connector: Connector = tcp_connector,
        limits: Limits = Limits(),
        ports: dict[str, int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        extra_forbidden: tuple[IPNetwork, ...] = (),
    ) -> None:
        self._extra_forbidden = extra_forbidden
        self._config = config
        self._audit = audit
        self._resolve = resolver
        self._connect = connector
        self._limits = limits
        self._ports = ports  # overrides the configured ports (tests bind port 0)
        self._monotonic = monotonic
        self._servers: list[asyncio.Server] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._active: dict[str, int] = dict.fromkeys(config.callers, 0)
        self._buckets: dict[str, tuple[float, float]] = {}  # caller -> (tokens, last refill), audited denials
        self._open_buckets: dict[str, tuple[float, float]] = {}  # caller -> (tokens, last refill), tunnel opens
        # Records not written individually, per (caller, decision, block_reason): denials over the audit rate, and any
        # best-effort record the audit service refused. Written as one aggregate each by flush_suppressed().
        self._suppressed: dict[tuple[str, str, str | None], _Tally] = {}
        self._flusher: asyncio.Task[None] | None = None
        self.bound_ports: dict[str, int] = {}

    # ---- lifecycle ---------------------------------------------------------------------------------

    async def start(self) -> dict[str, int]:
        try:
            for caller, policy in self._config.callers.items():
                port = self._ports[caller] if self._ports is not None else policy.port
                handler = self._make_handler(policy)
                server = await asyncio.start_server(handler, self._config.listen_host, port, backlog=32)
                self._servers.append(server)
                self.bound_ports[caller] = server.sockets[0].getsockname()[1]
        except BaseException:
            await self.close()
            raise
        self._flusher = asyncio.create_task(self._flush_loop())
        return dict(self.bound_ports)

    async def close(self) -> None:
        """Stop listening, drop every open tunnel, then wait for the handlers to finish."""
        if self._flusher is not None:
            self._flusher.cancel()
            await asyncio.gather(self._flusher, return_exceptions=True)
            self._flusher = None
        for server in self._servers:
            server.close()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for server in self._servers:
            await server.wait_closed()
        self._servers.clear()
        await self.flush_suppressed()

    def _make_handler(
        self, policy: CallerPolicy
    ) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._on_connection(policy, reader, writer)

        return handler

    # ---- one connection ----------------------------------------------------------------------------

    async def _on_connection(
        self, policy: CallerPolicy, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        caller = policy.caller
        if sum(self._active.values()) >= self._limits.max_total or self._active[caller] >= self._limits.max_per_caller:
            await self._refuse_busy(caller, writer)
            return
        self._active[caller] += 1
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await self._session(policy, reader, writer)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("egress: unexpected error for %s", caller)  # no request data in the message
        finally:
            self._active[caller] -= 1
            if task is not None:
                self._tasks.discard(task)
            await _close(writer)

    async def _refuse_busy(self, caller: str, writer: asyncio.StreamWriter) -> None:
        await _reply_status(writer, 503)
        record = egress_record(
            caller, None, None, decision="blocked", status="refused", code="TOO_MANY_CONNECTIONS", reason="limit"
        )
        await self._audit_denial(caller, record)
        await _close(writer)

    async def _session(self, policy: CallerPolicy, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        caller = policy.caller
        host: str | None = None
        port: int | None = None
        tunnel_open = False
        try:
            try:
                request = parse_connect(await read_head(reader, self._limits.head_timeout_s))
            except RequestError as exc:
                raise Denied(
                    exc.code,
                    reason=REQUEST_REASONS.get(exc.code, "malformed_request"),
                    status=exc.status,
                    result="refused",
                ) from None
            host, port = request.host, request.port
            if not self._take_open_token(caller):
                raise Denied("RATE_LIMITED", reason="limit", status=429, result="refused")
            self._check_policy(policy, request)
            addresses = await self._resolve_checked(request.host)
            await _reply_status(writer, 200, "Connection Established")
            tunnel_open = True
            try:
                hello = await read_client_hello(reader, self._limits.hello_timeout_s)
                sni = parse_client_hello_sni(hello)
            except TlsError as exc:
                raise Denied(
                    exc.code,
                    reason=_TLS_REASONS.get(exc.code),
                    result="timeout" if exc.code == "TLS_TIMEOUT" else "refused",
                ) from None
            if sni != request.host:
                raise Denied("SNI_MISMATCH", reason="sni_mismatch")
        except Denied as denied:
            if not tunnel_open:
                await _reply_status(writer, denied.status)
            record = egress_record(
                caller, host, port, decision="blocked", status=denied.result, code=denied.code, reason=denied.reason
            )
            await self._audit_denial(caller, record)
            log.info("egress denied caller=%s code=%s", caller, denied.code)
            return

        # Decision is final: record it before a single byte goes upstream (fail closed if we cannot).
        try:
            await self._audit.append(egress_record(caller, host, port, decision="allowed", status="ok"))
        except AuditError as exc:
            log.error("egress closing tunnel for %s: audit unavailable (%s)", caller, exc.code)
            return
        upstream = await self._connect_upstream(addresses)
        if upstream is None:
            await self._audit_best_effort(
                egress_record(caller, host, port, decision="allowed", status="error", code="UPSTREAM_UNREACHABLE")
            )
            return
        up_reader, up_writer = upstream
        counts = [0, 0]  # client->upstream, upstream->client
        try:
            up_writer.write(hello)
            counts[0] += len(hello)
            await up_writer.drain()
            await self._pipe(reader, writer, up_reader, up_writer, counts)
        finally:
            await _close(up_writer)
            await self._audit_best_effort(
                egress_record(
                    caller, host, port, decision="allowed", status="ok", bytes_in=counts[1], bytes_out=counts[0]
                )
            )

    # ---- policy ------------------------------------------------------------------------------------

    @staticmethod
    def _check_policy(policy: CallerPolicy, request: ConnectRequest) -> None:
        if looks_like_ip_literal(request.host):
            raise Denied("IP_LITERAL", reason="not_allowlisted")
        if request.port != HTTPS_PORT:
            raise Denied("BAD_PORT", reason="not_allowlisted")
        if request.host not in policy.hosts:
            raise Denied("NOT_ALLOWLISTED", reason="not_allowlisted")

    async def _resolve_checked(self, host: str) -> list[IPAddress]:
        try:
            async with asyncio.timeout(self._limits.resolve_timeout_s):
                raw = await self._resolve(host)
        except (OSError, TimeoutError):
            raise Denied("DNS_FAILED", reason=None, status=502, result="error") from None
        if not raw:
            raise Denied("DNS_FAILED", reason=None, status=502, result="error")
        checked: list[IPAddress] = []
        for text in raw[:MAX_ANSWERS]:  # every answer is judged, even those we would never use
            ip = normalize(text)
            if ip is None or not is_public_address(ip, self._extra_forbidden):
                raise Denied("PRIVATE_ADDRESS", reason="private_address")  # one bad answer poisons the lot
            if ip not in checked:
                checked.append(ip)
        return checked[:MAX_ADDRESSES]

    # ---- upstream ----------------------------------------------------------------------------------

    async def _connect_upstream(
        self, addresses: list[IPAddress]
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        for ip in addresses:
            try:
                async with asyncio.timeout(self._limits.connect_timeout_s):
                    return await self._connect(ip, HTTPS_PORT)
            except (OSError, TimeoutError):
                continue
        return None

    async def _pipe(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        up_reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
        counts: list[int],
    ) -> None:
        idle = self._limits.idle_timeout_s
        tasks = [
            asyncio.create_task(_pump(client_reader, up_writer, idle, counts, 0)),
            asyncio.create_task(_pump(up_reader, client_writer, idle, counts, 1)),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _take_denial_token(self, caller: str) -> bool:
        return _take_token(
            self._buckets, caller, self._limits.denial_burst, self._limits.denial_rate_per_s, self._monotonic()
        )

    def _take_open_token(self, caller: str) -> bool:
        return _take_token(
            self._open_buckets, caller, self._limits.open_burst, self._limits.open_rate_per_s, self._monotonic()
        )

    async def _audit_denial(self, caller: str, record: dict[str, Any]) -> None:
        """Audit a refusal unless this caller is flooding denials; then count it for the aggregate record."""
        if self._take_denial_token(caller):
            await self._audit_best_effort(record)
        else:
            self._count_unwritten(record)

    def _count_unwritten(self, record: dict[str, Any]) -> None:
        egress = record["egress"]
        key = (str(egress["caller"]), str(egress["decision"]), egress.get("block_reason"))
        tally = self._suppressed.setdefault(key, _Tally())
        tally.add(1, int(egress.get("bytes_in", 0)), int(egress.get("bytes_out", 0)))

    async def flush_suppressed(self) -> None:
        """Write one aggregate record per (caller, decision, block_reason) for records not written individually.

        Denials over the rate become ``DENIALS_SUPPRESSED`` (decision blocked). Allowed-tunnel records the audit service
        refused (a close record with its byte counts, an unreachable upstream) become ``AUDIT_RECORDS_LOST`` (decision
        allowed) with the byte counts summed. An aggregate that cannot be written either stays pending for the next flush.
        """
        pending, self._suppressed = self._suppressed, {}
        for (caller, decision, reason), tally in sorted(
            pending.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or "")
        ):
            if decision == "blocked":
                record = egress_record(
                    caller, None, None, decision="blocked", status="refused", code="DENIALS_SUPPRESSED", reason=reason
                )
            else:
                record = egress_record(
                    caller,
                    None,
                    None,
                    decision="allowed",
                    status="error",
                    code="AUDIT_RECORDS_LOST",
                    bytes_in=tally.bytes_in,
                    bytes_out=tally.bytes_out,
                )
            record["egress"]["task_id"] = f"suppressed-{tally.count}"  # audit-record.v1 has no count field (proposal)
            try:
                await self._audit.append(record)
            except AuditError as exc:
                log.error("egress aggregate record not written yet (%s)", exc.code)
                self._suppressed.setdefault((caller, decision, reason), _Tally()).merge(tally)

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._limits.suppress_flush_interval_s)
            await self.flush_suppressed()

    async def _audit_best_effort(self, record: dict[str, Any]) -> None:
        """Write a record whose loss must not stop anything; a refused one is counted in the next aggregate (N3)."""
        try:
            await self._audit.append(record)
        except AuditError as exc:
            log.error("egress audit record not written (%s); counted in the next aggregate", exc.code)
            self._count_unwritten(record)


@dataclass
class _Tally:
    """Records folded into one aggregate: how many, and the byte counts they carried."""

    count: int = 0
    bytes_in: int = 0
    bytes_out: int = 0

    def add(self, count: int, bytes_in: int, bytes_out: int) -> None:
        self.count += count
        self.bytes_in += bytes_in
        self.bytes_out += bytes_out

    def merge(self, other: _Tally) -> None:
        self.add(other.count, other.bytes_in, other.bytes_out)


def _take_token(buckets: dict[str, tuple[float, float]], key: str, burst: float, rate: float, now: float) -> bool:
    """Token bucket: refill at ``rate`` per second up to ``burst``, then take one token if there is one."""
    tokens, last = buckets.get(key, (burst, now))
    tokens = min(burst, tokens + (now - last) * rate)
    allowed = tokens >= 1.0
    buckets[key] = (tokens - 1.0 if allowed else tokens, now)
    return allowed


async def _pump(
    src: asyncio.StreamReader, dst: asyncio.StreamWriter, idle: float, counts: list[int], index: int
) -> None:
    try:
        while True:
            data = await asyncio.wait_for(src.read(CHUNK), idle)
            if not data:
                return
            counts[index] += len(data)
            dst.write(data)
            await dst.drain()
    except (OSError, TimeoutError):
        return


async def _reply_status(writer: asyncio.StreamWriter, status: int, text: str | None = None) -> None:
    reason = text or STATUS_TEXT.get(status, "Error")
    if status == 200:
        payload = f"HTTP/1.1 200 {reason}\r\n\r\n"
    else:
        payload = f"HTTP/1.1 {status} {reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
    with contextlib.suppress(OSError):
        writer.write(payload.encode("ascii"))
        await writer.drain()


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(Exception):
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), 2)
