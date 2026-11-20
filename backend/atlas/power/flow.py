"""The wake flow (PLAN.md §5.4): get the LLM host to a state where the model answers, or fail cleanly.

``HostGate.ensure_ready`` is what the agent loop calls before a request that needs the LLM:

1. Fast path: the server answers an authenticated ``GET /v1/models`` and the target model is loaded.
2. Otherwise ask the ``PowerController`` to wake the host (policy checked, audited, packets at
   0/2/5/10/20 s) and poll once a second until the hard deadline (60 s interactive, 120 s unattended):
   ``packet`` (nothing answers) -> ``link`` (hostd answers) -> ``api`` (authenticated /v1/models) ->
   ``model`` (a 1-token completion succeeded, the only proof of readiness).
3. On the way it checks free VRAM through hostd before the model is loaded (D16): too little means
   ``HOST_BUSY`` immediately, not a wait.
4. On failure the request is queued (FIFO, 24 h TTL) and the UI told why. A later success, even long
   after the deadline, delivers each queued request once (CT-4).

State events follow the events.v1 state machine: ``waking_host`` (stage and elapsed), then
``thinking`` on success, or ``error`` and ``llm_unreachable`` on failure. Timing uses the injected
clock; deadlines handed to the backend are ``clock.monotonic()`` values, which equal
``time.monotonic()`` for the real ``SystemClock``.
"""

from __future__ import annotations

import secrets
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from atlas.core.pending import EventSink
from atlas.llm.interface import LLMBackend, NotReady, ReadinessStage, Ready
from atlas.power.controller import Clock, HostPowerController
from atlas.power.hostd import HostdApi, HostdError, HostdForbidden, HostdUnreachable
from atlas.power.interface import WakeAttempt, WakeReason, WakeRefusal, WakeRefused

POLL_S: Final = 1.0
PROGRESS_EVERY_S: Final = 5.0
FAST_PATH_TTL_S: Final = 60.0
QUEUE_TTL_S: Final = 24 * 3600.0
QUEUE_MAX: Final = 20
_STAGES: Final = (ReadinessStage.PACKET, ReadinessStage.LINK, ReadinessStage.API, ReadinessStage.MODEL)

# Spoken/typed lines (PLAN.md §5.4, §5.6). Exact wording is part of the product, so it lives in one place.
LINE_WAKING: Final = "One moment, waking my core."
LINE_STILL_WAKING: Final = "Still waking."
LINE_WAKE_FAILED: Final = "My core didn't wake. I've saved your request and will retry; you can also type it."
LINE_SIGNED_OUT: Final = "My core needs you to sign in on the desktop."
LINE_BUSY: Final = "The core is busy. I've queued that."
LINE_AUTH: Final = "My core rejected my credentials. I've saved your request."


class GateCode(StrEnum):
    WAKE_FAILED = "WAKE_FAILED"
    SIGNED_OUT = "SIGNED_OUT"
    HOST_BUSY = "HOST_BUSY"
    LLM_AUTH_FAILED = "LLM_AUTH_FAILED"
    LINK_DOWN = "LINK_DOWN"
    # wake refused by policy (PowerController): same names as WakeRefusal
    RATE_LIMITED = "RATE_LIMITED"
    BACKOFF = "BACKOFF"
    OUTSIDE_ACTIVE_HOURS = "OUTSIDE_ACTIVE_HOURS"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"


LINES: Final = {
    GateCode.WAKE_FAILED: LINE_WAKE_FAILED,
    GateCode.SIGNED_OUT: LINE_SIGNED_OUT,
    GateCode.HOST_BUSY: LINE_BUSY,
    GateCode.LLM_AUTH_FAILED: LINE_AUTH,
    GateCode.LINK_DOWN: LINE_WAKE_FAILED,
}


@dataclass(frozen=True, slots=True)
class HostReady:
    elapsed_ms: int
    warmup_ms: int
    woke: bool  # False on the fast path


@dataclass(frozen=True, slots=True)
class HostUnavailable:
    code: GateCode
    stage: ReadinessStage
    elapsed_ms: int
    queued: bool

    @property
    def line(self) -> str:
        return LINES.get(self.code, LINE_WAKE_FAILED)


@dataclass(frozen=True, slots=True)
class QueuedRequest:
    request_id: str
    text: str  # private content: in memory only, never logged or audited
    queued_at: float


