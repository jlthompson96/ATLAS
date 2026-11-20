"""A small HTTP/1.1 client on asyncio streams (standard library only).

Used for the two plain-HTTP links to the LLM host (LM Studio and ``atlas-hostd``), which run inside
the WireGuard tunnel (contracts/services.md section 2). It is deliberately minimal: HTTP only (no TLS,
no redirects, no proxies, no keep-alive), one request per connection, hard caps on header and body
sizes, and every wait bounded by an absolute ``time.monotonic()`` deadline.

Nothing here logs. Exceptions carry no URLs, headers or bodies, so a bearer token can't leak through
an error message.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Mapping
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

MAX_HEADER_BYTES: Final = 32 * 1024
CONNECT_TIMEOUT_S: Final = 5.0
_STREAM_LIMIT: Final = 256 * 1024


class HttpTransportError(Exception):
    """Connect failure, reset, malformed response or truncated body."""


class HttpDeadlineError(HttpTransportError):
    """The absolute deadline passed."""


class BodyTooLargeError(HttpTransportError):
    pass


@dataclass(frozen=True, slots=True)
class Endpoint:
    host: str
    port: int


def parse_http_url(url: str) -> Endpoint:
    """``http://host:port`` only. Anything else (https, paths, credentials) is a configuration error."""
    parts = urlsplit(url)
    if parts.scheme != "http":
        raise ValueError("only http:// URLs are supported (the link is inside WireGuard)")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("URL must be just http://host:port")
    if not parts.hostname:
        raise ValueError("URL has no host")
    return Endpoint(parts.hostname, parts.port or 80)


def _remaining(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    left = deadline - time.monotonic()
    if left <= 0:
        raise HttpDeadlineError("deadline passed")
    return left


async def _bounded[T](awaitable: Awaitable[T], deadline: float | None, cap: float | None = None) -> T:
    left = _remaining(deadline)
    if cap is not None:
        left = cap if left is None else min(left, cap)
    try:
        return await asyncio.wait_for(awaitable, left)
    except TimeoutError:
        raise HttpDeadlineError("timed out") from None
    except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError) as exc:
        raise HttpTransportError(type(exc).__name__) from None


class HttpResponse:
    def __init__(
        self,
        status: int,
        headers: Mapping[str, str],
        reader: asyncio.StreamReader,
        deadline: float | None,
    ) -> None:
        self.status = status
        self.headers = headers
        self._reader = reader
        self._deadline = deadline
        self._chunked = headers.get("transfer-encoding", "").lower() == "chunked"
        length = headers.get("content-length")
        self._remaining_len: int | None = None
        if length is not None and not self._chunked:
            if not length.isdigit():
                raise HttpTransportError("bad content-length")
            self._remaining_len = int(length)

    async def chunks(self) -> AsyncIterator[bytes]:
        """Body fragments as they arrive (chunked framing removed)."""
        if self._chunked:
            while True:
                line = await _bounded(self._reader.readline(), self._deadline)
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise HttpTransportError("bad chunk size") from None
                if size == 0:
                    return
                data = await _bounded(self._reader.readexactly(size), self._deadline)
                await _bounded(self._reader.readexactly(2), self._deadline)
                yield data
        elif self._remaining_len is not None:
            while self._remaining_len > 0:
                data = await _bounded(self._reader.read(min(self._remaining_len, 65536)), self._deadline)
                if not data:
                    raise HttpTransportError("truncated body")
                self._remaining_len -= len(data)
                yield data
        else:
            while True:
                data = await _bounded(self._reader.read(65536), self._deadline)
                if not data:
                    return
                yield data

    async def read(self, limit: int) -> bytes:
        """The whole body, or ``BodyTooLargeError`` if it exceeds ``limit`` bytes."""
        out = bytearray()
        async for part in self.chunks():
            out += part
            if len(out) > limit:
                raise BodyTooLargeError("body too large")
        return bytes(out)


@contextlib.asynccontextmanager
async def request(
    endpoint: Endpoint,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    deadline: float | None = None,
) -> AsyncIterator[HttpResponse]:
    """Send one request and yield the response once its headers have arrived."""
    if not path.startswith("/") or any(c in path for c in " \r\n"):
        raise ValueError("bad request path")
    lines = [f"{method} {path} HTTP/1.1", f"Host: {endpoint.host}:{endpoint.port}", "Connection: close"]
    for name, value in (headers or {}).items():
        if any(c in name + value for c in "\r\n"):
            raise ValueError("bad header")
        lines.append(f"{name}: {value}")
    if body is not None:
        lines.append(f"Content-Length: {len(body)}")
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

    reader, writer = await _bounded(
        asyncio.open_connection(endpoint.host, endpoint.port, limit=_STREAM_LIMIT), deadline, CONNECT_TIMEOUT_S
    )
    try:
        writer.write(head + (body or b""))
        await _bounded(writer.drain(), deadline)
        raw = await _bounded(reader.readuntil(b"\r\n\r\n"), deadline)
        if len(raw) > MAX_HEADER_BYTES:
            raise HttpTransportError("headers too large")
        status_line, *header_lines = raw.decode("latin-1").split("\r\n")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/1.") or not parts[1].isdigit():
            raise HttpTransportError("bad status line")
        parsed: dict[str, str] = {}
        for line in header_lines:
            if line:
                name, _, value = line.partition(":")
                parsed[name.strip().lower()] = value.strip()
        yield HttpResponse(int(parts[1]), parsed, reader, deadline)
    finally:
        writer.close()
        with contextlib.suppress(OSError, asyncio.CancelledError, RuntimeError):
            await writer.wait_closed()


async def fetch(
    endpoint: Endpoint,
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: bytes | None = None,
    deadline: float | None = None,
    max_body: int = 1024 * 1024,
) -> tuple[int, bytes]:
    """One request, whole (size-capped) body: ``(status, body)``."""
    async with request(endpoint, method, path, headers=headers, body=body, deadline=deadline) as resp:
        return resp.status, await resp.read(max_body)
