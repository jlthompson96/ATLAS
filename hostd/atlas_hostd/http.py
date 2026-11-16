"""A deliberately small HTTP/1.1 server on asyncio streams (standard library only).

Scope: JSON requests with ``Content-Length`` bodies, one request per connection (``Connection: close``), hard
limits on head size, header count, body size and read time. The source address is checked before anything is
parsed. Request and response bodies are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final

log = logging.getLogger("atlas_hostd.http")

MAX_HEAD_BYTES: Final = 8192
MAX_HEADERS: Final = 64
MAX_BODY_BYTES: Final = 4096
READ_TIMEOUT_S: Final = 5.0

_REASONS: Final = {
    200: "OK",
    202: "Accepted",
    204: "No Content",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    target: str  # raw request target, may include a query string
    content_type: str | None
    body: bytes
    peer: str  # normalised source IP


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)


Handler = Callable[[Request], Awaitable[Response]]
SourceCheck = Callable[[str], bool]


class BadHttp(Exception):
    """Malformed or over-limit request. ``code`` becomes the error body's code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_ip(host: str) -> str:
    """Canonical IP string; IPv4-mapped IPv6 addresses become plain IPv4."""
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return host
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(addr)


def error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> Response:
    return Response(status, {"code": code, "message": message[:200]}, headers or {})


def encode_response(response: Response) -> bytes:
    payload = b"" if response.body is None else json.dumps(response.body, separators=(",", ":")).encode()
    lines = [f"HTTP/1.1 {response.status} {_REASONS.get(response.status, 'Status')}"]
    if response.status != 204:
        lines.append("Content-Type: application/json")
    lines.append(f"Content-Length: {len(payload)}")
    lines.append("Cache-Control: no-store")
    lines.append("Connection: close")
    lines.extend(f"{k}: {v}" for k, v in response.headers.items())
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + payload


async def _read_request(reader: asyncio.StreamReader, peer: str) -> Request:
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT_S)
    except asyncio.LimitOverrunError as exc:
        raise BadHttp("INVALID_BODY", "request head too large") from exc
    except (asyncio.IncompleteReadError, TimeoutError) as exc:
        raise BadHttp("INVALID_BODY", "incomplete request") from exc
    if len(head) > MAX_HEAD_BYTES:
        raise BadHttp("INVALID_BODY", "request head too large")
    try:
        text = head[:-4].decode("ascii")
    except UnicodeDecodeError as exc:
        raise BadHttp("INVALID_BODY", "request head is not ASCII") from exc
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise BadHttp("INVALID_BODY", "malformed request line")
    method, target, _ = parts
    if not target.startswith("/"):
        raise BadHttp("INVALID_BODY", "request target must be a path")
    if len(lines) - 1 > MAX_HEADERS:
        raise BadHttp("INVALID_BODY", "too many headers")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not name or name != name.strip():
            raise BadHttp("INVALID_BODY", "malformed header")
        key = name.lower()
        if key in headers and key in ("content-length", "transfer-encoding", "content-type"):
            raise BadHttp("INVALID_BODY", "duplicate header")
        headers[key] = value.strip()
    if "transfer-encoding" in headers:
        raise BadHttp("INVALID_BODY", "transfer-encoding is not supported; send Content-Length")
    length = 0
    if "content-length" in headers:
        raw = headers["content-length"]
        if not raw.isascii() or not raw.isdigit() or len(raw) > 9:
            raise BadHttp("INVALID_BODY", "invalid Content-Length")
        length = int(raw)
        if length > MAX_BODY_BYTES:
            raise BadHttp("BODY_TOO_LARGE", "request body too large")
    body = b""
    if length:
        try:
            body = await asyncio.wait_for(reader.readexactly(length), READ_TIMEOUT_S)
        except (asyncio.IncompleteReadError, TimeoutError) as exc:
            raise BadHttp("INVALID_BODY", "incomplete body") from exc
    return Request(method=method, target=target, content_type=headers.get("content-type"), body=body, peer=peer)


class HttpServer:
    """One listener. ``source_ok`` is evaluated on the peer address before the request is read."""

    def __init__(self, name: str, host: str, port: int, source_ok: SourceCheck, handler: Handler) -> None:
        self._name = name
        self._host = host
        self._port = port
        self._source_ok = source_ok
        self._handler = handler
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """The bound port (useful when configured with 0)."""
        if self._server is None or not self._server.sockets:
            return self._port
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        # ``host`` is a concrete address (config rejects 0.0.0.0 and ::), so only that interface is bound.
        self._server = await asyncio.start_server(
            self._on_connection, host=self._host, port=self._port, limit=MAX_HEAD_BYTES * 2
        )
        log.info("listener %s bound on %s:%d", self._name, self._host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started = time.perf_counter()
        status = 0
        peer = "?"
        try:
            peername = writer.get_extra_info("peername")
            peer = normalize_ip(str(peername[0])) if peername else "?"
            if not self._source_ok(peer):
                response = error_response(403, "FORBIDDEN_SOURCE", "source address not allowed")
                await self._send(writer, response)
                # Let a client that already sent its request finish before the socket closes, so the 403 is not
                # lost to a connection reset.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(reader.read(MAX_HEAD_BYTES), 0.2)
                status = 403
                return
            try:
                request = await _read_request(reader, peer)
                response = await self._handler(request)
            except BadHttp as exc:
                response = error_response(400, exc.code, exc.message)
            except Exception:
                log.exception("unhandled error in %s handler", self._name)
                response = error_response(500, "INTERNAL_ERROR", "internal error")
            status = response.status
            await self._send(writer, response)
        except (ConnectionError, OSError):
            log.debug("connection error on %s", self._name)
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
            log.info("%s peer=%s status=%d ms=%.1f", self._name, peer, status, (time.perf_counter() - started) * 1000)

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, response: Response) -> None:
        writer.write(encode_response(response))
        await writer.drain()
