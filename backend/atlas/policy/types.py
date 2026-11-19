"""Enumerations shared by the policy engine, atlas-core and atlas-executor (PLAN.md §2.9).

These are code-level vocabulary, not a wire contract. Where a value also appears on the wire it
mirrors the contract exactly (``Context`` = audit-record.v1 ``context``; ``ProposalWarning`` =
events.v1 ``action.pending.warnings``; ``ActionKind`` = events.v1 ``ActionKind``), and
``tests/test_policy_engine.py`` checks the mirrors against the schemas.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Context(StrEnum):
    """Execution context of an LLM call or tool call (audit-record.v1 ``context``)."""

    INTERACTIVE = "interactive"  # the user is present and asked for this
    UNATTENDED = "unattended"  # scheduled or background work; nobody is watching
    QUARANTINED = "quarantined"  # the quarantined reader: untrusted input, no tools
    SYSTEM = "system"  # not tied to an LLM context


class ToolTag(StrEnum):
    """What a tool can do. The registry and the policy engine both key off this tag."""

    READ = "read"  # reads trusted or metadata-only state; no side effects outside ATLAS
    READER = "reader"  # hands an untrusted item to the quarantined reader (ask_reader)
    QUEUE = "queue"  # enqueues local work (one-off research); no external side effects
    PROPOSE = "propose"  # creates a pending action that a human must approve
    WRITE = "write"  # acts on the outside world. Exists only inside atlas-executor.


class ActionKind(StrEnum):
    """events.v1 ``ActionKind``. There is deliberately no trade kind (RT-15)."""

    EMAIL_SEND = "email.send"
    DISCORD_POST = "discord.post"
    RESEARCH_SCHEDULE = "research.schedule"
    PORTFOLIO_IMPORT = "portfolio.import"


class ProposalWarning(StrEnum):
    """events.v1 ``action.pending.warnings``."""

    NEW_RECIPIENT = "new_recipient"
    NOT_ON_ALLOWLIST = "not_on_allowlist"
    TAINTED_ARGS = "tainted_args"
    UNATTENDED_ORIGIN = "unattended_origin"


class ActionState(StrEnum):
    PENDING = "pending"
    DECIDING = "deciding"  # a decision passed every check and is being recorded; others are refused (review F5)
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


class DecisionChannel(StrEnum):
    """Where an approval decision physically came from. Only ``HUD`` can ever approve (RT-13, AP-2)."""

    HUD = "hud"  # action.decide on /ws/events, forwarded by atlas-ui
    VOICE = "voice"  # anything that came through atlas-voice
    TEXT = "text"  # text.input or the CLI: goes to the planner, never to approvals
    PLANNER = "planner"  # the model itself


# Tags each context's registry may contain. WRITE appears in none of them: write tools live only
# in atlas-executor, which acts on signed human approvals (§2.9 rules 3 and 4, UW-1).
CONTEXT_TAGS: Final[dict[Context, frozenset[ToolTag]]] = {
    Context.INTERACTIVE: frozenset({ToolTag.READ, ToolTag.READER, ToolTag.QUEUE, ToolTag.PROPOSE}),
    Context.UNATTENDED: frozenset({ToolTag.READ, ToolTag.READER, ToolTag.PROPOSE}),
    Context.QUARANTINED: frozenset(),
    Context.SYSTEM: frozenset(),
}
