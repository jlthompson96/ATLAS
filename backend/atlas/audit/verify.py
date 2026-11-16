"""Offline verification of an audit directory (PT-13) and outcome-unknown detection (FI-3).

``verify_dir`` re-derives everything from the files: record schema, hash chain, gap-free ``seq`` across
segments, purge anchors, Ed25519 checkpoint signatures, the checkpoint sidecar (truncation evidence) and
an optional off-box ``expect_head``. It never writes. Fails closed: any doubt is an error.

Limits that no on-box check can remove (hence the nightly off-box copy of checkpoints, PLAN 2.12):
dropping the tail after the last checkpoint, or replacing the key *and* the whole chain together.
"""

from __future__ import annotations

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
    outcome_unknown: list[OutcomeUnknown] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def find_outcome_unknown(records: list[dict[str, Any]]) -> list[OutcomeUnknown]:
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
) -> VerifyReport:
    report = VerifyReport()
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
        elif rec["kind"] == "purge":
            purges.append(rec)
        hashes[seq] = rec["hash"]
        prev_hash = rec["hash"]
        prev_seq = seq
        report.records += 1

    first = records[0]
    if first["seq"] != 0:
        anchored = any(
            p["purge"]["to_seq"] + 1 == first["seq"] and p["purge"]["anchor_hash"] == first["prev_hash"] for p in purges
        )
        if not anchored:
            report.errors.append(
                f"chain starts at seq {first['seq']} but no purge record anchors it (segments deleted without a purge)"
            )
    elif first["prev_hash"] != ZERO_HASH:
        report.errors.append("genesis prev_hash is not 64 zeros")

    report.last_seq = prev_seq
    report.head_hash = prev_hash if isinstance(prev_hash, str) else None
    _check_sidecar(data_dir, report, chain_checkpoints, hashes)
    if expect_head is not None:
        want_seq, want_hash = expect_head
        if want_seq > prev_seq:
            report.errors.append(f"chain is shorter than the expected head (seq {prev_seq} < {want_seq}): truncated")
        elif want_seq in hashes and hashes[want_seq] != want_hash:
            report.errors.append(f"record {want_seq} differs from the expected off-box hash")
        elif want_seq not in hashes:
            report.warnings.append(f"expected head seq {want_seq} predates the surviving chain (purged)")
    report.outcome_unknown = find_outcome_unknown(records)
    return report


def _check_sidecar(
    data_dir: Path, report: VerifyReport, chain_checkpoints: list[dict[str, Any]], hashes: dict[int, str]
) -> None:
    sidecar = _read_sidecar(data_dir, report)
    if sidecar is None:
        if chain_checkpoints:
            report.errors.append(f"{CHECKPOINT_SIDECAR} is missing")
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
        elif seq >= first_seq and hashes.get(seq) != entry["hash"]:
            report.errors.append(f"checkpoint at seq {seq} differs between the chain and the sidecar")
    missing = [cp for cp in chain_checkpoints if cp["hash"] not in sidecar_hashes]
    if len(missing) > 1 or (missing and missing[0] is not chain_checkpoints[-1]):
        report.errors.append("checkpoints in the chain are missing from the sidecar")
