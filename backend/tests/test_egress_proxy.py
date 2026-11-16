"""atlas-egress CONNECT proxy end to end against a local fake upstream (no real internet).

The audit sink is a real ``AuditLog`` so every record the proxy emits must pass audit-record.v1 and the
per-kind actor rules (egress records only from atlas-egress), and the chain must still verify afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.audit.client import AuditError
from atlas.audit.log import AuditConfig, AuditLog
from atlas.audit.segments import list_segments
from atlas.audit.verify import verify_dir
from atlas.egress.addresses import IPAddress
from atlas.egress.config import load_config
from atlas.egress.proxy import EgressProxy, Limits
from tests.egress_support import client_hello, real_client_hello

PUBLIC_A = "142.250.80.46"
PUBLIC_B = "162.159.135.232"


class LogSink:
    """AuditSink backed by the real audit log; can be switched to fail like an unreachable service."""

    def __init__(self, log: AuditLog) -> None:
        self.log = log
        self.down = False

    async def append(self, record: dict[str, Any]) -> object:
        if self.down:
            raise AuditError("AUDIT_UNAVAILABLE")
        return self.log.append("atlas-egress", record)  # raises SubmissionRejected if the proxy emits a bad record


@dataclass
class Upstream:
    connections: int = 0
    received: bytearray = field(default_factory=bytearray)
    addresses: list[str] = field(default_factory=list)
    server: asyncio.Server | None = None
    port: int = 0


@dataclass
class Env:
    proxy: EgressProxy
    ports: dict[str, int]
    sink: LogSink
    upstream: Upstream
    answers: dict[str, list[str]]

    def records(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for _, path in list_segments(self.sink.log.data_dir):
            out += [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        return [r for r in out if r["kind"] == "egress"]

    async def records_after(self, count: int, deadline_s: float = 3.0) -> list[dict[str, Any]]:
        async with asyncio.timeout(deadline_s):
            while len(self.records()) < count:  # noqa: ASYNC110  (polling a file, not an event)
                await asyncio.sleep(0.01)
        return self.records()


@contextlib.asynccontextmanager
async def environment(tmp_path: Path, limits: Limits | None = None) -> AsyncIterator[Env]:
    upstream = Upstream()

    async def upstream_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream.connections += 1
        try:
            while data := await reader.read(4096):
                upstream.received += data
                writer.write(b"echo:" + data)
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    upstream.server = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
    upstream.port = upstream.server.sockets[0].getsockname()[1]
    answers: dict[str, list[str]] = {
        "gmail.googleapis.com": [PUBLIC_A],
        "oauth2.googleapis.com": [PUBLIC_A],
        "discord.com": [PUBLIC_B],
        "api.open-meteo.com": [PUBLIC_A],
        "gateway.discord.gg": [PUBLIC_B],
    }

    async def resolver(host: str) -> list[str]:
        if host not in answers:
            raise OSError("NXDOMAIN")
        return answers[host]

    async def connector(ip: IPAddress, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        upstream.addresses.append(f"{ip}:{port}")
        return await asyncio.open_connection("127.0.0.1", upstream.port)  # the "internet" is this local server

    log = AuditLog.open(AuditConfig(data_dir=tmp_path / "audit"), Ed25519PrivateKey.generate())
    sink = LogSink(log)
    config = load_config()
    proxy = EgressProxy(
        config,
        sink,
        resolver=resolver,
        connector=connector,
        limits=limits or Limits(head_timeout_s=1, hello_timeout_s=1),
        ports=dict.fromkeys(config.callers, 0),
    )
    ports = await proxy.start()
    try:
        yield Env(proxy, ports, sink, upstream, answers)
    finally:
        await proxy.close()
        upstream.server.close()
        log.close()


async def connect(env: Env, caller: str, head: bytes) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", env.ports[caller])
    writer.write(head)
    await writer.drain()
    return reader, writer


async def status_of(reader: asyncio.StreamReader) -> int | None:
    """HTTP status from the proxy's reply, or None if it just hung up."""
    try:
        line = await asyncio.wait_for(reader.readline(), 2)
    except (ConnectionError, TimeoutError):
        return None
    return int(line.split()[1]) if line else None


