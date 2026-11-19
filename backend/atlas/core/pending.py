"""Pending actions: ``propose_action`` builds the exact payload a human will approve (PLAN.md §2.9, §4.5).

1. ``draft_payload`` resolves the planner's arguments into the exact payload. ``$VAR`` references
   are resolved here, in code the planner never sees the output of: the payload goes to the HUD (as
   ``payload_canonical``) and later to the executor, never back into a planner prompt. The union of
   the referenced values' provenance travels with the draft.
2. The policy engine decides (always ``PROPOSE`` for a valid draft, or ``DENY``).
3. ``ActionProposer.propose`` canonicalizes the payload with JCS, hashes it, writes the
   ``action_proposed`` audit record (durable, fail closed), stores the action and emits
   ``action.pending`` exactly as events.v1 defines it.

The planner learns only the action ID and that approval is pending.
"""

from __future__ import annotations

import secrets
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from atlas.core.auditing import AuditSink, action_record
from atlas.core.schema import Array, Enum, Int, Obj, PlainText, Ref, RefOr
from atlas.core.taint import (
    EMPTY_PROVENANCE,
    VAR_NAME_RE,
    Provenance,
    TaintedStr,
    UntrustedContent,
    VariableStore,
)
from atlas.policy.engine import ArgTaint, Decision, ProposedAction, Recipient, Verdict
from atlas.policy.payloads import (
    EMAIL_RE,
    MAX_EMAIL_BODY,
    PROPOSABLE_KINDS,
    canonical_text,
    payload_sha256,
    valid_email,
    validate_payload,
)
from atlas.policy.types import ActionState, Context, ProposalWarning

DEFAULT_TTL_MS: Final = 60 * 60 * 1000  # one hour, as in the mock scenarios
MAX_PARTS: Final = 20


class EventSink(Protocol):
    """Where atlas-core sends events.v1 server payloads (atlas-ui adds the envelope)."""

    async def emit(self, type_: str, payload: Mapping[str, Any]) -> None: ...


class DraftError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_EMAIL_TEXT = PlainText(254, pattern=EMAIL_RE.pattern, single_line=True)
_PARTS = Array(RefOr(PlainText(4000)), max_items=MAX_PARTS, min_items=1)

PROPOSE_ARGS: Final = Obj(
    {
        "kind": Enum(tuple(sorted(k.value for k in PROPOSABLE_KINDS)), description="What to do."),
        "summary": PlainText(200, min_len=1, single_line=True, description="One line for the approval card."),
        "to": Array(RefOr(_EMAIL_TEXT), max_items=10, description="email.send: addresses or $VAR names."),
        "cc": Array(RefOr(_EMAIL_TEXT), max_items=10),
        "subject": RefOr(PlainText(200, single_line=True)),
        "body": Array(RefOr(PlainText(4000)), max_items=MAX_PARTS, min_items=1, description="Text parts and/or $VAR names, joined in order."),
        "in_reply_to": Ref(description="email.send: the $EMAIL_n item being answered."),
        "channel_id": PlainText(20, pattern="[0-9]{17,20}", description="discord.post: an allowlisted channel ID."),
        "content": _PARTS,
        "question": PlainText(300, min_len=1, single_line=True, description="research.schedule: the question."),
        "every_minutes": Int(1, 10080),
    },
    required=frozenset({"kind", "summary"}),
)

_KIND_FIELDS: Final[dict[str, tuple[frozenset[str], frozenset[str]]]] = {
    "email.send": (frozenset({"to", "subject", "body"}), frozenset({"cc", "in_reply_to"})),
    "discord.post": (frozenset({"channel_id", "content"}), frozenset()),
    "research.schedule": (frozenset({"question", "every_minutes"}), frozenset()),
}


@dataclass(frozen=True, slots=True)
class Draft:
    kind: str
    payload: dict[str, Any]
    args: ArgTaint
    action: ProposedAction


