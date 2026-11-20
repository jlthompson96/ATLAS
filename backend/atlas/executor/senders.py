"""Write tools: the only code in ATLAS tagged ``write`` (PLAN.md §2.9 rules 3 and 4).

A ``Sender`` acts on the outside world. Senders exist only inside atlas-executor and run only after
``atlas.executor.verify`` accepted a signed human approval. No atlas-core module imports this one,
so no planner registry (interactive or unattended) can ever contain a sender (UW-1; checked by
``tests/test_core_registry.py``).

Phase 1 ships ``FakeSender`` only. Phase 6 adds:

* ``GmailSender``: ``gmail.send`` through the ``atlas-executor`` CONNECT proxy port (3133), with the
  ``google-send-refresh-token`` credential attached at the transport layer.
* ``DiscordPoster``: the poster bot (Send Messages only, allowlisted channels).

Both implement the same interface and must never log payload content.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from atlas.policy.types import ToolTag


@dataclass(frozen=True, slots=True)
class SendOutcome:
    ok: bool
    code: str | None = None  # UPPER_SNAKE on failure, e.g. SEND_FAILED, GRANT_EXPIRED
    remote_id: str | None = None  # provider message ID, for the audit trail


class Sender(ABC):
    tag: ClassVar[ToolTag] = ToolTag.WRITE
    kind: ClassVar[str]  # the ActionKind this sender performs
    audit_tool: ClassVar[str]  # audit-record.v1 ``tool`` for write_ahead / write_result

    @abstractmethod
    async def send(self, payload: dict[str, Any]) -> SendOutcome: ...


class FakeSender(Sender):
    """Records what it would have sent. Used by tests and the development executor."""

    def __init__(self, kind: str, *, fail_with: str | None = None) -> None:
        self.kind = kind  # type: ignore[misc]
        self.audit_tool = kind  # type: ignore[misc]
        self.sent: list[dict[str, Any]] = []
        self._fail_with = fail_with

    async def send(self, payload: dict[str, Any]) -> SendOutcome:
        if self._fail_with is not None:
            return SendOutcome(False, self._fail_with)
        self.sent.append(payload)
        return SendOutcome(True, None, f"fake-{len(self.sent)}")
