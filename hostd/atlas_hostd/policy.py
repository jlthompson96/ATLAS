"""Sleep decision logic (PLAN.md §5.4 idle-sleep policy). Pure functions: no I/O, no clock reads.

hostd refuses sleep unless every host-side condition holds. The Pi-side conditions (20 min without an ATLAS LLM
request, task queue empty or blocked) are enforced by ``atlas-core`` before it calls ``POST /v1/sleep``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from atlas_hostd.config import SleepConfig
from atlas_hostd.models import (
    SLEEP_PREFERENCE,
    RefusalReason,
    ResumeInfo,
    SleepState,
    WakeClaim,
    WakeSource,
)

#: The Pi abandons a wake after 60 s; a cycle starting later than this after the first packet was not that wake.
CLAIM_WINDOW_S = 90
_CLAIM_SOURCES = frozenset({WakeSource.NETWORK, WakeSource.UNKNOWN})


def preferred_sleep_state(available: tuple[SleepState, ...]) -> SleepState | None:
    """S3 if offered, else Modern Standby, else hibernate (the slow fallback); ``None`` means shutdown only."""
    for state in SLEEP_PREFERENCE:
        if state in available:
            return state
    return None


@dataclass(frozen=True, slots=True)
class CycleInfo:
    """When the current awake cycle began, and what started it."""

    started_at: datetime
    source: WakeSource


def current_cycle(boot_time: datetime, resume: ResumeInfo | None) -> CycleInfo:
    """The wake cycle in progress: the last resume, or the boot itself if the machine never slept since.

    A WoL from full shutdown is a boot, not a resume, so without this fallback a boot-woken host could never
    be put back to sleep by ATLAS. A boot has no wake source Windows reports, so it counts as ``unknown``.
    """
    if resume is not None and resume.at >= boot_time:
        return CycleInfo(started_at=resume.at, source=resume.source)
    return CycleInfo(started_at=boot_time, source=WakeSource.UNKNOWN)


def claim_is_valid(claim: WakeClaim | None, cycle: CycleInfo, skew_s: int) -> bool:
    """hostd.v1 1.1.0: ``first_packet_at - skew <= cycle start <= first_packet_at + 90 s`` and the wake source is
    ``network`` or ``unknown``. Both bounds are inclusive."""
    if claim is None:
        return False
    if cycle.started_at < claim.first_packet_at - timedelta(seconds=skew_s):
        return False
    if cycle.started_at > claim.first_packet_at + timedelta(seconds=CLAIM_WINDOW_S):
        return False
    return cycle.source in _CLAIM_SOURCES


@dataclass(frozen=True, slots=True)
class PolicyInputs:
    now: datetime
    sleep_states: tuple[SleepState, ...]
    cycle: CycleInfo
    helper_connected: bool
    input_idle_s: int | None  # None when the helper is not connected
    fullscreen_app: bool
    foreign_gpu_busy: bool
    active_leases: int


def host_refusals(inputs: PolicyInputs, cfg: SleepConfig) -> tuple[RefusalReason, ...]:
    """Host-side conditions only (no wake claim). This is ``sleep_eligibility.reasons`` in ``/v1/status``."""
    found: set[RefusalReason] = set()
    if preferred_sleep_state(inputs.sleep_states) is None:
        found.add(RefusalReason.SLEEP_UNSUPPORTED)
    if inputs.now - inputs.cycle.started_at < timedelta(seconds=cfg.resume_grace_s):
        found.add(RefusalReason.RESUME_GRACE)
    # No helper means nobody is signed in or the helper died: user activity counts as none (§5.4).
    if inputs.helper_connected and inputs.input_idle_s is not None and inputs.input_idle_s < cfg.input_idle_s:
        found.add(RefusalReason.USER_INPUT_RECENT)
    if inputs.fullscreen_app:
        found.add(RefusalReason.FULLSCREEN_APP)
    if inputs.foreign_gpu_busy:
        found.add(RefusalReason.FOREIGN_GPU_BUSY)
    if inputs.active_leases > 0:
        found.add(RefusalReason.LEASE_ACTIVE)
    return _ordered(found)


def sleep_refusals(inputs: PolicyInputs, cfg: SleepConfig, claim: WakeClaim | None) -> tuple[RefusalReason, ...]:
    """All conditions for a ``POST /v1/sleep``: host-side plus the wake-origin rule."""
    found = set(host_refusals(inputs, cfg))
    if not cfg.allow_sleep_if_user_woke and not claim_is_valid(claim, inputs.cycle, cfg.claim_clock_skew_s):
        found.add(RefusalReason.NOT_ATLAS_WOKEN)
    return _ordered(found)


def _ordered(found: set[RefusalReason]) -> tuple[RefusalReason, ...]:
    return tuple(reason for reason in RefusalReason if reason in found)
