"""The approval flow, core side (PLAN.md §2.9 rule 4, §4.5; AP-1..AP-4, RT-13).

``ApprovalService.decide`` is called with an ``action.decide`` that atlas-ui forwarded over
``core.sock``. atlas-ui authenticates the device (pairing token, Origin) and verifies the PIN
(``stepup-pin-hash`` is its credential); it registers each step-up token it grants here with
``StepUpRegistry.register``. Core then enforces, itself and not just in the UI:

* the decision came from the HUD channel (voice can never approve, RT-13/AP-2);
* the device is known to core's own device policy and may approve (kiosk cannot by default, AP-4);
* the step-up token was granted to *that* device and is unexpired (60 s);
* the action is still pending (a second decision is a replay and is refused) and unexpired (AP-3);
* the hash the HUD computed equals the stored ``payload_sha256`` (AP-1).

The checks themselves are the pure ``atlas.policy.engine.decide_approval``. On approval core builds
an approval record (format: ``atlas.executor.approval_record``), signs it with the Ed25519
``approval-signing-key``, writes the ``approval`` audit record (durable) and only then hands the
signed record and the exact ``payload_canonical`` to the executor.

Nothing on the planner side can reach this module: the orchestrator holds no reference to it and
no tool calls it (tests pin both).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from atlas.audit.client import AuditError
from atlas.core.auditing import AuditSink, action_record
from atlas.core.pending import EventSink, PendingAction, PendingActionStore
from atlas.executor.approval_record import (
    MAX_APPROVAL_TTL_MS,
    ApprovalRecord,
    ApprovalSigner,
    SignedApproval,
    new_approval_id,
    new_nonce,
)
from atlas.policy.engine import ApprovalInput, decide_approval
from atlas.policy.types import ActionKind, ActionState, Context, DecisionChannel

STEP_UP_TTL_MS: Final = 60_000  # events.v1 StepupGranted: 60 s after the PIN was verified
APPROVAL_TTL_MS: Final = 5 * 60 * 1000  # how long the executor may act on a signed approval
MAX_STEP_UP_TOKENS: Final = 64
_SHA_RE: Final = re.compile(r"[0-9a-f]{64}")


def _token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


class StepUpRegistry:
    """Step-up tokens granted by atlas-ui: bound to one device, optionally to one action, single use.

    Stored as hashes only. A token is consumed by the first decision that passes every check
    (owner decision after the review: stricter than events.v1's "valid 60 s", no contract change).
    If atlas-ui knows which pending action was on screen when the PIN was entered, it passes
    ``action_id`` and the token is good for that action only.
    """

    def __init__(self, clock_ms: Callable[[], int]) -> None:
        self._clock = clock_ms
        self._tokens: dict[bytes, tuple[str, int, str | None]] = {}  # digest -> (device_id, expires_at, action_id)

    def register(self, device_id: str, token: str, expires_at_ms: int, *, action_id: str | None = None) -> None:
        now = self._clock()
        if expires_at_ms > now + STEP_UP_TTL_MS:
            raise ValueError("step-up tokens live at most 60 s")
        self._prune(now)
        if len(self._tokens) >= MAX_STEP_UP_TOKENS:
            raise OverflowError("too many live step-up tokens")
        self._tokens[_token_digest(token)] = (device_id, expires_at_ms, action_id)

    def _find(self, token: str) -> bytes | None:
        digest = _token_digest(token)
        for known in self._tokens:
            if hmac.compare_digest(known, digest):
                return known
        return None

    def valid(self, device_id: str, token: str, action_id: str | None = None) -> bool:
        now = self._clock()
        self._prune(now)
        known = self._find(token)
        if known is None:
            return False
        owner, expires, bound = self._tokens[known]
        return owner == device_id and now < expires and (bound is None or bound == action_id)

    def consume(self, token: str) -> None:
        known = self._find(token)
        if known is not None:
            del self._tokens[known]

    def _prune(self, now: int) -> None:
        for digest in [d for d, (_, exp, _) in self._tokens.items() if exp <= now]:
            del self._tokens[digest]


@dataclass(frozen=True, slots=True)
class DevicePolicy:
    device_id: str
    role: Literal["kiosk", "remote"]
    can_approve: bool


class DeviceDirectory:
    """Core's own view of paired devices (config). The kiosk cannot approve unless settings say so."""

    def __init__(self, devices: Mapping[str, DevicePolicy] | None = None) -> None:
        self._devices = dict(devices or {})

    def add(self, device_id: str, role: Literal["kiosk", "remote"], *, can_approve: bool | None = None) -> None:
        self._devices[device_id] = DevicePolicy(
            device_id, role, role == "remote" if can_approve is None else can_approve
        )

    def get(self, device_id: str) -> DevicePolicy | None:
        return self._devices.get(device_id)


class ExecutorLink(Protocol):
    """How core reaches atlas-executor (``executor.sock`` in production)."""

    async def submit(self, envelope: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class DecideResult:
    ok: bool
    code: str | None = None  # events.v1 request.result code
    signed: SignedApproval | None = None


class ApprovalService:
    def __init__(
        self,
        pending: PendingActionStore,
        devices: DeviceDirectory,
        stepups: StepUpRegistry,
        signer: ApprovalSigner,
        audit: AuditSink,
        events: EventSink,
        clock_ms: Callable[[], int],
        *,
        approval_ttl_ms: int = APPROVAL_TTL_MS,
    ) -> None:
        if not 0 < approval_ttl_ms <= MAX_APPROVAL_TTL_MS:
            raise ValueError("approval TTL out of range")
        self._pending = pending
        self._devices = devices
        self._stepups = stepups
        self._signer = signer
        self._audit = audit
        self._events = events
        self._clock = clock_ms
        self._ttl = approval_ttl_ms

    async def expire_due(self) -> list[str]:
        now = self._clock()
        expired = []
        for action in self._pending.due_to_expire(now):
            self._pending.set_state(action, ActionState.EXPIRED, now)
            await self._events.emit("action.resolved", {"action_id": action.action_id, "outcome": "expired", "at": now})
            expired.append(action.action_id)
        return expired

    async def decide(
        self,
        *,
        channel: DecisionChannel,
        device_id: str,
        action_id: str,
        decision: str,
        payload_sha256: str,
        step_up_token: str,
    ) -> DecideResult:
        now = self._clock()
        action = self._pending.get(action_id)
        device = self._devices.get(device_id)
        verdict = decide_approval(
            ApprovalInput(
                channel=channel,
                decision=decision,
                device_known=device is not None,
                device_can_approve=device is not None and device.can_approve,
                step_up_valid=self._stepups.valid(device_id, step_up_token, action_id),
                action_state=None if action is None else action.state,
                hash_matches=action is not None
                and _SHA_RE.fullmatch(payload_sha256) is not None
                and hmac.compare_digest(action.payload_sha256, payload_sha256),
                now_ms=now,
                expires_at_ms=0 if action is None else action.expires_at_ms,
            )
        )
        if not verdict.ok:
            if verdict.code == "EXPIRED" and action is not None and action.state is ActionState.PENDING:
                self._pending.set_state(action, ActionState.EXPIRED, now)
                await self._events.emit("action.resolved", {"action_id": action_id, "outcome": "expired", "at": now})
            return DecideResult(False, verdict.code)
        assert action is not None  # noqa: S101 - decide_approval refuses unknown actions
        # Claim the action and spend the step-up token *before* the first await (review F5): a second
        # decision arriving while this one is being audited sees DECIDING and is refused with BUSY.
        self._pending.set_state(action, ActionState.DECIDING, now)
        self._stepups.consume(step_up_token)
        try:
            if decision == "deny":
                result = await self._deny(action, device_id, now)
            else:
                result = await self._approve(action, device_id, now)
        except BaseException:
            self._pending.set_state(action, ActionState.PENDING, now)
            raise
        if not result.ok:
            self._pending.set_state(action, ActionState.PENDING, now)  # nothing recorded; the user may retry
        return result

    async def _deny(self, action: PendingAction, device_id: str, now: int) -> DecideResult:
        try:
            await self._audit.append(self._audit_record(action, device_id, "deny", new_approval_id(), None))
        except AuditError:
            return DecideResult(False, "BUSY")
        self._pending.set_state(action, ActionState.DENIED, now)
        await self._events.emit("action.resolved", {"action_id": action.action_id, "outcome": "denied", "at": now})
        return DecideResult(True)

    async def _approve(self, action: PendingAction, device_id: str, now: int) -> DecideResult:
        decided_at = max(now, action.created_at_ms + 1)  # strictly after creation, as the executor requires
        record = ApprovalRecord(
            approval_id=new_approval_id(),
            action_id=action.action_id,
            action_kind=action.kind,
            payload_sha256=action.payload_sha256,
            origin_context=action.origin_context.value,
            decision_context=Context.INTERACTIVE.value,
            approved_via=DecisionChannel.HUD.value,
            device_id=device_id,
            decision="approve",
            step_up=True,
            action_created_at=action.created_at_ms,
            decided_at=decided_at,
            expires_at=min(decided_at + self._ttl, max(action.expires_at_ms, decided_at + 1)),
            nonce=new_nonce(),
            key_id=self._signer.key_id,
        )
        signed = self._signer.sign(record)
        try:
            reply = await self._audit.append(
                self._audit_record(action, device_id, "approve", record.approval_id, record.expires_at)
            )
        except AuditError:
            return DecideResult(False, "BUSY")  # no durable approval record, no approval
        if not reply.durable:
            return DecideResult(False, "BUSY")
        self._pending.set_state(action, ActionState.APPROVED, now)
        await self._events.emit("action.resolved", {"action_id": action.action_id, "outcome": "approved", "at": now})
        return DecideResult(True, None, signed)

    @staticmethod
    def _audit_record(
        action: PendingAction, device_id: str, decision: str, approval_id: str, expires_at: int | None
    ) -> dict[str, Any]:
        return action_record(
            "approval",
            context=Context.INTERACTIVE,
            action_id=action.action_id,
            action_kind=action.kind,
            payload_sha256=action.payload_sha256,
            correlation_id=action.correlation_id,
            device_id=device_id,
            approval_id=approval_id,
            decision=decision,
            step_up=True,
            tainted=action.provenance.tainted,
            expires_at=expires_at,
        )

    async def execute(self, signed: SignedApproval, link: ExecutorLink) -> dict[str, Any]:
        """Hand an approved action to the executor and report the outcome on the event stream."""
        action = self._pending.get(signed.record.action_id)
        if action is None or action.state is not ActionState.APPROVED:
            return {"ok": False, "code": "NOT_APPROVED"}
        if action.kind not in (ActionKind.EMAIL_SEND.value, ActionKind.DISCORD_POST.value):
            return {"ok": False, "code": "UNSUPPORTED_KIND"}
        reply = await link.submit(signed.envelope(action.payload_canonical))
        now = self._clock()
        if reply.get("ok") is True:
            self._pending.set_state(action, ActionState.EXECUTED, now)
            await self._events.emit(
                "action.resolved", {"action_id": action.action_id, "outcome": "executed", "at": now}
            )
        else:
            self._pending.set_state(action, ActionState.FAILED, now)
            code = "SEND_FAILED" if reply.get("code") == "SEND_FAILED" else "EXECUTOR_REJECTED"
            await self._events.emit(
                "action.resolved", {"action_id": action.action_id, "outcome": "failed", "code": code, "at": now}
            )
        return reply
