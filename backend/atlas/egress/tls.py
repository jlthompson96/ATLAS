"""Read one TLS ClientHello from a client and extract its SNI, strictly.

Policy is fail-closed: a ClientHello that is not a single, well-formed handshake record with exactly one
``host_name`` server_name entry is refused. Anything unusual (fragmented hello, trailing bytes, duplicate
extensions, non-ASCII or IP-literal names) is an error, never a guess. Only the bytes read here are
forwarded upstream; nothing is decrypted.
"""

from __future__ import annotations

import asyncio
from typing import Final

from atlas.egress.config import is_valid_hostname

MAX_RECORD: Final = 16384  # TLS plaintext record limit
RECORD_HEADER: Final = 5
HANDSHAKE: Final = 0x16
CLIENT_HELLO: Final = 0x01
EXT_SERVER_NAME: Final = 0x0000
NAME_TYPE_HOST: Final = 0x00


class TlsError(Exception):
    """The ClientHello is unusable. ``code`` is a stable UPPER_SNAKE reason (it goes in audit ``result.code``)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Cursor:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def take(self, n: int) -> bytes:
        if n < 0 or self._pos + n > len(self._data):
            raise TlsError("TLS_INVALID")
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk

    def uint(self, n: int) -> int:
        return int.from_bytes(self.take(n), "big")

    def vector(self, length_bytes: int) -> bytes:
        return self.take(self.uint(length_bytes))

    @property
    def remaining(self) -> int:
        return len(self._data) - self._pos


def parse_client_hello_sni(record: bytes) -> str:
    """Return the lower-case SNI host name from one complete TLS record holding a ClientHello."""
    top = _Cursor(record)
    if top.uint(1) != HANDSHAKE or top.uint(1) != 3 or top.uint(1) > 4:  # legacy_record_version 0x0300..0x0304
        raise TlsError("TLS_INVALID")
    fragment = top.vector(2)
    if top.remaining:
        raise TlsError("TLS_INVALID")
    hs = _Cursor(fragment)
    if hs.uint(1) != CLIENT_HELLO:
        raise TlsError("TLS_INVALID")
    declared = hs.uint(3)
    if declared > hs.remaining:
        raise TlsError("TLS_UNSUPPORTED")  # ClientHello fragmented across records: refuse rather than reassemble
    if declared < hs.remaining:
        raise TlsError("TLS_INVALID")
    hs.take(2 + 32)  # client_version, random
    hs.vector(1)  # legacy_session_id
    hs.vector(2)  # cipher_suites
    hs.vector(1)  # legacy_compression_methods
    if hs.remaining == 0:
        raise TlsError("SNI_MISSING")
    extensions = _Cursor(hs.vector(2))
    if hs.remaining:
        raise TlsError("TLS_INVALID")
    names: list[bytes] = []
    seen_server_name = False
    while extensions.remaining:
        ext_type = extensions.uint(2)
        body = extensions.vector(2)
        if ext_type != EXT_SERVER_NAME:
            continue
        if seen_server_name:
            raise TlsError("TLS_INVALID")
        seen_server_name = True
        listing = _Cursor(body)
        entries = _Cursor(listing.vector(2))
        if listing.remaining:
            raise TlsError("TLS_INVALID")
        while entries.remaining:
            name_type = entries.uint(1)
            name = entries.vector(2)
            if name_type == NAME_TYPE_HOST:
                names.append(name)
    if not names:
        raise TlsError("SNI_MISSING")
    if len(names) != 1:
        raise TlsError("TLS_INVALID")
    try:
        host = names[0].decode("ascii")
    except UnicodeDecodeError:
        raise TlsError("SNI_INVALID") from None
    host = host.lower()
    if not is_valid_hostname(host):  # also rejects IP literals, trailing dots, wildcards
        raise TlsError("SNI_INVALID")
    return host


async def read_client_hello(reader: asyncio.StreamReader, timeout_s: float) -> bytes:
    """Read exactly one TLS record (5-byte header + body) within a total deadline."""
    try:
        async with asyncio.timeout(timeout_s):
            header = await reader.readexactly(RECORD_HEADER)
            if header[0] != HANDSHAKE or header[1] != 3:
                raise TlsError("TLS_INVALID")
            length = int.from_bytes(header[3:5], "big")
            if not 4 <= length <= MAX_RECORD:
                raise TlsError("TLS_INVALID")
            return header + await reader.readexactly(length)
    except TimeoutError:
        raise TlsError("TLS_TIMEOUT") from None
    except asyncio.IncompleteReadError:
        raise TlsError("TLS_INVALID") from None
