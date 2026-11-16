"""ClientHello SNI extraction and the CONNECT head parser: strict, fail-closed."""

from __future__ import annotations

import asyncio

import pytest

from atlas.egress.http import MAX_HEAD_BYTES, MAX_HEADERS, RequestError, parse_connect, read_head
from atlas.egress.tls import TlsError, parse_client_hello_sni, read_client_hello
from tests.egress_support import client_hello, real_client_hello, sni_extension

# --- SNI -----------------------------------------------------------------------------------------------


def test_sni_from_a_synthetic_hello() -> None:
    assert parse_client_hello_sni(client_hello("gmail.googleapis.com")) == "gmail.googleapis.com"


def test_sni_is_lowercased() -> None:
    assert parse_client_hello_sni(client_hello("GMail.GoogleAPIs.com")) == "gmail.googleapis.com"


@pytest.mark.parametrize("name", ["api.open-meteo.com", "discord.com", "gateway.discord.gg"])
def test_sni_from_a_real_openssl_hello(name: str) -> None:
    assert parse_client_hello_sni(real_client_hello(name)) == name


def code_of(data: bytes) -> str:
    with pytest.raises(TlsError) as exc:
        parse_client_hello_sni(data)
    return exc.value.code


def test_missing_sni_extension() -> None:
    assert code_of(client_hello(sni=None, extensions=b"")) == "SNI_MISSING"


def test_no_extensions_block_at_all() -> None:
    assert code_of(client_hello(sni=None)) == "SNI_MISSING"


def test_other_name_types_only_is_missing() -> None:
    assert code_of(client_hello(extensions=sni_extension("x.example.com", name_type=1))) == "SNI_MISSING"


def test_two_host_names_are_refused() -> None:
    assert code_of(client_hello(extensions=sni_extension("a.example.com", "b.example.com"))) == "TLS_INVALID"


def test_two_server_name_extensions_are_refused() -> None:
    both = sni_extension("a.example.com") + sni_extension("a.example.com")
    assert code_of(client_hello(extensions=both)) == "TLS_INVALID"


@pytest.mark.parametrize(
    "name", ["10.0.0.1", "[::1]", "a.example.com.", "*.example.com", "bad name.example.com", "localhost"]
)
def test_invalid_sni_values(name: str) -> None:
    assert code_of(client_hello(name)) == "SNI_INVALID"


def test_non_ascii_sni() -> None:
    assert code_of(client_hello(extensions=sni_extension("bücher.example"))) == "SNI_INVALID"


def test_fragmented_hello_is_refused_not_reassembled() -> None:
    assert code_of(client_hello(handshake_length_delta=50)) == "TLS_UNSUPPORTED"


def test_trailing_bytes_in_the_record_are_refused() -> None:
    assert code_of(client_hello(trailing_in_record=b"\x00")) == "TLS_INVALID"


def test_not_a_client_hello() -> None:
    hello = bytearray(client_hello())
    hello[5] = 0x02  # ServerHello
    assert code_of(bytes(hello)) == "TLS_INVALID"
    assert code_of(b"GET / HTTP/1.1\r\n\r\n") == "TLS_INVALID"
    assert code_of(b"") == "TLS_INVALID"


@pytest.mark.parametrize("cut", [6, 20, 60, 80])
def test_truncated_hello_is_refused(cut: int) -> None:
    data = client_hello()
    with pytest.raises(TlsError):
        parse_client_hello_sni(data[:cut])


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


def feed(data: bytes, *, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


def test_read_client_hello_reads_exactly_one_record() -> None:
    hello = client_hello()

    async def go() -> bytes:
        return await read_client_hello(feed(hello + b"EXTRA"), 1)

    assert run(go()) == hello


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"GET /", "TLS_INVALID"),
        (b"\x16\x03\x01\xff\xff", "TLS_INVALID"),  # length above the TLS record limit
        (b"\x16\x03\x01\x00\x02ab", "TLS_INVALID"),  # too short to hold a handshake header
        (client_hello()[:30], "TLS_INVALID"),  # connection closes mid-record
    ],
)
def test_read_client_hello_rejects(data: bytes, code: str) -> None:
    async def go() -> None:
        await read_client_hello(feed(data), 1)

    with pytest.raises(TlsError) as exc:
        run(go())
    assert exc.value.code == code


def test_read_client_hello_times_out() -> None:
    async def go() -> None:
        await read_client_hello(feed(b"\x16\x03", eof=False), 0.05)

    with pytest.raises(TlsError) as exc:
        run(go())
    assert exc.value.code == "TLS_TIMEOUT"


# --- CONNECT head ----------------------------------------------------------------------------------------


def test_parse_connect_basic() -> None:
    req = parse_connect(
        b"CONNECT Gmail.GoogleAPIs.com:443 HTTP/1.1\r\nHost: gmail.googleapis.com:443\r\nProxy-Connection: keep-alive"
    )
    assert (req.host, req.port) == ("gmail.googleapis.com", 443)


