"""Bounded parsing of the HTTP/1.1 ``CONNECT host:port`` request head.

Nothing here trusts the client: the head is read under one total deadline (slowloris), capped in size and
in header count, must be plain ASCII with CRLF line endings, and may not carry a body or pipelined bytes.
Only the request line matters for policy; header values are never interpreted or logged.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Final

MAX_HEAD_BYTES: Final = 8192
MAX_HEADERS: Final = 32

_TOKEN: Final = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_HOST_CHARS: Final = re.compile(r"[A-Za-z0-9.-]+")
_V6_CHARS: Final = re.compile(r"[0-9A-Fa-f:.]+")
_FORBIDDEN_HEADERS: Final = frozenset({"content-length", "transfer-encoding", "upgrade"})


class RequestError(Exception):
    """The request head is unacceptable. ``code`` is UPPER_SNAKE; ``status`` is the HTTP reply status."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class ConnectRequest:
    host: str  # lower-cased, syntactically plausible; may still be an IP literal (policy refuses those)
    port: int


async def read_head(reader: asyncio.StreamReader, timeout_s: float) -> bytes:
    """Read up to and including the blank line, under one overall deadline. No bytes may follow it."""
    buffer = b""
    try:
        async with asyncio.timeout(timeout_s):
            while b"\r\n\r\n" not in buffer:
                chunk = await reader.read(1024)
                if not chunk:
                    raise RequestError("BAD_REQUEST")
                buffer += chunk
                if len(buffer) > MAX_HEAD_BYTES:
                    raise RequestError("HEADER_TOO_LARGE", 431)
    except TimeoutError:
        raise RequestError("HEADER_TIMEOUT", 408) from None
    except OSError:  # the client reset the connection mid-head
        raise RequestError("CLIENT_RESET") from None
    head, _, extra = buffer.partition(b"\r\n\r\n")
    if extra:
        raise RequestError("BAD_REQUEST")  # a client must wait for our 200 before sending tunnel bytes
    return head


def parse_connect(head: bytes) -> ConnectRequest:
    """Parse the head (without its terminating blank line). Raises ``RequestError`` on anything unexpected."""
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError:
        raise RequestError("BAD_REQUEST") from None
    if "\r" in text.replace("\r\n", "") or "\n" in text.replace("\r\n", "") or "\x00" in text:
        raise RequestError("BAD_REQUEST")
    lines = text.split("\r\n")
    if len(lines) - 1 > MAX_HEADERS:
        raise RequestError("TOO_MANY_HEADERS", 431)
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[2] not in ("HTTP/1.1", "HTTP/1.0"):
        raise RequestError("BAD_REQUEST")
    method, target, _version = parts
    if method != "CONNECT":
        raise RequestError("METHOD_NOT_ALLOWED", 405)
    for line in lines[1:]:
        name, sep, _value = line.partition(":")
        if not sep or not _TOKEN.fullmatch(name):  # also rejects obs-fold continuation lines
            raise RequestError("BAD_REQUEST")
        if name.lower() in _FORBIDDEN_HEADERS:
            raise RequestError("BAD_REQUEST")
    return _parse_authority(target)


def _parse_authority(target: str) -> ConnectRequest:
    """Syntax only (``host:port`` or ``[v6]:port``); the proxy decides policy (IP literal, port, allowlist)."""
    host, sep, port_text = target.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
        valid_host = bool(_V6_CHARS.fullmatch(host))
    else:
        valid_host = bool(_HOST_CHARS.fullmatch(host))
    if not sep or not valid_host or not port_text.isascii() or not port_text.isdigit() or len(port_text) > 5:
        raise RequestError("BAD_REQUEST")
    return ConnectRequest(host=host.lower(), port=int(port_text))
