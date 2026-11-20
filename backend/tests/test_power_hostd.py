"""HostdClient against a fake atlas-hostd, with response bodies shaped like contracts/hostd.v1.yaml; plus WoL packets."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable, Coroutine
from typing import Any

import pytest

from atlas.net.http import parse_http_url
from atlas.power.hostd import (
    HostdClient,
    HostdError,
    HostdForbidden,
    HostdUnreachable,
    SleepNo,
    SleepOk,
)
from atlas.power.wol import UdpWolSender, magic_packet, parse_mac
from tests.net_support import FakeServer, Reply, Request, closed_port

HEALTH = {"status": "ok", "api_version": "1.1.0", "boot_id": "boot-7", "uptime_s": 42}
STATUS = {
    "hostd_version": "0.1.0",
    "api_version": "1.1.0",
    "boot_id": "boot-7",
    "os": {"name": "Windows 11 Pro", "build": "26100"},
    "power": {
        "sleep_states_available": ["s3"],
        "preferred_sleep_state": "s3",
        "booted_at": "2026-11-17T08:00:00Z",
        "last_resume_at": None,
        "last_wake_source": "network",
    },
    "llm_server": {"kind": "lmstudio", "running": True, "port_listening": True},
    "gpu": None,
    "activity": {
        "user_signed_in": True,
        "helper_connected": True,
        "input_idle_s": 5,
        "fullscreen_app": False,
        "foreign_gpu_busy": False,
        "reported_at": None,
    },
    "leases": [],
    "sleep_eligibility": {"eligible": False, "reasons": ["user_input_recent"]},
}
GPU = {
    "name": "RTX 3070",
    "driver_version": "560.1",
    "vram_total_mb": 8192,
    "vram_used_mb": 1200,
    "vram_free_mb": 6992,
    "util_pct": 3,
    "temp_c": 40,
    "throttling": [],
    "llm_server_vram_mb": 0,
    "foreign_vram_mb": 1200,
    "foreign_process_count": 2,
    "sampled_at": "2026-11-17T10:00:00Z",
}


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def hostd(routes: dict[tuple[str, str], Reply]) -> Callable[[Request], Reply]:
    def handle(req: Request) -> Reply:
        return routes.get((req.method, req.path), Reply.of_json({"code": "NOT_FOUND", "message": "no"}, 404))

    return handle


async def with_hostd(
    routes: dict[tuple[str, str], Reply], body: Callable[[FakeServer, HostdClient], Coroutine[Any, Any, Any]]
) -> Any:
    server = await FakeServer(hostd(routes)).start()
    try:
        return await body(server, HostdClient(server.url))
    finally:
        await server.close()


def test_health_and_status_are_parsed() -> None:
    async def body(server: FakeServer, client: HostdClient) -> None:
        h = await client.health()
        assert (h.api_version, h.boot_id, h.uptime_s) == ("1.1.0", "boot-7", 42)
        s = await client.status()
        assert s.user_signed_in and s.llm_server.running and not s.sleep_eligible
        assert s.sleep_blockers == ("user_input_recent",) and s.last_wake_source == "network"

    run(with_hostd({("GET", "/v1/health"): Reply.of_json(HEALTH), ("GET", "/v1/status"): Reply.of_json(STATUS)}, body))


def test_gpu_report_and_missing_nvml() -> None:
    async def ok(server: FakeServer, client: HostdClient) -> None:
        gpu = await client.gpu()
        assert gpu is not None and gpu.vram_free_mb == 6992 and gpu.foreign_vram_mb == 1200

    run(with_hostd({("GET", "/v1/gpu"): Reply.of_json(GPU)}, ok))

    async def no_nvml(server: FakeServer, client: HostdClient) -> None:
        assert await client.gpu() is None

    run(with_hostd({("GET", "/v1/gpu"): Reply.of_json({"code": "NVML_UNAVAILABLE", "message": "x"}, 503)}, no_nvml))


def test_sleep_accepted_sends_the_contract_body() -> None:
    accepted = {"request_id": "s1", "accepted": True, "sleep_state": "s3", "sleep_at": "2026-11-17T10:00:02Z"}

    async def body(server: FakeServer, client: HostdClient) -> None:
        wake = {"wake_id": "w1", "first_packet_at": "2026-11-17T09:00:00Z"}
        answer = await client.sleep(request_id="s1", reason="idle", atlas_idle_s=1300, wake=wake)
        assert answer == SleepOk("s3")
        sent = server.requests[-1].json()
        assert sent == {"request_id": "s1", "reason": "idle", "atlas_idle_s": 1300, "wake": wake}
        assert server.requests[-1].headers["content-type"] == "application/json"
        none = await client.sleep(request_id="s2", reason="idle", atlas_idle_s=0, wake=None)
        assert none == SleepOk("s3") and server.requests[-1].json()["wake"] is None

    run(with_hostd({("POST", "/v1/sleep"): Reply.of_json(accepted, 202)}, body))


def test_sleep_refused_lists_the_reasons() -> None:
    refused = {"request_id": "s1", "accepted": False, "reasons": ["fullscreen_app", "lease_active"]}

    async def body(server: FakeServer, client: HostdClient) -> None:
        answer = await client.sleep(request_id="s1", reason="idle", atlas_idle_s=1300, wake=None)
        assert answer == SleepNo(("fullscreen_app", "lease_active"))

    run(with_hostd({("POST", "/v1/sleep"): Reply.of_json(refused, 409)}, body))


def test_403_means_wrong_source_address_and_is_distinct_from_unreachable() -> None:
    async def body(server: FakeServer, client: HostdClient) -> None:
        with pytest.raises(HostdForbidden):
            await client.health()

    run(with_hostd({("GET", "/v1/health"): Reply.of_json({"code": "FORBIDDEN", "message": "no"}, 403)}, body))
    with pytest.raises(HostdUnreachable):
        run(HostdClient(f"http://127.0.0.1:{closed_port()}").health())


@pytest.mark.parametrize(
    "reply",
    [Reply(body=b"not json"), Reply.of_json([1, 2]), Reply.of_json({"status": "ok"}), Reply.of_json({}, 500)],
)
def test_garbage_from_hostd_is_an_error_not_a_crash(reply: Reply) -> None:
    async def body(server: FakeServer, client: HostdClient) -> None:
        with pytest.raises(HostdError):
            await client.health()

    run(with_hostd({("GET", "/v1/health"): reply}, body))


def test_a_silent_hostd_is_unreachable_within_the_health_budget() -> None:
    async def body(server: FakeServer, client: HostdClient) -> None:
        with pytest.raises(HostdUnreachable):
            await client.health()

    run(with_hostd({("GET", "/v1/health"): Reply(stall=True)}, body))


def test_requests_carry_no_credentials_and_only_plain_urls_parse() -> None:
    async def body(server: FakeServer, client: HostdClient) -> None:
        await client.health()
        assert "authorization" not in server.requests[0].headers  # hostd has no HTTP credential (hostd.v1.yaml)

    run(with_hostd({("GET", "/v1/health"): Reply.of_json(HEALTH)}, body))
    assert parse_http_url("http://10.77.0.2:8770").port == 8770
    with pytest.raises(ValueError):
        HostdClient("https://10.77.0.2:8770")


# --- Wake-on-LAN ------------------------------------------------------------------------------------------------


def test_magic_packet_is_six_ff_then_the_mac_sixteen_times() -> None:
    mac = parse_mac("AA:bb:cc:dd:ee:01")
    packet = magic_packet(mac)
    assert len(packet) == 102 and packet[:6] == b"\xff" * 6
    assert all(packet[6 + 6 * i : 12 + 6 * i] == mac for i in range(16))
    assert parse_mac("aa-bb-cc-dd-ee-01") == mac


@pytest.mark.parametrize(
    "bad", ["", "aabbccddeeff", "aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee:gg", "aa:bb-cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff:00"]
)
def test_bad_macs_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_mac(bad)


def test_udp_sender_delivers_the_packet_to_the_target() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(2)
        port = receiver.getsockname()[1]
        UdpWolSender("aa:bb:cc:dd:ee:01", broadcast="127.0.0.1", port=port).send_magic()
        data, _ = receiver.recvfrom(256)
    assert data == magic_packet(parse_mac("aa:bb:cc:dd:ee:01"))