class RequestQueue:
    """Requests that couldn't be served because the core was unreachable. FIFO, 24 h TTL, delivered once."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._items: deque[QueuedRequest] = deque()

    def put(self, text: str) -> str:
        if len(self._items) >= QUEUE_MAX:
            self._items.popleft()
        item = QueuedRequest(f"q{secrets.token_hex(4)}", text, self._clock.monotonic())
        self._items.append(item)
        return item.request_id

    def _expire(self) -> None:
        now = self._clock.monotonic()
        while self._items and now - self._items[0].queued_at > QUEUE_TTL_S:
            self._items.popleft()

    def drain(self) -> list[QueuedRequest]:
        """Remove and return everything still due. A second call returns nothing: each request is delivered once."""
        self._expire()
        out = list(self._items)
        self._items.clear()
        return out

    def __len__(self) -> int:
        self._expire()
        return len(self._items)

    def ids(self) -> list[str]:
        self._expire()
        return [i.request_id for i in self._items]


async def emit_state(
    events: EventSink,
    state: str,
    *,
    code: str | None = None,
    detail: str | None = None,
    wake: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"state": state}
    if code is not None:
        payload["code"] = code
    if detail is not None:
        payload["detail"] = detail
    if wake is not None:
        payload["wake"] = wake
    await events.emit("state.changed", payload)


async def emit_alert(events: EventSink, level: str, code: str, message: str) -> None:
    await events.emit(
        "alert", {"alert_id": f"a{secrets.token_hex(4)}", "level": level, "code": code, "message": message}
    )


class HostGate:
    def __init__(
        self,
        controller: HostPowerController,
        backend: LLMBackend,
        hostd: HostdApi,
        *,
        model: str,
        events: EventSink,
        clock: Clock,
        queue: RequestQueue | None = None,
        model_vram_mb: int | None = None,
        poll_s: float = POLL_S,
    ) -> None:
        self._controller = controller
        self._backend = backend
        self._hostd = hostd
        self._model = model
        self._events = events
        self._clock = clock
        self.queue = queue or RequestQueue(clock)
        self._vram_need = model_vram_mb
        self._poll = poll_s
        self._proven_at: float | None = None
        self._offline = False

    async def ensure_ready(
        self, reason: WakeReason = WakeReason.INTERACTIVE, *, request_text: str | None = None
    ) -> HostReady | HostUnavailable:
        started = self._clock.monotonic()
        fast = await self._fast_path()
        if isinstance(fast, HostUnavailable):
            return await self._fail(fast.code, fast.stage, started, request_text, wake_attempt=None)
        if fast:
            await self._recovered()
            return HostReady(0, 0, woke=False)

        woken = await self._controller.wake(reason)
        attempt: WakeAttempt | None = None  # None: host is up but the model isn't ready yet; poll without packets
        if isinstance(woken, WakeRefused):
            if woken.reason is not WakeRefusal.ALREADY_UP:
                code = GateCode[woken.reason.name]
                return await self._fail(code, ReadinessStage.PACKET, started, request_text, wake_attempt=None)
        else:
            attempt = woken
        deadline_s = (
            attempt.deadline_monotonic - attempt.started_at_monotonic
            if attempt is not None
            else self._controller_deadline(reason)
        )
        return await self._poll_until_ready(attempt, started, deadline_s, request_text)

    def _controller_deadline(self, reason: WakeReason) -> float:
        return 120.0 if reason is WakeReason.SCHEDULED_RESEARCH else 60.0

    async def _fast_path(self) -> bool | HostUnavailable:
        health = await self._backend.health(self._clock.monotonic() + 3.0)
        if not health.reachable:
            return False
        if not health.authenticated:
            return HostUnavailable(GateCode.LLM_AUTH_FAILED, ReadinessStage.API, 0, False)
        if health.model_loaded == self._model:
            return True
        now = self._clock.monotonic()
        return health.model_loaded is None and self._proven_at is not None and now - self._proven_at < FAST_PATH_TTL_S

    async def _poll_until_ready(
        self, attempt: WakeAttempt | None, started: float, deadline_s: float, request_text: str | None
    ) -> HostReady | HostUnavailable:
        deadline = started + deadline_s
        stage = ReadinessStage.PACKET
        last_emit = -1e18
        last_stage: ReadinessStage | None = None
        vram_checked = False
        while True:
            now = self._clock.monotonic()
            elapsed = now - started
            if now >= deadline:
                break
            self._controller.tick()
            try:
                await self._hostd.health()
                stage = max(stage, ReadinessStage.LINK, key=_STAGES.index)
            except HostdUnreachable:
                pass
            except HostdForbidden:
                return await self._fail(GateCode.LINK_DOWN, stage, started, request_text, wake_attempt=attempt)
            except HostdError:
                stage = max(stage, ReadinessStage.LINK, key=_STAGES.index)

            if stage is not ReadinessStage.PACKET:
                health = await self._backend.health(deadline)
                if health.reachable and not health.authenticated:
                    return await self._fail(
                        GateCode.LLM_AUTH_FAILED, stage, started, request_text, wake_attempt=attempt
                    )
                if health.reachable:
                    stage = max(stage, ReadinessStage.API, key=_STAGES.index)
                    if not vram_checked and health.model_loaded != self._model:
                        vram_checked = True
                        if await self._vram_short():
                            return await self._fail(
                                GateCode.HOST_BUSY, stage, started, request_text, wake_attempt=attempt
                            )
                    result = await self._backend.ensure_ready(self._model, deadline)
                    if isinstance(result, Ready):
                        return await self._succeed(attempt, started, result)
                    if result.reason == "auth_failed":
                        return await self._fail(
                            GateCode.LLM_AUTH_FAILED, stage, started, request_text, wake_attempt=attempt
                        )
                    stage = max(stage, _backend_stage(result), key=_STAGES.index)

            if stage is not last_stage or now - last_emit >= PROGRESS_EVERY_S:
                last_stage, last_emit = stage, now
                await emit_state(
                    self._events,
                    "waking_host",
                    wake={
                        "stage": stage.value,
                        "elapsed_ms": int(elapsed * 1000),
                        "deadline_ms": int(deadline_s * 1000),
                    },
                )
            await self._clock.sleep(self._poll)

        code = GateCode.WAKE_FAILED
        if stage is ReadinessStage.LINK and await self._signed_out():
            code = GateCode.SIGNED_OUT
        return await self._fail(code, stage, started, request_text, wake_attempt=attempt)

    async def _signed_out(self) -> bool:
        try:
            status = await self._hostd.status()
        except HostdError:
            return False
        return not status.user_signed_in and not status.llm_server.running

    async def _vram_short(self) -> bool:
        if self._vram_need is None:
            return False
        try:
            gpu = await self._hostd.gpu()
        except HostdError:
            return False  # can't tell: let the load proceed rather than block on a broken report
        return gpu is not None and gpu.vram_free_mb + gpu.llm_server_vram_mb < self._vram_need

    async def _succeed(self, attempt: WakeAttempt | None, started: float, ready: Ready) -> HostReady:
        elapsed_ms = int((self._clock.monotonic() - started) * 1000)
        self._proven_at = self._clock.monotonic()
        if attempt is not None:
            await self._controller.complete_wake(attempt, ok=True, stage=ReadinessStage.MODEL.value)
        await emit_state(self._events, "thinking")
        await self._recovered()
        return HostReady(elapsed_ms, ready.warmup_ms, woke=True)

    async def _recovered(self) -> None:
        if self._offline:
            self._offline = False
            await emit_alert(self._events, "info", "CORE_RECOVERED", "The core is back.")

    async def _fail(
        self,
        code: GateCode,
        stage: ReadinessStage,
        started: float,
        request_text: str | None,
        *,
        wake_attempt: WakeAttempt | None,
    ) -> HostUnavailable:
        elapsed_ms = int((self._clock.monotonic() - started) * 1000)
        self._offline = True
        self._proven_at = None
        if wake_attempt is not None:
            await self._controller.complete_wake(wake_attempt, ok=False, stage=stage.value, code=code.value)
        queued = request_text is not None
        if request_text is not None:
            self.queue.put(request_text)
        if code is GateCode.HOST_BUSY:
            await emit_alert(self._events, "warn", "HOST_BUSY", "Not enough free GPU memory for the model.")
            await emit_state(self._events, "idle")
        elif code is GateCode.LLM_AUTH_FAILED:
            await emit_alert(self._events, "critical", "LLM_AUTH_FAILED", "The LM Studio token was rejected.")
            await emit_state(self._events, "error", code="LLM_AUTH_FAILED", detail="TOKEN REJECTED")
            await emit_state(self._events, "llm_unreachable", code="CORE_UNREACHABLE")
        elif code in (GateCode.WAKE_FAILED, GateCode.SIGNED_OUT, GateCode.LINK_DOWN):
            if code is GateCode.SIGNED_OUT:
                await emit_alert(
                    self._events, "warn", "CORE_UNREACHABLE", "Sign in on the desktop to start the model server."
                )
            else:
                await emit_alert(
                    self._events, "critical", "WAKE_FAILED", f"The core did not wake (last stage: {stage.value})."
                )
            await emit_state(self._events, "error", code="WAKE_FAILED", detail=f"STAGE {stage.value.upper()}")
            await emit_state(
                self._events,
                "llm_unreachable",
                code={"SIGNED_OUT": "SIGNED_OUT", "LINK_DOWN": "LINK_DOWN"}.get(code.value, "WAKE_FAILED"),
            )
        else:  # a policy refusal: nothing was attempted
            await emit_state(self._events, "idle")
        return HostUnavailable(code, stage, elapsed_ms, queued)


def _backend_stage(result: NotReady) -> ReadinessStage:
    return result.stage
