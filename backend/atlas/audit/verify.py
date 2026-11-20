"""Offline verification of an audit directory (PT-13) and outcome-unknown detection (FI-3).

``verify_dir`` re-derives everything from the files: record schema, hash chain, gap-free ``seq`` across
segments, purge anchors, Ed25519 checkpoint signatures, the checkpoint sidecar (truncation evidence) and
an optional off-box ``expect_head``. It never writes. Fails closed: any doubt is an error.

Trust rules (review F1). Nothing unsigned vouches for anything:

* A chain that does not start at seq 0 is accepted only if a ``purge`` record anchors it AND a
  signature-verified checkpoint at or after that purge record covers it. (audit-record.v1 has no
  signature field on ``purge``, so coverage comes from the next checkpoint, whose signed head hash commits
  to the purge record, and through it to ``anchor_hash``. ``purge_segments`` signs one immediately.)
* An ``expect_head``, or a sidecar entry, that predates the surviving chain is an error unless such a
  covered purge accounts for that seq range.
* The writer signs a checkpoint right after genesis (known issue G1), so with a trusted key
  (``require_checkpoint``, the CLI's ``--pubkey``) a chain with no verified checkpoint is an error: it is a
  fresh chain written without the key. ``expect_genesis`` (recorded off-box at provisioning) catches a
  replaced chain even when the key next to it was replaced too. Neither catches a rollback to a genuine
  prefix; only ``expect_head`` does, and a report without one is ``anchored == False``.
* Records after the last verified checkpoint are counted and reported; more than ``max_unsigned_records``
  of them is an error because the writer would have signed a checkpoint by then. Within that bound they
  are the live tail, which an attacker without an off-box anchor can still rewrite or drop
  (docs/runbooks/audit.md). There is deliberately no wall-clock rule (known issue N1): ``ts`` is chosen
  by whoever wrote the record, so it only ever constrained honest writers, and the writer's hourly timer
  is monotonic and restarts with the process. The writer signs any unsigned tail when it starts instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from atlas.audit.keys import KeyFileError, key_id_for, load_public_key, verify_checkpoint
from atlas.audit.record import ZERO_HASH, record_hash, validate_record
from atlas.audit.segments import (
    CHECKPOINT_SIDECAR,
    PUBLIC_KEY_FILE,
    list_segments,
    parse_record_line,
    read_lines,
)

DEFAULT_MAX_UNSIGNED_RECORDS = 1000  # == the writer's default checkpoint_every


@dataclass(frozen=True)
class OutcomeUnknown:
    """A ``write_ahead`` with no matching ``write_result``: the action may or may not have happened."""

    seq: int
    ts: int
    approval_id: str
    tool: str
    action_id: str


@dataclass
class VerifyReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    records: int = 0
    segments: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    head_hash: str | None = None
    checkpoints_verified: int = 0
    records_after_last_checkpoint: int = 0
    outcome_unknown: list[OutcomeUnknown] = field(default_factory=list)
    genesis_hash: str | None = None  # set when the chain still starts at seq 0
    anchored: bool = False  # an off-box expect_head was checked (truncation and rollback are covered)

    @property
    def ok(self) -> bool:
        return not self.errors


def find_outcome_unknown(records: Iterable[dict[str, Any]]) -> list[OutcomeUnknown]:
    """``write_ahead`` records never followed by a ``write_result`` for the same approval and tool."""
    pending: dict[tuple[str, str], OutcomeUnknown] = {}
    for rec in records:
        kind = rec.get("kind")
        action = rec.get("action")
        if kind not in ("write_ahead", "write_result") or not isinstance(action, dict):
            continue
        key = (str(action.get("approval_id")), str(rec.get("tool")))
        if kind == "write_ahead":
            pending[key] = OutcomeUnknown(
                seq=int(rec["seq"]),
                ts=int(rec["ts"]),
                approval_id=key[0],
                tool=key[1],
                action_id=str(action.get("action_id")),
            )
        else:
            pending.pop(key, None)
    return sorted(pending.values(), key=lambda o: o.seq)


def stream_write_records(data_dir: Path) -> Iterator[dict[str, Any]]:
    """Yield only write_ahead/write_result records, reading each segment line by line (constant memory).

    Safe while the writer appends: an unterminated last line is skipped. Unparseable lines are skipped here
    (this is not the integrity check; ``verify_dir`` is).
    """
    for _, path in list_segments(data_dir):
        with path.open("rb") as handle:
            for raw in handle:
                if not raw.endswith(b"\n") or (b'"write_ahead"' not in raw and b'"write_result"' not in raw):
                    continue
                try:
                    yield parse_record_line(raw.rstrip(b"\n"))
                except ValueError:
                    continue


def outcome_unknown_in_dir(data_dir: Path) -> list[OutcomeUnknown]:
    return find_outcome_unknown(stream_write_records(data_dir))


def _read_sidecar(data_dir: Path, report: VerifyReport) -> list[dict[str, Any]] | None:
    path = data_dir / CHECKPOINT_SIDECAR
    if not path.is_file():
        return None
    lines, torn = read_lines(path)
    if torn:
        report.warnings.append("checkpoint sidecar has an unterminated final line (torn write), ignored")
    out: list[dict[str, Any]] = []
    for n, line in enumerate(lines, 1):
        try:
            out.append(parse_record_line(line))
        except ValueError as exc:
            report.errors.append(f"checkpoint sidecar line {n} unreadable: {exc}")
    return out


def load_records(data_dir: Path, report: VerifyReport) -> list[dict[str, Any]]:
    """Read every segment strictly. Structural problems are added to ``report`` and stop the read."""
    records: list[dict[str, Any]] = []
    segments = list_segments(data_dir)
    report.segments = len(segments)
    for index, (first_seq, path) in enumerate(segments):
        lines, torn = read_lines(path)
        if torn:
            report.warnings.append(f"{path.name}: unterminated final line (torn write), ignored")
        if not lines:
            if index != len(segments) - 1:
                report.errors.append(f"{path.name}: empty segment before the end of the chain")
            continue
        for n, line in enumerate(lines, 1):
            try:
                rec = parse_record_line(line)
            except ValueError as exc:
                report.errors.append(f"{path.name}:{n}: unreadable record: {exc}")
                return records
            if n == 1 and rec.get("seq") != first_seq:
                report.errors.append(f"{path.name}: file name says first seq {first_seq}, record says {rec.get('seq')}")
                return records
            records.append(rec)
    return records


def verify_dir(
    data_dir: Path,
    public_key: Ed25519PublicKey | None = None,
    expect_head: tuple[int, str] | None = None,
    *,
    max_unsigned_records: int = DEFAULT_MAX_UNSIGNED_RECORDS,
    expect_genesis: str | None = None,
    require_checkpoint: bool = False,
) -> VerifyReport:
    report = VerifyReport(anchored=expect_head is not None)
    if public_key is None:
        key_path = data_dir / PUBLIC_KEY_FILE
        try:
            public_key = load_public_key(key_path.read_text(encoding="ascii"))
        except (OSError, UnicodeDecodeError, KeyFileError) as exc:
            report.errors.append(f"no usable checkpoint public key ({PUBLIC_KEY_FILE}): {exc}")
            return report
    records = load_records(data_dir, report)
    if not records:
        if not report.errors:
            report.errors.append("no audit records found")
        return report

    key_id = key_id_for(public_key)
    report.first_seq = int(records[0]["seq"])
    chain_checkpoints: list[dict[str, Any]] = []
    purges: list[dict[str, Any]] = []
    prev_hash = records[0].get("prev_hash")
    prev_seq = report.first_seq - 1
    hashes: dict[int, str] = {}
    last_cp_record: dict[str, Any] | None = None
    for rec in records:
        problems = validate_record(rec)
        seq = rec.get("seq")
        if problems:
            report.errors.append(f"seq {seq}: schema violation: {problems[0]}")
            return report
        if seq != prev_seq + 1:
            report.errors.append(f"seq gap: expected {prev_seq + 1}, found {seq} (record deleted or reordered)")
            return report
        if rec["prev_hash"] != prev_hash:
            report.errors.append(f"seq {seq}: prev_hash does not match the previous record (chain broken)")
            return report
        if rec["hash"] != record_hash(rec):
            report.errors.append(f"seq {seq}: hash does not match the record contents (record modified)")
            return report
        if seq == 0 and rec["kind"] != "genesis":
            report.errors.append("seq 0 is not a genesis record")
            return report
        if rec["kind"] == "genesis" and seq != 0:
            report.errors.append(f"seq {seq}: genesis record in the middle of the chain")
            return report
        if rec["kind"] == "checkpoint":
            cp = rec["checkpoint"]
            if cp["to_seq"] != seq - 1 or cp["from_seq"] > cp["to_seq"]:
                report.errors.append(f"seq {seq}: checkpoint range is inconsistent")
                return report
            if cp["to_seq"] in hashes and hashes[cp["to_seq"]] != cp["head_hash"]:
                report.errors.append(f"seq {seq}: checkpoint head_hash does not match record {cp['to_seq']}")
                return report
            if cp["key_id"] != key_id:
                report.errors.append(f"seq {seq}: checkpoint signed with unknown key {cp['key_id']}")
                return report
            if not verify_checkpoint(public_key, cp["to_seq"], cp["head_hash"], cp["signature"]):
                report.errors.append(f"seq {seq}: checkpoint signature is invalid")
                return report
            report.checkpoints_verified += 1
            chain_checkpoints.append(rec)
            last_cp_record = rec
        elif rec["kind"] == "purge":
            purges.append(rec)
        hashes[seq] = rec["hash"]
        prev_hash = rec["hash"]
        prev_seq = seq
        report.records += 1

    report.last_seq = prev_seq
    report.head_hash = prev_hash if isinstance(prev_hash, str) else None
    last_signed_to = last_cp_record["checkpoint"]["to_seq"] if last_cp_record else -1
    covered = [p for p in purges if p["seq"] <= last_signed_to]  # a verified checkpoint commits to these

    first = records[0]
    if first["seq"] != 0:
        anchored = any(
            p["purge"]["to_seq"] + 1 == first["seq"] and p["purge"]["anchor_hash"] == first["prev_hash"]
            for p in covered
        )
        if not anchored:
            report.errors.append(
                f"chain starts at seq {first['seq']} but no purge record covered by a signed checkpoint anchors it "
                "(segments deleted, or a forged purge)"
            )
    elif first["prev_hash"] != ZERO_HASH:
        report.errors.append("genesis prev_hash is not 64 zeros")
    else:
        report.genesis_hash = first["hash"]
    if expect_genesis is not None:
        if first["seq"] != 0:
            report.warnings.append(
                "the genesis record has been purged: --expect-genesis is not checked directly; "
                "the signed purge anchor stands in for it (use --pubkey)"
            )
        elif first["hash"] != expect_genesis:
            report.errors.append("genesis record differs from the expected off-box genesis hash: log replaced")
    if require_checkpoint and report.checkpoints_verified == 0:
        report.errors.append(
            "no checkpoint verifies with the trusted key: the writer signs one right after genesis, "
            "so this chain was written without the key (forged or replaced log)"
        )

    _check_tail(report, records, last_cp_record, max_unsigned_records)
    _check_sidecar(data_dir, report, chain_checkpoints, hashes, covered, records, max_unsigned_records)
    if expect_head is not None:
        _check_expected_head(report, expect_head, hashes, covered)
    report.outcome_unknown = find_outcome_unknown(records)
    return report


def _check_tail(
    report: VerifyReport,
    records: list[dict[str, Any]],
    last_cp_record: dict[str, Any] | None,
    max_records: int,
) -> None:
    after = (records[-1]["seq"] - last_cp_record["seq"]) if last_cp_record else len(records)
    report.records_after_last_checkpoint = after
    if after > max_records:
        report.errors.append(
            f"{after} records after the last verified checkpoint (limit {max_records}): the writer would have signed one"
        )


def _covers(covered: list[dict[str, Any]], seq: int) -> dict[str, Any] | None:
    """The covered purge that accounts for ``seq``: history up to its ``to_seq`` was removed (an earlier purge
    record may itself have been purged by a later one, so only the upper bound is meaningful)."""
    found = [p for p in covered if seq <= p["purge"]["to_seq"]]
    return min(found, key=lambda p: p["purge"]["to_seq"]) if found else None


def _check_expected_head(
    report: VerifyReport, expect_head: tuple[int, str], hashes: dict[int, str], covered: list[dict[str, Any]]
) -> None:
    want_seq, want_hash = expect_head
    last = report.last_seq if report.last_seq is not None else -1
    if want_seq > last:
        report.errors.append(f"chain is shorter than the expected head (seq {last} < {want_seq}): truncated")
    elif want_seq in hashes:
        if hashes[want_seq] != want_hash:
            report.errors.append(f"record {want_seq} differs from the expected off-box hash")
    else:
        purge = _covers(covered, want_seq)
        if purge is None:
            report.errors.append(
                f"expected head seq {want_seq} predates the chain and no signed purge accounts for it: log replaced"
            )
        elif want_seq == purge["purge"]["to_seq"] and want_hash != purge["purge"]["anchor_hash"]:
            report.errors.append(f"expected head {want_seq} differs from the hash anchored by the signed purge")


def _check_sidecar(
    data_dir: Path,
    report: VerifyReport,
    chain_checkpoints: list[dict[str, Any]],
    hashes: dict[int, str],
    covered: list[dict[str, Any]],
    records: list[dict[str, Any]],
    max_records: int,
) -> None:
    sidecar = _read_sidecar(data_dir, report)
    if not sidecar:
        long_chain = len(records) > max_records
        if chain_checkpoints and (sidecar is None or len(chain_checkpoints) > 1):
            report.errors.append(f"{CHECKPOINT_SIDECAR} is missing or empty")
        elif long_chain:
            report.errors.append(f"{CHECKPOINT_SIDECAR} is missing or empty on a chain long enough to have checkpoints")
        return
    first_seq = report.first_seq or 0
    last_seq = report.last_seq if report.last_seq is not None else -1
    sidecar_hashes: set[str] = set()
    for entry in sidecar:
        seq = entry.get("seq")
        if not isinstance(seq, int) or not isinstance(entry.get("hash"), str):
            report.errors.append("checkpoint sidecar entry is malformed")
            continue
        sidecar_hashes.add(entry["hash"])
        if seq > last_seq:
            report.errors.append(
                f"chain ends at seq {last_seq} but checkpoint {seq} was recorded in the sidecar: log truncated"
            )
        elif seq < first_seq:
            if _covers(covered, seq) is None:
                report.errors.append(
                    f"sidecar records checkpoint {seq} below the chain start {first_seq} and no signed purge covers it"
                )
        elif hashes.get(seq) != entry["hash"]:
            report.errors.append(f"checkpoint at seq {seq} differs between the chain and the sidecar")
    missing = [cp for cp in chain_checkpoints if cp["hash"] not in sidecar_hashes]
    if len(missing) > 1 or (missing and missing[0] is not chain_checkpoints[-1]):
        report.errors.append("checkpoints in the chain are missing from the sidecar")
