"""Regression tests for the open audit issues in docs/known-issues.md (N1, N4, G1). Each failed before its fix.

The reviewer's reproduction scripts were lost; these rebuild the scenarios they described.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.audit.__main__ import main as audit_cli
from atlas.audit.jcs import canonicalize
from atlas.audit.keys import load_public_key, public_key_b64
from atlas.audit.log import AuditConfig, AuditLog
from atlas.audit.record import ZERO_HASH, record_hash
from atlas.audit.segments import CHECKPOINT_SIDECAR, list_segments
from atlas.audit.verify import verify_dir
from tests.conftest import T0, Clock, tool_call

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


# --- G1: a fresh forged chain must not pass as a new install --------------------------------------------------------


def forge_fresh_chain(directory: Path, records: int = 3) -> str:
    """Delete everything, sidecar included, and write a new genesis chain without the signing key. Returns its genesis
    hash. This is what someone with write access but without the key can do (the reviewer's G1 reproduction)."""
    for _, path in list_segments(directory):
        path.unlink()
    (directory / CHECKPOINT_SIDECAR).unlink(missing_ok=True)
    chain: list[dict[str, object]] = []
    prev = ZERO_HASH
    for seq in range(records):
        rec: dict[str, object] = {"v": 1, "seq": seq, "ts": T0 + seq, "actor": "atlas-audit", "prev_hash": prev}
        if seq == 0:
            rec.update(kind="genesis", context="system", genesis={"chain_id": "atlas-pi-01", "note": "chain created"})
        else:
            rec.update(kind="system", context="system", system={"event": "service_start"})
        rec["hash"] = prev = record_hash(rec)
        chain.append(rec)
    (directory / "segments" / "seg-0000000000.jsonl").write_bytes(b"".join(canonicalize(r) + b"\n" for r in chain))
    return str(chain[0]["hash"])


def genuine(make_log: MakeLog) -> tuple[AuditLog, Path, str]:
    """An honest chain, the off-box copy of its public key, and its genesis hash as recorded at provisioning."""
    log = make_log()
    for i in range(4):
        log.append("atlas-core", tool_call(i))
    log.close()
    pubfile = log.data_dir.parent / "offbox.pub"
    pubfile.write_text((log.data_dir / "checkpoint.pub").read_text())
    first = list_segments(log.data_dir)[0][1].read_text().splitlines()[0]
    return log, pubfile, str(json.loads(first)["hash"])


def test_g1_genesis_is_signed_immediately(make_log: MakeLog) -> None:
    log = make_log()
    assert kinds(log) == ["genesis", "checkpoint"]
    assert verify_dir(log.data_dir).checkpoints_verified == 1
    assert (log.data_dir / CHECKPOINT_SIDECAR).read_text().count("\n") == 1  # and the sidecar has it


def test_g1_fresh_forged_chain_with_the_sidecar_deleted_fails_with_the_trusted_key(make_log: MakeLog) -> None:
    log, pubfile, _ = genuine(make_log)
    assert audit_cli(["verify", str(log.data_dir), "--pubkey", str(pubfile)]) == 0
    forge_fresh_chain(log.data_dir)
    assert audit_cli(["verify", str(log.data_dir), "--pubkey", str(pubfile)]) == 1
    report = verify_dir(log.data_dir, load_public_key(pubfile.read_text()), require_checkpoint=True)
    assert not report.ok
    assert any("no checkpoint" in e for e in report.errors)


def test_g1_expect_genesis_catches_a_replaced_chain_even_with_a_replaced_key(make_log: MakeLog) -> None:
    log, _, genesis = genuine(make_log)
    assert audit_cli(["verify", str(log.data_dir), "--expect-genesis", genesis]) == 0
    # The attacker also replaces checkpoint.pub with a key of their own and signs a new chain with it.
    for _, path in list_segments(log.data_dir):
        path.unlink()
    (log.data_dir / CHECKPOINT_SIDECAR).unlink()
    attacker = Ed25519PrivateKey.generate()
    (log.data_dir / "checkpoint.pub").write_text(public_key_b64(attacker) + "\n")
    forged = AuditLog.open(AuditConfig(data_dir=log.data_dir), attacker)
    forged.append("atlas-core", tool_call())
    forged.close()
    assert audit_cli(["verify", str(log.data_dir)]) == 0  # nothing on the box can tell
    assert audit_cli(["verify", str(log.data_dir), "--expect-genesis", genesis]) == 1


def test_g1_expect_genesis_is_validated(make_log: MakeLog) -> None:
    log, _, _ = genuine(make_log)
    assert audit_cli(["verify", str(log.data_dir), "--expect-genesis", "abc"]) == 2


def test_g1_purged_genesis_is_a_warning_with_expect_genesis(make_log: MakeLog, clock: Clock) -> None:
    log = make_log()
    genesis = json.loads(list_segments(log.data_dir)[0][1].read_text().splitlines()[0])["hash"]
    log.append("atlas-core", tool_call())
    clock.advance(24 * 3600)
    log.append("atlas-core", tool_call())
    log.purge_segments([list_segments(log.data_dir)[0][1].stem], "retention")
    log.close()
    report = verify_dir(log.data_dir, expect_genesis=genesis)
    assert report.ok, report.errors
    assert any("genesis" in w for w in report.warnings)  # the signed purge anchor stands in for it


def test_g1_verify_without_an_off_box_head_is_unanchored(make_log: MakeLog, capsys: pytest.CaptureFixture[str]) -> None:
    log, pubfile, genesis = genuine(make_log)
    capsys.readouterr()
    assert audit_cli(["verify", str(log.data_dir), "--pubkey", str(pubfile), "--expect-genesis", genesis]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[-1].startswith("UNANCHORED:")
    assert "OK:" not in out
    report = verify_dir(log.data_dir)
    assert report.last_seq is not None and report.head_hash is not None
    anchor = f"{report.last_seq}:{report.head_hash}"
    assert audit_cli(["verify", str(log.data_dir), "--pubkey", str(pubfile), "--expect-head", anchor]) == 0
    assert capsys.readouterr().out.splitlines()[-1].startswith("OK:")
