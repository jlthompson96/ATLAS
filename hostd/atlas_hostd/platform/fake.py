"""Controllable platform for tests and for running hostd on a non-Windows dev machine."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from atlas_hostd.models import (
    GpuSample,
    LlmServerKind,
    OsInfo,
    ResumeInfo,
    SleepState,
    WakeSource,
)

EPOCH = datetime(2026, 11, 16, 12, 0, 0, tzinfo=UTC)


def make_gpu_sample(**overrides: object) -> GpuSample:
    base: dict[str, object] = {
        "name": "Example GPU 24GB",
        "driver_version": "000.00",
        "vram_total_mb": 24576,
        "vram_used_mb": 10240,
        "vram_free_mb": 14336,
        "util_pct": 0,
        "temp_c": 40,
        "throttling": (),
        "llm_server_vram_mb": 9000,
        "foreign_vram_mb": 1240,
        "foreign_process_count": 3,
    }
    base.update(overrides)
    return GpuSample(**base)  # type: ignore[arg-type]


@dataclass
class FakePlatform:
    """Everything is a plain attribute the test can set. ``advance`` moves both clocks together."""

    wall: datetime = EPOCH
    mono: float = 1000.0
    boot_at: datetime = field(default_factory=lambda: EPOCH - timedelta(hours=3))
    boot_identifier: str = "boot-fake-0001"
    os: OsInfo = field(default_factory=lambda: OsInfo(name="Windows 11 Pro", build="26100"))
    states: tuple[SleepState, ...] = (SleepState.S3, SleepState.HIBERNATE)
    resume: ResumeInfo | None = None
    gpu: GpuSample | None = field(default_factory=make_gpu_sample)
    llm_kind: LlmServerKind = LlmServerKind.LMSTUDIO
    listening_ports: set[int] = field(default_factory=lambda: {1234})
    signed_in: bool = True
    # recorded effects
    keep_awake_calls: list[bool] = field(default_factory=list)
    suspend_calls: list[SleepState] = field(default_factory=list)
    closed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # --- test controls -------------------------------------------------------------------------------------
    def advance(self, seconds: float) -> None:
        with self._lock:
            self.wall += timedelta(seconds=seconds)
            self.mono += seconds

    def set_resume(self, at: datetime, source: WakeSource = WakeSource.NETWORK) -> None:
        self.resume = ResumeInfo(at=at, source=source)

    # --- Platform ------------------------------------------------------------------------------------------
    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def boot_time(self) -> datetime:
        return self.boot_at

    def boot_id(self) -> str:
        return self.boot_identifier

    def os_info(self) -> OsInfo:
        return self.os

    def sleep_states(self) -> tuple[SleepState, ...]:
        return self.states

    def last_resume(self) -> ResumeInfo | None:
        return self.resume

    def gpu_sample(self, llm_process_names: frozenset[str]) -> GpuSample | None:
        return self.gpu

    def llm_server_kind(self, processes: Mapping[LlmServerKind, tuple[str, ...]]) -> LlmServerKind:
        return self.llm_kind

    def port_listening(self, host: str, port: int) -> bool:
        return port in self.listening_ports

    def user_signed_in(self) -> bool:
        return self.signed_in

    def set_keep_awake(self, active: bool) -> None:
        self.keep_awake_calls.append(active)

    def suspend(self, state: SleepState) -> None:
        self.suspend_calls.append(state)

    def close(self) -> None:
        self.closed = True

    @property
    def keep_awake(self) -> bool:
        return bool(self.keep_awake_calls and self.keep_awake_calls[-1])


@dataclass
class FakeHelperProbe:
    session: int = 1
    idle_s: int = 0
    fullscreen: bool = False
    heavy_count: int = 0

    def session_id(self) -> int:
        return self.session

    def input_idle_s(self) -> int:
        return self.idle_s

    def fullscreen_app(self) -> bool:
        return self.fullscreen

    def gpu_heavy_process_count(self, exclude_process_names: frozenset[str], threshold_pct: int) -> int:
        return self.heavy_count