def test_parse_connect_ipv6_bracket_form_is_parsed_for_policy_to_refuse() -> None:
    req = parse_connect(b"CONNECT [::1]:443 HTTP/1.1")
    assert req.host == "::1"


@pytest.mark.parametrize(
    ("head", "code"),
    [
        (b"GET http://example.com/ HTTP/1.1", "METHOD_NOT_ALLOWED"),
        (b"connect a.example.com:443 HTTP/1.1", "METHOD_NOT_ALLOWED"),
        (b"POST a.example.com:443 HTTP/1.1", "METHOD_NOT_ALLOWED"),
        (b"CONNECT a.example.com:443", "BAD_REQUEST"),
        (b"CONNECT a.example.com:443 HTTP/2", "BAD_REQUEST"),
        (b"CONNECT  a.example.com:443 HTTP/1.1", "BAD_REQUEST"),
        (b"CONNECT a.example.com HTTP/1.1", "BAD_REQUEST"),
        (b"CONNECT a.example.com:abc HTTP/1.1", "BAD_REQUEST"),
        (b"CONNECT a.example.com:443443 HTTP/1.1", "BAD_REQUEST"),
        (b"CONNECT a.example.com:-1 HTTP/1.1", "BAD_REQUEST"),
        (b"CONNECT http://a.example.com:443 HTTP/1.1", "BAD_REQUEST"),
        (b"CONNECT user@a.example.com:443 HTTP/1.1", "BAD_REQUEST"),
        (b"CONNECT a.example.com/x:443 HTTP/1.1", "BAD_REQUEST"),
        (b"CONNECT a:b:443 HTTP/1.1", "BAD_REQUEST"),
        (b"CONNECT :443 HTTP/1.1", "BAD_REQUEST"),
        (b"CONNECT a.example.com:443 HTTP/1.1\nHost: x", "BAD_REQUEST"),  # bare LF
        (b"CONNECT a.example.com:443 HTTP/1.1\rHost: x", "BAD_REQUEST"),  # bare CR
        (b"CONNECT a.example.com:443 HTTP/1.1\r\nHost: x\x00", "BAD_REQUEST"),
        (b"CONNECT a.example.com:443 HTTP/1.1\r\nBad Header: x", "BAD_REQUEST"),
        (b"CONNECT a.example.com:443 HTTP/1.1\r\n folded: x", "BAD_REQUEST"),
        (b"CONNECT a.example.com:443 HTTP/1.1\r\nno colon", "BAD_REQUEST"),
        (b"CONNECT a.example.com:443 HTTP/1.1\r\nContent-Length: 0", "BAD_REQUEST"),
        (b"CONNECT a.example.com:443 HTTP/1.1\r\nTransfer-Encoding: chunked", "BAD_REQUEST"),
        (b"CONNECT a.example.com:443 HTTP/1.1\r\nHost: \xff", "BAD_REQUEST"),
    ],
)
def test_parse_connect_rejects(head: bytes, code: str) -> None:
    with pytest.raises(RequestError) as exc:
        parse_connect(head)
    assert exc.value.code == code


def test_too_many_headers() -> None:
    head = b"CONNECT a.example.com:443 HTTP/1.1" + b"".join(b"\r\nX-%d: y" % i for i in range(MAX_HEADERS + 1))
    with pytest.raises(RequestError) as exc:
        parse_connect(head)
    assert exc.value.code == "TOO_MANY_HEADERS"


def head_reader(data: bytes, *, eof: bool = True) -> asyncio.StreamReader:
    return feed(data, eof=eof)


def test_read_head_returns_the_head_without_the_blank_line() -> None:
    async def go() -> bytes:
        return await read_head(head_reader(b"CONNECT a.example.com:443 HTTP/1.1\r\n\r\n"), 1)

    assert run(go()) == b"CONNECT a.example.com:443 HTTP/1.1"


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"CONNECT a.example.com:443 HTTP/1.1\r\n\r\n\x16\x03\x01", "BAD_REQUEST"),  # bytes pipelined before our 200
        (b"CONNECT a.example.com:443 HTTP/1.1\r\n", "BAD_REQUEST"),  # EOF before the blank line
        (b"X: " + b"a" * (MAX_HEAD_BYTES + 10), "HEADER_TOO_LARGE"),
    ],
)
def test_read_head_rejects(data: bytes, code: str) -> None:
    async def go() -> None:
        await read_head(head_reader(data), 1)

    with pytest.raises(RequestError) as exc:
        run(go())
    assert exc.value.code == code


def test_read_head_deadline_is_total_not_per_read() -> None:
    async def go() -> None:
        reader = head_reader(b"CONNECT a.example.com:443 HTTP/1.1\r\n", eof=False)

        async def drip() -> None:
            for _ in range(50):  # one byte every 20 ms never completes the head, and never trips a per-read timeout
                await asyncio.sleep(0.02)
                reader.feed_data(b"X")

        task = asyncio.create_task(drip())
        try:
            await read_head(reader, 0.2)
        finally:
            task.cancel()

    with pytest.raises(RequestError) as exc:
        run(go())
    assert exc.value.code == "HEADER_TIMEOUT"
    assert exc.value.status == 408
