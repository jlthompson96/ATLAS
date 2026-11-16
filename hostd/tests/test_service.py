"""Handlers, leases, access control and validation, driven through ``HostdApp.handle`` with ``FakePlatform``."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from helpers import PI, call, post_helper, sleep_body

from atlas_hostd.config import Config
from atlas_hostd.models import LlmServerKind
from atlas_hostd.platform.fake import EPOCH, FakePlatform, make_gpu_sample
from atlas_hostd.service import HostdApp, Listener

# ---- access control ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("peer", ["127.0.0.1", "10.77.0.2", "10.77.0.3", "192.0.2.7", "::1", "garbage"])
async def test_main_listener_rejects_any_source_but_the_pi(app: HostdApp, peer: str) -> None:
    for method, path in (("GET", "/v1/health"), ("GET", "/v1/status"), ("POST", "/v1/sleep"), ("PUT", "/v1/leases/x")):
        response = await call(app, method, path, peer=peer)
        assert response.status == 403
        assert response.body is not None and response.body["code"] == "FORBIDDEN_SOURCE"


async def test_main_listener_accepts_ipv4_mapped_pi_address(app: HostdApp) -> None:
    assert (await call(app, "GET", "/v1/health", peer="::ffff:10.77.0.1")).status == 200


@pytest.mark.parametrize("peer", [PI, "10.77.0.2", "192.0.2.7"])
async def test_helper_listener_is_loopback_only(app: HostdApp, peer: str) -> None:
    response = await post_helper_from(app, peer)
    assert response.status == 403
    assert response.body is not None and response.body["code"] == "FORBIDDEN_SOURCE"


async def post_helper_from(app: HostdApp, peer: str) -> Any:
    from helpers import helper_body

    return await call(app, "POST", "/v1/helper/activity", helper_body(), peer=peer, listener=Listener.HELPER)


@pytest.mark.parametrize("peer", ["127.0.0.1", "127.0.0.5", "::1"])
async def test_helper_listener_accepts_loopback(app: HostdApp, peer: str) -> None:
    assert (await post_helper_from(app, peer)).status == 204


async def test_listeners_do_not_serve_each_others_routes(app: HostdApp) -> None:
    assert (await call(app, "POST", "/v1/helper/activity", {}, peer=PI)).status == 404
    assert (await call(app, "GET", "/v1/health", peer="127.0.0.1", listener=Listener.HELPER)).status == 404


async def test_unknown_route_and_wrong_method(app: HostdApp) -> None:
    assert (await call(app, "GET", "/v1/nope")).status == 404
    wrong = await call(app, "DELETE", "/v1/status")
    assert wrong.status == 405 and wrong.headers["Allow"] == "GET"


# ---- status endpoints ----------------------------------------------------------------------------------------


async def test_health(app: HostdApp, platform: FakePlatform) -> None:
    platform.advance(12)
    body = (await call(app, "GET", "/v1/health")).body
    assert body == {"status": "ok", "api_version": "1.1.0", "boot_id": "boot-fake-0001", "uptime_s": 12}


async def test_gpu_report_and_nvml_unavailable(app: HostdApp, platform: FakePlatform) -> None:
    gpu = (await call(app, "GET", "/v1/gpu")).body
    assert gpu is not None
    assert gpu["vram_free_mb"] == 14336 and gpu["sampled_at"] == "2026-11-16T12:00:00Z"
    platform.gpu = None
    response = await call(app, "GET", "/v1/gpu")
    assert response.status == 503 and response.body is not None
    assert response.body["code"] == "NVML_UNAVAILABLE"
    status = (await call(app, "GET", "/v1/status")).body
    assert status is not None and status["gpu"] is None


async def test_status_reports_power_llm_and_leases(app: HostdApp, platform: FakePlatform) -> None:
    platform.listening_ports = set()
    platform.llm_kind = LlmServerKind.NONE
    await call(app, "PUT", "/v1/leases/l1", {"ttl_s": 90, "reason": "model_warmup"})
    status = (await call(app, "GET", "/v1/status")).body
    assert status is not None
    assert status["power"] == {
        "sleep_states_available": ["s3", "hibernate"],
        "preferred_sleep_state": "s3",
        "booted_at": "2026-11-16T09:00:00Z",
        "last_resume_at": "2026-11-16T11:30:00Z",
        "last_wake_source": "network",
    }
    assert status["llm_server"] == {"kind": "none", "running": False, "port_listening": False}
    assert [lease["lease_id"] for lease in status["leases"]] == ["l1"]
    assert status["sleep_eligibility"]["reasons"] == ["lease_active"]


async def test_status_with_no_resume_and_signed_out(app: HostdApp, platform: FakePlatform) -> None:
    platform.resume = None
    platform.signed_in = False
    status = (await call(app, "GET", "/v1/status")).body
    assert status is not None
    assert status["power"]["last_resume_at"] is None
    assert status["power"]["booted_at"] == "2026-11-16T09:00:00Z"
    # With no resume since boot, the wake-cycle start is the boot; Windows records no source for it.
    assert status["power"]["last_wake_source"] == "unknown"
    assert status["activity"] == {
        "user_signed_in": False,
        "helper_connected": False,
        "input_idle_s": None,
        "fullscreen_app": False,
        "foreign_gpu_busy": False,
        "reported_at": None,
    }


async def test_activity_reflects_helper(app: HostdApp, platform: FakePlatform) -> None:
    platform.signed_in = False
    await post_helper(app, idle=100, fullscreen=True)
    platform.advance(10)
    activity = (await call(app, "GET", "/v1/activity")).body
    assert activity == {
        "user_signed_in": True,  # a live helper implies a signed-in user
        "helper_connected": True,
        "input_idle_s": 110,
        "fullscreen_app": True,
        "foreign_gpu_busy": False,
        "reported_at": "2026-11-16T12:00:00Z",
    }


# ---- leases --------------------------------------------------------------------------------------------------


async def test_lease_lifecycle_create_renew_expire_delete(app: HostdApp, platform: FakePlatform) -> None:
    created = await call(app, "PUT", "/v1/leases/research-1", {"ttl_s": 60, "reason": "research_run"})
    assert created.status == 200
    assert created.body == {"lease_id": "research-1", "reason": "research_run", "expires_at": "2026-11-16T12:01:00Z"}
    assert platform.keep_awake_calls == [True]

    platform.advance(40)
    renewed = await call(app, "PUT", "/v1/leases/research-1", {"ttl_s": 60, "reason": "research_run"})
    assert renewed.body is not None and renewed.body["expires_at"] == "2026-11-16T12:01:40Z"
    platform.advance(50)  # 10 s after the original expiry: only alive because of the renewal
    await app.tick()
    assert platform.keep_awake_calls == [True]

    platform.advance(11)  # past the renewed expiry
    await app.tick()
    assert platform.keep_awake_calls == [True, False]
    status = (await call(app, "GET", "/v1/status")).body
    assert status is not None and status["leases"] == []

    await call(app, "PUT", "/v1/leases/research-1", {"ttl_s": 30, "reason": "backup"})
    assert platform.keep_awake
    deleted = await call(app, "DELETE", "/v1/leases/research-1")
    assert deleted.status == 204 and deleted.body is None
    assert platform.keep_awake_calls == [True, False, True, False]


async def test_delete_of_unknown_lease_is_204(app: HostdApp, platform: FakePlatform) -> None:
    assert (await call(app, "DELETE", "/v1/leases/never-existed")).status == 204
    assert platform.keep_awake_calls == []


async def test_keep_awake_is_held_until_the_last_lease_goes(app: HostdApp, platform: FakePlatform) -> None:
    await call(app, "PUT", "/v1/leases/a", {"ttl_s": 30, "reason": "backup"})
    await call(app, "PUT", "/v1/leases/b", {"ttl_s": 300, "reason": "backup"})
    platform.advance(31)
    await app.tick()
    assert platform.keep_awake
    await call(app, "DELETE", "/v1/leases/b")
    assert not platform.keep_awake
    assert platform.keep_awake_calls == [True, False]  # a single acquire and a single release


async def test_expired_lease_no_longer_blocks_sleep_even_before_a_tick(app: HostdApp, platform: FakePlatform) -> None:
    await call(app, "PUT", "/v1/leases/a", {"ttl_s": 30, "reason": "backup"})
    platform.advance(30)
    assert (await call(app, "POST", "/v1/sleep", sleep_body("x"))).status == 202


@pytest.mark.parametrize("ttl", [30, 1800])
async def test_ttl_bounds_are_inclusive(app: HostdApp, ttl: int) -> None:
    assert (await call(app, "PUT", "/v1/leases/a", {"ttl_s": ttl, "reason": "backup"})).status == 200


@pytest.mark.parametrize("ttl", [0, 29, 1801, -5, 60.5, "60", True, None])
async def test_ttl_out_of_range_is_400(app: HostdApp, platform: FakePlatform, ttl: object) -> None:
    response = await call(app, "PUT", "/v1/leases/a", {"ttl_s": ttl, "reason": "backup"})
    assert response.status == 400
    assert platform.keep_awake_calls == []


async def test_lease_limit_is_16(app: HostdApp) -> None:
    for i in range(16):
        assert (await call(app, "PUT", f"/v1/leases/l{i}", {"ttl_s": 60, "reason": "backup"})).status == 200
    over = await call(app, "PUT", "/v1/leases/l16", {"ttl_s": 60, "reason": "backup"})
    assert over.status == 400 and over.body is not None and over.body["code"] == "LEASE_LIMIT"
    assert (await call(app, "PUT", "/v1/leases/l0", {"ttl_s": 60, "reason": "backup"})).status == 200  # renewal ok


async def test_shutdown_releases_keep_awake(app: HostdApp, platform: FakePlatform) -> None:
    await call(app, "PUT", "/v1/leases/a", {"ttl_s": 60, "reason": "backup"})
    await app.shutdown()
    assert not platform.keep_awake


# ---- request validation --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"ttl_s": 60},
        {"reason": "backup"},
        {"ttl_s": 60, "reason": "nap"},
        {"ttl_s": 60, "reason": "backup", "extra": 1},
        [],
        "x",
    ],
)
async def test_bad_lease_bodies(app: HostdApp, body: object) -> None:
    assert (await call(app, "PUT", "/v1/leases/a", body)).status == 400


@pytest.mark.parametrize("lease_id", ["-bad", "has space", "x" * 65, "a%2Fb", "é"])
async def test_bad_lease_ids(app: HostdApp, lease_id: str) -> None:
    response = await call(app, "PUT", f"/v1/leases/{lease_id}", {"ttl_s": 60, "reason": "backup"})
    assert response.status == 400


async def test_non_json_and_wrong_content_type(app: HostdApp) -> None:
    assert (await call(app, "PUT", "/v1/leases/a", raw=b"{not json")).status == 400
    ok_body = {"ttl_s": 60, "reason": "backup"}
    assert (await call(app, "PUT", "/v1/leases/a", ok_body, content_type="text/plain")).status == 400
    assert (
        await call(app, "PUT", "/v1/leases/a", ok_body, content_type="application/json; charset=utf-8")
    ).status == 200


def _sleep(**changes: Any) -> dict[str, Any]:
    body = sleep_body()
    body.update(changes)
    return body


@pytest.mark.parametrize(
    "body",
    [
        _sleep(request_id="-x"),
        _sleep(request_id=5),
        _sleep(reason="because"),
        _sleep(atlas_idle_s=-1),
        _sleep(atlas_idle_s="1"),
        _sleep(wake={"wake_id": "w"}),
        _sleep(wake={"wake_id": "w", "first_packet_at": "yesterday"}),
        _sleep(wake={"wake_id": "w", "first_packet_at": "2026-11-16T11:00:00"}),  # no offset
        _sleep(wake={"wake_id": "w", "first_packet_at": "2026-11-16T11:00:00Z", "x": 1}),
        _sleep(wake="now"),
        {k: v for k, v in sleep_body().items() if k != "wake"},
        {**sleep_body(), "unexpected": True},
    ],
)
async def test_bad_sleep_bodies(app: HostdApp, platform: FakePlatform, body: object) -> None:
    response = await call(app, "POST", "/v1/sleep", body)
    assert response.status == 400
    assert response.body is not None and response.body["code"] == "INVALID_BODY"
    assert platform.suspend_calls == []


async def test_sleep_accepts_timestamp_offsets_and_fractions(app: HostdApp) -> None:
    body = sleep_body()
    body["wake"] = {
        "wake_id": "w",
        "first_packet_at": (EPOCH - timedelta(minutes=31)).isoformat()[:-6] + ".123456+00:00",
    }
    assert (await call(app, "POST", "/v1/sleep", body)).status == 202


@pytest.mark.parametrize(
    "change",
    [
        {"session_id": -1},
        {"input_idle_s": -1},
        {"fullscreen_app": "yes"},
        {"gpu_heavy_process_count": 1.5},
        {"reported_at": "now"},
        {"extra": 1},
    ],
)
async def test_bad_helper_reports(app: HostdApp, change: dict[str, Any]) -> None:
    from helpers import helper_body

    body = {**helper_body(), **change}
    response = await call(app, "POST", "/v1/helper/activity", body, peer="127.0.0.1", listener=Listener.HELPER)
    assert response.status == 400


async def test_validation_errors_do_not_echo_input(app: HostdApp) -> None:
    marker = "tok_MARKER_value"
    response = await call(app, "PUT", "/v1/leases/a", {"ttl_s": 60, "reason": marker})
    assert response.status == 400
    assert response.body is not None and marker not in response.body["message"]


def test_default_config_never_binds_wildcard() -> None:
    assert Config().listener.address == "10.77.0.2"
    assert Config().helper_listener.address == "127.0.0.1"


async def test_no_sleep_state_gives_null_preferred_and_unsupported_reason(
    app: HostdApp, platform: FakePlatform
) -> None:
    platform.states = ()
    status = (await call(app, "GET", "/v1/status")).body
    assert status is not None
    assert status["power"]["sleep_states_available"] == []
    assert status["power"]["preferred_sleep_state"] is None
    assert "sleep_unsupported" in status["sleep_eligibility"]["reasons"]


async def test_gpu_utilisation_ignored_as_foreign_load_while_a_lease_is_held(
    app: HostdApp, platform: FakePlatform
) -> None:
    platform.gpu = make_gpu_sample(util_pct=90)
    await call(app, "PUT", "/v1/leases/l1", {"ttl_s": 60, "reason": "interactive_session"})
    activity = (await call(app, "GET", "/v1/activity")).body
    assert activity is not None and activity["foreign_gpu_busy"] is False
