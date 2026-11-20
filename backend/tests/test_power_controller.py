"""HostPowerController policy: wake rate limits and windows, backoff, idempotence, sleep conditions, audit."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from atlas.core.auditing import RecordingAuditSink
from atlas.power.controller import HostPowerController, PowerConfig
from atlas.power.hostd import SleepNo, SleepOk
from atlas.power.interface import (
    PowerState,
    SleepAccepted,
    SleepReason,
    SleepRefused,
    WakeAttempt,
    WakeReason,
    WakeRefusal,
    WakeRefused,
)
from tests.power_support import FakeClock, FakeHostd, FakeSender, World


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def make(
    *, busy: bool = False, config: PowerConfig | None = None
) -> tuple[HostPowerController, FakeClock, World, FakeHostd, FakeSender, RecordingAuditSink]:
    clock = FakeClock()
    world = World(clock)
    hostd, sender, audit = FakeHostd(world), FakeSender(clock), RecordingAuditSink("atlas-core")
    controller = HostPowerController(hostd, sender, audit, clock=clock, config=config, queue_busy=lambda: busy)
    return controller, clock, world, hostd, sender, audit


def power(audit: RecordingAuditSink) -> list[dict[str, Any]]:
    return [r["power"] for r in audit.of_kind("power")]


def refused(result: object) -> WakeRefusal:
    assert isinstance(result, WakeRefused)
    return result.reason


# --- state ------------------------------------------------------------------------------------------------------


def test_state_follows_hostd_and_the_wake_in_flight() -> None:
    c, clock, world, *_ = make()
    assert run(c.state()) is PowerState.UNKNOWN  # never saw it, never put it to sleep
    attempt = run(c.wake(WakeReason.MANUAL))
    assert isinstance(attempt, WakeAttempt) and run(c.state()) is PowerState.WAKING
    world.hostd_up_at = clock.t
    clock.advance(3)  # past the 2 s probe cache
    assert run(c.state()) is PowerState.UP


def test_state_is_asleep_only_after_atlas_put_it_to_sleep() -> None:
    c, clock, world, _hostd, *_ = make()
    world.hostd_up_at = clock.t
    c.note_llm_request()
    clock.advance(25 * 60)
    # complete a wake so there is a claim: asleep is only claimed for a host ATLAS woke and slept
    world.hostd_up_at = None
    attempt = run(c.wake(WakeReason.INTERACTIVE))
    assert isinstance(attempt, WakeAttempt)
    world.hostd_up_at = clock.t
    run(c.complete_wake(attempt, ok=True, stage="model"))
    clock.advance(25 * 60)
    assert isinstance(run(c.request_sleep(SleepReason.IDLE)), SleepAccepted)
    world.hostd_up_at = None  # the host went to sleep
    clock.advance(3)
    assert run(c.state()) is PowerState.ASLEEP


def test_a_403_from_hostd_is_unknown_not_up() -> None:
    c, _, world, hostd, *_ = make()
    world.hostd_up_at = 0.0
    hostd.forbidden = True
    assert run(c.state()) is PowerState.UNKNOWN


# --- wake policy ------------------------------------------------------------------------------------------------------


def test_wake_is_idempotent_while_in_flight_and_sends_the_first_packet_at_once() -> None:
    c, _clock, _, _, sender, audit = make()
    first = run(c.wake(WakeReason.INTERACTIVE))
    second = run(c.wake(WakeReason.SPECULATIVE))
    assert isinstance(first, WakeAttempt) and second is first
    assert len(sender.sent_at) == 1 and [e["event"] for e in power(audit)] == ["wake_requested"]
    assert first.deadline_monotonic - first.started_at_monotonic == 60


def test_scheduled_research_has_a_120s_deadline_and_only_runs_in_the_window() -> None:
    c, clock, *_ = make()
    clock.set_hour(14)
    assert refused(run(c.wake(WakeReason.SCHEDULED_RESEARCH))) is WakeRefusal.OUTSIDE_WINDOW  # OW-1: no wake at 14:00
    clock.set_hour(1)
    attempt = run(c.wake(WakeReason.SCHEDULED_RESEARCH))
    assert isinstance(attempt, WakeAttempt) and attempt.deadline_monotonic - attempt.started_at_monotonic == 120


def test_research_window_edges() -> None:
    for hour, allowed in [(0, False), (1, True), (5, True), (6, False), (23, False)]:
        c, clock, *_ = make()
        clock.set_hour(hour)
        result = run(c.wake(WakeReason.SCHEDULED_RESEARCH))
        assert isinstance(result, WakeAttempt) == allowed, hour


def test_speculative_wakes_only_in_active_hours() -> None:
    for hour, allowed in [(6, False), (7, True), (22, True), (23, False), (3, False)]:
        c, clock, *_ = make()
        clock.set_hour(hour)
        result = run(c.wake(WakeReason.SPECULATIVE))
        assert isinstance(result, WakeAttempt) == allowed, hour
        if not allowed:
            assert refused(result) is WakeRefusal.OUTSIDE_ACTIVE_HOURS


def test_speculative_wakes_are_limited_to_six_per_hour_with_a_retry_hint() -> None:
    c, clock, *_ = make()
    for _ in range(6):
        attempt = run(c.wake(WakeReason.SPECULATIVE))
        assert isinstance(attempt, WakeAttempt)
        clock.advance(61)  # attempt expires, the host never came up
    result = run(c.wake(WakeReason.SPECULATIVE))
    assert refused(result) is WakeRefusal.RATE_LIMITED
    assert isinstance(result, WakeRefused) and result.retry_after_s is not None and 0 < result.retry_after_s <= 3600
    clock.advance(3600)
    assert isinstance(run(c.wake(WakeReason.SPECULATIVE)), WakeAttempt)
    # interactive wakes are never rate limited
    clock.advance(61)
    assert isinstance(run(c.wake(WakeReason.INTERACTIVE)), WakeAttempt)


def test_retry_wakes_back_off_1_5_15_minutes_then_hold_until_a_manual_wake() -> None:
    c, clock, _, _, _, audit = make()
    for expected_wait in (60, 300, 900):
        attempt = run(c.wake(WakeReason.INTERACTIVE if expected_wait == 60 else WakeReason.RETRY))
        assert isinstance(attempt, WakeAttempt)
        run(c.complete_wake(attempt, ok=False, stage="packet", code="WAKE_FAILED"))
        too_soon = run(c.wake(WakeReason.RETRY))
        assert refused(too_soon) is WakeRefusal.BACKOFF
        assert isinstance(too_soon, WakeRefused) and too_soon.retry_after_s is not None
        assert expected_wait - 1 <= too_soon.retry_after_s <= expected_wait
        clock.advance(expected_wait)
    held = run(c.wake(WakeReason.RETRY))
    assert isinstance(held, WakeAttempt)  # the third backoff elapsed, one more try
    run(c.complete_wake(held, ok=False, stage="packet", code="WAKE_FAILED"))
    clock.advance(10_000)
    held_for_good = run(c.wake(WakeReason.RETRY))
    assert refused(held_for_good) is WakeRefusal.BACKOFF and held_for_good.retry_after_s is None  # type: ignore[union-attr]
    manual = run(c.wake(WakeReason.MANUAL))
    assert isinstance(manual, WakeAttempt)
    run(c.complete_wake(manual, ok=True, stage="model"))
    assert run(c.wake(WakeReason.RETRY))  # a successful wake clears the hold
    assert any(e["event"] == "wake_failed" and e["stage"] == "packet" for e in power(audit))


def test_wake_refuses_when_the_host_is_already_up() -> None:
    c, _clock, world, *_ = make()
    world.hostd_up_at = 0.0
    assert refused(run(c.wake(WakeReason.INTERACTIVE))) is WakeRefusal.ALREADY_UP


def test_every_decision_is_audited_with_a_valid_power_record() -> None:
    c, clock, _world, *_ = make()
    clock.set_hour(3)
    run(c.wake(WakeReason.SPECULATIVE))
    attempt = run(c.wake(WakeReason.INTERACTIVE))
    assert isinstance(attempt, WakeAttempt)
    run(c.complete_wake(attempt, ok=True, stage="model"))
    events = power(c._audit)  # type: ignore[arg-type]
    assert [e["event"] for e in events] == ["wake_refused", "wake_requested", "wake_ready"]
    assert events[0]["code"] == "OUTSIDE_ACTIVE_HOURS" and events[0]["reason"] == "speculative"
    assert events[1]["wake_id"] == attempt.wake_id and events[2]["stage"] == "model"
    assert "elapsed_ms" in events[2]


# --- sleep policy -------------------------------------------------------------------------------------------------


def woken(
    c: HostPowerController, clock: FakeClock, world: World, reason: WakeReason = WakeReason.INTERACTIVE
) -> WakeAttempt:
    attempt = run(c.wake(reason))
    assert isinstance(attempt, WakeAttempt)
    world.hostd_up_at = clock.t
    run(c.complete_wake(attempt, ok=True, stage="model"))
    return attempt


def test_sleep_waits_for_20_minutes_without_an_llm_request() -> None:
    c, clock, world, hostd, _, audit = make()
    attempt = woken(c, clock, world)
    clock.advance(19 * 60)
    early = run(c.request_sleep(SleepReason.IDLE))
    assert isinstance(early, SleepRefused) and early.reasons == ("atlas_active",) and "sleep" not in hostd.calls
    c.note_llm_request()
    clock.advance(21 * 60)
    ok = run(c.request_sleep(SleepReason.IDLE))
    assert ok == SleepAccepted("s3")
    sent = hostd.sleep_requests[0]
    assert sent["reason"] == "idle" and sent["idle"] >= 20 * 60 and sent["wake"]["wake_id"] == attempt.wake_id
    assert sent["wake"]["first_packet_at"].endswith("Z")
    assert [e["event"] for e in power(audit)][-2:] == ["sleep_requested", "sleep_accepted"]


def test_sleep_is_refused_while_the_task_queue_is_busy() -> None:
    c, clock, world, hostd, *_ = make(busy=True)
    woken(c, clock, world)
    clock.advance(60 * 60)
    result = run(c.request_sleep(SleepReason.QUEUE_DRAINED))
    assert isinstance(result, SleepRefused) and "queue_busy" in result.reasons and "sleep" not in hostd.calls


def test_queue_drained_sleep_needs_no_idle_wait_but_hostd_can_still_refuse() -> None:
    c, clock, world, hostd, _, audit = make()
    woken(c, clock, world)
    hostd.sleep_answer = SleepNo(("user_input_recent", "fullscreen_app"))
    result = run(c.request_sleep(SleepReason.QUEUE_DRAINED))  # OW-3: refused if user activity is present
    assert result == SleepRefused(("user_input_recent", "fullscreen_app"))
    hostd.sleep_answer = SleepOk("modern_standby")
    assert run(c.request_sleep(SleepReason.QUEUE_DRAINED)) == SleepAccepted("modern_standby")
    assert power(audit)[-1]["event"] == "sleep_accepted"


def test_unused_speculative_wake_sleeps_after_a_5_minute_grace_but_not_if_used() -> None:
    c, clock, world, _hostd, *_ = make()
    woken(c, clock, world, WakeReason.SPECULATIVE)
    clock.advance(4 * 60)
    assert isinstance(run(c.request_sleep(SleepReason.SPECULATIVE_UNUSED)), SleepRefused)
    clock.advance(2 * 60)
    assert isinstance(run(c.request_sleep(SleepReason.SPECULATIVE_UNUSED)), SleepAccepted)

    c2, clock2, world2, _hostd2, *_ = make()
    woken(c2, clock2, world2, WakeReason.SPECULATIVE)
    c2.note_llm_request()  # the follow-up request arrived
    clock2.advance(10 * 60)
    assert isinstance(run(c2.request_sleep(SleepReason.SPECULATIVE_UNUSED)), SleepRefused)


def test_user_requested_sleep_skips_the_idle_timer_only() -> None:
    c, clock, world, _hostd, *_ = make()
    woken(c, clock, world)
    assert isinstance(run(c.request_sleep(SleepReason.IDLE)), SleepRefused)
    assert isinstance(run(c.request_sleep(SleepReason.IDLE, user_requested=True)), SleepAccepted)
    c2, clock2, world2, *_ = make(busy=True)
    woken(c2, clock2, world2)
    assert isinstance(run(c2.request_sleep(SleepReason.IDLE, user_requested=True)), SleepRefused)


def test_hostd_down_during_sleep_request_is_a_refusal_not_an_exception() -> None:
    c, clock, world, _hostd, *_ = make()
    woken(c, clock, world)
    clock.advance(30 * 60)
    world.hostd_up_at = None
    result = run(c.request_sleep(SleepReason.IDLE))
    assert result == SleepRefused(("hostd_unreachable",))


def test_maybe_sleep_only_for_a_host_atlas_woke_and_retries_politely() -> None:
    c, clock, world, hostd, *_ = make()
    world.hostd_up_at = clock.t  # the user woke it, ATLAS has no claim
    clock.advance(3600)
    assert run(c.maybe_sleep()) is None and "sleep" not in hostd.calls

    c, clock, world, hostd, *_ = make()
    woken(c, clock, world)
    clock.advance(10 * 60)
    assert run(c.maybe_sleep()) is None  # not idle long enough
    clock.advance(11 * 60)
    hostd.sleep_answer = SleepNo(("user_input_recent",))
    assert run(c.maybe_sleep()) == SleepRefused(("user_input_recent",))
    asked = len(hostd.sleep_requests)
    clock.advance(30)
    assert run(c.maybe_sleep()) is None and len(hostd.sleep_requests) == asked  # waits 60 s before asking again
    clock.advance(40)
    hostd.sleep_answer = SleepOk("s3")
    assert run(c.maybe_sleep()) == SleepAccepted("s3")
    assert run(c.maybe_sleep()) is None  # claim consumed: nothing more to do


def test_maybe_sleep_uses_the_speculative_reason_for_an_unused_speculative_wake() -> None:
    c, clock, world, hostd, *_ = make()
    woken(c, clock, world, WakeReason.SPECULATIVE)
    clock.advance(6 * 60)
    assert run(c.maybe_sleep()) == SleepAccepted("s3")
    assert hostd.sleep_requests[0]["reason"] == "speculative_unused"