def connect_head(host: str, port: int = 443) -> bytes:
    return f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode()


async def expect_closed(reader: asyncio.StreamReader) -> None:
    with contextlib.suppress(ConnectionError):
        assert await asyncio.wait_for(reader.read(1), 2) == b""


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- the allowed path --------------------------------------------------------------------------------------


def test_allowed_host_is_tunnelled_audited_and_pinned_to_the_checked_address(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await connect(env, "atlas-google", connect_head("gmail.googleapis.com"))
            assert await status_of(reader) == 200
            await reader.readuntil(b"\r\n")  # blank line after the status
            hello = real_client_hello("gmail.googleapis.com")
            writer.write(hello)
            writer.write(b"ping")
            await writer.drain()
            echoed = await asyncio.wait_for(reader.readexactly(len(b"echo:") + len(hello) + 4), 2)
            assert echoed.startswith(b"echo:")
            assert bytes(env.upstream.received) == hello + b"ping"  # ClientHello forwarded verbatim, nothing added
            assert env.upstream.addresses == [f"{PUBLIC_A}:443"]  # connected to the address that was checked
            writer.close()
            records = await env.records_after(2)
            opened, closed = [r["egress"] for r in records[-2:]]
            assert opened["decision"] == "allowed"
            assert opened["caller"] == "atlas-google"
            assert opened["host"] == "gmail.googleapis.com"
            assert opened["port"] == 443
            assert closed["bytes_out"] == len(hello) + 4
            assert closed["bytes_in"] >= len(b"echo:") + len(hello) + 4  # the echo server prefixes each chunk
            assert all(r["actor"] == "atlas-egress" for r in records[-2:])
        assert verify_dir(env.sink.log.data_dir).ok

    run(go())


def test_same_host_through_each_callers_own_port(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            for caller, host in [
                ("atlas-executor", "discord.com"),
                ("atlas-discord", "gateway.discord.gg"),
                ("atlas-panels", "api.open-meteo.com"),
            ]:
                reader, writer = await connect(env, caller, connect_head(host))
                assert await status_of(reader) == 200
                await reader.readuntil(b"\r\n")
                writer.write(client_hello(host))
                await writer.drain()
                writer.close()
                await env.records_after(2 * (1 + ["atlas-executor", "atlas-discord", "atlas-panels"].index(caller)))
            records = env.records()
            assert {r["egress"]["decision"] for r in records} == {"allowed"}
            assert [r["egress"]["caller"] for r in records[::2]] == ["atlas-executor", "atlas-discord", "atlas-panels"]

    run(go())


# --- policy denials ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("caller", "host"),
    [
        ("atlas-google", "discord.com"),  # allowed for another caller, not this one
        ("atlas-panels", "gmail.googleapis.com"),
        ("atlas-market", "api.open-meteo.com"),
        ("atlas-search", "gmail.googleapis.com"),  # empty allowlist until Phase 5
        ("atlas-google", "evil.example.org"),
        ("atlas-google", "gmail.googleapis.com."),  # no trailing-dot normalisation
        ("atlas-google", "sub.gmail.googleapis.com"),  # exact match, no suffix matching
        ("atlas-google", "gmail.googleapis.com.evil.example.org"),
        ("atlas-google", "x" * 300 + ".example.org"),
    ],
)
def test_host_not_on_the_callers_allowlist_is_denied(tmp_path: Path, caller: str, host: str) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await connect(env, caller, connect_head(host))
            assert await status_of(reader) == 403
            [record] = await env.records_after(1)
            assert record["egress"]["decision"] == "blocked"
            assert record["egress"]["block_reason"] == "not_allowlisted"
            assert record["egress"]["caller"] == caller
            assert record["result"]["code"] == "NOT_ALLOWLISTED"
            assert env.upstream.connections == 0
            writer.close()

    run(go())


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "8.8.8.8", "2130706433", "0x7f000001", "[::1]", "[2606:4700:4700::1111]", "169.254.169.254"]
)
def test_ip_literals_are_denied(tmp_path: Path, host: str) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await connect(env, "atlas-google", connect_head(host))
            assert await status_of(reader) == 403
            [record] = await env.records_after(1)
            assert record["result"]["code"] == "IP_LITERAL"
            assert record["egress"]["block_reason"] == "not_allowlisted"
            assert env.upstream.connections == 0 and env.proxy is not None
            writer.close()

    run(go())


