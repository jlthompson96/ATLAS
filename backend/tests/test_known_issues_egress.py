"""Regression tests for known issue N3 (docs/known-issues.md): one caller's tunnel flood must not exhaust the egress
audit budget that every other caller depends on. Each failed before the fix.

The reviewer's reproduction (7,944 allowed tunnels in 3 s from one caller to its own host) is rebuilt end to end:
the proxy writes through a real ``AuditClient`` to a real ``AuditServer`` with its production budgets.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.audit.client import AuditClient
from atlas.audit.log import AuditConfig, AuditLog
from atlas.audit.segments import list_segments
from atlas.audit.server import AuditServer
from atlas.egress.addresses import IPAddress
from atlas.egress.config import load_config
from atlas.egress.proxy import EgressProxy, Limits
from tests.egress_support import client_hello
from tests.test_audit_server import FakeCreds, FakeUsers, line, raw_exchange
from tests.test_egress_proxy import connect, connect_head, environment, status_of


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def egress_records(log: AuditLog) -> list[dict[str, Any]]:
    out = [json.loads(x) for _, p in list_segments(log.data_dir) for x in p.read_text().splitlines()]
    return [r for r in out if r["kind"] == "egress"]


@contextlib.asynccontextmanager
async def proxy_with_real_audit(tmp: Path) -> AsyncIterator[tuple[EgressProxy, dict[str, int], AuditLog]]:
    """The production wiring: proxy -> AuditClient -> Unix socket -> AuditServer (default budgets) -> AuditLog."""

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError):
            while data := await reader.read(4096):
                writer.write(data)
                await writer.drain()
        writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    upstream_port = upstream.sockets[0].getsockname()[1]

    async def resolver(host: str) -> list[str]:
        return ["142.250.80.46"]

    async def connector(ip: IPAddress, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    log = AuditLog.open(AuditConfig(data_dir=tmp / "audit"), Ed25519PrivateKey.generate())
    creds = FakeCreds()
    creds.uid = 1003  # every connection to the audit socket comes from atlas-egress
    server = AuditServer(log, creds, FakeUsers(), tick_interval_s=3600)
    sock = tmp / "a.sock"
    await server.listen_path(sock)
    client = AuditClient(sock)
    config = load_config()
    proxy = EgressProxy(
        config,
        client,
        resolver=resolver,
        connector=connector,
        limits=Limits(head_timeout_s=1, hello_timeout_s=1),
        ports=dict.fromkeys(config.callers, 0),
    )
    ports = await proxy.start()
    try:
        yield proxy, ports, log
    finally:
        await proxy.close()
        await client.close()
        await server.close()
        upstream.close()
        log.close()


async def tunnel(ports: dict[str, int], caller: str, host: str) -> tuple[int | None, bytes]:
    """Open one tunnel, send a ClientHello, return the CONNECT status and what came back through the tunnel."""
    reader, writer = await connect_and(ports, caller, host)
    try:
        status = await status_of(reader)
        if status != 200:
            return status, b""
        await reader.readuntil(b"\r\n")
        hello = client_hello(host)
        writer.write(hello)
        await writer.drain()
        try:
            echoed = await asyncio.wait_for(reader.readexactly(len(hello)), 2)
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            echoed = b""
        return status, echoed
    finally:
        writer.close()


async def connect_and(ports: dict[str, int], caller: str, host: str) -> tuple[asyncio.StreamReader, Any]:
    reader, writer = await asyncio.open_connection("127.0.0.1", ports[caller])
    writer.write(connect_head(host))
    await writer.drain()
    return reader, writer


def test_n3_one_callers_tunnel_flood_does_not_starve_another_caller() -> None:
    async def go() -> None:
        with tempfile.TemporaryDirectory(prefix="atl") as directory:
            async with proxy_with_real_audit(Path(directory)) as (proxy, ports, log):
                statuses = []
                for _ in range(120):  # a caller hammering its own allowed host
                    status, _ = await tunnel(ports, "atlas-discord", "discord.com")
                    statuses.append(status)
                status, echoed = await tunnel(ports, "atlas-google", "gmail.googleapis.com")
                assert status == 200
                assert echoed == client_hello("gmail.googleapis.com")  # the tunnel really carries data
                assert 429 in statuses  # the flooding caller is slowed down by the proxy, not by the audit service
                assert statuses.count(200) < 60
                await proxy.flush_suppressed()
                await asyncio.sleep(0.1)
                google = [r for r in egress_records(log) if r["egress"]["caller"] == "atlas-google"]
                assert google and google[0]["egress"]["decision"] == "allowed"

    run(go())


def test_n3_tunnel_opens_are_rate_limited_per_caller_with_429(tmp_path: Path) -> None:
    async def go() -> None:
        limits = Limits(head_timeout_s=1, hello_timeout_s=1, open_burst=2, open_rate_per_s=0.0)
        async with environment(tmp_path, limits) as env:
            for expected in (200, 200, 429):
                reader, writer = await connect(env, "atlas-panels", connect_head("api.open-meteo.com"))
                assert await status_of(reader) == expected
                writer.close()
            reader, writer = await connect(env, "atlas-google", connect_head("gmail.googleapis.com"))
            assert await status_of(reader) == 200  # another caller has its own bucket
            writer.close()
            records = await env.records_after(1)
            limited = [r for r in records if r["result"].get("code") == "RATE_LIMITED"]
            assert len(limited) == 1
            assert limited[0]["egress"]["caller"] == "atlas-panels"
            assert limited[0]["egress"]["block_reason"] == "limit"
            assert limited[0]["result"]["status"] == "refused"

    run(go())


def test_n3_atlas_egress_has_a_larger_audit_budget_than_other_actors(make_log: Any) -> None:
    async def go() -> None:
        log = make_log()
        creds = FakeCreds()
        server = AuditServer(log, creds, FakeUsers(), tick_interval_s=3600, monotonic=lambda: 0.0)
        with tempfile.TemporaryDirectory(prefix="atl") as directory:
            sock = Path(directory) / "a.sock"
            await server.listen_path(sock)
            try:
                body = {"kind": "system", "context": "system", "system": {"event": "service_start"}}
                payload = line({"op": "append", "record": body}) * 120
                creds.uid = 1001  # atlas-core: the default budget (burst 50)
                core = await raw_exchange(sock, payload)
                creds.uid = 1003  # atlas-egress writes for every caller
                egress = await raw_exchange(sock, payload)
            finally:
                await server.close()
        assert sum(r["ok"] for r in core) == 50
        assert all(r["ok"] for r in egress)

    run(go())


def test_n3_lost_best_effort_records_are_counted_in_the_aggregate(tmp_path: Path) -> None:
    async def go() -> None:
        async with environment(tmp_path) as env:
            reader, writer = await connect(env, "atlas-google", connect_head("gmail.googleapis.com"))
            assert await status_of(reader) == 200
            await reader.readuntil(b"\r\n")
            hello = client_hello()
            writer.write(hello)
            await writer.drain()
            await asyncio.wait_for(reader.readexactly(5 + len(hello)), 2)  # "echo:" + hello
            await env.records_after(1)  # the open record is written (fail closed if not)
            env.sink.down = True  # the audit service now refuses: the close record (with byte counts) is lost
            writer.close()
            for i in range(3):  # and so are these denial records
                r, w = await connect(env, "atlas-google", connect_head(f"x{i}.attacker.example"))
                assert await status_of(r) == 403
                w.close()
            await asyncio.sleep(0.2)
            assert len(env.records()) == 1
            await env.proxy.flush_suppressed()  # still down: nothing may be dropped
            env.sink.down = False
            await env.proxy.flush_suppressed()
            records = await env.records_after(3)
            lost = [r for r in records if r["result"].get("code") == "AUDIT_RECORDS_LOST"]
            assert len(lost) == 1
            assert lost[0]["egress"]["caller"] == "atlas-google"
            assert lost[0]["egress"]["task_id"] == "suppressed-1"
            assert lost[0]["egress"]["bytes_out"] == len(hello)
            assert lost[0]["egress"]["bytes_in"] == 5 + len(hello)
            denied = [r for r in records if r["result"].get("code") == "DENIALS_SUPPRESSED"]
            assert [(d["egress"]["block_reason"], d["egress"]["task_id"]) for d in denied] == [
                ("not_allowlisted", "suppressed-3")
            ]

    run(go())