def _text_of(value: str, store: VariableStore, used: list[Provenance]) -> tuple[str, bool]:
    """Resolve one literal-or-$VAR value. Returns (text, came_from_var)."""
    if VAR_NAME_RE.fullmatch(value) is None:
        return value, False
    var = store.lookup(value)
    if var is None:
        raise DraftError("UNDEFINED_VARIABLE")
    if not isinstance(var.value, TaintedStr):
        raise DraftError("ITEM_NOT_TEXT")  # an item ref can only be in_reply_to
    used.append(var.provenance)
    return var.value._reveal(), True


def draft_payload(args: Mapping[str, Any], store: VariableStore, channel_labels: Mapping[str, str]) -> Draft:
    kind = args["kind"]
    required, optional = _KIND_FIELDS[kind]
    present = set(args) - {"kind", "summary"}
    if not required <= present or not present <= required | optional:
        raise DraftError("INVALID_ARGS")
    used: list[Provenance] = []
    has_refs = False
    payload: dict[str, Any]
    action: ProposedAction
    if kind == "email.send":
        recipients: dict[str, list[Recipient]] = {"to": [], "cc": []}
        for name in ("to", "cc"):
            for entry in args.get(name, []):
                text, from_var = _text_of(entry, store, used)
                has_refs |= from_var
                address = text.strip()
                if not valid_email(address):
                    raise DraftError("INVALID_RECIPIENT")
                recipients[name].append(Recipient(address, from_var))
        subject, from_var = _text_of(args["subject"], store, used)
        has_refs |= from_var
        subject = " ".join(subject.split())[:200]
        body_parts: list[str] = []
        for part in args["body"]:
            text, from_var = _text_of(part, store, used)
            has_refs |= from_var
            body_parts.append(text)
        body = "".join(body_parts)
        if len(body) > MAX_EMAIL_BODY:
            raise DraftError("PAYLOAD_TOO_LARGE")
        payload = {
            "kind": kind,
            "to": [r.address for r in recipients["to"]],
            "cc": [r.address for r in recipients["cc"]],
            "subject": subject,
            "body": body,
        }
        if "in_reply_to" in args:
            item = store.lookup(args["in_reply_to"])
            if item is None:
                raise DraftError("UNDEFINED_VARIABLE")
            if not isinstance(item.value, UntrustedContent):
                raise DraftError("NOT_AN_ITEM")
            has_refs = True
            used.append(item.provenance)
            payload["in_reply_to"] = item.value.source.resource_id
        action = ProposedAction(kind, tuple(recipients["to"] + recipients["cc"]))
    elif kind == "discord.post":
        channel_id = args["channel_id"]
        parts: list[str] = []
        for part in args["content"]:
            text, from_var = _text_of(part, store, used)
            has_refs |= from_var
            parts.append(text)
        payload = {
            "kind": kind,
            "channel_id": channel_id,
            "channel_label": channel_labels.get(channel_id, channel_id),
            "content": "".join(parts),
        }
        action = ProposedAction(kind, channel_id=channel_id)
    else:  # research.schedule: literal question only (research never sees private data, §2.13)
        payload = {"kind": kind, "question": args["question"], "every_minutes": args["every_minutes"]}
        action = ProposedAction(kind, every_minutes=args["every_minutes"])
    provenance = EMPTY_PROVENANCE.union(*used)
    return Draft(kind, payload, ArgTaint(has_refs=has_refs, provenance=provenance), action)


def clean_summary(text: str) -> str:
    """The approval card's one-liner: the planner's own words, single line, no control characters."""
    kept = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    return " ".join(kept.split())[:200] or "Proposed action"