@pytest.mark.parametrize("port", [80, 8443, 22, 25, 1, 65535, 444])
def test_only_port_443(tmp_path: Path, port: int) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await connect(env, "atlas-google", connect_head("gmail.googleapis.com", port))
            assert await status_of(reader) == 403
            [record] = await env.records_after(1)
            assert record["result"]["code"] == "BAD_PORT"
            assert record["egress"]["port"] == port
            assert env.upstream.connections == 0
            writer.close()

    run(go())


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "HEAD", "OPTIONS", "connect"])
def test_non_connect_methods_are_denied(tmp_path: Path, method: str) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await connect(
                env,
                "atlas-google",
                f"{method} http://gmail.googleapis.com/ HTTP/1.1\r\nHost: gmail.googleapis.com\r\n\r\n".encode(),
            )
            assert await status_of(reader) == 405
            [record] = await env.records_after(1)
            assert record["egress"]["block_reason"] == "method"
            assert env.upstream.connections == 0
            writer.close()

    run(go())


@pytest.mark.parametrize(
    "answers",
    [
        ["10.0.0.5"],
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["192.168.1.10"],
        ["100.64.1.1"],
        ["::1"],
        ["::ffff:127.0.0.1"],
        ["fe80::1"],
        [PUBLIC_A, "10.0.0.5"],  # one private answer among public ones poisons the lot
        ["10.0.0.5", PUBLIC_A],
        ["not-an-address"],
    ],
)
def test_private_resolution_is_denied_dns_rebinding(tmp_path: Path, answers: list[str]) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            env.answers["gmail.googleapis.com"] = answers
            reader, writer = await connect(env, "atlas-google", connect_head("gmail.googleapis.com"))
            assert await status_of(reader) == 403
            [record] = await env.records_after(1)
            assert record["egress"]["block_reason"] == "private_address"
            assert env.upstream.connections == 0 and env.upstream.addresses == []
            writer.close()

    run(go())


