"""The policy engine: pure, deterministic decisions (PLAN.md §2.9 "Rules, enforced in code rather than prompts").

``decide`` maps (context, tool, argument taint, proposed action, allowlists, per-request counters) to
``ALLOW_READ``, ``PROPOSE`` or ``DENY`` with reason codes, warnings and provenance. ``decide_approval``
maps an approval attempt (channel, device, step-up, hash, expiry, action state) to accept or refuse.

Both functions read no clock, no globals and no I/O: every input is a parameter, so the same input
always gives the same decision and the tables in ``tests/test_policy_engine.py`` cover them fully.
The caller (atlas-core) audits every decision.

Rules:

1. Unknown or unregistered tools are denied (never guessed at).
2. No trade tool exists or can be allowed (RT-15).
3. ``write`` tools are never callable from a planner: writes happen only in atlas-executor, after a
   signed human approval. The planner asks with ``propose_action``.
4. The quarantined and system contexts have no tools at all.
5. A tool tag outside the context's registry tags is denied (unattended has no queue or write).
6. Read and queue tools refuse ``$VAR`` arguments. This is stricter than "tainted arguments force a
   proposal": a read has nothing to propose, and a tainted research question would be an
   exfiltration channel (RT-8, T5).
7. ``ask_reader`` must name an untrusted item; that is its whole purpose (the quarantine).
8. Every ``propose_action`` is a proposal. Nothing executes without approval. Tainted arguments, or
   a planner context that has seen untrusted-derived data in this request, set
   ``provenance.tainted`` so the HUD shows the flag; ``$VAR`` arguments add the ``tainted_args``
   warning; recipients outside the allowlist add ``not_on_allowlist`` (RT-14); unknown recipients add
   ``new_recipient``; the unattended context adds ``unattended_origin``.
9. Discord posts must target an allowlisted channel (§2.11). Schedules need approval and can't run
   more often than hourly, and their question can't carry ``$VAR`` content (RT-12).
10. Approval decisions come only from the HUD channel, from a known device allowed to approve, with
    a valid step-up token and the exact payload hash, before expiry, once (RT-13, AP-1..AP-4).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from atlas.core.taint import EMPTY_PROVENANCE, Provenance
from atlas.policy.payloads import MIN_SCHEDULE_MINUTES, PROPOSABLE_KINDS, normalize_address
from atlas.policy.types import CONTEXT_TAGS, ActionState, Context, DecisionChannel, ProposalWarning, ToolTag

MAX_PROPOSALS_PER_REQUEST: Final = 3
MAX_QUEUE_PER_REQUEST: Final = 2
_TRADE_RE: Final = re.compile(r"(?:^|[_.])(?:trade|trading|sell|buy|order|transfer|withdraw|swap)(?:$|[_.])")


class Verdict(StrEnum):
    ALLOW_READ = "allow_read"
    PROPOSE = "propose"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class Allowlists:
    """User configuration (trusted). Addresses are compared after ``normalize_address``."""

    recipients: frozenset[str] = frozenset()  # addresses the user allows sending to
    known_recipients: frozenset[str] = frozenset()  # addresses already written to before
    discord_channels: frozenset[str] = frozenset()  # channel IDs the poster bot may use

    def allows(self, address: str) -> bool:
        return normalize_address(address) in self.recipients

    def knows(self, address: str) -> bool:
        return normalize_address(address) in self.known_recipients or self.allows(address)


@dataclass(frozen=True, slots=True)
class ArgTaint:
    has_refs: bool = False  # any argument is a $VAR
    refs_to_items: bool = False  # a $VAR names an untrusted item (what ask_reader needs)
    provenance: Provenance = EMPTY_PROVENANCE  # union over every referenced value

    @property
    def tainted(self) -> bool:
        return self.provenance.tainted


@dataclass(frozen=True, slots=True)
class Recipient:
    address: str
    tainted: bool  # came from a $VAR


@dataclass(frozen=True, slots=True)
class ProposedAction:
    kind: str
    recipients: tuple[Recipient, ...] = ()
    channel_id: str | None = None
    every_minutes: int | None = None


@dataclass(frozen=True, slots=True)
class PolicyInput:
    context: Context
    tool_name: str
    tool_tag: ToolTag | None  # None: the model named a tool that does not exist
    registered: bool  # the tool is in this context's registry
    args: ArgTaint = ArgTaint()
    action: ProposedAction | None = None
    influence: Provenance = EMPTY_PROVENANCE  # untrusted sources the planner has seen this request
    allowlists: Allowlists = Allowlists()
    calls_so_far: Mapping[str, int] = field(default_factory=dict)  # per tool name, this request
    proposals_so_far: int = 0


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    reasons: tuple[str, ...]
    warnings: tuple[ProposalWarning, ...] = ()
    provenance: Provenance = EMPTY_PROVENANCE

    @property
    def tainted(self) -> bool:
        return self.provenance.tainted

    @property
    def code(self) -> str:
        return self.reasons[0] if self.reasons else "OK"


def _deny(code: str) -> Decision:
    return Decision(Verdict.DENY, (code,))


def decide(inp: PolicyInput) -> Decision:
    """The decision for one tool call. Pure and deterministic."""
    if _TRADE_RE.search(inp.tool_name.lower()):
        return _deny("NO_TRADE_TOOLS")
    if inp.tool_tag is None or not inp.registered:
        return _deny("UNKNOWN_TOOL")
    if inp.tool_tag is ToolTag.WRITE:
        return _deny("WRITE_REQUIRES_APPROVAL")
    allowed_tags = CONTEXT_TAGS.get(inp.context, frozenset())
    if not allowed_tags:
        return _deny("CONTEXT_HAS_NO_TOOLS")
    if inp.tool_tag not in allowed_tags:
        return _deny("TOOL_NOT_IN_CONTEXT")

    if inp.tool_tag is ToolTag.READER:
        if not inp.args.refs_to_items:
            return _deny("READER_NEEDS_ITEM")
        return Decision(Verdict.ALLOW_READ, ("READER",), provenance=inp.args.provenance)

    if inp.tool_tag in (ToolTag.READ, ToolTag.QUEUE):
        if inp.args.has_refs:
            return _deny("TAINTED_ARG_REFUSED")
        if inp.tool_tag is ToolTag.QUEUE and inp.calls_so_far.get(inp.tool_name, 0) >= MAX_QUEUE_PER_REQUEST:
            return _deny("QUEUE_LIMIT")
        return Decision(Verdict.ALLOW_READ, ("READ",))

    # ToolTag.PROPOSE
    action = inp.action
    if action is None:
        return _deny("INVALID_ACTION")
    if _TRADE_RE.search(action.kind.lower()):
        return _deny("NO_TRADE_TOOLS")
    if action.kind not in {k.value for k in PROPOSABLE_KINDS}:
        return _deny("ACTION_KIND_NOT_PROPOSABLE")
    if inp.proposals_so_far >= MAX_PROPOSALS_PER_REQUEST:
        return _deny("PROPOSAL_LIMIT")
    if action.kind == "discord.post" and (
        action.channel_id is None or action.channel_id not in inp.allowlists.discord_channels
    ):
        return _deny("CHANNEL_NOT_ALLOWLISTED")
    if action.kind == "research.schedule":
        if action.every_minutes is None or action.every_minutes < MIN_SCHEDULE_MINUTES:
            return _deny("SCHEDULE_TOO_FREQUENT")
        if inp.args.has_refs:
            return _deny("TAINTED_ARG_REFUSED")

    warnings: list[ProposalWarning] = []
    if inp.args.has_refs and inp.args.tainted:
        warnings.append(ProposalWarning.TAINTED_ARGS)
    if any(not inp.allowlists.knows(r.address) for r in action.recipients):
        warnings.append(ProposalWarning.NEW_RECIPIENT)
    if any(not inp.allowlists.allows(r.address) for r in action.recipients):
        warnings.append(ProposalWarning.NOT_ON_ALLOWLIST)
    if inp.context is Context.UNATTENDED:
        warnings.append(ProposalWarning.UNATTENDED_ORIGIN)
    provenance = inp.args.provenance.union(inp.influence)
    reasons = ["APPROVAL_REQUIRED"]
    if provenance.tainted:
        reasons.append("TAINTED_PROVENANCE")
    return Decision(Verdict.PROPOSE, tuple(reasons), tuple(warnings), provenance)


# --- approvals -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ApprovalInput:
    channel: DecisionChannel
    decision: str  # "approve" | "deny"
    device_known: bool
    device_can_approve: bool
    step_up_valid: bool
    action_state: ActionState | None  # None: no such action
    hash_matches: bool
    now_ms: int
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class ApprovalVerdict:
    ok: bool
    code: str | None  # events.v1 request.result code when refused
    reason: str  # internal, for the audit record


def decide_approval(inp: ApprovalInput) -> ApprovalVerdict:
    """Whether an ``action.decide`` may take effect. Pure and deterministic.

    Denials go through the same checks as approvals: a deny is harmless, but treating both the same
    keeps the kiosk and voice paths unable to change pending state at all.
    """
    if inp.channel is not DecisionChannel.HUD:
        return ApprovalVerdict(
            False, "FORBIDDEN_DEVICE", "VOICE_CANNOT_APPROVE" if inp.channel is DecisionChannel.VOICE else "NOT_HUD"
        )
    if inp.decision not in ("approve", "deny"):
        return ApprovalVerdict(False, "INVALID_MESSAGE", "BAD_DECISION")
    if not inp.device_known:
        return ApprovalVerdict(False, "FORBIDDEN_DEVICE", "UNKNOWN_DEVICE")
    if not inp.device_can_approve:
        return ApprovalVerdict(False, "FORBIDDEN_DEVICE", "DEVICE_CANNOT_APPROVE")
    if inp.action_state is None or inp.action_state is not ActionState.PENDING:
        # Unknown, already decided (replay), expired or executed: nothing pending to decide on.
        return ApprovalVerdict(False, "UNKNOWN_ACTION", "NOT_PENDING" if inp.action_state else "UNKNOWN_ACTION")
    if inp.now_ms >= inp.expires_at_ms:
        return ApprovalVerdict(False, "EXPIRED", "EXPIRED")
    if not inp.step_up_valid:
        return ApprovalVerdict(False, "STEP_UP_REQUIRED", "STEP_UP_REQUIRED")
    if not inp.hash_matches:
        return ApprovalVerdict(False, "HASH_MISMATCH", "HASH_MISMATCH")
    return ApprovalVerdict(True, None, "APPROVED" if inp.decision == "approve" else "DENIED")
