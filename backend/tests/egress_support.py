"""Helpers for the egress tests: synthetic and real TLS ClientHellos."""

from __future__ import annotations

import contextlib
import ssl


def _vec(length_bytes: int, data: bytes) -> bytes:
    return len(data).to_bytes(length_bytes, "big") + data


def sni_extension(*names: str, name_type: int = 0) -> bytes:
    entries = b"".join(bytes([name_type]) + _vec(2, n.encode()) for n in names)
    return (0).to_bytes(2, "big") + _vec(2, _vec(2, entries))


def client_hello(
    sni: str | None = "gmail.googleapis.com",
    *,
    extensions: bytes | None = None,
    trailing_in_record: bytes = b"",
    handshake_length_delta: int = 0,
    record_version: bytes = b"\x03\x01",
) -> bytes:
    """A syntactically valid single-record TLS ClientHello (not a usable handshake, which the proxy never needs)."""
    ext = extensions if extensions is not None else (sni_extension(sni) if sni is not None else b"")
    other = (0x0A).to_bytes(2, "big") + _vec(2, b"\x00\x02\x00\x1d")  # supported_groups, so SNI is not alone
    body = (
        b"\x03\x03"
        + bytes(32)
        + _vec(1, bytes(32))
        + _vec(2, b"\x13\x01\x13\x02")
        + _vec(1, b"\x00")
        + _vec(2, other + ext)
    )
    if extensions is None and sni is None:
        body = b"\x03\x03" + bytes(32) + _vec(1, b"") + _vec(2, b"\x13\x01") + _vec(1, b"\x00")  # no extensions block
    handshake = b"\x01" + (len(body) + handshake_length_delta).to_bytes(3, "big") + body
    fragment = handshake + trailing_in_record
    return b"\x16" + record_version + _vec(2, fragment)


def real_client_hello(server_name: str) -> bytes:
    """A genuine ClientHello produced by the stdlib ssl module (OpenSSL), as a real client would send."""
    context = ssl.create_default_context()
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=server_name)
    with contextlib.suppress(ssl.SSLWantReadError):
        tls.do_handshake()
    data = outgoing.read()
    assert data[:1] == b"\x16"
    return data
