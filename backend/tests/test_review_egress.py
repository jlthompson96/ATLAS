"""Regression tests for the security review of atlas-egress (F3, F5, F6, F9). Each failed before its fix."""

from __future__ import annotations

import asyncio
import socket
import struct
from pathlib import Path
from typing import Any

import pytest

from atlas.egress.addresses import is_public_address, normalize, parse_networks
from atlas.egress.proxy import Limits
from atlas.egress.tls import ECH_EXTENSIONS, TlsError, parse_client_hello_sni
from tests.egress_support import _vec, client_hello, sni_extension
from tests.test_egress_proxy import connect, connect_head, environment, expect_closed, open_tunnel, status_of


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def ext(code: int, body: bytes = b"\x00" * 8) -> bytes:
    return code.to_bytes(2, "big") + _vec(2, body)


def code_of(data: bytes) -> str:
    with pytest.raises(TlsError) as exc:
        parse_client_hello_sni(data)
    return exc.value.code


# --- F3: ECH and duplicate extensions -----------------------------------------------------------------------------


@pytest.mark.parametrize("codepoint", sorted(ECH_EXTENSIONS))
def test_f3_ech_family_extensions_are_refused(codepoint: int) -> None:
    hello = client_hello(extensions=sni_extension("discord.com") + ext(codepoint))
    assert code_of(hello) == "ECH_REFUSED"


def test_f3_the_documented_codepoints_are_all_covered() -> None:
    assert {0xFE0D, 0xFE08, 0xFE09, 0xFE0A, 0xFE0B, 0xFE0C, 0xFFCE, 0xFD00} == ECH_EXTENSIONS


def test_f3_ech_is_refused_even_before_the_sni_is_read() -> None:
    assert code_of(client_hello(extensions=ext(0xFE0D) + sni_extension("discord.com"))) == "ECH_REFUSED"


@pytest.mark.parametrize("codepoint", [0x0010, 0x002B, 0x0A0A, 0x0000])
def test_f3_duplicate_extensions_of_any_type_are_refused(codepoint: int) -> None:
    body = sni_extension("discord.com")[4:] if codepoint == 0 else b"\x00\x00"
    both = sni_extension("discord.com") + ext(codepoint, body) + ext(codepoint, body)
    if codepoint == 0:
        both = sni_extension("discord.com") + sni_extension("discord.com")
    assert code_of(client_hello(extensions=both)) == "DUPLICATE_EXTENSION"


def test_f3_other_extensions_still_pass() -> None:
    hello = client_hello(extensions=sni_extension("discord.com") + ext(0x0010) + ext(0x002B))
    assert parse_client_hello_sni(hello) == "discord.com"


