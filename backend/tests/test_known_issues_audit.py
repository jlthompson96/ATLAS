"""Regression tests for the open audit issues in docs/known-issues.md (N1, N4, G1). Each failed before its fix.

The reviewer's reproduction scripts were lost; these rebuild the scenarios they described.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.audit.log import AuditConfig, AuditLog
from atlas.audit.segments import list_segments
from atlas.audit.verify import verify_dir
from tests.conftest import Clock, tool_call

MakeLog = Callable[..., AuditLog]


def crash(log: AuditLog) -> None:
    """Simulate SIGKILL or a power cut: nothing is flushed or signed, the descriptor just goes away."""
    fd = log._fd
    if fd is not None:
        os.close(fd)
        log._fd = None


def restart(log: AuditLog, key: Ed25519PrivateKey, clock: Clock, **cfg: int) -> AuditLog:
    """A new process: same files and wall clock, but a fresh monotonic clock (it does not survive a restart)."""
    fresh = Clock()
    fresh.ms = clock.ms
    fresh.mono = 5.0
    return AuditLog.open(
        AuditConfig(data_dir=log.data_dir, **cfg), key, wall_clock_ms=clock.wall, monotonic=fresh.monotonic
    )


def kinds(log: AuditLog) -> list[str]:
    return [json.loads(x)["kind"] for _, p in list_segments(log.data_dir) for x in p.read_text().splitlines()]


# --- N1: an unclean stop must not make an honest log fail verification ---------------------------------------------


def test_n1_honest_log_verifies_after_an_unclean_stop(make_log: MakeLog, key: Ed25519PrivateKey, clock: Clock) -> None:
    log = make_log()
    for i in range(5):
        log.append("atlas-core", tool_call(i))
        clock.advance(10 * 60)  # 50 minutes of honest records, under the hourly checkpoint timer
    crash(log)
    resumed = restart(log, key, clock)  # the monotonic checkpoint timer starts again from zero
    for i in range(5):
        resumed.append("atlas-core", tool_call(i))
        clock.advance(5 * 60)  # 25 more minutes: 75 minutes of records since the last signature on the old code
    crash(resumed)
    report = verify_dir(log.data_dir)
    assert report.ok, report.errors


def test_n1_open_signs_what_the_crashed_run_left_unsigned(
    make_log: MakeLog, key: Ed25519PrivateKey, clock: Clock
) -> None:
    log = make_log()
    log.append("atlas-core", tool_call())
    log.append("atlas-core", tool_call(1))
    crash(log)
    before = kinds(log)
    resumed = restart(log, key, clock)
    after = kinds(resumed)
    assert after[: len(before)] == before
    assert after[len(before) :] == ["checkpoint"]  # signed before anything new is appended
    assert verify_dir(log.data_dir).records_after_last_checkpoint == 0
    # a clean head (the last record is a checkpoint) gets no second, empty checkpoint
    crash(resumed)
    again = restart(resumed, key, clock)
    assert kinds(again) == after
    again.close()


def test_n1_a_second_crash_on_a_purged_chain_does_not_stop_the_service(
    make_log: MakeLog, key: Ed25519PrivateKey, clock: Clock
) -> None:
    log = make_log()
    for day in range(2):
        log.append("atlas-core", tool_call(day))
        clock.advance(24 * 3600)
    log.append("atlas-core", tool_call(9))
    log.purge_segments([list_segments(log.data_dir)[0][1].stem], "retention")
    # first crash, 50 minutes after the purge's checkpoint
    for i in range(5):
        log.append("atlas-core", tool_call(i))
        clock.advance(10 * 60)
    crash(log)
    second = restart(log, key, clock)  # a purged chain is fully verified at every start
    for i in range(3):
        second.append("atlas-core", tool_call(i))
        clock.advance(10 * 60)
    crash(second)  # a second crash inside the same hour
    third = restart(second, key, clock)  # on the old code: AuditCorrupt, and every egress fails closed
    third.append("atlas-core", tool_call())
    third.close()
    report = verify_dir(log.data_dir)
    assert report.ok, report.errors


def test_n1_verifier_no_longer_judges_the_tail_by_wall_clock(make_log: MakeLog, clock: Clock) -> None:
    """``ts`` is chosen by whoever wrote the record, so a time rule only ever constrained honest writers."""
    log = make_log(checkpoint_interval_s=10**6)
    log.append("atlas-core", tool_call())
    clock.advance(6 * 3600)  # e.g. an NTP step forward
    log.append("atlas-core", tool_call())
    crash(log)
    report = verify_dir(log.data_dir)
    assert report.ok, report.errors
    assert report.records_after_last_checkpoint == 2
