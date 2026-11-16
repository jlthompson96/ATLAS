"""atlas-audit writer and verifier: append/verify, tamper detection (PT-13), durability and crash (FI-3)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.audit import log as audit_log_module
from atlas.audit.__main__ import main as audit_cli
from atlas.audit.jcs import canonicalize
from atlas.audit.keys import load_public_key, public_key_b64
from atlas.audit.log import AuditConfig, AuditCorrupt, AuditLog, AuditUnavailable, SubmissionRejected
from atlas.audit.record import record_hash
from atlas.audit.segments import list_segments, parse_record_line
from atlas.audit.verify import VerifyReport, verify_dir
from tests.conftest import Clock, egress_body, tool_call, write_ahead, write_result

MakeLog = Callable[..., AuditLog]


def fill(log: AuditLog, n: int, start: int = 0) -> None:
    for i in range(start, start + n):
        log.append("atlas-core", tool_call(i))


def segment_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def write_segment(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_bytes(b"".join(canonicalize(r) + b"\n" for r in records))


def rehash_from(records: list[dict[str, Any]], start: int) -> None:
    """Recompute hashes and links from ``start`` on, as an attacker with write access would."""
    for i in range(start, len(records)):
        records[i]["prev_hash"] = records[i - 1]["hash"] if i else records[i]["prev_hash"]
        records[i]["hash"] = record_hash(records[i])


def verify(log: AuditLog) -> VerifyReport:
    return verify_dir(log.data_dir)


# --- basics --------------------------------------------------------------------------------------------


def test_append_and_verify(make_log: MakeLog) -> None:
    log = make_log()
    fill(log, 5)
    log.append("atlas-egress", egress_body())
    log.append("atlas-executor", write_ahead())
    log.append("atlas-executor", write_result())
    log.close()
    report = verify(log)
    assert report.ok, report.errors
    assert report.records == 9  # genesis + 8
    assert (report.first_seq, report.last_seq) == (0, 8)
    assert report.outcome_unknown == []


def test_actor_is_set_by_the_service_not_the_body(make_log: MakeLog) -> None:
    log = make_log()
    with pytest.raises(SubmissionRejected) as exc:
        log.append("atlas-panels", {**tool_call(), "actor": "atlas-core"})
    assert exc.value.code == "ACTOR_MISMATCH"
    done = log.append("atlas-panels", tool_call())
    rec = parse_record_line(list_segments(log.data_dir)[0][1].read_bytes().splitlines()[done.seq])
    assert rec["actor"] == "atlas-panels"
    assert log.head[0] == done.seq  # the rejected record consumed no seq


@pytest.mark.parametrize(
    ("actor", "body"),
    [
        ("atlas-core", {**write_ahead(), "kind": "approval"}),
        ("atlas-core", write_ahead()),  # write_ahead is the executor's
        ("atlas-google", egress_body()),  # egress records come from atlas-egress only
        ("atlas-core", {"kind": "checkpoint", "context": "system"}),
        ("atlas-core", {**tool_call(), "seq": 99}),
        ("atlas-core", {**tool_call(), "args_redacted": {"x": 1.5}}),
        ("atlas-core", {**tool_call(), "unexpected": 1}),
    ],
)
def test_bad_submissions_are_refused_and_write_nothing(make_log: MakeLog, actor: str, body: dict[str, Any]) -> None:
    log = make_log()
    head = log.head
    with pytest.raises(SubmissionRejected):
        log.append(actor, body)
    assert log.head == head
    assert verify(log).ok


def test_reopen_continues_the_chain(make_log: MakeLog) -> None:
    log = make_log()
    fill(log, 3)
    log.close()
    again = make_log()
    assert again.head[0] == 3
    fill(again, 2, start=3)
    again.close()
    report = verify(again)
    assert report.ok, report.errors
    assert report.last_seq == 5


# --- durability and the write budget ---------------------------------------------------------------------


@pytest.fixture
def fsyncs(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = os.fsync

    def counting(fd: int) -> None:
        calls.append(fd)
        real(fd)

    monkeypatch.setattr(audit_log_module.os, "fsync", counting)
    return calls


def test_write_ahead_is_fsynced_before_the_append_returns(make_log: MakeLog, fsyncs: list[int]) -> None:
    log = make_log()
    fsyncs.clear()
    done = log.append("atlas-executor", write_ahead())
    assert done.durable
    assert fsyncs, "write_ahead must be fsynced before acknowledging (FI-3)"


def test_read_events_are_batched_then_flushed_by_time_or_size(
    make_log: MakeLog, clock: Clock, fsyncs: list[int]
) -> None:
    log = make_log(flush_interval_s=30, flush_bytes=10_000_000)
    fsyncs.clear()
    fill(log, 10)
    assert fsyncs == []  # batched: written to the page cache, not forced to the card
    clock.advance(31)
    log.tick()
    assert len(fsyncs) == 1  # one fsync covers the whole batch (write budget, PLAN 3.5)
    fill(log, 3, start=10)
    assert len(fsyncs) == 1


def test_size_threshold_forces_a_flush(make_log: MakeLog, fsyncs: list[int]) -> None:
    log = make_log(flush_bytes=500)
    fsyncs.clear()
    fill(log, 8)
    assert fsyncs


def test_io_error_poisons_the_log(make_log: MakeLog, monkeypatch: pytest.MonkeyPatch) -> None:
    log = make_log()

    def boom(fd: int, data: Any) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(audit_log_module.os, "write", boom)
    with pytest.raises(OSError, match="space"):
        fill(log, 1)
    monkeypatch.undo()
    with pytest.raises(AuditUnavailable):
        fill(log, 1)  # refuses until restart re-validates the segment


# --- crash behaviour (FI-3) ------------------------------------------------------------------------------


def test_crash_between_write_ahead_and_write_result_is_outcome_unknown(make_log: MakeLog) -> None:
    log = make_log()
    log.append("atlas-executor", write_ahead("apr-7", "gmail.send"))
    log.append("atlas-executor", write_ahead("apr-8", "discord.post"))
    log.append("atlas-executor", write_result("apr-8", "discord.post"))
    # crash: no close(), no write_result for apr-7. A new process opens the same directory.
    revived = make_log()
    report = verify(revived)
    assert report.ok, report.errors
    assert [(o.approval_id, o.tool) for o in report.outcome_unknown] == [("apr-7", "gmail.send")]
    revived.append("atlas-executor", write_result("apr-7", "gmail.send"))
    assert verify(revived).outcome_unknown == []


def test_torn_tail_is_ignored_and_the_chain_continues_in_a_new_segment(make_log: MakeLog) -> None:
    log = make_log()
    fill(log, 4)
    seg = list_segments(log.data_dir)[0][1]
    with seg.open("ab") as handle:
        handle.write(b'{"v":1,"seq":5,"ts":17632')  # power cut mid-write: never acknowledged
    report = verify(log)
    assert report.ok
    assert any("torn" in w for w in report.warnings)
    revived = make_log()
    assert revived.head[0] == 4
    fill(revived, 2, start=4)
    revived.close()
    final = verify(revived)
    assert final.ok, final.errors
    assert final.last_seq == 6
    assert len(list_segments(revived.data_dir)) == 2


def test_empty_leftover_segment_is_set_aside(make_log: MakeLog) -> None:
    log = make_log()
    fill(log, 2)
    log.close()
    (log.data_dir / "segments" / "seg-0000000003.jsonl").write_bytes(b"")  # crash right after create
    revived = make_log()
    fill(revived, 1)
    revived.close()
    assert verify(revived).ok


def test_open_refuses_a_tampered_active_segment(make_log: MakeLog) -> None:
    log = make_log()
    fill(log, 3)
    log.close()
    path = list_segments(log.data_dir)[-1][1]
    records = segment_lines(path)
    records[2]["tool"] = "other_tool"
    write_segment(path, records)
    with pytest.raises(AuditCorrupt):
        make_log()


def test_open_refuses_a_different_checkpoint_key(make_log: MakeLog, tmp_path: Path, clock: Clock) -> None:
    log = make_log()
    log.close()
    with pytest.raises(AuditCorrupt):
        AuditLog.open(AuditConfig(data_dir=log.data_dir), Ed25519PrivateKey.generate(), wall_clock_ms=clock.wall)


def test_open_refuses_when_segments_vanished_but_the_sidecar_remains(make_log: MakeLog) -> None:
    log = make_log(checkpoint_every=3)
    fill(log, 5)
    log.close()
    for _, path in list_segments(log.data_dir):
        path.unlink()
    with pytest.raises(AuditCorrupt):
        make_log()


# --- tamper detection (PT-13) ----------------------------------------------------------------------------


@pytest.fixture
def chain(make_log: MakeLog) -> AuditLog:
    log = make_log(checkpoint_every=4)
    fill(log, 12)
    log.close()
    assert verify(log).ok
    return log


def only_segment(log: AuditLog) -> Path:
    segments = list_segments(log.data_dir)
    assert len(segments) == 1
    return segments[0][1]


def test_modified_record_is_detected(chain: AuditLog) -> None:
    path = only_segment(chain)
    records = segment_lines(path)
    records[3]["context"] = "unattended"
    write_segment(path, records)
    report = verify(chain)
    assert not report.ok
    assert "modified" in report.errors[0]


def test_modified_and_rehashed_record_breaks_the_next_link(chain: AuditLog) -> None:
    path = only_segment(chain)
    records = segment_lines(path)
    records[3]["context"] = "unattended"
    records[3]["hash"] = record_hash(records[3])
    write_segment(path, records)
    report = verify(chain)
    assert not report.ok
    assert "chain broken" in report.errors[0]


def test_full_rewrite_of_the_chain_is_caught_by_the_checkpoint_signature(chain: AuditLog) -> None:
    path = only_segment(chain)
    records = segment_lines(path)
    records[3]["context"] = "unattended"
    rehash_from(records, 3)  # attacker fixes every hash and link after the edit
    write_segment(path, records)
    report = verify(chain)
    assert not report.ok
    assert any("checkpoint" in e for e in report.errors)


def test_deleted_record_is_detected(chain: AuditLog) -> None:
    path = only_segment(chain)
    records = segment_lines(path)
    del records[5]
    write_segment(path, records)
    report = verify(chain)
    assert not report.ok
    assert "seq gap" in report.errors[0]


def test_reordered_records_are_detected(chain: AuditLog) -> None:
    path = only_segment(chain)
    records = segment_lines(path)
    records[4], records[5] = records[5], records[4]
    write_segment(path, records)
    assert not verify(chain).ok


def test_truncation_before_the_last_checkpoint_is_detected_via_the_sidecar(chain: AuditLog) -> None:
    path = only_segment(chain)
    records = segment_lines(path)
    write_segment(path, records[:6])  # a perfectly valid prefix of the chain
    report = verify(chain)
    assert not report.ok
    assert "truncated" in report.errors[0]


def test_truncation_against_an_off_box_head_is_detected(make_log: MakeLog) -> None:
    log = make_log(checkpoint_every=1000)
    fill(log, 10)
    seq, head = log.head
    log.close()
    path = list_segments(log.data_dir)[0][1]
    records = segment_lines(path)
    write_segment(path, records[:8])  # valid prefix, newer than any checkpoint
    assert verify_dir(log.data_dir).ok  # on-box alone cannot see it (documented limit)
    assert not verify_dir(log.data_dir, expect_head=(seq, head)).ok
    assert any("shorter" in e for e in verify_dir(log.data_dir, expect_head=(seq, head)).errors)


def test_deleted_sidecar_is_detected(chain: AuditLog) -> None:
    (chain.data_dir / "checkpoints.jsonl").unlink()
    assert not verify(chain).ok


def test_checkpoint_signed_by_another_key_is_detected(chain: AuditLog) -> None:
    other = Ed25519PrivateKey.generate()
    report = verify_dir(chain.data_dir, load_public_key(public_key_b64(other)))
    assert not report.ok
    assert "unknown key" in report.errors[0]


def test_forged_checkpoint_signature_is_detected(chain: AuditLog) -> None:
    path = only_segment(chain)
    records = segment_lines(path)
    index = next(i for i, r in enumerate(records) if r["kind"] == "checkpoint")
    sig = records[index]["checkpoint"]["signature"]
    records[index]["checkpoint"]["signature"] = ("A" if sig[0] != "A" else "B") + sig[1:]
    rehash_from(records, index)
    write_segment(path, records)
    report = verify(chain)
    assert not report.ok
    assert any("signature" in e for e in report.errors)


def test_garbage_and_duplicate_keys_are_rejected(chain: AuditLog) -> None:
    path = only_segment(chain)
    with path.open("ab") as handle:
        handle.write(b'{"v":1,"v":1}\n')
    assert not verify(chain).ok


def test_checkpoint_cadence_by_count_and_by_time(make_log: MakeLog, clock: Clock) -> None:
    log = make_log(checkpoint_every=5, checkpoint_interval_s=3600)
    fill(log, 12)
    assert verify(log).checkpoints_verified == 2
    before = verify(log).checkpoints_verified
    clock.advance(3601)
    log.tick()
    assert verify(log).checkpoints_verified == before + 1
    clock.advance(3601)
    log.tick()  # nothing new since the last checkpoint: no empty checkpoints
    assert verify(log).checkpoints_verified == before + 1


# --- segments, rotation, purge ---------------------------------------------------------------------------


def test_daily_rotation_keeps_seq_gap_free(make_log: MakeLog, clock: Clock) -> None:
    log = make_log()
    fill(log, 3)
    clock.advance(24 * 3600)
    fill(log, 3, start=3)
    clock.advance(24 * 3600)
    fill(log, 2, start=6)
    log.close()
    assert len(list_segments(log.data_dir)) == 3
    report = verify(log)
    assert report.ok, report.errors
    assert report.last_seq == 8 + report.checkpoints_verified  # hourly checkpoints fire across the day gaps
    assert report.checkpoints_verified == 2


def test_size_rotation(make_log: MakeLog) -> None:
    log = make_log(max_segment_bytes=1200)
    fill(log, 20)
    log.close()
    assert len(list_segments(log.data_dir)) > 2
    assert verify(log).ok


def rotated(make_log: MakeLog, clock: Clock) -> AuditLog:
    log = make_log()
    for day in range(3):
        fill(log, 3, start=day * 3)
        clock.advance(24 * 3600)
    fill(log, 1, start=99)
    return log


def test_deleting_the_oldest_segment_without_a_purge_record_is_detected(make_log: MakeLog, clock: Clock) -> None:
    log = rotated(make_log, clock)
    log.close()
    list_segments(log.data_dir)[0][1].unlink()
    report = verify(log)
    assert not report.ok
    assert "purge" in report.errors[0]


def test_deleting_a_middle_segment_is_detected(make_log: MakeLog, clock: Clock) -> None:
    log = rotated(make_log, clock)
    log.close()
    list_segments(log.data_dir)[1][1].unlink()
    assert "seq gap" in verify(log).errors[0]


def test_renamed_segment_is_detected(make_log: MakeLog, clock: Clock) -> None:
    log = rotated(make_log, clock)
    log.close()
    segments = list_segments(log.data_dir)
    segments[1][1].rename(segments[1][1].with_name("seg-0000000099.jsonl"))
    assert not verify(log).ok


def test_purge_reanchors_the_chain_and_verifies(make_log: MakeLog, clock: Clock) -> None:
    log = rotated(make_log, clock)
    ids = [p.stem for _, p in list_segments(log.data_dir)]
    log.purge_segments(ids[:2], "retention")
    fill(log, 2, start=200)
    log.close()
    remaining = list_segments(log.data_dir)
    assert len(remaining) == 2  # the older two are gone
    report = verify(log)
    assert report.ok, report.errors
    assert report.first_seq is not None and report.first_seq > 0
    # the purge record is itself in the chain and says what was removed
    purge = next(r for _, p in remaining for r in segment_lines(p) if r["kind"] == "purge")
    assert purge["purge"]["segments"] == ids[:2]


def test_purge_record_is_written_before_files_are_deleted(make_log: MakeLog, clock: Clock) -> None:
    log = rotated(make_log, clock)
    segments = list_segments(log.data_dir)
    saved = {path.name: path.read_bytes() for _, path in segments[:1]}
    log.purge_segments([segments[0][1].stem], "retention")
    log.close()
    # A crash after the purge record but before the unlink leaves the old file behind: still a valid chain.
    for name, data in saved.items():
        (log.data_dir / "segments" / name).write_bytes(data)
    report = verify(log)
    assert report.ok, report.errors
    assert report.first_seq == 0


def test_purge_refuses_non_prefix_and_the_active_segment(make_log: MakeLog, clock: Clock) -> None:
    log = rotated(make_log, clock)
    ids = [p.stem for _, p in list_segments(log.data_dir)]
    with pytest.raises(ValueError, match="oldest"):
        log.purge_segments([ids[1]], "retention")
    with pytest.raises(ValueError, match="active"):
        log.purge_segments(ids, "retention")
    with pytest.raises(ValueError, match="unknown"):
        log.purge_segments(["seg-9999999999"], "retention")


def test_purged_anchor_must_match(make_log: MakeLog, clock: Clock) -> None:
    log = rotated(make_log, clock)
    ids = [p.stem for _, p in list_segments(log.data_dir)]
    log.purge_segments(ids[:1], "user_request")
    log.close()
    segments = list_segments(log.data_dir)
    for _, path in segments:
        records = segment_lines(path)
        for rec in records:
            if rec["kind"] == "purge":
                rec["purge"]["anchor_hash"] = "f" * 64
        rehash_from(records, 1 if records[0]["seq"] else 0)
        write_segment(path, records)
    assert not verify(log).ok


# --- CLI -------------------------------------------------------------------------------------------------


def test_cli_verify_exit_codes(chain: AuditLog, capsys: pytest.CaptureFixture[str]) -> None:
    assert audit_cli(["verify", str(chain.data_dir)]) == 0
    assert "OK:" in capsys.readouterr().out
    path = only_segment(chain)
    records = segment_lines(path)
    records[2]["context"] = "unattended"
    write_segment(path, records)
    assert audit_cli(["verify", str(chain.data_dir)]) == 1
    assert "AUDIT_CHAIN_BROKEN" in capsys.readouterr().out


def test_cli_verify_lists_outcome_unknown(make_log: MakeLog, capsys: pytest.CaptureFixture[str]) -> None:
    log = make_log()
    log.append("atlas-executor", write_ahead("apr-3"))
    log.close()
    assert audit_cli(["verify", str(log.data_dir)]) == 0
    assert "OUTCOME_UNKNOWN" in capsys.readouterr().out
