"""Fakes for the power and wake-flow tests: a virtual clock and a scripted "desktop" timeline.

``World`` says when each layer of the desktop comes up (hostd, the LM Studio API, the model) and
when it might die, all in virtual seconds from the wake. ``FakeHostd`` and ``FakeBackend`` answer
from it. ``FakeClock.sleep`` just advances virtual time, so a 60 s wake runs in microseconds and the
timing assertions (CT-3 "exactly 60 s") are exact.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from atlas.core.fake_llm import ScriptedBackend
from atlas.llm.interface import Health, ModelInfo, NotReady, ReadinessStage, Ready
from atlas.power.hostd import (
    GpuReport,
    HostdError,
    HostdForbidden,
    HostdHealth,
    HostdStatus,
    HostdUnreachable,
    LlmServerState,
    SleepNo,
    SleepOk,
)
from atlas.ui_server.protocol import Contract, Sequencer, StreamChecker

MODEL = "qwen-test-9b"
T_START = datetime(2026, 11, 17, 10, 0, 0, tzinfo=UTC)  # a Tuesday, mid-morning


class FakeClock:
    def __init__(self, start: datetime = T_START) -> None:
        self.t = 1_000.0
        self._start = start
        self._wall0 = self.t

    def monotonic(self) -> float:
        return self.t

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self.t - self._wall0)

    async def sleep(self, seconds: float) -> None:
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds

    def set_hour(self, hour: int) -> None:
        self._start = self._start.replace(hour=hour, minute=0, second=0)
        self._wall0 = self.t


@dataclass
class World:
    """Seconds (on the fake clock, absolute) at which each layer is up. ``None`` = never."""

    clock: FakeClock
    hostd_up_at: float | None = None
    api_up_at: float | None = None
    model_up_at: float | None = None
    dies_at: float | None = None  # power cut: everything goes away
    auth_ok: bool = True
    signed_in: bool = True
    vram_free_mb: int = 7000
    model_loaded_at_start: bool = False

    def _alive(self) -> bool:
        return self.dies_at is None or self.clock.t < self.dies_at

    def up(self, at: float | None) -> bool:
        return at is not None and self.clock.t >= at and self._alive()

    def at(self, offset: float) -> float:
        return self.clock.t + offset


class FakeHostd:
    def __init__(self, world: World) -> None:
        self.w = world
        self.sleep_answer: SleepOk | SleepNo = SleepOk("s3")
        self.calls: list[str] = []
        self.sleep_requests: list[dict[str, Any]] = []
        self.forbidden = False
        self.gpu_missing = False

    async def health(self) -> Any:
        self.calls.append("health")
        if self.forbidden:
            raise HostdForbidden("no")
        if not self.w.up(self.w.hostd_up_at):
            raise HostdUnreachable("down")
        return HostdHealth("1.1.0", "boot-1", 5)

    async def status(self) -> HostdStatus:
        self.calls.append("status")
        if not self.w.up(self.w.hostd_up_at):
            raise HostdError("down")
        running = self.w.up(self.w.api_up_at)
        return HostdStatus(
            "boot-1", self.w.signed_in, LlmServerState("lmstudio", running, running), True, (), "network"
        )

    async def gpu(self) -> GpuReport | None:
        self.calls.append("gpu")
        if self.gpu_missing:
            return None
        return GpuReport(8000, self.w.vram_free_mb, 0, 800)

    async def sleep(
        self, *, request_id: str, reason: str, atlas_idle_s: int, wake: Mapping[str, str] | None
    ) -> SleepOk | SleepNo:
        self.calls.append("sleep")
        if not self.w.up(self.w.hostd_up_at):
            raise HostdUnreachable("down")
        self.sleep_requests.append({"request_id": request_id, "reason": reason, "idle": atlas_idle_s, "wake": wake})
        return self.sleep_answer


class FakeBackend(ScriptedBackend):
    """Scripted chat, but health/readiness follow the world's timeline."""

    def __init__(self, world: World) -> None:
        super().__init__()
        self.w = world
        self.health_calls = 0
        self.ready_calls = 0

    async def health(self, deadline: float) -> Health:
        self.health_calls += 1
        if not self.w.up(self.w.api_up_at):
            return Health(False, False, None)
        if not self.w.auth_ok:
            return Health(True, False, None)
        loaded = MODEL if (self.w.up(self.w.model_up_at) or self.w.model_loaded_at_start) else None
        return Health(True, True, loaded)

    async def ensure_ready(self, model: str, deadline: float) -> Ready | NotReady:
        self.ready_calls += 1
        if not self.w.up(self.w.api_up_at):
            return NotReady(ReadinessStage.PACKET, "unreachable")
        if not self.w.auth_ok:
            return NotReady(ReadinessStage.API, "auth_failed")
        if self.w.up(self.w.model_up_at):
            return Ready(model, 800)
        return NotReady(ReadinessStage.API, "loading")

    async def list_models(self, deadline: float) -> Sequence[ModelInfo]:
        return [ModelInfo(MODEL, True, 16384)]


@dataclass
class FakeSender:
    clock: FakeClock
    sent_at: list[float] = field(default_factory=list)

    def send_magic(self) -> None:
        self.sent_at.append(self.clock.t)


class ContractEvents:
    """An EventSink that validates every payload against events.v1 and the orb state machine."""

    def __init__(self, state: str = "idle") -> None:
        contract = Contract.load()
        self._contract = contract
        self._seq = Sequencer(clock=lambda: 1)
        self._checker = StreamChecker(contract, state=state)
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def emit(self, type_: str, payload: Mapping[str, Any]) -> None:
        frame = self._seq.envelope(type_, payload)
        self._contract.validate("server", frame)
        self._checker.check(frame)
        self.events.append((type_, dict(payload)))

    @property
    def state(self) -> str | None:
        return self._checker.state

    def states(self) -> list[str]:
        return [p["state"] for t, p in self.events if t == "state.changed"]

    def alerts(self) -> list[str]:
        return [p["code"] for t, p in self.events if t == "alert"]

    def last_state(self) -> dict[str, Any]:
        return [p for t, p in self.events if t == "state.changed"][-1]
