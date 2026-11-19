"""The policy engine is a pure function: tables of inputs and the decisions they must give."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from atlas.core.taint import EMPTY_PROVENANCE, Provenance, Source, SourceKind
from atlas.policy import payloads
from atlas.policy.engine import (
    Allowlists,
    ApprovalInput,
    ArgTaint,
    PolicyInput,
    ProposedAction,
    Recipient,
    Verdict,
    decide,
    decide_approval,
)
from atlas.policy.types import ActionKind, ActionState, Context, DecisionChannel, ProposalWarning, ToolTag

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
EVENTS = json.loads((CONTRACTS / "events.v1.schema.json").read_text(encoding="utf-8"))
AUDIT = json.loads((CONTRACTS / "audit-record.v1.schema.json").read_text(encoding="utf-8"))
EMAIL = Provenance.of(Source(SourceKind.EMAIL, "gmail:msg/1"))
TAINTED = ArgTaint(has_refs=True, provenance=EMAIL)
ALLOW = Allowlists(recipients=frozenset({"dana@example.org"}), discord_channels=frozenset({"110000000000000055"}))


def inp(tool: str = "read_index", tag: ToolTag | None = ToolTag.READ, **kw: Any) -> PolicyInput:
    return PolicyInput(
        context=kw.pop("context", Context.INTERACTIVE),
        tool_name=tool,
        tool_tag=tag,
        registered=kw.pop("registered", tag is not None),
        allowlists=kw.pop("allowlists", ALLOW),
        **kw,
    )


def email(*addrs: str, tainted: bool = False) -> ProposedAction:
    return ProposedAction("email.send", tuple(Recipient(a, tainted) for a in addrs))


# --- mirrors of the contracts -------------------------------------------------------------------------


def test_enums_mirror_the_contracts() -> None:
    d = EVENTS["$defs"]
    assert {k.value for k in ActionKind} == set(d["ActionKind"]["enum"])
    warnings = d["ActionPending"]["oneOf"][0]["properties"]["warnings"]["items"]["enum"]
    assert {w.value for w in ProposalWarning} == set(warnings)
    assert {c.value for c in Context} == set(AUDIT["properties"]["context"]["enum"])
    assert {s.value for s in SourceKind} == set(d["SourceRef"]["properties"]["kind"]["enum"])


# --- tools ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("policy_input", "verdict", "code"),
    [
        (inp("frobnicate", None), Verdict.DENY, "UNKNOWN_TOOL"),
        (inp("read_index", ToolTag.READ, registered=False), Verdict.DENY, "UNKNOWN_TOOL"),
        (inp("sell_all_holdings", None), Verdict.DENY, "NO_TRADE_TOOLS"),
        (inp("execute_trade", ToolTag.READ), Verdict.DENY, "NO_TRADE_TOOLS"),
        (inp("send_email", ToolTag.WRITE), Verdict.DENY, "WRITE_REQUIRES_APPROVAL"),
        (inp("send_email", ToolTag.WRITE, context=Context.UNATTENDED), Verdict.DENY, "WRITE_REQUIRES_APPROVAL"),
        (inp(context=Context.QUARANTINED), Verdict.DENY, "CONTEXT_HAS_NO_TOOLS"),
        (inp(context=Context.SYSTEM), Verdict.DENY, "CONTEXT_HAS_NO_TOOLS"),
        (inp("queue_research", ToolTag.QUEUE, context=Context.UNATTENDED), Verdict.DENY, "TOOL_NOT_IN_CONTEXT"),
        (inp(), Verdict.ALLOW_READ, "READ"),
        (inp(args=TAINTED), Verdict.DENY, "TAINTED_ARG_REFUSED"),
        (inp("queue_research", ToolTag.QUEUE), Verdict.ALLOW_READ, "READ"),
        (inp("queue_research", ToolTag.QUEUE, args=TAINTED), Verdict.DENY, "TAINTED_ARG_REFUSED"),
        (inp("queue_research", ToolTag.QUEUE, calls_so_far={"queue_research": 2}), Verdict.DENY, "QUEUE_LIMIT"),
        (inp("ask_reader", ToolTag.READER), Verdict.DENY, "READER_NEEDS_ITEM"),
        (inp("ask_reader", ToolTag.READER, args=replace(TAINTED, refs_to_items=True)), Verdict.ALLOW_READ, "READER"),
        (inp("propose_action", ToolTag.PROPOSE), Verdict.DENY, "INVALID_ACTION"),
        (inp("propose_action", ToolTag.PROPOSE, action=ProposedAction("portfolio.trade")), Verdict.DENY, "NO_TRADE_TOOLS"),
        (inp("propose_action", ToolTag.PROPOSE, action=ProposedAction("portfolio.import")), Verdict.DENY, "ACTION_KIND_NOT_PROPOSABLE"),
        (inp("propose_action", ToolTag.PROPOSE, action=email("dana@example.org"), proposals_so_far=3), Verdict.DENY, "PROPOSAL_LIMIT"),
        (inp("propose_action", ToolTag.PROPOSE, action=ProposedAction("discord.post", channel_id="1")), Verdict.DENY, "CHANNEL_NOT_ALLOWLISTED"),
        (inp("propose_action", ToolTag.PROPOSE, action=ProposedAction("research.schedule", every_minutes=5)), Verdict.DENY, "SCHEDULE_TOO_FREQUENT"),
        (inp("propose_action", ToolTag.PROPOSE, action=ProposedAction("research.schedule", every_minutes=60), args=TAINTED), Verdict.DENY, "TAINTED_ARG_REFUSED"),
        (inp("propose_action", ToolTag.PROPOSE, action=ProposedAction("research.schedule", every_minutes=60)), Verdict.PROPOSE, "APPROVAL_REQUIRED"),
    ],
)  # fmt: skip
def test_tool_decisions(policy_input: PolicyInput, verdict: Verdict, code: str) -> None:
    decision = decide(policy_input)
    assert (decision.verdict, decision.code) == (verdict, code)


def test_every_valid_proposal_proposes_and_is_flagged_when_tainted() -> None:
    clean = decide(inp("propose_action", ToolTag.PROPOSE, action=email("dana@example.org")))
    assert clean.verdict is Verdict.PROPOSE and not clean.tainted and clean.warnings == ()

    tainted = decide(inp("propose_action", ToolTag.PROPOSE, action=email("dana@example.org"), args=TAINTED))
    assert tainted.verdict is Verdict.PROPOSE and tainted.tainted
    assert tainted.warnings == (ProposalWarning.TAINTED_ARGS,)
    assert "TAINTED_PROVENANCE" in tainted.reasons


def test_influence_alone_sets_the_provenance_flag_without_tainted_args() -> None:
    d = decide(inp("propose_action", ToolTag.PROPOSE, action=email("dana@example.org"), influence=EMAIL))
    assert d.tainted and ProposalWarning.TAINTED_ARGS not in d.warnings
    assert d.provenance == EMAIL


def test_recipient_warnings_rt14() -> None:
    d = decide(inp("propose_action", ToolTag.PROPOSE, action=email("Dana@Example.org", "sam@example.net")))
    assert d.warnings == (ProposalWarning.NEW_RECIPIENT, ProposalWarning.NOT_ON_ALLOWLIST)
    known = replace(ALLOW, known_recipients=frozenset({"sam@example.net"}))
    d = decide(inp("propose_action", ToolTag.PROPOSE, action=email("sam@example.net"), allowlists=known))
    assert d.warnings == (ProposalWarning.NOT_ON_ALLOWLIST,)


def test_unattended_proposals_are_marked() -> None:
    d = decide(inp("propose_action", ToolTag.PROPOSE, action=email("dana@example.org"), context=Context.UNATTENDED))
    assert d.verdict is Verdict.PROPOSE and d.warnings == (ProposalWarning.UNATTENDED_ORIGIN,)


def test_decide_is_deterministic() -> None:
    i = inp(
        "propose_action", ToolTag.PROPOSE, action=email("x@example.com", tainted=True), args=TAINTED, influence=EMAIL
    )
    assert len({decide(i) for _ in range(20)}) == 1


def test_no_allow_without_registration_for_any_tag() -> None:
    for tag in ToolTag:
        for context in Context:
            assert decide(inp("tool", tag, registered=False, context=context)).verdict is Verdict.DENY


# --- approvals -------------------------------------------------------------------------------------------

OK = ApprovalInput(
    channel=DecisionChannel.HUD,
    decision="approve",
    device_known=True,
    device_can_approve=True,
    step_up_valid=True,
    action_state=ActionState.PENDING,
    hash_matches=True,
    now_ms=1000,
    expires_at_ms=2000,
)


@pytest.mark.parametrize(
    ("change", "code", "reason"),
    [
        ({"channel": DecisionChannel.VOICE}, "FORBIDDEN_DEVICE", "VOICE_CANNOT_APPROVE"),
        ({"channel": DecisionChannel.TEXT}, "FORBIDDEN_DEVICE", "NOT_HUD"),
        ({"channel": DecisionChannel.PLANNER}, "FORBIDDEN_DEVICE", "NOT_HUD"),
        ({"decision": "maybe"}, "INVALID_MESSAGE", "BAD_DECISION"),
        ({"device_known": False}, "FORBIDDEN_DEVICE", "UNKNOWN_DEVICE"),
        ({"device_can_approve": False}, "FORBIDDEN_DEVICE", "DEVICE_CANNOT_APPROVE"),
        ({"action_state": None}, "UNKNOWN_ACTION", "UNKNOWN_ACTION"),
        ({"action_state": ActionState.APPROVED}, "UNKNOWN_ACTION", "NOT_PENDING"),
        ({"action_state": ActionState.DENIED}, "UNKNOWN_ACTION", "NOT_PENDING"),
        ({"now_ms": 2000}, "EXPIRED", "EXPIRED"),
        ({"step_up_valid": False}, "STEP_UP_REQUIRED", "STEP_UP_REQUIRED"),
        ({"hash_matches": False}, "HASH_MISMATCH", "HASH_MISMATCH"),
    ],
)
def test_approval_refusals(change: dict[str, Any], code: str, reason: str) -> None:
    verdict = decide_approval(replace(OK, **change))
    assert (verdict.ok, verdict.code, verdict.reason) == (False, code, reason)


def test_approval_and_denial_accepted() -> None:
    assert decide_approval(OK).ok
    assert decide_approval(replace(OK, decision="deny")).reason == "DENIED"


def test_voice_never_approves_whatever_else_is_true() -> None:
    for state in [*ActionState, None]:
        for flag in (True, False):
            v = decide_approval(replace(OK, channel=DecisionChannel.VOICE, action_state=state, step_up_valid=flag))
            assert not v.ok


def test_refusal_codes_are_in_the_request_result_catalogue() -> None:
    catalogue = (CONTRACTS / "README.md").read_text(encoding="utf-8")
    for code in (
        "FORBIDDEN_DEVICE",
        "UNKNOWN_ACTION",
        "EXPIRED",
        "STEP_UP_REQUIRED",
        "HASH_MISMATCH",
        "INVALID_MESSAGE",
    ):
        assert f"`{code}`" in catalogue


# --- payloads ----------------------------------------------------------------------------------------------

GOOD_EMAIL = {"kind": "email.send", "to": ["dana@example.org"], "cc": [], "subject": "Hi", "body": "Hello"}


@pytest.mark.parametrize(
    "payload",
    [
        GOOD_EMAIL,
        {**GOOD_EMAIL, "in_reply_to": "gmail:msg/18c2f01"},
        {"kind": "discord.post", "channel_id": "110000000000000055", "channel_label": "#builds", "content": "hi"},
        {"kind": "research.schedule", "question": "latest news", "every_minutes": 60},
    ],
)
def test_valid_payloads(payload: dict[str, Any]) -> None:
    assert payloads.validate_payload(payload) == []


@pytest.mark.parametrize(
    "payload",
    [
        {**GOOD_EMAIL, "to": []},
        {**GOOD_EMAIL, "to": ["not an address"]},
        {**GOOD_EMAIL, "to": ["a@example.com", "A@example.com"]},
        {**GOOD_EMAIL, "to": ["x@[127.0.0.1]"]},
        {**GOOD_EMAIL, "to": ["a@example.com, b@example.com"]},
        {**GOOD_EMAIL, "subject": "Hi\r\nBcc: attacker@example.com"},
        {**GOOD_EMAIL, "body": ""},
        {**GOOD_EMAIL, "extra": 1},
        {k: v for k, v in GOOD_EMAIL.items() if k != "cc"},
        {**GOOD_EMAIL, "kind": "portfolio.trade"},
        {"kind": "discord.post", "channel_id": "abc", "channel_label": "#x", "content": "hi"},
        {"kind": "discord.post", "channel_id": "110000000000000055", "channel_label": "#x", "content": "x" * 2001},
        {"kind": "research.schedule", "question": "q", "every_minutes": 5},
        {"kind": "research.schedule", "question": "q", "every_minutes": True},
        {"kind": "research.schedule", "question": "q", "every_minutes": 60.0},
        {**GOOD_EMAIL, "body": "x" * 70_000},
        "not an object",
    ],
)
def test_invalid_payloads(payload: Any) -> None:
    assert payloads.validate_payload(payload) != []


def test_canonical_form_and_hash() -> None:
    text = payloads.canonical_text(GOOD_EMAIL)
    assert text == '{"body":"Hello","cc":[],"kind":"email.send","subject":"Hi","to":["dana@example.org"]}'
    assert payloads.parse_canonical(text) == GOOD_EMAIL
    for variant in (text.replace(",", ", "), '{"to":["dana@example.org"],' + text[1:-1].split(',"to"')[0] + "}"):
        with pytest.raises(ValueError):
            payloads.parse_canonical(variant)
    assert payloads.payload_sha256(text) == payloads.payload_sha256(text) and len(payloads.payload_sha256(text)) == 64
    assert EMPTY_PROVENANCE.tainted is False
