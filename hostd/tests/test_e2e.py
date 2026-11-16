"""End to end: real sockets on 127.0.0.1 with ephemeral ports, real HTTP bytes, ``FakePlatform`` underneath."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

import jsonschema
import pytest
from helpers import helper_body, sleep_body
from test_contract import BASE, REGISTRY, response_schema, spec_operations

from atlas_hostd import http as http_module
from atlas_hostd.config import Config, HelperListenerConfig, ListenerConfig, SleepConfig
from atlas_hostd.models import SleepState, WakeSource
from atlas_hostd.platform.fake import EPOCH, FakePlatform
from atlas_hostd.server import RunningHostd, start


@dataclass(frozen=True)
class Reply:
    status: int
    headers: dict[str, str]
    body: Any


async def raw_request(port: int, payload: bytes) -> Reply:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(payload)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
    assert int(headers["content-length"]) == len(body)
    return Reply(int(lines[0].split(" ")[1]), headers, json.loads(body) if body else None)


async def request(
    port: int, method: str, path: str, body: Any = None, *, content_type: str = "application/json"
) -> Reply:
    payload = b"" if body is None else json.dumps(body).encode()
    head = f"{method} {path} HTTP/1.1\r\nHost: test\r\n"
    if payload:
        head += f"Content-Type: {content_type}\r\nContent-Length: {len(payload)}\r\n"
    return await raw_request(port, head.encode() + b"\r\n" + payload)


def make_config(allowed_source: str = "127.0.0.1") -> Config:
    return Config(
        listener=ListenerConfig(address="127.0.0.1", port=0, allowed_source=allowed_source),
        helper_listener=HelperListenerConfig(port=0),
        sleep=SleepConfig(sleep_delay_s=0.0),
    )


@asynccontextmanager
async def running(platform: FakePlatform, config: Config | None = None) -> AsyncIterator[RunningHostd]:
    server = await start(config or make_config(), platform)
    try:
        yield server
    finally:
        await server.stop()


def validate(method: str, template: str, reply: Reply) -> None:
    operation = spec_operations()[(method, template)]
    schema = response_schema(operation, reply.status)
    if schema is None:
        assert reply.body is None
        return
    jsonschema.Draft202012Validator(schema, registry=REGISTRY).validate(reply.body)
    assert BASE  # the registry is anchored to the contract document


async def test_full_flow_over_real_http(platform: FakePlatform) -> None:
    async with running(platform) as server:
        main, helper = server.main.port, server.helper.port
        assert main != 0 and helper != 0 and main != helper

        health = await request(main, "GET", "/v1/health")
        assert health.status == 200 and health.body["status"] == "ok"
        validate("GET", "/v1/health", health)

        status = await request(main, "GET", "/v1/status")
        assert status.status == 200
        validate("GET", "/v1/status", status)

        gpu = await request(main, "GET", "/v1/gpu")
        assert gpu.status == 200
        validate("GET", "/v1/gpu", gpu)

        # The helper reports user activity on its own listener; hostd then refuses to sleep.
        posted = await request(helper, "POST", "/v1/helper/activity", helper_body(idle=3))
        assert posted.status == 204 and posted.body is None
        activity = await request(main, "GET", "/v1/activity")
        assert activity.body["helper_connected"] is True and activity.body["input_idle_s"] == 3
        validate("GET", "/v1/activity", activity)
        refused = await request(main, "POST", "/v1/sleep", sleep_body("r1"))
        assert refused.status == 409 and refused.body["reasons"] == ["user_input_recent"]
        validate("POST", "/v1/sleep", refused)

        # Lease create, hold, release.
        lease = await request(main, "PUT", "/v1/leases/e2e-1", {"ttl_s": 60, "reason": "research_run"})
        assert lease.status == 200
        validate("PUT", "/v1/leases/{lease_id}", lease)
        assert platform.keep_awake
        released = await request(main, "DELETE", "/v1/leases/e2e-1")
        assert released.status == 204
        assert not platform.keep_awake

        # The user walks away; ATLAS-woken host accepts the sleep and then suspends.
        platform.advance(2000)
        platform.set_resume(EPOCH - timedelta(minutes=30), WakeSource.NETWORK)
        await request(helper, "POST", "/v1/helper/activity", helper_body(idle=1500))
        accepted = await request(
            main, "POST", "/v1/sleep", sleep_body("r2", first_packet_at=EPOCH - timedelta(minutes=31))
        )
        assert accepted.status == 202, accepted.body
        validate("POST", "/v1/sleep", accepted)
        await server.app.wait_for_pending_sleep()
        assert platform.suspend_calls == [SleepState.S3]


async def test_listener_binds_only_the_configured_address(platform: FakePlatform) -> None:
    async with running(platform) as server:
        assert server.main._server is not None and server.helper._server is not None
        for listener in (server.main, server.helper):
            names = {sock.getsockname()[0] for sock in listener._server.sockets}  # type: ignore[union-attr]
            assert names == {"127.0.0.1"}


async def test_source_other_than_the_pi_gets_403_over_the_wire(platform: FakePlatform) -> None:
    # We connect from 127.0.0.1 but only 10.77.0.1 is allowed.
    async with running(platform, make_config(allowed_source="10.77.0.1")) as server:
        port = server.main.port
        for method, path, body in (
            ("GET", "/v1/health", None),
            ("POST", "/v1/sleep", sleep_body()),
            ("PUT", "/v1/leases/x", {"ttl_s": 60, "reason": "backup"}),
        ):
            reply = await request(port, method, path, body)
            assert reply.status == 403
            assert reply.body["code"] == "FORBIDDEN_SOURCE"
            validate(method, path if not path.startswith("/v1/leases") else "/v1/leases/{lease_id}", reply)
        assert platform.keep_awake_calls == [] and platform.suspend_calls == []


async def test_health_is_fast(platform: FakePlatform) -> None:
    async with running(platform) as server:
        await request(server.main.port, "GET", "/v1/health")  # warm up
        started = time.perf_counter()
        for _ in range(10):
            await request(server.main.port, "GET", "/v1/health")
        assert (time.perf_counter() - started) / 10 < 0.05  # contract: < 50 ms


async def test_malformed_and_oversized_requests_are_400(platform: FakePlatform) -> None:
    async with running(platform) as server:
        port = server.main.port
        big = b"x" * (http_module.MAX_BODY_BYTES + 1)
        too_big = await raw_request(
            port,
            b"PUT /v1/leases/a HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: "
            + str(len(big)).encode()
            + b"\r\n\r\n"
            + big,
        )
        assert too_big.status == 400 and too_big.body["code"] == "BODY_TOO_LARGE"

        chunked = await raw_request(port, b"POST /v1/sleep HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        assert chunked.status == 400

        for payload in (
            b"GARBAGE\r\n\r\n",
            b"GET v1/health HTTP/1.1\r\n\r\n",
            b"GET /v1/health HTTP/1.1\r\nBad Header\r\n\r\n",
            b"POST /v1/sleep HTTP/1.1\r\nContent-Length: abc\r\n\r\n",
            b"POST /v1/sleep HTTP/1.1\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}",
        ):
            reply = await raw_request(port, payload)
            assert reply.status == 400, payload
            assert reply.body["code"] in ("INVALID_BODY", "BODY_TOO_LARGE")


async def test_slow_client_times_out_without_blocking_others(
    platform: FakePlatform, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_module, "READ_TIMEOUT_S", 0.2)
    async with running(platform) as server:
        port = server.main.port
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /v1/hea")  # never finishes
        await writer.drain()
        healthy = await request(port, "GET", "/v1/health")
        assert healthy.status == 200
        data = await asyncio.wait_for(reader.read(), 2)
        assert data.startswith(b"HTTP/1.1 400")
        writer.close()


async def test_query_string_is_ignored_and_405_has_allow(platform: FakePlatform) -> None:
    async with running(platform) as server:
        assert (await request(server.main.port, "GET", "/v1/health?x=1")).status == 200
        wrong = await request(server.main.port, "POST", "/v1/health", {})
        assert wrong.status == 405 and wrong.headers["allow"] == "GET"


async def test_shutdown_releases_keep_awake(platform: FakePlatform) -> None:
    server = await start(replace(make_config()), platform)
    await request(server.main.port, "PUT", "/v1/leases/a", {"ttl_s": 60, "reason": "backup"})
    assert platform.keep_awake
    await server.stop()
    assert not platform.keep_awake and platform.closed
