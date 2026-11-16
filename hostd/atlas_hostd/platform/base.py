"""The platform seam. All decision logic lives outside this layer and is tested against ``FakePlatform``.

Methods are synchronous and may block briefly (NVML, ``powercfg``, event-log queries). The service calls them
through ``asyncio.to_thread``.
"""

from __future__ import annotations

import socket
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from atlas_hostd.models import GpuSample, LlmServerKind, OsInfo, ResumeInfo, SleepState


class Platform(Protocol):
    """Everything hostd needs from the operating system."""

    def now(self) -> datetime:
        """Wall clock, timezone-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards (lease expiry, helper freshness)."""
        ...

    def boot_time(self) -> datetime:
        """When the OS booted (not when it last resumed)."""
        ...

    def boot_id(self) -> str:
        """Matches ``Id``; changes on every OS boot, not on resume."""
        ...

    def os_info(self) -> OsInfo: ...

    def sleep_states(self) -> tuple[SleepState, ...]:
        """Sleep states the hardware offers (``powercfg /a``)."""
        ...

    def last_resume(self) -> ResumeInfo | None:
        """Last resume since boot and the generalised source, or ``None`` if the machine never slept."""
        ...

    def gpu_sample(self, llm_process_names: frozenset[str]) -> GpuSample | None:
        """NVML reading; ``None`` if NVML is unavailable."""
        ...

    def llm_server_kind(self, processes: Mapping[LlmServerKind, tuple[str, ...]]) -> LlmServerKind:
        """Which LLM server process is running (``NONE`` if none)."""
        ...

    def port_listening(self, host: str, port: int) -> bool: ...

    def user_signed_in(self) -> bool:
        """An interactive session exists (active or disconnected). A locked screen still counts."""
        ...

    def set_keep_awake(self, active: bool) -> None:
        """Hold (or release) the system-required power request."""
        ...

    def suspend(self, state: SleepState) -> None:
        """Enter sleep. Never shutdown. May block until the machine resumes."""
        ...

    def close(self) -> None: ...


class HelperProbe(Protocol):
    """What the per-user helper can see from inside the signed-in session."""

    def session_id(self) -> int: ...

    def input_idle_s(self) -> int: ...

    def fullscreen_app(self) -> bool: ...

    def gpu_heavy_process_count(self, exclude_process_names: frozenset[str], threshold_pct: int) -> int: ...


def tcp_port_listening(host: str, port: int, timeout_s: float = 0.3) -> bool:
    """Plain TCP connect test, shared by the real platform implementations."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False
