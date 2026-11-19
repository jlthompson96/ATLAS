"""atlas-executor's request handling: verify, claim, write-ahead, send, write-result (PLAN.md §2.12).

Order matters and every step fails closed:

1. ``ApprovalVerifier.verify``: signature, hash, approval, context, expiry (no side effects).
2. ``ApprovalVerifier.claim``: the nonce, approval ID and action ID become used, durably. A repeat is
   ``REPLAYED``. Claiming first means a crash later can never let the same approval send twice.
3. ``write_ahead`` audit record. It must be acknowledged *durable* (fsynced) or nothing is sent
   (``AUDIT_UNAVAILABLE``). A ``write_ahead`` without a ``write_result`` after a crash means
   "outcome unknown": atlas-audit surfaces it and nothing retries automatically (FI-3).
4. The sender acts.
5. ``write_result`` audit record with the outcome.

Payload content is never logged; logs carry action IDs, codes and hashes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from atlas.audit.client import AuditError
from atlas.core.auditing import AuditSink, action_record
from atlas.executor.senders import Sender
from atlas.executor.verify import ApprovalVerifier, Rejected, Verified
from atlas.policy.types import Context

log = logging.getLogger("atlas.executor")


@dataclass(frozen=True, slots=True)
class ExecResult:
    ok: bool
    code: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"ok": True, "status": "executed"} if self.ok else {"ok": False, "code": self.code}


class Executor:
    def __init__(self, verifier: ApprovalVerifier, audit: AuditSink, senders: Mapping[str, Sender]) -> None:
        for kind, sender in senders.items():
            if sender.kind != kind:
                raise ValueError("sender registered under the wrong kind")
        self._verifier = verifier
        self._audit = audit
        self._senders = dict(senders)
        self.executions = 0  # sends attempted after a durable write_ahead (UW-2 checks it stays 0)

    async def execute(self, envelope: Any) -> ExecResult:
        try:
            verified = self._verifier.verify(envelope)
            sender = self._senders.get(verified.record.action_kind)
            if sender is None:
                raise Rejected("UNSUPPORTED_KIND")
            self._verifier.claim(verified)
        except Rejected as exc:
            log.warning("refused an execution request: %s", exc.code)
            return ExecResult(False, exc.code)

        try:
            reply = await self._audit.append(self._record("write_ahead", verified, sender))
        except AuditError:
            log.error("write_ahead not recorded for %s; not acting", verified.record.action_id)
            return ExecResult(False, "AUDIT_UNAVAILABLE")
        if not reply.durable:
            log.error("write_ahead for %s not durable; not acting", verified.record.action_id)
            return ExecResult(False, "AUDIT_UNAVAILABLE")

        self.executions += 1
        try:
            outcome = await sender.send(verified.payload)
        except Exception:
            log.exception("sender raised for %s", verified.record.action_id)
            outcome = None
        ok = outcome is not None and outcome.ok
        code = None if ok else (outcome.code if outcome is not None and outcome.code else "SEND_FAILED")
        result: dict[str, Any] = {"status": "ok"} if ok else {"status": "error", "code": code}
        try:
            await self._audit.append(self._record("write_result", verified, sender, result=result))
        except AuditError:
            # The write_ahead without a write_result is surfaced as "outcome unknown"; never retried.
            log.error("write_result not recorded for %s", verified.record.action_id)
        return ExecResult(True) if ok else ExecResult(False, code)

    @staticmethod
    def _record(kind: str, verified: Verified, sender: Sender, **extra: Any) -> dict[str, Any]:
        rec = verified.record
        return action_record(
            kind,
            context=Context.INTERACTIVE,  # every executed action was approved by a present human
            action_id=rec.action_id,
            action_kind=rec.action_kind,
            payload_sha256=rec.payload_sha256,
            tool=sender.audit_tool,
            approval_id=rec.approval_id,
            **extra,
        )
