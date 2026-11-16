"""Tracks reports from the per-user helper.

The helper listener is loopback-only but any local process can reach it, so reports are treated conservatively:
the most active report wins (smallest idle time, any fullscreen flag, summed GPU-heavy count), freshness is
judged by hostd's own receive time (never the helper's ``reported_at``), and the number of tracked sessions is
bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from atlas_hostd.models import HelperReport

STALE_AFTER_S = 45.0  # contract: disconnected after 45 s of silence
MAX_SESSIONS = 16


@dataclass(frozen=True, slots=True)
class ActivitySnapshot:
    helper_connected: bool
    input_idle_s: int | None
    fullscreen_app: bool
    gpu_heavy_process_count: int
    reported_at: datetime | None  # hostd's wall-clock time of the newest fresh report


@dataclass(slots=True)
class _Seen:
    report: HelperReport
    received_mono: float
    received_wall: datetime


class ActivityTracker:
    def __init__(self) -> None:
        self._sessions: dict[int, _Seen] = {}

    def record(self, report: HelperReport, *, now_mono: float, now_wall: datetime) -> None:
        self._drop_stale(now_mono)
        if report.session_id not in self._sessions and len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions, key=lambda k: self._sessions[k].received_mono)
            del self._sessions[oldest]
        self._sessions[report.session_id] = _Seen(report, now_mono, now_wall)

    def snapshot(self, now_mono: float) -> ActivitySnapshot:
        self._drop_stale(now_mono)
        if not self._sessions:
            return ActivitySnapshot(False, None, False, 0, None)
        idle = min(int(s.report.input_idle_s + (now_mono - s.received_mono)) for s in self._sessions.values())
        newest = max(self._sessions.values(), key=lambda s: s.received_mono)
        return ActivitySnapshot(
            helper_connected=True,
            input_idle_s=max(idle, 0),
            fullscreen_app=any(s.report.fullscreen_app for s in self._sessions.values()),
            gpu_heavy_process_count=sum(s.report.gpu_heavy_process_count for s in self._sessions.values()),
            reported_at=newest.received_wall,
        )

    def _drop_stale(self, now_mono: float) -> None:
        for key in [k for k, s in self._sessions.items() if now_mono - s.received_mono > STALE_AFTER_S]:
            del self._sessions[key]
