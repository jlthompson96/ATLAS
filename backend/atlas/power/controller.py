"""``HostPowerController``: Wake-on-LAN + ``atlas-hostd`` behind the PowerController contract.

Policy lives here, in code (contract rule 1, PLAN.md §5.4):

* speculative wakes: at most 6 per hour, only 07:00 to 23:00 local time (D12);
* scheduled research wakes: only inside the 01:00 to 06:00 window (D11), 120 s deadline;
* ``retry`` wakes obey the 1, 5, 15 minute backoff after failures, then hold until a manual or
  interactive wake succeeds;
* magic packets go out at 0, 2, 5, 10 and 20 s of an attempt (``tick`` sends the due ones);
* sleep is requested only after the Pi-side conditions hold (20 min without an LLM request and an
  empty queue; 5 min grace for an unused speculative wake), then hostd decides.

Every wake and sleep decision is written to the audit log as a ``power`` record. Time, packets,
hostd and the audit sink are injected so the tests run on a fake clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from atlas.core.auditing import AuditSink
from atlas.power.hostd import HostdApi, HostdError, HostdForbidden, HostdUnreachable, SleepNo
from atlas.power.interface import (
    PowerController,
    PowerState,
    SleepAccepted,
    SleepReason,
    SleepRefused,
    WakeAttempt,
    WakeReason,
    WakeRefusal,
    WakeRefused,
)
from atlas.power.wol import PacketSender


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def now(self) -> datetime:
        """Timezone-aware local time."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> datetime:
        return datetime.now().astimezone()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class PowerConfig:
    active_hours: tuple[int, int] = (7, 23)  # speculative wakes, [start, end) local hours
    research_window: tuple[int, int] = (1, 6)  # scheduled research wakes
    speculative_per_hour: int = 6
    interactive_deadline_s: float = 60.0
    unattended_deadline_s: float = 120.0
    packet_offsets_s: tuple[float, ...] = (0.0, 2.0, 5.0, 10.0, 20.0)
    retry_backoff_s: tuple[float, ...] = (60.0, 300.0, 900.0)
    idle_sleep_s: float = 20 * 60.0
    speculative_grace_s: float = 5 * 60.0
    sleep_retry_s: float = 60.0  # after a refused sleep, don't ask hostd again for this long
    health_cache_s: float = 2.0