def test_f3_proxy_never_forwards_an_ech_hello(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await open_tunnel(env, "atlas-discord", "discord.com")
            inner = ext(0xFE0D, b"\x00INNER-SNI=attacker.example" + bytes(20))
            writer.write(client_hello(extensions=sni_extension("discord.com") + inner))
            await writer.drain()
            await expect_closed(reader)
            [record] = await env.records_after(1)
            assert record["egress"]["block_reason"] == "sni_mismatch"
            assert record["result"]["code"] == "ECH_REFUSED"
            assert env.upstream.connections == 0
            writer.close()

    run(go())


# --- F5: denial flood ---------------------------------------------------------------------------------------------


def test_f5_denial_flood_is_bounded_and_fully_accounted(tmp_path: Path) -> None:
    async def go() -> None:
        limits = Limits(head_timeout_s=1, hello_timeout_s=1, denial_burst=5, denial_rate_per_s=0.0)
        async with environment(tmp_path, limits) as env:
            for i in range(60):
                reader, writer = await connect(env, "atlas-discord", connect_head(f"x{i}.attacker.example"))
                assert await status_of(reader) == 403  # every request is still refused
                writer.close()
            await asyncio.sleep(0.2)
            assert len(env.records()) == 5  # burst only, not 60
            await env.proxy.flush_suppressed()
            records = await env.records_after(6)
            aggregate = [r for r in records if r["result"].get("code") == "DENIALS_SUPPRESSED"]
            assert [(a["egress"]["caller"], a["egress"]["block_reason"]) for a in aggregate] == [
                ("atlas-discord", "not_allowlisted")
            ]
            assert aggregate[0]["egress"]["task_id"] == "suppressed-55"  # 60 - 5, nothing lost
            await env.proxy.flush_suppressed()
            assert len(env.records()) == 6  # nothing pending: no empty aggregates

    run(go())


def test_f5_one_callers_flood_does_not_silence_another_caller(tmp_path: Path) -> None:
    async def go() -> None:
        limits = Limits(head_timeout_s=1, hello_timeout_s=1, denial_burst=2, denial_rate_per_s=0.0)
        async with environment(tmp_path, limits) as env:
            for i in range(10):
                reader, writer = await connect(env, "atlas-discord", connect_head(f"x{i}.attacker.example"))
                await status_of(reader)
                writer.close()
            reader, writer = await connect(env, "atlas-panels", connect_head("evil.example.org"))
            await status_of(reader)
            writer.close()
            records = await env.records_after(3)
            assert [r["egress"]["caller"] for r in records].count("atlas-panels") == 1

    run(go())


def test_f5_allowed_connections_are_never_suppressed(tmp_path: Path) -> None:
    async def go() -> None:
        limits = Limits(head_timeout_s=1, hello_timeout_s=1, denial_burst=0, denial_rate_per_s=0.0)
        async with environment(tmp_path, limits) as env:
            reader, writer = await open_tunnel(env, "atlas-google", "gmail.googleapis.com")
            writer.write(client_hello())
            await writer.drain()
            records = await env.records_after(1)
            assert records[0]["egress"]["decision"] == "allowed"
            writer.close()

    run(go())


# --- F6: the LAN's own IPv6 prefix --------------------------------------------------------------------------------


def test_f6_site_prefixes_are_never_public() -> None:
    lan = parse_networks("2001:db8:1234:5600::/64, 198.51.100.0/24")
    assert not is_public_address(normalize("2001:db8:1234:5600::10") or pytest.fail(), lan)
    # without the site prefix, a LAN host on a global-looking prefix would pass the generic table
    global_lan = parse_networks("2606:4700:4700::/48")
    ip = normalize("2606:4700:4700::1111")
    assert ip is not None
    assert is_public_address(ip)
    assert not is_public_address(ip, global_lan)
    other = normalize("2606:4700:4701::1")
    assert other is not None and is_public_address(other, global_lan)


@pytest.mark.parametrize("bad", ["not-a-network", "2001:db8::/129", "10.0.0.1/33"])
def test_f6_malformed_prefix_configuration_fails(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_networks(bad)


def test_f6_proxy_refuses_a_resolution_inside_the_lan_prefix(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            env.proxy._extra_forbidden = parse_networks("2606:4700:4700::/48")
            env.answers["gmail.googleapis.com"] = ["2606:4700:4700::1111"]
            reader, writer = await connect(env, "atlas-google", connect_head("gmail.googleapis.com"))
            assert await status_of(reader) == 403
            [record] = await env.records_after(1)
            assert record["egress"]["block_reason"] == "private_address"
            writer.close()

    run(go())


# --- F9: client resets --------------------------------------------------------------------------------------------


def reset(writer: asyncio.StreamWriter) -> None:
    sock = writer.get_extra_info("socket")
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))  # close() sends RST
    writer.close()


def test_f9_reset_during_the_head_is_audited(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await connect(env, "atlas-google", b"CONNECT gmail.googleapis.com:443 HTTP/1.1\r\n")
            await asyncio.sleep(0.1)
            reset(writer)
            [record] = await env.records_after(1)
            assert record["egress"]["decision"] == "blocked"
            assert record["result"]["code"] in ("CLIENT_RESET", "BAD_REQUEST")  # RST or EOF, never silent
            del reader

    run(go())


def test_f9_reset_during_the_hello_is_audited(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await open_tunnel(env, "atlas-google", "gmail.googleapis.com")
            writer.write(client_hello()[:20])  # part of a ClientHello, then RST
            await writer.drain()
            await asyncio.sleep(0.1)
            reset(writer)
            [record] = await env.records_after(1)
            assert record["egress"]["block_reason"] == "sni_mismatch"
            assert record["result"]["code"] in ("CLIENT_RESET", "TLS_INVALID")
            assert env.upstream.connections == 0
            del reader

    run(go())


def test_f9_reset_denials_are_rate_limited_like_the_rest(tmp_path: Path) -> None:
    async def go() -> None:
        limits = Limits(head_timeout_s=1, hello_timeout_s=1, denial_burst=3, denial_rate_per_s=0.0)
        async with environment(tmp_path, limits) as env:
            for _ in range(15):
                reader, writer = await connect(env, "atlas-google", b"CONNECT gm")
                await asyncio.sleep(0.01)
                reset(writer)
                del reader
            await asyncio.sleep(0.5)
            assert len(env.records()) == 3
            await env.proxy.flush_suppressed()
            suppressed = [r for r in env.records() if r["result"].get("code") == "DENIALS_SUPPRESSED"]
            assert sum(int(r["egress"]["task_id"].split("-")[1]) for r in suppressed) == 12

    run(go())
