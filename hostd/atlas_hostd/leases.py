"""Keep-awake leases. Time is the platform's monotonic clock; leases never survive a restart."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from atlas_hostd.models import LeaseReason

TTL_MIN_S = 30
TTL_MAX_S = 1800


class LeaseLimitError(Exception):
    """Creating another lease would exceed the configured maximum."""


@dataclass(frozen=True, slots=True)
class LeaseView:
    lease_id: str
    reason: LeaseReason
    expires_at: datetime


@dataclass(slots=True)
class _Entry:
    reason: LeaseReason
    expires_mono: float
    expires_at: datetime


class LeaseTable:
    def __init__(self, max_leases: int) -> None:
        self._max = max_leases
        self._entries: dict[str, _Entry] = {}

    def put(self, lease_id: str, reason: LeaseReason, ttl_s: int, *, now_mono: float, now_wall: datetime) -> LeaseView:
        """Create or renew. A renewal replaces the TTL (it does not add to it) and may change the reason."""
        if not TTL_MIN_S <= ttl_s <= TTL_MAX_S:
            raise ValueError("ttl_s out of range")
        self.expire(now_mono)
        if lease_id not in self._entries and len(self._entries) >= self._max:
            raise LeaseLimitError
        entry = _Entry(
            reason=reason,
            expires_mono=now_mono + ttl_s,
            expires_at=now_wall + timedelta(seconds=ttl_s),
        )
        self._entries[lease_id] = entry
        return LeaseView(lease_id, reason, entry.expires_at)

    def delete(self, lease_id: str) -> bool:
        return self._entries.pop(lease_id, None) is not None

    def expire(self, now_mono: float) -> int:
        """Drop expired leases; returns how many were dropped."""
        dead = [key for key, entry in self._entries.items() if entry.expires_mono <= now_mono]
        for key in dead:
            del self._entries[key]
        return len(dead)

    def active(self, now_mono: float) -> list[LeaseView]:
        self.expire(now_mono)
        return [LeaseView(key, e.reason, e.expires_at) for key, e in sorted(self._entries.items())]

    def clear(self) -> None:
        self._entries.clear()
