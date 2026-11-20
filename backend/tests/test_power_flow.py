"""Wake flow (PLAN.md 5.4) on a virtual clock: CT-1..4, FI-1, FI-2, FI-7, FI-9, FI-10 (Mac-testable logic).

The clock is fake, so "exactly 60 s" is exact. Every event the flow emits is validated against events.v1
and the orb state machine by ``ContractEvents``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any

from atlas.core.auditing import RecordingAuditSink
from atlas.core.fallback import Intent, LocalResponder, parse_intent
from atlas.llm.interface import ReadinessStage
from atlas.power.controller import HostPowerController
from atlas.power.flow import (
    LINE_BUSY,
    LINE_SIGNED_OUT,
    LINE_WAKE_FAILED,
    GateCode,
    HostGate,
    HostReady,
    HostUnavailable,
)
from atlas.power.interface import PowerState, WakeReason
from tests.power_support import MODEL, ContractEvents, FakeBackend, FakeClock, FakeHostd, FakeSender, World


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@dataclass
class Rig:
    clock: FakeClock
    world: World
    hostd: FakeHostd
    backend: FakeBackend
    sender: FakeSender
    audit: RecordingAuditSink
    controller: HostPowerController
    events: ContractEvents
    gate: HostGate


def rig(*, vram_need: int | None = None, **world: Any) -> Rig:
    clock = FakeClock()
    w = World(clock, **world)
    hostd, backend, sender = FakeHostd(w), FakeBackend(w), FakeSender(clock)
    audit = RecordingAuditSink("atlas-core")
    controller = HostPowerController(hostd, sender, audit, clock=clock)
    events = ContractEvents("thinking")  # the CLI announces "thinking" before it asks the gate
    gate = HostGate(controller, backend, hostd, model=MODEL, events=events, clock=clock, model_vram_mb=vram_need)
    return Rig(clock, w, hostd, backend, sender, audit, controller, events, gate)


def power_events(r: Rig) -> list[str]:
    return [rec["power"]["event"] for rec in r.audit.of_kind("power")]


def all_up(r: Rig, at: float = 0.0) -> None:
    """Everything up ``at`` seconds from now (0 = already up)."""
    t = r.clock.t + at
    r.world.hostd_up_at = r.world.api_up_at = r.world.model_up_at = t


# --- CT-1: normal wake ----------------------------------------------------------------------------------


def test_ct1_normal_wake_reaches_ready_in_stage_order_within_30s() -> None:
    r = rig()
    t0 = r.clock.t
    r.world.hostd_up_at, r.world.api_up_at, r.world.model_up_at = t0 + 8, t0 + 15, t0 + 22
    result = run(r.gate.ensure_ready(request_text="hello"))
    assert isinstance(result, HostReady) and result.woke
    assert 22_000 <= result.elapsed_ms <= 30_000
    stages = [p["wake"]["stage"] for t, p in r.events.events if t == "state.changed" and "wake" in p]
    assert stages[0] == "packet" and stages[-1] in ("api", "model")
    assert stages == sorted(stages, key=["packet", "link", "api", "model"].index)  # never backwards
    assert r.events.states()[-1] == "thinking"
    assert len(r.gate.queue) == 0
    assert power_events(r) == ["wake_requested", "wake_ready"]
    assert r.controller.current_attempt is None


def test_wake_packets_go_out_at_0_2_5_10_20_seconds() -> None:
    r = rig()
    all_up(r, 40)
    run(r.gate.ensure_ready())
    assert [round(t - 1000.0) for t in r.sender.sent_at] == [0, 2, 5, 10, 20]


# --- CT-2: ack within 2 s ---------------------------------------------------------------------------------


def test_ct2_first_event_is_waking_host_at_time_zero() -> None:
    r = rig()
    all_up(r, 25)
    run(r.gate.ensure_ready())
    kind, payload = r.events.events[0]
    assert kind == "state.changed" and payload["state"] == "waking_host"
    assert payload["wake"] == {"stage": "packet", "elapsed_ms": 0, "deadline_ms": 60_000}


# --- CT-3 / FI-1: the host never wakes ------------------------------------------------------------------


def test_ct3_host_that_never_wakes_fails_at_exactly_60s_and_queues_the_request() -> None:
    r = rig()
    result = run(r.gate.ensure_ready(request_text="what's on my calendar"))
    assert isinstance(result, HostUnavailable)
    assert result.code is GateCode.WAKE_FAILED and result.stage is ReadinessStage.PACKET
    assert abs(result.elapsed_ms - 60_000) <= 1_000
    assert result.queued and len(r.gate.queue) == 1
    assert result.line == LINE_WAKE_FAILED
    assert "WAKE_FAILED" in r.events.alerts()
    assert r.events.states()[-2:] == ["error", "llm_unreachable"]
    assert r.events.last_state()["code"] == "WAKE_FAILED"
    assert power_events(r) == ["wake_requested", "wake_failed"]
    failed = r.audit.of_kind("power")[-1]["power"]
    assert failed["stage"] == "packet" and failed["code"] == "WAKE_FAILED"


def test_fi1_local_commands_still_work_with_the_host_off() -> None:
    r = rig()
    run(r.gate.ensure_ready(request_text="research tulips"))
    responder = LocalResponder(r.clock.now, r.controller)
    intent = parse_intent("what time is it")
    assert intent is Intent.TIME
    assert run(responder.respond(intent)).startswith("It's 10:01 AM")  # 60 s of virtual time passed
    status = run(responder.respond(Intent.HOST_STATUS))
    assert "can't reach" in status  # unknown, not claimed asleep: ATLAS never put it to sleep


# --- CT-4: 59 s versus 61 s, and a late arrival -------------------------------------------------------------


def test_ct4_host_ready_at_59s_answers_once() -> None:
    r = rig()
    t0 = r.clock.t
    r.world.hostd_up_at, r.world.api_up_at, r.world.model_up_at = t0 + 20, t0 + 40, t0 + 59
    result = run(r.gate.ensure_ready(request_text="hi"))
    assert isinstance(result, HostReady)
    assert len(r.gate.queue) == 0


def test_ct4_host_ready_at_61s_times_out_then_delivers_the_queued_request_exactly_once() -> None:
    r = rig()
    t0 = r.clock.t
    r.world.hostd_up_at, r.world.api_up_at, r.world.model_up_at = t0 + 30, t0 + 50, t0 + 61
    first = run(r.gate.ensure_ready(request_text="remind me to call Dana"))
    assert isinstance(first, HostUnavailable) and first.queued
    assert r.events.states()[-1] == "llm_unreachable"
    r.clock.advance(5)  # the late readiness arrives after the timeout
    second = run(r.gate.ensure_ready())  # the next interaction finds the core ready
    assert isinstance(second, HostReady) and not second.woke
    delivered = r.gate.queue.drain()
    assert [d.text for d in delivered] == ["remind me to call Dana"]
    assert r.gate.queue.drain() == []  # once
    assert r.events.alerts().count("CORE_RECOVERED") == 1
    run(r.gate.ensure_ready())
    assert r.events.alerts().count("CORE_RECOVERED") == 1


def test_queue_expires_after_24_hours_and_is_fifo() -> None:
    r = rig()
    r.gate.queue.put("one")
    r.gate.queue.put("two")
    r.clock.advance(23 * 3600)
    r.gate.queue.put("three")
    r.clock.advance(2 * 3600)  # "one" and "two" are now 25 h old
    assert [q.text for q in r.gate.queue.drain()] == ["three"]


# --- FI-2: the host dies mid-wake ------------------------------------------------------------------------


def test_fi2_host_dying_at_the_api_stage_times_out_gracefully_without_duplicates() -> None:
    r = rig()
    t0 = r.clock.t
    r.world.hostd_up_at, r.world.api_up_at, r.world.model_up_at = t0 + 4, t0 + 8, t0 + 30
    r.world.dies_at = t0 + 12
    result = run(r.gate.ensure_ready(request_text="summarize my inbox"))
    assert isinstance(result, HostUnavailable) and result.code is GateCode.WAKE_FAILED
    assert result.stage is ReadinessStage.API  # the last stage reached before the power cut
    assert abs(result.elapsed_ms - 60_000) <= 1_000
    assert len(r.gate.queue) == 1
    # Power returns later: the queued request is delivered once, never re-queued.
    r.clock.advance(100)
    r.world.dies_at = None
    all_up(r)
    again = run(r.gate.ensure_ready())
    assert isinstance(again, HostReady)
    assert len(r.gate.queue.drain()) == 1 and len(r.gate.queue) == 0


# --- FI-7: token rotated ------------------------------------------------------------------------------------


def test_fi7_auth_failure_stops_at_once_without_a_retry_storm() -> None:
    r = rig(auth_ok=False, hostd_up_at=0.0, api_up_at=0.0)
    result = run(r.gate.ensure_ready(request_text="hi"))
    assert isinstance(result, HostUnavailable) and result.code is GateCode.LLM_AUTH_FAILED
    assert result.elapsed_ms == 0
    assert r.backend.health_calls == 1 and r.backend.ready_calls == 0
    assert r.sender.sent_at == []  # no wake attempted
    assert "LLM_AUTH_FAILED" in r.events.alerts()
    assert r.events.states()[-2:] == ["error", "llm_unreachable"]


def test_auth_failure_discovered_mid_wake_also_stops_immediately() -> None:
    r = rig()
    t0 = r.clock.t
    r.world.hostd_up_at, r.world.api_up_at, r.world.model_up_at = t0 + 3, t0 + 6, t0 + 20
    r.world.auth_ok = False
    result = run(r.gate.ensure_ready(request_text="hi"))
    assert isinstance(result, HostUnavailable) and result.code is GateCode.LLM_AUTH_FAILED
    assert result.elapsed_ms < 10_000


# --- FI-9: signed out after a reboot --------------------------------------------------------------------------


def test_fi9_hostd_up_but_nobody_signed_in_reports_signed_out() -> None:
    r = rig(signed_in=False)
    r.world.hostd_up_at = r.clock.t + 5  # api never comes up
    result = run(r.gate.ensure_ready(request_text="what's the weather"))
    assert isinstance(result, HostUnavailable) and result.code is GateCode.SIGNED_OUT
    assert result.line == LINE_SIGNED_OUT and result.queued
    assert r.events.last_state() == {"state": "llm_unreachable", "code": "SIGNED_OUT"}
    assert power_events(r)[-1] == "wake_failed"


def test_signed_in_but_api_down_is_a_plain_wake_failure() -> None:
    r = rig(signed_in=True)
    r.world.hostd_up_at = r.clock.t + 5
    result = run(r.gate.ensure_ready(request_text="x"))
    assert isinstance(result, HostUnavailable) and result.code is GateCode.WAKE_FAILED
    assert result.stage is ReadinessStage.LINK


# --- FI-10: GPU busy ---------------------------------------------------------------------------------------------


def test_fi10_not_enough_free_vram_reports_busy_and_never_loads_the_model() -> None:
    r = rig(vram_need=7_000, vram_free_mb=3_000, hostd_up_at=0.0, api_up_at=0.0)
    result = run(r.gate.ensure_ready(request_text="draft a reply"))
    assert isinstance(result, HostUnavailable) and result.code is GateCode.HOST_BUSY
    assert result.line == LINE_BUSY and result.queued
    assert r.backend.ready_calls == 0  # D16: no model load when the VRAM check failed
    assert "HOST_BUSY" in r.events.alerts()
    assert r.events.states()[-1] == "idle"  # busy is not "unreachable"
    assert result.elapsed_ms < 5_000


def test_enough_vram_proceeds_to_load() -> None:
    r = rig(vram_need=7_000, vram_free_mb=7_500, hostd_up_at=0.0, api_up_at=0.0)
    r.world.model_up_at = r.clock.t + 3
    assert isinstance(run(r.gate.ensure_ready()), HostReady)


def test_vram_check_is_skipped_when_the_model_is_already_loaded() -> None:
    r = rig(vram_need=7_000, vram_free_mb=100, hostd_up_at=0.0, api_up_at=0.0, model_up_at=0.0)
    result = run(r.gate.ensure_ready())
    assert isinstance(result, HostReady) and not result.woke
    assert "gpu" not in r.hostd.calls


def test_missing_nvml_does_not_block_a_load() -> None:
    r = rig(vram_need=7_000, hostd_up_at=0.0, api_up_at=0.0)
    r.hostd.gpu_missing = True
    r.world.model_up_at = r.clock.t + 3
    assert isinstance(run(r.gate.ensure_ready()), HostReady)


# --- policy refusals reach the user without a wait ---------------------------------------------------------------------


def test_a_refused_retry_wake_fails_fast_and_names_the_policy() -> None:
    r = rig()
    run(r.gate.ensure_ready(request_text="a"))  # one failure arms the backoff
    r.events = ContractEvents("thinking")
    r.gate._events = r.events
    t = r.clock.t
    result = run(r.gate.ensure_ready(WakeReason.RETRY, request_text="a"))
    assert isinstance(result, HostUnavailable) and result.code is GateCode.BACKOFF
    assert r.clock.t == t  # nothing was waited
    assert r.events.states()[-1] == "idle"


def test_host_already_up_but_model_loading_polls_without_sending_packets() -> None:
    r = rig(hostd_up_at=0.0, api_up_at=0.0)
    r.world.model_up_at = r.clock.t + 12
    result = run(r.gate.ensure_ready())
    assert isinstance(result, HostReady)
    assert r.sender.sent_at == []
    assert power_events(r) == ["wake_refused"]
    assert run(r.controller.state()) is PowerState.UP
