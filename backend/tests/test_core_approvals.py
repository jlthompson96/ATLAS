"""The approval flow on atlas-core's side (PLAN.md §4.5; AP-1..AP-4, RT-13).

Proposals are created through the real tool path (propose_action -> policy -> proposer), then
decided through ``ApprovalService`` the way atlas-ui would forward an ``action.decide``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Coroutine
from typing import Any

import pytest

from atlas.audit.record import validate_record
from atlas.core.approvals import StepUpRegistry
from atlas.core.auditing import RecordingAuditSink
from atlas.core.fake_llm import ScriptedBackend, call_turn, text_turn
from atlas.core.pending import clean_summary
from atlas.executor.approval_record import signing_message, verify_signature
from atlas.policy.types import ActionState, DecisionChannel
from tests.core_support import MOCK_PIN_TOKEN, Harness, validate_event

PROPOSE = (
    "propose_action",
    {
        "kind": "email.send",
        "summary": "Reply to Dana",
        "to": ["dana@example.org"],
        "subject": "Re: Saturday",
        "body": ["2pm works."],
    },
)


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def harness_with_proposal() -> tuple[Harness, dict[str, Any]]:
    h = Harness(backend=ScriptedBackend([call_turn(PROPOSE), text_turn("Proposed.")]))
    out = run(h.run("reply to Dana that 2pm works"))
    assert out.pending_actions
    (event,) = h.pending_events()
    return h, event


def decide(h: Harness, event: dict[str, Any], **kw: Any) -> Any:
    args = {
        "channel": DecisionChannel.HUD,
        "device_id": "hud-remote",
        "action_id": event["action_id"],
        "decision": "approve",
        "payload_sha256": event["payload_sha256"],
        "step_up_token": MOCK_PIN_TOKEN,
    }
    args.update(kw)
    return run(h.approvals.decide(**args))


def test_pending_action_matches_events_v1_exactly() -> None:
    h, event = harness_with_proposal()
    validate_event("action.pending", event)  # also done by the sink on every emit
    assert hashlib.sha256(event["payload_canonical"].encode()).hexdigest() == event["payload_sha256"]
    assert json.loads(event["payload_canonical"]) == {
        "kind": "email.send",
        "to": ["dana@example.org"],
        "cc": [],
        "subject": "Re: Saturday",
        "body": "2pm works.",
    }
    assert event["provenance"] == {"tainted": False, "sources": []}
    assert "warnings" not in event  # on the allowlist, no taint, interactive
    assert event["expires_at"] - event["created_at"] == 3_600_000
    (proposed,) = h.core_audit.of_kind("action_proposed")
    assert proposed["action"]["payload_sha256"] == event["payload_sha256"] and proposed["action"]["tainted"] is False


def test_approval_signs_a_record_that_binds_the_decision() -> None:
    h, event = harness_with_proposal()
    h.stepups.register("hud-remote", MOCK_PIN_TOKEN, h.clock.wall() + 60_000)
    result = decide(h, event)
    assert result.ok and result.signed is not None
    record = result.signed.record
    assert (record.action_id, record.payload_sha256, record.action_kind) == (
        event["action_id"],
        event["payload_sha256"],
        "email.send",
    )
    assert (record.origin_context, record.decision_context, record.approved_via, record.device_id) == (
        "interactive",
        "interactive",
        "hud",
        "hud-remote",
    )
    assert record.step_up is True and record.decision == "approve"
    assert record.action_created_at < record.decided_at < record.expires_at <= record.decided_at + 10 * 60_000
    assert verify_signature(h.key.public_key(), record.to_json(), result.signed.signature)
    assert signing_message(record.to_json()).startswith(b"atlas-approval-v1\n")
    (approval,) = h.core_audit.of_kind("approval")
    assert approval["device_id"] == "hud-remote" and approval["action"]["approval_id"] == record.approval_id
    assert approval["action"]["decision"] == "approve" and approval["action"]["step_up"] is True
    assert validate_record(approval) == []
    assert h.runtime.pending.get(event["action_id"]).state is ActionState.APPROVED  # type: ignore[union-attr]
    assert h.events.of_type("action.resolved")[-1]["outcome"] == "approved"


def test_ap1_hash_mismatch_is_refused() -> None:
    h, event = harness_with_proposal()
    h.stepups.register("hud-remote", MOCK_PIN_TOKEN, h.clock.wall() + 60_000)
    assert decide(h, event, payload_sha256="0" * 64).code == "HASH_MISMATCH"
    h.assert_no_unapproved_action()


def test_ap2_rt13_voice_can_never_approve() -> None:
    h, event = harness_with_proposal()
    h.stepups.register("hud-remote", MOCK_PIN_TOKEN, h.clock.wall() + 60_000)
    for channel in (DecisionChannel.VOICE, DecisionChannel.TEXT, DecisionChannel.PLANNER):
        assert decide(h, event, channel=channel).code == "FORBIDDEN_DEVICE"
    h.assert_no_unapproved_action()


def test_ap3_expired_action_is_refused_and_resolved_expired() -> None:
    h, event = harness_with_proposal()
    h.clock.advance(3601)
    h.stepups.register("hud-remote", MOCK_PIN_TOKEN, h.clock.wall() + 60_000)
    assert decide(h, event).code == "EXPIRED"
    assert h.events.of_type("action.resolved")[-1] == {
        "action_id": event["action_id"],
        "outcome": "expired",
        "at": h.clock.wall(),
    }
    assert decide(h, event).code == "UNKNOWN_ACTION"
    assert h.core_audit.of_kind("approval") == []


def test_expire_due_resolves_old_actions() -> None:
    h, event = harness_with_proposal()
    h.clock.advance(3600)
    assert run(h.approvals.expire_due()) == [event["action_id"]]


def test_ap4_kiosk_cannot_approve_by_default_but_settings_can_allow_it() -> None:
    h, event = harness_with_proposal()
    h.stepups.register("kiosk-1", MOCK_PIN_TOKEN, h.clock.wall() + 60_000)
    assert decide(h, event, device_id="kiosk-1").code == "FORBIDDEN_DEVICE"
    assert decide(h, event, device_id="unknown-device").code == "FORBIDDEN_DEVICE"
    h.devices.add("kiosk-1", "kiosk", can_approve=True)
    assert decide(h, event, device_id="kiosk-1").ok


def test_step_up_is_required_bound_to_the_device_and_expires() -> None:
    h, event = harness_with_proposal()
    assert decide(h, event).code == "STEP_UP_REQUIRED"  # nothing registered
    h.stepups.register("kiosk-1", MOCK_PIN_TOKEN, h.clock.wall() + 60_000)
    assert decide(h, event).code == "STEP_UP_REQUIRED"  # granted to another device
    h.stepups.register("hud-remote", "another-token-abcdefgh", h.clock.wall() + 60_000)
    h.clock.advance(61)
    assert decide(h, event, step_up_token="another-token-abcdefgh").code == "STEP_UP_REQUIRED"  # expired
    h.assert_no_unapproved_action()


def test_step_up_tokens_live_at_most_60_seconds_and_are_bounded() -> None:
    reg = StepUpRegistry(lambda: 1000)
    with pytest.raises(ValueError):
        reg.register("d", "t" * 20, 1000 + 61_000)
    for i in range(64):
        reg.register("d", f"token-{i:04d}-abcdefgh", 2000)
    with pytest.raises(OverflowError):
        reg.register("d", "one-too-many-abcdefgh", 2000)


def test_a_second_decision_is_a_replay_and_is_refused() -> None:
    h, event = harness_with_proposal()
    h.stepups.register("hud-remote", MOCK_PIN_TOKEN, h.clock.wall() + 60_000)
    assert decide(h, event).ok
    assert decide(h, event).code == "UNKNOWN_ACTION"
    assert decide(h, event, decision="deny").code == "UNKNOWN_ACTION"
    assert len(h.core_audit.of_kind("approval")) == 1


def test_deny_is_audited_and_resolves_the_action() -> None:
    h, event = harness_with_proposal()
    h.stepups.register("hud-remote", MOCK_PIN_TOKEN, h.clock.wall() + 60_000)
    result = decide(h, event, decision="deny")
    assert result.ok and result.signed is None
    (approval,) = h.core_audit.of_kind("approval")
    assert approval["action"]["decision"] == "deny"
    assert h.events.of_type("action.resolved")[-1]["outcome"] == "denied"
    assert decide(h, event).code == "UNKNOWN_ACTION"


@pytest.mark.parametrize(
    "sink",
    [RecordingAuditSink("atlas-core", fail_with="AUDIT_UNAVAILABLE"), RecordingAuditSink("atlas-core", durable=False)],
)
def test_no_durable_approval_record_no_approval(sink: RecordingAuditSink) -> None:
    h, event = harness_with_proposal()
    h.approvals._audit = sink
    h.stepups.register("hud-remote", MOCK_PIN_TOKEN, h.clock.wall() + 60_000)
    assert decide(h, event).code == "BUSY"
    assert h.runtime.pending.get(event["action_id"]).state is ActionState.PENDING  # type: ignore[union-attr]


def test_execute_reports_executed_and_failed() -> None:
    h, event = harness_with_proposal()
    h.stepups.register("hud-remote", MOCK_PIN_TOKEN, h.clock.wall() + 60_000)
    signed = decide(h, event).signed
    assert run(h.approvals.execute(signed, h.link)) == {"ok": True, "status": "executed"}
    assert h.events.of_type("action.resolved")[-1]["outcome"] == "executed"
    # Not approved (any more): core refuses before reaching the executor.
    assert run(h.approvals.execute(signed, h.link)) == {"ok": False, "code": "NOT_APPROVED"}


def test_executor_rejection_is_surfaced_as_failed() -> None:
    h, event = harness_with_proposal()
    h.stepups.register("hud-remote", MOCK_PIN_TOKEN, h.clock.wall() + 60_000)
    signed = decide(h, event).signed
    h.clock.advance(6 * 60)  # past the 5-minute approval window
    assert run(h.approvals.execute(signed, h.link)) == {"ok": False, "code": "EXPIRED"}
    resolved = h.events.of_type("action.resolved")[-1]
    assert (resolved["outcome"], resolved["code"]) == ("failed", "EXECUTOR_REJECTED")
    assert h.email.sent == []


def test_summary_is_one_clean_line() -> None:
    assert clean_summary("Reply\nto\x1b[31m Dana‮") == "Reply to[31m Dana"
    assert clean_summary("") == "Proposed action"
    assert len(clean_summary("x" * 500)) == 200
