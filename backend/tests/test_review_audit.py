"""Regression tests for the security review of atlas-audit (F1, F4, F5, F9). Each failed before its fix."""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.audit import log as audit_log_module
from atlas.audit.__main__ import main as audit_cli
from atlas.audit.jcs import canonicalize
from atlas.audit.keys import load_public_key
from atlas.audit.log import AuditConfig, AuditCorrupt, AuditLog, SubmissionRejected, load_body
from atlas.audit.record import record_hash
from atlas.audit.segments import SEGMENT_RE, list_segments
from atlas.audit.server import AuditServer
from atlas.audit.verify import outcome_unknown_in_dir, verify_dir
from tests.conftest import Clock, tool_call, write_ahead
from tests.test_audit_server import FakeCreds, FakeUsers, line, raw_exchange

MakeLog = Callable[..., AuditLog]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def forged_record(seq: int, prev: str, **fields: Any) -> dict[str, Any]:
    rec = {"v": 1, "seq": seq, "ts": 1_763_279_000_000 + seq, "prev_hash": prev, **fields}
    rec["hash"] = record_hash(rec)
    return rec


def write_forged_purge_chain(directory: Path, head_seq: int) -> None:
    """exp3.py: delete every segment, then write a purge-anchored chain without the signing key."""
    for _, path in list_segments(directory):
        path.unlink()
    first = head_seq + 1
    anchor = "ab" * 32
    purge = forged_record(
        first,
        anchor,
        kind="purge",
        actor="atlas-audit",
        context="system",
        purge={
            "reason": "retention",
            "from_seq": 0,
            "to_seq": head_seq,
            "anchor_hash": anchor,
            "segments": ["seg-0000000000"],
        },
    )
    after = forged_record(
        first + 1,
        purge["hash"],
        kind="system",
        actor="atlas-audit",
        context="system",
        system={"event": "chain_verified"},
    )
    (directory / "segments" / f"seg-{first:010d}.jsonl").write_bytes(
        b"".join(canonicalize(r) + b"\n" for r in (purge, after))
    )


@pytest.fixture
def honest(make_log: MakeLog, key: Ed25519PrivateKey) -> tuple[AuditLog, Ed25519PrivateKey]:
    log = make_log(checkpoint_every=50)
    for _ in range(120):
        log.append("atlas-executor", write_ahead())
    log.checkpoint_now()
    return log, key


# --- F1 -----------------------------------------------------------------------------------------------------


def test_f1_forged_purge_with_trusted_key_and_expect_head_is_rejected(
    honest: tuple[AuditLog, Ed25519PrivateKey],
) -> None:
    log, _ = honest
    head_seq, head_hash = log.head
    log.close()
    pub = load_public_key((log.data_dir / "checkpoint.pub").read_text())
    assert verify_dir(log.data_dir, pub, (head_seq, head_hash)).ok
    write_forged_purge_chain(log.data_dir, head_seq)
    report = verify_dir(log.data_dir, pub, (head_seq, head_hash))
    assert not report.ok
    text = " ".join(report.errors)
    assert "no purge record covered by a signed checkpoint" in text
    assert "predates the chain" in text  # the off-box anchor is no longer a warning
    assert "below the chain start" in text  # nor are the sidecar entries
    pubfile = log.data_dir / "pub"
    pubfile.write_text((log.data_dir / "checkpoint.pub").read_text())
    assert (
        audit_cli(["verify", str(log.data_dir), "--pubkey", str(pubfile), "--expect-head", f"{head_seq}:{head_hash}"])
        == 1
    )


def test_f1_service_refuses_to_resume_on_a_forged_purge_chain(
    honest: tuple[AuditLog, Ed25519PrivateKey], clock: Clock
) -> None:
    log, key = honest
    head_seq, _ = log.head
    log.close()
    write_forged_purge_chain(log.data_dir, head_seq)
    with pytest.raises(AuditCorrupt):
        AuditLog.open(AuditConfig(data_dir=log.data_dir), key, wall_clock_ms=clock.wall)


def test_f1_forged_purge_is_rejected_even_without_any_anchor_when_the_sidecar_remains(
    honest: tuple[AuditLog, Ed25519PrivateKey],
) -> None:
    log, _ = honest
    head_seq, _ = log.head
    log.close()
    write_forged_purge_chain(log.data_dir, head_seq)
    assert not verify_dir(log.data_dir).ok