@dataclass
class PendingAction:
    action_id: str
    kind: str
    summary: str
    payload_canonical: str
    payload_sha256: str
    provenance: Provenance
    warnings: tuple[ProposalWarning, ...]
    created_at_ms: int
    expires_at_ms: int
    origin_context: Context
    correlation_id: str
    state: ActionState = ActionState.PENDING
    history: list[tuple[ActionState, int]] = field(default_factory=list)

    def to_event(self) -> dict[str, Any]:
        """events.v1 ``action.pending`` (unredacted form)."""
        event: dict[str, Any] = {
            "action_id": self.action_id,
            "kind": self.kind,
            "summary": self.summary,
            "payload_canonical": self.payload_canonical,
            "payload_sha256": self.payload_sha256,
            "provenance": self.provenance.to_event(),
            "created_at": self.created_at_ms,
            "expires_at": self.expires_at_ms,
            "redacted": False,
        }
        if self.warnings:
            event["warnings"] = [w.value for w in self.warnings]
        return event


class PendingActionStore:
    MAX_PENDING: Final = 50

    def __init__(self) -> None:
        self._actions: dict[str, PendingAction] = {}

    def add(self, action: PendingAction) -> None:
        if action.action_id in self._actions:
            raise ValueError("duplicate action id")
        if len(self.pending()) >= self.MAX_PENDING:
            raise OverflowError("too many pending actions")
        self._actions[action.action_id] = action

    def get(self, action_id: str) -> PendingAction | None:
        return self._actions.get(action_id)

    def set_state(self, action: PendingAction, state: ActionState, at_ms: int) -> None:
        action.history.append((action.state, at_ms))
        action.state = state

    def pending(self) -> list[PendingAction]:
        return [a for a in self._actions.values() if a.state is ActionState.PENDING]

    def all(self) -> Sequence[PendingAction]:
        return list(self._actions.values())

    def due_to_expire(self, now_ms: int) -> list[PendingAction]:
        return [a for a in self.pending() if a.expires_at_ms <= now_ms]


class ProposalFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ActionProposer:
    def __init__(
        self,
        store: PendingActionStore,
        audit: AuditSink,
        events: EventSink,
        clock_ms: Callable[[], int],
        *,
        ttl_ms: int = DEFAULT_TTL_MS,
        new_id: Callable[[], str] = lambda: "act-" + secrets.token_hex(8),
    ) -> None:
        self._store = store
        self._audit = audit
        self._events = events
        self._clock = clock_ms
        self._ttl = ttl_ms
        self._new_id = new_id

    async def propose(
        self, draft: Draft, decision: Decision, summary: str, *, context: Context, correlation_id: str
    ) -> PendingAction:
        if decision.verdict is not Verdict.PROPOSE:
            raise ProposalFailed("NOT_A_PROPOSAL")
        if validate_payload(draft.payload):
            raise ProposalFailed("INVALID_PAYLOAD")
        canonical = canonical_text(draft.payload)
        now = self._clock()
        action = PendingAction(
            action_id=self._new_id(),
            kind=draft.kind,
            summary=clean_summary(summary),
            payload_canonical=canonical,
            payload_sha256=payload_sha256(canonical),
            provenance=decision.provenance,
            warnings=decision.warnings,
            created_at_ms=now,
            expires_at_ms=now + self._ttl,
            origin_context=context,
            correlation_id=correlation_id,
        )
        record = action_record(
            "action_proposed",
            context=context,
            action_id=action.action_id,
            action_kind=action.kind,
            payload_sha256=action.payload_sha256,
            tool="propose_action",
            correlation_id=correlation_id,
            tainted=decision.tainted,
            expires_at=action.expires_at_ms,
        )
        reply = await self._audit.append(record)  # AuditError propagates: no audit, no proposal
        if not reply.durable:
            raise ProposalFailed("AUDIT_NOT_DURABLE")
        self._store.add(action)
        await self._events.emit("action.pending", action.to_event())
        return action


class ListEventSink:
    """Collects emitted events (tests, CLI). ``validate`` may check each against events.v1."""

    def __init__(self, validate: Callable[[str, Mapping[str, Any]], None] | None = None) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self._validate = validate

    async def emit(self, type_: str, payload: Mapping[str, Any]) -> None:
        if self._validate is not None:
            self._validate(type_, payload)
        self.events.append((type_, dict(payload)))

    def of_type(self, type_: str) -> list[dict[str, Any]]:
        return [p for t, p in self.events if t == type_]
