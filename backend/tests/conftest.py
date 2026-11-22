"""Shared fixtures for the atlas-audit / atlas-egress tests. Keys are generated at runtime, never stored."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.audit.log import AuditConfig, AuditLog

T0 = 1_763_279_000_000  # 2025-11-16 08:23:20 UTC, same era as the golden chain
Body = dict[str, Any]


class Clock:
    """Controllable wall and monotonic clocks."""

    def __init__(self) -> None:
        self.ms = T0
        self.mono = 1000.0

    def advance(self, seconds: float) -> None:
        self.ms += int(seconds * 1000)
        self.mono += seconds

    def wall(self) -> int:
        return self.ms

    def monotonic(self) -> float:
        return self.mono


def tool_call(i: int = 0) -> Body:
    return {
        "kind": "tool_call",
        "context": "interactive",
        "correlation_id": f"req-{i:04d}",
        "tool": "search_memory",
        "args_redacted": {"limit": i},
        "data_accessed": [],
        "result": {"status": "ok"},
    }


def service_event(event: str = "service_start") -> Body:
    """A record every service may write (audit-record.v1 1.2.0 pins most other kinds to one writer)."""
    return {"kind": "system", "context": "system", "system": {"event": event}}


def write_ahead(approval: str = "apr-1", tool: str = "gmail.send") -> Body:
    return {
        "kind": "write_ahead",
        "context": "interactive",
        "tool": tool,
        "action": {
            "action_id": "act-1",
            "action_kind": "email.send",
            "payload_sha256": "1" * 64,
            "approval_id": approval,
        },
    }


def write_result(approval: str = "apr-1", tool: str = "gmail.send") -> Body:
    return {**write_ahead(approval, tool), "kind": "write_result", "result": {"status": "ok"}}


def egress_body(decision: str = "allowed") -> Body:
    return {
        "kind": "egress",
        "context": "system",
        "egress": {
            "caller": "atlas-google",
            "mode": "connect",
            "host": "gmail.googleapis.com",
            "port": 443,
            "decision": decision,
        },
        "result": {"status": "ok"},
    }


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def make_log(tmp_path: Path, key: Ed25519PrivateKey, clock: Clock) -> Callable[..., AuditLog]:
    def factory(**overrides: Any) -> AuditLog:
        cfg = AuditConfig(
            data_dir=tmp_path / "audit", **{k: v for k, v in overrides.items() if k in AuditConfig.__dataclass_fields__}
        )
        extra = {k: v for k, v in overrides.items() if k not in AuditConfig.__dataclass_fields__}
        return AuditLog.open(cfg, key, wall_clock_ms=clock.wall, monotonic=clock.monotonic, **extra)

    return factory