def test_f1_signed_purge_is_accepted_and_anchors_the_old_head(
    make_log: MakeLog, clock: Clock, key: Ed25519PrivateKey
) -> None:
    log = make_log(checkpoint_every=1000)
    for _ in range(3):
        for i in range(3):
            log.append("atlas-core", tool_call(i))
        clock.advance(24 * 3600)
    log.append("atlas-core", tool_call(99))
    segments = list_segments(log.data_dir)
    old_head_seq = segments[1][0] - 1  # last record of the first segment
    old_hash = json.loads(segments[0][1].read_text().splitlines()[-1])["hash"]
    log.purge_segments([segments[0][1].stem], "retention")
    log.close()
    pub = load_public_key((log.data_dir / "checkpoint.pub").read_text())
    ok = verify_dir(log.data_dir, pub, (old_head_seq, old_hash))
    assert ok.ok, ok.errors
    wrong = verify_dir(log.data_dir, pub, (old_head_seq, "f" * 64))
    assert not wrong.ok and "anchored by the signed purge" in wrong.errors[0]
    # the service resumes on a legitimately purged chain
    resumed = AuditLog.open(AuditConfig(data_dir=log.data_dir), key, wall_clock_ms=clock.wall)
    assert resumed.head[0] > old_head_seq
    resumed.close()


def test_f1_purge_signs_a_checkpoint_before_deleting_anything(make_log: MakeLog, clock: Clock) -> None:
    log = make_log()
    log.append("atlas-core", tool_call())
    clock.advance(24 * 3600)
    log.append("atlas-core", tool_call())
    first = list_segments(log.data_dir)[0][1]
    log.purge_segments([first.stem], "user_request")
    kinds = [json.loads(x)["kind"] for _, p in list_segments(log.data_dir) for x in p.read_text().splitlines()]
    assert kinds[-2:] == ["purge", "checkpoint"]


def test_f1_unsigned_tail_beyond_the_checkpoint_cadence_is_an_error_and_is_reported(make_log: MakeLog) -> None:
    log = make_log(checkpoint_every=5000)  # the writer would never have signed within the verifier's limit
    for i in range(1100):
        log.append("atlas-core", tool_call(i % 50))
    log.close()
    report = verify_dir(log.data_dir)
    assert report.records_after_last_checkpoint == 1100  # everything after the genesis checkpoint
    assert not report.ok
    assert any("records after the last verified checkpoint" in e for e in report.errors)
    relaxed = verify_dir(log.data_dir, max_unsigned_records=5000)
    assert relaxed.ok, relaxed.errors


def test_f1_live_tail_within_limits_is_reported_not_rejected(make_log: MakeLog) -> None:
    log = make_log(checkpoint_every=1000)
    for i in range(10):
        log.append("atlas-core", tool_call(i))
    log.close()
    report = verify_dir(log.data_dir)
    assert report.ok
    assert report.records_after_last_checkpoint == 10


def test_f1_recover_refuses_a_chain_older_than_the_sidecar(
    honest: tuple[AuditLog, Ed25519PrivateKey], clock: Clock
) -> None:
    log, key = honest
    log.close()
    path = list_segments(log.data_dir)[-1][1]
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines[:40]) + "\n")  # valid prefix, shorter than the last checkpoint in the sidecar
    with pytest.raises(AuditCorrupt, match="sidecar"):
        AuditLog.open(AuditConfig(data_dir=log.data_dir), key, wall_clock_ms=clock.wall)


def test_f1_recover_refuses_a_sidecar_that_disagrees_with_the_chain(
    honest: tuple[AuditLog, Ed25519PrivateKey], clock: Clock
) -> None:
    log, key = honest
    log.close()
    side = log.data_dir / "checkpoints.jsonl"
    entries = [json.loads(x) for x in side.read_text().splitlines()]
    entries[-1]["hash"] = "0" * 64
    side.write_bytes(b"".join(canonicalize(e) + b"\n" for e in entries))
    with pytest.raises(AuditCorrupt, match="differs"):
        AuditLog.open(AuditConfig(data_dir=log.data_dir), key, wall_clock_ms=clock.wall)


# --- F4 -----------------------------------------------------------------------------------------------------