def test_dns_is_resolved_once_per_request(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            calls = 0
            original = env.proxy._resolve

            async def counting(host: str) -> list[str]:
                nonlocal calls
                calls += 1
                return await original(host)

            env.proxy._resolve = counting
            reader, writer = await connect(env, "atlas-google", connect_head("gmail.googleapis.com"))
            assert await status_of(reader) == 200
            await reader.readuntil(b"\r\n")
            writer.write(client_hello())
            await writer.drain()
            await env.records_after(1)
            assert calls == 1 and env.upstream.addresses == [f"{PUBLIC_A}:443"]
            writer.close()

    run(go())


def test_dns_failure_is_a_clean_502(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            del env.answers["gmail.googleapis.com"]
            reader, writer = await connect(env, "atlas-google", connect_head("gmail.googleapis.com"))
            assert await status_of(reader) == 502
            [record] = await env.records_after(1)
            assert record["result"] == {"status": "error", "code": "DNS_FAILED"}
            writer.close()

    run(go())


# --- SNI -------------------------------------------------------------------------------------------------------


async def open_tunnel(env: Env, caller: str, host: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await connect(env, caller, connect_head(host))
    assert await status_of(reader) == 200
    await reader.readuntil(b"\r\n")
    return reader, writer


@pytest.mark.parametrize(
    ("hello", "code"),
    [
        (client_hello("evil.example.org"), "SNI_MISMATCH"),
        (client_hello("discord.com"), "SNI_MISMATCH"),  # allowed for others, but it is not the CONNECT host
        (client_hello("gmail.googleapis.com.evil.example.org"), "SNI_MISMATCH"),
        (client_hello(sni=None, extensions=b""), "SNI_MISSING"),
        (client_hello(sni=None), "SNI_MISSING"),
        (client_hello("10.1.2.3"), "SNI_INVALID"),
        (client_hello(handshake_length_delta=10), "TLS_UNSUPPORTED"),
        (b"GET / HTTP/1.1\r\nHost: gmail.googleapis.com\r\n\r\n", "TLS_INVALID"),  # plaintext through a CONNECT tunnel
    ],
)
def test_sni_must_equal_the_connect_host(tmp_path: Path, hello: bytes, code: str) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await open_tunnel(env, "atlas-google", "gmail.googleapis.com")
            writer.write(hello)
            await writer.drain()
            await expect_closed(reader)
            [record] = await env.records_after(1)
            assert record["egress"]["decision"] == "blocked"
            assert record["egress"]["block_reason"] == "sni_mismatch"
            assert record["result"]["code"] == code
            assert env.upstream.connections == 0  # nothing reached the upstream, not even the ClientHello
            writer.close()

    run(go())


def test_sni_comparison_ignores_case_only(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await open_tunnel(env, "atlas-google", "gmail.googleapis.com")
            writer.write(client_hello("GMAIL.googleapis.COM"))
            await writer.drain()
            await env.records_after(1)
            assert env.upstream.connections == 1
            writer.close()
            del reader

    run(go())


# --- resource limits ---------------------------------------------------------------------------------------------


def test_oversized_head_is_refused(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await connect(
                env, "atlas-google", b"CONNECT gmail.googleapis.com:443 HTTP/1.1\r\nX-Pad: " + b"a" * 20000
            )
            [record] = await env.records_after(1)
            assert record["result"]["code"] == "HEADER_TOO_LARGE"
            assert record["egress"]["decision"] == "blocked"
            assert record["egress"]["host"] == "-"  # nothing from an unparsed head is echoed into the audit log
            assert env.upstream.connections == 0
            writer.close()
            del reader

    run(go())


def test_too_many_headers_is_refused(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            head = (
                b"CONNECT gmail.googleapis.com:443 HTTP/1.1\r\n"
                + b"".join(b"X-%d: y\r\n" % i for i in range(100))
                + b"\r\n"
            )
            reader, writer = await connect(env, "atlas-google", head)
            assert await status_of(reader) == 431
            [record] = await env.records_after(1)
            assert record["result"]["code"] == "TOO_MANY_HEADERS"
            writer.close()

    run(go())


def test_slowloris_head_times_out(tmp_path: Path) -> None:
    async def go() -> None:
        limits = Limits(head_timeout_s=0.3, hello_timeout_s=1)
        async with environment(tmp_path, limits) as env:
            reader, writer = await connect(env, "atlas-google", b"CONNECT gmail.googleapis.com:443 HTTP/1.1\r\n")
            with contextlib.suppress(ConnectionError):  # the proxy hangs up once its deadline passes
                for _ in range(5):  # keeps the connection "alive" without ever finishing the head
                    writer.write(b"X")
                    await writer.drain()
                    await asyncio.sleep(0.1)
            assert await status_of(reader) in (408, None)
            [record] = await env.records_after(1)
            assert record["result"] == {"status": "refused", "code": "HEADER_TIMEOUT"}
            assert env.upstream.connections == 0
            writer.close()

    run(go())


def test_client_that_never_sends_a_hello_times_out(tmp_path: Path) -> None:
    async def go() -> None:
        limits = Limits(head_timeout_s=1, hello_timeout_s=0.2)
        async with environment(tmp_path, limits) as env:
            reader, writer = await open_tunnel(env, "atlas-google", "gmail.googleapis.com")
            await expect_closed(reader)
            [record] = await env.records_after(1)
            assert record["result"]["code"] == "TLS_TIMEOUT"
            assert env.upstream.connections == 0
            writer.close()

    run(go())


def test_bytes_pipelined_before_our_200_are_refused(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await connect(
                env, "atlas-google", connect_head("gmail.googleapis.com") + client_hello("evil.example.org")
            )
            assert await status_of(reader) == 400
            [record] = await env.records_after(1)
            assert record["result"]["code"] == "BAD_REQUEST"
            writer.close()

    run(go())


def test_request_with_a_body_is_refused(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await connect(
                env, "atlas-google", b"CONNECT gmail.googleapis.com:443 HTTP/1.1\r\nContent-Length: 5\r\n\r\n"
            )
            assert await status_of(reader) == 400
            await env.records_after(1)
            writer.close()

    run(go())


def test_connection_limit_per_caller_and_busy_audit_is_rate_limited(tmp_path: Path) -> None:
    async def go() -> None:
        limits = Limits(head_timeout_s=2, hello_timeout_s=1, max_per_caller=2, busy_audit_interval_s=3600)
        async with environment(tmp_path, limits) as env:
            held = [await connect(env, "atlas-google", b"CONNECT gmail") for _ in range(2)]  # occupy both slots
            await asyncio.sleep(0.1)
            for _ in range(5):
                reader, writer = await connect(env, "atlas-google", b"")
                assert await status_of(reader) == 503
                writer.close()
            other_reader, other_writer = await connect(env, "atlas-panels", connect_head("api.open-meteo.com"))
            assert await status_of(other_reader) == 200  # another caller's budget is untouched
            records = await env.records_after(1)
            busy = [r for r in records if r["result"].get("code") == "TOO_MANY_CONNECTIONS"]
            assert len(busy) == 1  # five refusals, one audit record
            other_writer.close()
            for _, w in held:
                w.close()

    run(go())


# --- audit is mandatory --------------------------------------------------------------------------------------------


def test_if_audit_is_down_the_tunnel_is_closed_before_any_upstream_byte(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            env.sink.down = True
            reader, writer = await open_tunnel(env, "atlas-google", "gmail.googleapis.com")
            writer.write(client_hello())
            await writer.drain()
            await expect_closed(reader)
            assert env.upstream.connections == 0 and env.upstream.received == b""
            writer.close()

    run(go())


def test_denials_still_deny_when_audit_is_down(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            env.sink.down = True
            reader, writer = await connect(env, "atlas-google", connect_head("evil.example.org"))
            assert await status_of(reader) == 403
            writer.close()

    run(go())


def test_unreachable_upstream_is_audited(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:

            async def refuse(ip: IPAddress, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
                raise ConnectionRefusedError

            env.proxy._connect = refuse
            reader, writer = await open_tunnel(env, "atlas-google", "gmail.googleapis.com")
            writer.write(client_hello())
            await writer.drain()
            await expect_closed(reader)
            records = await env.records_after(2)
            assert records[-1]["result"] == {"status": "error", "code": "UPSTREAM_UNREACHABLE"}
            writer.close()

    run(go())


# --- hygiene --------------------------------------------------------------------------------------------------------


def test_logs_do_not_contain_targets_or_headers(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    canary_host = "exfil-9f8e7d.evil.example.org"

    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await connect(
                env,
                "atlas-google",
                f"CONNECT {canary_host}:443 HTTP/1.1\r\nAuthorization: Bearer canary-token-123\r\n\r\n".encode(),
            )
            await status_of(reader)
            await env.records_after(1)
            writer.close()

    with caplog.at_level(logging.DEBUG):
        run(go())
    assert "canary-token-123" not in caplog.text
    assert canary_host not in caplog.text
    assert "denied" in caplog.text  # the decision itself is logged


def test_every_record_the_proxy_emits_is_valid_and_the_chain_verifies(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            for head in (
                connect_head("evil.example.org"),
                connect_head("gmail.googleapis.com", 80),
                b"GET / HTTP/1.1\r\n\r\n",
                connect_head("127.0.0.1"),
            ):
                reader, writer = await connect(env, "atlas-google", head)
                await status_of(reader)
                writer.close()
            await env.records_after(4)
        assert verify_dir(env.sink.log.data_dir).ok

    run(go())
