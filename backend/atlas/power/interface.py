"""PowerController contract, v1 (PLAN.md §5.4, §5.8).

Kept separate from ``LLMBackend`` so swapping LLM servers never changes how the host is woken or
put to sleep. The implementation (Wake-on-LAN + ``atlas-hostd``) lives in ``atlas.power``.

Rules every implementation follows:

* **Policy in code, not prompts.** ``wake`` enforces the rate limit (speculative wakes ≤ 6/h,
  07:00 to 23:00, D12), the off-hours window for scheduled research (01:00 to 06:00, D11) and the
  backoff after failures. A refused wake is a normal return value, not an exception.
* **Everything is audited.** Each call writes an audit record of kind ``power``
  (contracts/audit-record.v1.schema.json) before returning.
* **Sleep is a request.** ``atlas-hostd`` has the final say and may refuse (hostd.v1.yaml).
* **Readiness is not this interface's job.** ``wake`` sends packets; ``LLMBackend.ensure_ready``
  proves the model is usable.

Contract revision: 1.0.0. Changes follow contracts/README.md.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

CONTRACT_REVISION: Final = "1.0.0"


class PowerState(StrEnum):
    ASLEEP = "asleep"
    WAKING = "waking"
    UP = "up"
    UNKNOWN = "unknown"


class WakeReason(StrEnum):
    INTERACTIVE = "interactive"  # a user request needs the LLM now
    SPECULATIVE = "speculative"  # wake word heard; may not be needed (§5.4 mitigation 1)
    SCHEDULED_RESEARCH = "scheduled_research"  # off-hours window only
    MANUAL = "manual"  # user pressed "wake core"
    RETRY = "retry"  # backoff retry of a queued request


class WakeRefusal(StrEnum):
    RATE_LIMITED = "rate_limited"
    OUTSIDE_ACTIVE_HOURS = "outside_active_hours"  # speculative wakes
    OUTSIDE_WINDOW = "outside_window"  # scheduled research
    BACKOFF = "backoff"  # recent failures; next attempt not due yet
    ALREADY_UP = "already_up"


class SleepReason(StrEnum):
    IDLE = "idle"  # 20 min with no ATLAS request and the queue empty or blocked
    QUEUE_DRAINED = "queue_drained"  # after an off-hours research run and backup
    SPECULATIVE_UNUSED = "speculative_unused"  # 5 min grace after an unused speculative wake


@dataclass(frozen=True, slots=True)
class WakeAttempt:
    """Packets have been (or are being) sent. Progress is reported through ``state()``."""

    wake_id: str
    reason: WakeReason
    started_at_monotonic: float
    deadline_monotonic: float  # 60 s interactive, 120 s unattended


@dataclass(frozen=True, slots=True)
class WakeRefused:
    reason: WakeRefusal
    retry_after_s: int | None = None


@dataclass(frozen=True, slots=True)
class SleepAccepted:
    sleep_state: str  # "s3" | "modern_standby" | "hibernate", as reported by atlas-hostd


@dataclass(frozen=True, slots=True)
class SleepRefused:
    reasons: tuple[str, ...]  # hostd RefusalReason values, or Pi-side codes such as "queue_busy"


class PowerController(abc.ABC):
    @abc.abstractmethod
    async def state(self) -> PowerState:
        """Current belief about the host. Never blocks longer than a cached health probe."""

    @abc.abstractmethod
    async def wake(self, reason: WakeReason) -> WakeAttempt | WakeRefused:
        """Policy-checked, audited, rate-limited. Idempotent while a wake is already in flight:
        returns the in-flight ``WakeAttempt``."""

    @abc.abstractmethod
    async def request_sleep(self, reason: SleepReason) -> SleepAccepted | SleepRefused:
        """Checks the Pi-side conditions, then asks ``atlas-hostd`` (POST /v1/sleep)."""
