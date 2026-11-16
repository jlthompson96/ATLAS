"""Sleep decision logic: every RefusalReason, wake claims, resume grace, idempotency (FI-9, OW-1 support)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from helpers import call, post_helper, sleep_body

from atlas_hostd.config import Config, SleepConfig
from atlas_hostd.models import SleepState, WakeSource
from atlas_hostd.platform.fake import EPOCH, FakePlatform, make_gpu_sample
from atlas_hostd.service import HostdApp


async def sleep(app: HostdApp, **kwargs: object) -> tuple[int, dict[str, object]]:
    response = await call(app, "POST", "/v1/sleep", sleep_body(**kwargs))  # type: ignore[arg-type]
    assert response.body is not None
    return response.status, response.body


async def test_accepts_when_all_conditions_hold(app: HostdApp, platform: FakePlatform) -> None:
    status, body = await sleep(app)
    assert status == 202
    assert body["accepted"] is True
    assert body["sleep_state"] == "s3"
    await app.wait_for_pending_sleep()
    assert platform.suspend_calls == [SleepState.S3]


async def test_reply_comes_before_sleep(platform: FakePlatform) -> None:
    app = HostdApp(platform, Config(sleep=SleepConfig(sleep_delay_s=0.2)))
    status, body = await sleep(app)
    assert status == 202
    assert body["sleep_at"] == "2026-11-16T12:00:00Z"  # now + 0.2 s, second precision
    assert platform.suspend_calls == []  # not yet
    await app.wait_for_pending_sleep()
    assert platform.suspend_calls == [SleepState.S3]


async def test_prefers_modern_standby_over_hibernate(app: HostdApp, platform: FakePlatform) -> None:
    platform.states = (SleepState.MODERN_STANDBY, SleepState.HIBERNATE)
    status, body = await sleep(app)
    assert (status, body["sleep_state"]) == (202, "modern_standby")


async def test_hibernate_only_is_the_slow_fallback(app: HostdApp, platform: FakePlatform) -> None:
    platform.states = (SleepState.HIBERNATE,)
    status, body = await sleep(app)
    assert (status, body["sleep_state"]) == (202, "hibernate")


async def test_refused_user_input_recent(app: HostdApp) -> None:
    await post_helper(app, idle=60)
    status, body = await sleep(app)
    assert status == 409
    assert body["reasons"] == ["user_input_recent"]
    assert body["accepted"] is False


async def test_user_input_exactly_at_threshold_is_idle(app: HostdApp) -> None:
    await post_helper(app, idle=1200)
    assert (await sleep(app))[0] == 202


async def test_idle_time_keeps_growing_between_reports(app: HostdApp, platform: FakePlatform) -> None:
    await post_helper(app, idle=1100)
    assert (await sleep(app, request_id="a"))[0] == 409
    platform.advance(30)  # still inside the 45 s freshness window; idle is now 1130 s
    assert (await sleep(app, request_id="b"))[0] == 409
    await post_helper(app, idle=1190)
    platform.advance(20)  # 1210 s
    assert (await sleep(app, request_id="c"))[0] == 202


async def test_refused_fullscreen_app(app: HostdApp) -> None:
    await post_helper(app, fullscreen=True)
    status, body = await sleep(app)
    assert (status, body["reasons"]) == (409, ["fullscreen_app"])


async def test_refused_foreign_gpu_busy_from_helper(app: HostdApp) -> None:
    await post_helper(app, heavy=1)
    status, body = await sleep(app)
    assert (status, body["reasons"]) == (409, ["foreign_gpu_busy"])


async def test_refused_foreign_gpu_busy_from_nvml_utilisation(app: HostdApp, platform: FakePlatform) -> None:
    platform.gpu = make_gpu_sample(util_pct=85)
    status, body = await sleep(app)
    assert (status, body["reasons"]) == (409, ["foreign_gpu_busy"])


async def test_refused_lease_active(app: HostdApp) -> None:
    put = await call(app, "PUT", "/v1/leases/l1", {"ttl_s": 60, "reason": "research_run"})
    assert put.status == 200
    status, body = await sleep(app)
    assert (status, body["reasons"]) == (409, ["lease_active"])


async def test_sleep_allowed_after_lease_released(app: HostdApp) -> None:
    await call(app, "PUT", "/v1/leases/l1", {"ttl_s": 60, "reason": "research_run"})
    await call(app, "DELETE", "/v1/leases/l1")
    assert (await sleep(app))[0] == 202


async def test_refused_not_atlas_woken_without_claim(app: HostdApp) -> None:
    status, body = await sleep(app, first_packet_at=None)
    assert (status, body["reasons"]) == (409, ["not_atlas_woken"])


async def test_refused_not_atlas_woken_when_user_pressed_power_button(app: HostdApp, platform: FakePlatform) -> None:
    platform.set_resume(EPOCH - timedelta(minutes=30), WakeSource.POWER_BUTTON)
    status, body = await sleep(app)
    assert (status, body["reasons"]) == (409, ["not_atlas_woken"])


@pytest.mark.parametrize("source", [WakeSource.INPUT_DEVICE, WakeSource.TIMER])
async def test_claim_rejected_for_non_network_sources(
    app: HostdApp, platform: FakePlatform, source: WakeSource
) -> None:
    platform.set_resume(EPOCH - timedelta(minutes=30), source)
    assert (await sleep(app))[1]["reasons"] == ["not_atlas_woken"]


async def test_claim_accepts_unknown_wake_source(app: HostdApp, platform: FakePlatform) -> None:
    platform.set_resume(EPOCH - timedelta(minutes=30), WakeSource.UNKNOWN)
    assert (await sleep(app))[0] == 202


async def test_claim_rejected_when_resume_predates_first_packet(app: HostdApp) -> None:
    # The Pi's packet went out *after* the host's last resume: the host was already awake (user woke it).
    status, body = await sleep(app, first_packet_at=EPOCH - timedelta(minutes=10))
    assert (status, body["reasons"]) == (409, ["not_atlas_woken"])


async def test_claim_clock_skew_tolerance_is_five_seconds(app: HostdApp, platform: FakePlatform) -> None:
    resume_at = EPOCH - timedelta(minutes=30)
    platform.set_resume(resume_at, WakeSource.NETWORK)
    assert (await sleep(app, request_id="in", first_packet_at=resume_at + timedelta(seconds=5)))[0] == 202
    status, body = await sleep(app, request_id="out", first_packet_at=resume_at + timedelta(seconds=6))
    assert (status, body["reasons"]) == (409, ["not_atlas_woken"])


async def test_boot_counts_as_cycle_start_when_never_slept(app: HostdApp, platform: FakePlatform) -> None:
    # Woken from full shutdown by WoL: no resume event, but the boot itself began after the packet.
    platform.resume = None
    status, _ = await sleep(app, first_packet_at=platform.boot_at - timedelta(seconds=40))
    assert status == 202


async def test_resume_before_boot_is_ignored(app: HostdApp, platform: FakePlatform) -> None:
    platform.set_resume(platform.boot_at - timedelta(days=1), WakeSource.POWER_BUTTON)
    status, _ = await sleep(app, first_packet_at=platform.boot_at - timedelta(seconds=40))
    assert status == 202  # stale pre-boot resume does not poison the claim


async def test_opt_in_allows_sleeping_a_host_the_user_woke(platform: FakePlatform) -> None:
    app = HostdApp(platform, Config(sleep=SleepConfig(sleep_delay_s=0.0, allow_sleep_if_user_woke=True)))
    platform.set_resume(EPOCH - timedelta(minutes=30), WakeSource.POWER_BUTTON)
    assert (await sleep(app, first_packet_at=None))[0] == 202


async def test_refused_sleep_unsupported(app: HostdApp, platform: FakePlatform) -> None:
    platform.states = ()
    status, body = await sleep(app)
    assert (status, body["reasons"]) == (409, ["sleep_unsupported"])


async def test_refused_resume_grace_then_allowed(app: HostdApp, platform: FakePlatform) -> None:
    platform.set_resume(EPOCH - timedelta(seconds=90), WakeSource.NETWORK)
    status, body = await sleep(app, request_id="a", first_packet_at=EPOCH - timedelta(seconds=100))
    assert (status, body["reasons"]) == (409, ["resume_grace"])
    platform.advance(31)  # 121 s since resume
    assert (await sleep(app, request_id="b", first_packet_at=EPOCH - timedelta(seconds=100)))[0] == 202


async def test_multiple_reasons_are_all_reported_in_contract_order(app: HostdApp, platform: FakePlatform) -> None:
    platform.states = ()
    platform.set_resume(EPOCH - timedelta(seconds=10), WakeSource.POWER_BUTTON)
    await post_helper(app, idle=1, fullscreen=True, heavy=2)
    await call(app, "PUT", "/v1/leases/l1", {"ttl_s": 60, "reason": "backup"})
    status, body = await sleep(app)
    assert status == 409
    assert body["reasons"] == [
        "user_input_recent",
        "fullscreen_app",
        "foreign_gpu_busy",
        "lease_active",
        "not_atlas_woken",
        "sleep_unsupported",
        "resume_grace",
    ]


async def test_signed_out_host_can_sleep(app: HostdApp, platform: FakePlatform) -> None:
    """FI-9 support: nobody signed in and no helper means user activity counts as none."""
    platform.signed_in = False
    status, _ = await sleep(app)
    assert status == 202


async def test_helper_goes_stale_after_45_seconds(app: HostdApp, platform: FakePlatform) -> None:
    await post_helper(app, idle=1)
    assert (await sleep(app, request_id="a"))[0] == 409
    platform.advance(46)
    activity = (await call(app, "GET", "/v1/activity")).body
    assert activity is not None and activity["helper_connected"] is False
    assert (await sleep(app, request_id="b"))[0] == 202


async def test_most_active_helper_report_wins(app: HostdApp) -> None:
    await post_helper(app, idle=9999, session=1)
    await post_helper(app, idle=3, session=2)  # a second (or forged) report of activity
    assert (await sleep(app))[1]["reasons"] == ["user_input_recent"]


async def test_request_id_is_idempotent(app: HostdApp, platform: FakePlatform) -> None:
    first = await call(app, "POST", "/v1/sleep", sleep_body("same"))
    await app.wait_for_pending_sleep()
    await post_helper(app, idle=1)  # would now be refused if re-evaluated
    second = await call(app, "POST", "/v1/sleep", sleep_body("same"))
    assert (second.status, second.body) == (first.status, first.body)
    assert platform.suspend_calls == [SleepState.S3]  # slept once


async def test_refusal_is_replayed_for_the_same_request_id(app: HostdApp) -> None:
    await post_helper(app, idle=1)
    first = await call(app, "POST", "/v1/sleep", sleep_body("same"))
    await post_helper(app, idle=9999)
    second = await call(app, "POST", "/v1/sleep", sleep_body("same"))
    assert first.status == second.status == 409


async def test_sleep_cancelled_if_lease_arrives_during_reply_delay(platform: FakePlatform) -> None:
    app = HostdApp(platform, Config(sleep=SleepConfig(sleep_delay_s=0.2)))
    assert (await sleep(app))[0] == 202
    await call(app, "PUT", "/v1/leases/late", {"ttl_s": 60, "reason": "interactive_session"})
    await app.wait_for_pending_sleep()
    assert platform.suspend_calls == []


async def test_status_eligibility_matches_host_side_conditions(app: HostdApp, platform: FakePlatform) -> None:
    status = (await call(app, "GET", "/v1/status")).body
    assert status is not None
    assert status["sleep_eligibility"] == {"eligible": True, "reasons": []}
    await post_helper(app, fullscreen=True)
    status = (await call(app, "GET", "/v1/status")).body
    assert status is not None
    assert status["sleep_eligibility"] == {"eligible": False, "reasons": ["fullscreen_app"]}


# ---- wake-claim window boundaries (hostd.v1 1.1.0): first_packet_at - 5 s <= cycle start <= first_packet_at + 90 s


@pytest.mark.parametrize(
    ("offset_s", "accepted"),
    [(-6, False), (-5, True), (0, True), (90, True), (91, False)],
)
async def test_claim_window_boundaries_for_a_resume(
    app: HostdApp, platform: FakePlatform, offset_s: int, accepted: bool
) -> None:
    # offset_s = cycle start minus first_packet_at
    resume_at = EPOCH - timedelta(minutes=30)
    platform.set_resume(resume_at, WakeSource.NETWORK)
    first_packet = resume_at - timedelta(seconds=offset_s)
    status, body = await sleep(app, first_packet_at=first_packet)
    assert status == (202 if accepted else 409), body
    if not accepted:
        assert body["reasons"] == ["not_atlas_woken"]


@pytest.mark.parametrize(
    ("offset_s", "accepted"),
    [(-6, False), (-5, True), (0, True), (90, True), (91, False)],
)
async def test_claim_window_boundaries_for_a_cold_boot(
    app: HostdApp, platform: FakePlatform, offset_s: int, accepted: bool
) -> None:
    platform.resume = None  # cycle start is booted_at, source unknown
    first_packet = platform.boot_at - timedelta(seconds=offset_s)
    status, body = await sleep(app, first_packet_at=first_packet)
    assert status == (202 if accepted else 409), body
    if not accepted:
        assert body["reasons"] == ["not_atlas_woken"]


async def test_old_failed_wake_attempt_cannot_claim_a_much_later_resume(app: HostdApp, platform: FakePlatform) -> None:
    platform.set_resume(EPOCH - timedelta(minutes=30), WakeSource.NETWORK)
    status, _ = await sleep(app, first_packet_at=EPOCH - timedelta(hours=3))
    assert status == 409