def nested(depth: int) -> dict[str, Any]:
    value: Any = 1
    for _ in range(depth):
        value = {"a": value}
    return value


def test_f4_deep_nesting_is_rejected_without_poisoning_the_log(make_log: MakeLog) -> None:
    log = make_log()
    raw = (
        '{"kind":"tool_call","context":"interactive","tool":"x","args_redacted":'
        + '{"a":' * 1100
        + "1"
        + "}" * 1100
        + ',"data_accessed":[],"result":{"status":"ok"}}'
    ).encode()
    with pytest.raises(SubmissionRejected):
        log.append("atlas-discord", load_body(raw))
    assert not log.failed
    log.append("atlas-egress", {"kind": "system", "context": "system", "system": {"event": "service_start"}})


@pytest.mark.parametrize(("depth", "ok"), [(7, True), (8, False), (9, False)])
def test_f4_args_depth_cap(make_log: MakeLog, depth: int, ok: bool) -> None:
    log = make_log()
    body = {**tool_call(), "args_redacted": {"a": nested(depth - 1)}}  # args_redacted itself is level 1
    if ok:
        log.append("atlas-core", body)
    else:
        with pytest.raises(SubmissionRejected):
            log.append("atlas-core", body)
    assert not log.failed


def test_f4_args_node_cap(make_log: MakeLog) -> None:
    log = make_log()
    wide = {f"k{i}": list(range(40)) for i in range(30)}  # 30 keys x 40 values = 1230 nodes
    with pytest.raises(SubmissionRejected):
        log.append("atlas-core", {**tool_call(), "args_redacted": wide})
    assert not log.failed