def _in_hours(now: datetime, window: tuple[int, int]) -> bool:
    start, end = window
    return start <= now.hour < end if start <= end else now.hour >= start or now.hour < end


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class HostPowerController(PowerController):
    def __init__(
        self,
        hostd: HostdApi,
        sender: PacketSender | None,
        audit: AuditSink,
        *,
        clock: Clock | None = None,
        config: PowerConfig | None = None,
        queue_busy: Callable[[], bool] = lambda: False,
    ) -> None:
        self._hostd = hostd
        self._sender = sender
        self._audit = audit
        self._clock: Clock = clock or SystemClock()
        self._cfg = config or PowerConfig()
        self._queue_busy = queue_busy
        self._attempt: WakeAttempt | None = None
        self._attempt_wall: datetime | None = None
        self._next_packet = 0
        self._speculative: deque[float] = deque()
        self._failures = 0
        self._retry_not_before = 0.0
        self._last_llm: float | None = None
        self._claim: dict[str, str] | None = None  # the wake that started the current host cycle
        self._claim_speculative = False
        self._llm_since_claim = False
        self._atlas_slept = False
        self._sleep_not_before = 0.0
        self._probe_at = -1e18
        self._probe_up: bool | None = None
        self._forbidden = False

    # --- PowerController ----------------------------------------------------------------------

    async def state(self) -> PowerState:
        now = self._clock.monotonic()
        if now - self._probe_at >= self._cfg.health_cache_s:
            self._forbidden = False
            try:
                await self._hostd.health()
                self._probe_up = True
            except HostdUnreachable:
                self._probe_up = False
            except HostdForbidden:
                self._probe_up = None
                self._forbidden = True
            except HostdError:
                self._probe_up = True  # it answered, just badly
            self._probe_at = self._clock.monotonic()
        if self._forbidden:
            return PowerState.UNKNOWN
        if self._probe_up:
            self._atlas_slept = False
            return PowerState.UP
        if self._attempt is not None and self._clock.monotonic() < self._attempt.deadline_monotonic:
            return PowerState.WAKING
        return PowerState.ASLEEP if self._atlas_slept else PowerState.UNKNOWN

    async def wake(self, reason: WakeReason) -> WakeAttempt | WakeRefused:
        now = self._clock.monotonic()
        if self._attempt is not None and now < self._attempt.deadline_monotonic:
            return self._attempt  # idempotent while in flight
        self._probe_at = -1e18  # force a fresh probe
        if await self.state() is PowerState.UP:
            return await self._refuse(reason, WakeRefusal.ALREADY_UP, None)
        wall = self._clock.now()
        now = self._clock.monotonic()
        if reason is WakeReason.SPECULATIVE:
            if not _in_hours(wall, self._cfg.active_hours):
                return await self._refuse(reason, WakeRefusal.OUTSIDE_ACTIVE_HOURS, None)
            while self._speculative and now - self._speculative[0] >= 3600:
                self._speculative.popleft()
            if len(self._speculative) >= self._cfg.speculative_per_hour:
                return await self._refuse(
                    reason, WakeRefusal.RATE_LIMITED, max(1, int(self._speculative[0] + 3600 - now))
                )
        elif reason is WakeReason.SCHEDULED_RESEARCH:
            if not _in_hours(wall, self._cfg.research_window):
                return await self._refuse(reason, WakeRefusal.OUTSIDE_WINDOW, None)
        elif reason is WakeReason.RETRY and self._failures:
            if self._failures > len(self._cfg.retry_backoff_s):
                return await self._refuse(reason, WakeRefusal.BACKOFF, None)  # held until a manual wake
            if now < self._retry_not_before:
                return await self._refuse(reason, WakeRefusal.BACKOFF, max(1, int(self._retry_not_before - now)))
        if reason is WakeReason.SPECULATIVE:
            self._speculative.append(now)
        deadline_s = (
            self._cfg.unattended_deadline_s
            if reason is WakeReason.SCHEDULED_RESEARCH
            else self._cfg.interactive_deadline_s
        )
        attempt = WakeAttempt(f"w{secrets.token_hex(6)}", reason, now, now + deadline_s)
        self._attempt = attempt
        self._attempt_wall = wall
        self._next_packet = 0
        await self._record("wake_requested", wake_id=attempt.wake_id, reason=reason.value)
        self.tick()
        return attempt

    async def request_sleep(self, reason: SleepReason, *, user_requested: bool = False) -> SleepAccepted | SleepRefused:
        """``user_requested``: the owner said so, so the Pi-side idle/grace timers don't apply (hostd still decides)."""
        now = self._clock.monotonic()
        idle_s = int(now - self._last_llm) if self._last_llm is not None else 10**6
        audit_reason = "queue_drained" if reason is SleepReason.QUEUE_DRAINED else "idle"
        await self._record("sleep_requested", reason=audit_reason)
        refusals: list[str] = []
        if self._queue_busy():
            refusals.append("queue_busy")
        idle_not_met = (reason is SleepReason.IDLE and idle_s < self._cfg.idle_sleep_s) or (
            reason is SleepReason.SPECULATIVE_UNUSED
            and (self._llm_since_claim or idle_s < self._cfg.speculative_grace_s)
        )
        if idle_not_met and not user_requested:
            refusals.append("atlas_active")
        if refusals:
            await self._record("sleep_refused", reason=audit_reason, code=refusals[0].upper())
            return SleepRefused(tuple(refusals))
        try:
            answer = await self._hostd.sleep(
                request_id=f"s{secrets.token_hex(6)}",
                reason=reason.value,
                atlas_idle_s=min(idle_s, 10**6),
                wake=self._claim,
            )
        except HostdError:
            await self._record("sleep_refused", reason=audit_reason, code="HOSTD_UNREACHABLE")
            return SleepRefused(("hostd_unreachable",))
        if isinstance(answer, SleepNo):
            await self._record("sleep_refused", reason=audit_reason, code=answer.reasons[0].upper())
            return SleepRefused(answer.reasons)
        self._atlas_slept = True
        self._claim = None
        self._probe_at = -1e18
        await self._record("sleep_accepted", reason=audit_reason)
        return SleepAccepted(answer.sleep_state)

    # --- used by the wake flow and the idle loop (not part of the contract) --------------------

    def tick(self) -> None:
        """Send the magic packets whose offset has passed. Called by ``wake`` and by every flow poll."""
        attempt = self._attempt
        if attempt is None or self._sender is None:
            return
        elapsed = self._clock.monotonic() - attempt.started_at_monotonic
        offsets = self._cfg.packet_offsets_s
        while self._next_packet < len(offsets) and elapsed >= offsets[self._next_packet]:
            self._next_packet += 1
            with contextlib.suppress(OSError):  # the flow times out and reports; there is nothing to log
                self._sender.send_magic()

    @property
    def current_attempt(self) -> WakeAttempt | None:
        return self._attempt

    def note_llm_request(self) -> None:
        self._last_llm = self._clock.monotonic()
        self._llm_since_claim = True

    async def complete_wake(self, attempt: WakeAttempt, *, ok: bool, stage: str, code: str | None = None) -> None:
        elapsed_ms = int((self._clock.monotonic() - attempt.started_at_monotonic) * 1000)
        if self._attempt is attempt:
            self._attempt = None
        if ok:
            self._failures = 0
            self._claim = {
                "wake_id": attempt.wake_id,
                "first_packet_at": _rfc3339(self._attempt_wall or self._clock.now()),
            }
            self._claim_speculative = attempt.reason is WakeReason.SPECULATIVE
            self._llm_since_claim = attempt.reason is not WakeReason.SPECULATIVE
            self._last_llm = self._clock.monotonic()
            self._atlas_slept = False
            await self._record("wake_ready", wake_id=attempt.wake_id, stage=stage, elapsed_ms=elapsed_ms)
        else:
            self._failures += 1
            index = min(self._failures, len(self._cfg.retry_backoff_s)) - 1
            self._retry_not_before = self._clock.monotonic() + self._cfg.retry_backoff_s[index]
            await self._record("wake_failed", wake_id=attempt.wake_id, stage=stage, elapsed_ms=elapsed_ms, code=code)

    async def maybe_sleep(self) -> SleepAccepted | SleepRefused | None:
        """The idle-sleep policy. Call periodically; returns ``None`` when nothing is due."""
        now = self._clock.monotonic()
        if now < self._sleep_not_before or self._claim is None:
            return None  # not woken by ATLAS (or already asleep): never put a host to sleep that we didn't wake
        if await self.state() is not PowerState.UP:
            return None
        reason = (
            SleepReason.SPECULATIVE_UNUSED
            if self._claim_speculative and not self._llm_since_claim
            else SleepReason.IDLE
        )
        idle = now - self._last_llm if self._last_llm is not None else 1e9
        due = self._cfg.speculative_grace_s if reason is SleepReason.SPECULATIVE_UNUSED else self._cfg.idle_sleep_s
        if idle < due or self._queue_busy():
            return None
        answer = await self.request_sleep(reason)
        if isinstance(answer, SleepRefused):
            self._sleep_not_before = self._clock.monotonic() + self._cfg.sleep_retry_s
        return answer

    # --- internals ----------------------------------------------------------------------------

    async def _refuse(self, reason: WakeReason, why: WakeRefusal, retry_after: int | None) -> WakeRefused:
        await self._record("wake_refused", reason=reason.value, code=why.name)
        return WakeRefused(why, retry_after)

    async def _record(self, event: str, **fields: Any) -> None:
        body: dict[str, Any] = {"event": event, **{k: v for k, v in fields.items() if v is not None}}
        await self._audit.append({"kind": "power", "context": "system", "power": body})


__all__: Final = ["Clock", "HostPowerController", "PowerConfig", "SystemClock"]
