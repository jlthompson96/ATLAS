"""Value types and enums shared by the decision logic, the platform layer and the HTTP layer.

The enum values are the wire values of ``contracts/hostd.v1.yaml``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class SleepState(StrEnum):
    S3 = "s3"
    MODERN_STANDBY = "modern_standby"
    HIBERNATE = "hibernate"


#: Preference order (PLAN.md §5.4): S3, else Modern Standby, never shutdown; hibernate is the slow fallback.
SLEEP_PREFERENCE: Final = (SleepState.S3, SleepState.MODERN_STANDBY, SleepState.HIBERNATE)


class WakeSource(StrEnum):
    NETWORK = "network"
    POWER_BUTTON = "power_button"
    INPUT_DEVICE = "input_device"
    TIMER = "timer"
    UNKNOWN = "unknown"


class RefusalReason(StrEnum):
    # Declaration order is the order reasons are reported in.
    USER_INPUT_RECENT = "user_input_recent"
    FULLSCREEN_APP = "fullscreen_app"
    FOREIGN_GPU_BUSY = "foreign_gpu_busy"
    LEASE_ACTIVE = "lease_active"
    NOT_ATLAS_WOKEN = "not_atlas_woken"
    SLEEP_UNSUPPORTED = "sleep_unsupported"
    RESUME_GRACE = "resume_grace"


class LeaseReason(StrEnum):
    INTERACTIVE_SESSION = "interactive_session"
    RESEARCH_RUN = "research_run"
    MODEL_WARMUP = "model_warmup"
    KEEP_AWAKE_SCHEDULE = "keep_awake_schedule"
    BACKUP = "backup"


class SleepRequestReason(StrEnum):
    IDLE = "idle"
    QUEUE_DRAINED = "queue_drained"
    SPECULATIVE_UNUSED = "speculative_unused"


class LlmServerKind(StrEnum):
    LMSTUDIO = "lmstudio"
    LLMSTER = "llmster"
    LLAMA_SERVER = "llama_server"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ResumeInfo:
    """The most recent resume from sleep or hibernate since boot."""

    at: datetime
    source: WakeSource


@dataclass(frozen=True, slots=True)
class OsInfo:
    name: str
    build: str


@dataclass(frozen=True, slots=True)
class GpuSample:
    """One NVML reading. ``sampled_at`` is added by the service from the platform clock."""

    name: str
    driver_version: str
    vram_total_mb: int
    vram_used_mb: int
    vram_free_mb: int
    util_pct: int
    temp_c: int
    throttling: tuple[str, ...]  # subset of "thermal", "power", "other"
    llm_server_vram_mb: int
    foreign_vram_mb: int
    foreign_process_count: int


@dataclass(frozen=True, slots=True)
class WakeClaim:
    wake_id: str
    first_packet_at: datetime


@dataclass(frozen=True, slots=True)
class SleepRequest:
    request_id: str
    reason: SleepRequestReason
    atlas_idle_s: int
    wake: WakeClaim | None


@dataclass(frozen=True, slots=True)
class LeaseRequest:
    ttl_s: int
    reason: LeaseReason


@dataclass(frozen=True, slots=True)
class HelperReport:
    session_id: int
    input_idle_s: int
    fullscreen_app: bool
    gpu_heavy_process_count: int
    reported_at: datetime


def fmt_ts(value: datetime) -> str:
    """RFC 3339 UTC with second precision, e.g. ``2026-11-16T12:00:00Z``."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