def test_f4_io_error_after_the_write_started_poisons_and_asks_for_a_restart(
    make_log: MakeLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        log = make_log()
        creds = FakeCreds()
        server = AuditServer(log, creds, FakeUsers(), tick_interval_s=3600)
        with tempfile.TemporaryDirectory(prefix="atl") as tmp:
            sock = Path(tmp) / "a.sock"
            await server.listen_path(sock)
            try:
                creds.uid = 1004

                def boom(fd: int, data: Any) -> int:
                    raise OSError(5, "I/O error")

                monkeypatch.setattr(audit_log_module.os, "write", boom)
                [reply] = await raw_exchange(sock, line({"op": "append", "record": tool_call()}))
                assert reply == {"ok": False, "code": "AUDIT_UNAVAILABLE"}
                assert server.fatal.is_set()  # the service exits non-zero; systemd restarts and re-validates
                assert log.failed
            finally:
                monkeypatch.undo()
                await server.close()

    run(scenario())


def test_f4_deep_request_over_the_socket_does_not_affect_other_clients(make_log: MakeLog) -> None:
    async def scenario() -> None:
        log = make_log()
        creds = FakeCreds()
        server = AuditServer(log, creds, FakeUsers(), tick_interval_s=3600)
        with tempfile.TemporaryDirectory(prefix="atl") as tmp:
            sock = Path(tmp) / "a.sock"
            await server.listen_path(sock)
            try:
                creds.uid = 1004
                depth = 1100
                req = (
                    '{"op":"append","record":{"kind":"tool_call","context":"interactive","tool":"x","args_redacted":'
                    + '{"a":' * depth
                    + "1"
                    + "}" * depth
                    + ',"data_accessed":[],"result":{"status":"ok"}}}\n'
                ).encode()
                [bad] = await raw_exchange(sock, req)
                assert bad["ok"] is False
                creds.uid = 1003
                good = {"kind": "system", "context": "system", "system": {"event": "service_start"}}
                [reply] = await raw_exchange(sock, line({"op": "append", "record": good}))
                assert reply["ok"] is True
                assert not server.fatal.is_set()
            finally:
                await server.close()

    run(scenario())


# --- F5 (audit side) ----------------------------------------------------------------------------------------


def test_f5_one_actor_cannot_exhaust_the_connection_limit(make_log: MakeLog) -> None:
    async def scenario() -> None:
        log = make_log()
        creds = FakeCreds()
        server = AuditServer(log, creds, FakeUsers(), tick_interval_s=3600, max_connections_per_actor=8)
        with tempfile.TemporaryDirectory(prefix="atl") as tmp:
            sock = Path(tmp) / "a.sock"
            await server.listen_path(sock)
            held = []
            try:
                creds.uid = 1004
                for _ in range(8):
                    held.append(await asyncio.open_unix_connection(str(sock)))
                await asyncio.sleep(0.1)
                [busy] = await raw_exchange(sock, line({"op": "append", "record": tool_call()}))
                assert busy == {"ok": False, "code": "BUSY"}
                creds.uid = 1003  # a different service is unaffected
                egress = {"kind": "system", "context": "system", "system": {"event": "service_start"}}
                [fine] = await raw_exchange(sock, line({"op": "append", "record": egress}))
                assert fine["ok"] is True
            finally:
                for _, w in held:
                    w.close()
                await server.close()

    run(scenario())


def test_f5_append_rate_limit_per_actor_counts_durable_kinds_too(make_log: MakeLog) -> None:
    async def scenario() -> None:
        log = make_log()
        creds = FakeCreds()
        now = [0.0]
        server = AuditServer(
            log, creds, FakeUsers(), tick_interval_s=3600, rate_per_s=1.0, burst=5.0, monotonic=lambda: now[0]
        )
        with tempfile.TemporaryDirectory(prefix="atl") as tmp:
            sock = Path(tmp) / "a.sock"
            await server.listen_path(sock)
            try:
                creds.uid = 1002  # atlas-executor writes durable write_ahead records
                codes = []
                for i in range(8):
                    [reply] = await raw_exchange(sock, line({"op": "append", "record": write_ahead(f"apr-{i}")}))
                    codes.append(reply.get("code", "ok"))
                assert codes == ["ok"] * 5 + ["RATE_LIMITED"] * 3
                creds.uid = 1003  # other actors have their own bucket
                good = {"kind": "system", "context": "system", "system": {"event": "service_start"}}
                [reply] = await raw_exchange(sock, line({"op": "append", "record": good}))
                assert reply["ok"] is True
                now[0] += 3.0  # tokens refill
                creds.uid = 1002
                [reply] = await raw_exchange(sock, line({"op": "append", "record": write_ahead("apr-9")}))
                assert reply["ok"] is True
            finally:
                await server.close()

    run(scenario())


def test_f5_free_space_floor_refuses_client_records_with_a_distinct_code(make_log: MakeLog) -> None:
    log = make_log(min_free_bytes=2**62)
    with pytest.raises(SubmissionRejected) as exc:
        log.append("atlas-core", tool_call())
    assert exc.value.code == "NO_SPACE"
    assert not log.failed
    log.checkpoint_now()  # internal records are not blocked: the chain can still be signed


# --- F9 -----------------------------------------------------------------------------------------------------


def test_f9_housekeeping_failure_requests_a_restart(make_log: MakeLog, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        log = make_log()

        def boom() -> None:
            raise OSError(5, "I/O error")

        monkeypatch.setattr(log, "tick", boom)
        server = AuditServer(log, FakeCreds(), FakeUsers(), tick_interval_s=0.01)
        with tempfile.TemporaryDirectory(prefix="atl") as tmp:
            await server.listen_path(Path(tmp) / "a.sock")
            try:
                await asyncio.wait_for(server.fatal.wait(), 2)
            finally:
                await server.close()

    run(scenario())


def test_f9_outcome_unknown_streams_and_ignores_garbage_and_torn_tails(make_log: MakeLog) -> None:
    log = make_log()
    log.append("atlas-executor", write_ahead("apr-1"))
    log.append("atlas-executor", write_ahead("apr-2"))
    log.close()
    seg = list_segments(log.data_dir)[0][1]
    with seg.open("ab") as handle:
        handle.write(b'{"v":1,"kind":"write_ahead","seq":')  # torn tail, no newline
    assert [o.approval_id for o in outcome_unknown_in_dir(log.data_dir)] == ["apr-1", "apr-2"]


def test_f9_server_does_not_load_the_whole_log_for_outcome_unknown() -> None:
    import atlas.audit.server as server_module

    assert not hasattr(server_module, "load_records")
    assert not hasattr(server_module, "VerifyReport")


def test_f9_segment_names_accept_ascii_digits_only() -> None:
    assert SEGMENT_RE.fullmatch("seg-0000000042.jsonl")
    assert not SEGMENT_RE.fullmatch("seg-" + "\u0660" * 10 + ".jsonl")  # Arabic-Indic digits
