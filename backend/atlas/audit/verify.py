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

Memory (known issue N6). The writer runs ``verify_dir`` at every start on a purged chain, which after a year of
retention is up to millions of records. It streams them in one pass and keeps only what a later check looks up:
checkpoint hashes, the last hash of each segment, purge records and unmatched ``write_ahead`` records. So memory
does not depend on the number of records; time still does (about 18 µs a record on the dev Mac).
"""

from __future__ import annotations

from collections.abc import Generator, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from atlas.audit.keys import KeyFileError, key_id_for, load_public_key, verify_checkpoint
from atlas.audit.record import ZERO_HASH, record_hash, validate_record
from atlas.audit.segments import (
    CHECKPOINT_SIDECAR,
    PUBLIC_KEY_FILE,
    iter_lines,
    list_segments,
    parse_record_line,
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
    purged_through: int = -1  # highest to_seq of a purge covered by a verified checkpoint (-1: none)
    anchored: bool = False  # an off-box expect_head was checked (truncation and rollback are covered)

    @property
    def ok(self) -> bool:
        return not self.errors


class _OutcomeTracker:
    """Pairs ``write_ahead`` with ``write_result`` one record at a time (the verifier never holds the chain)."""

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], OutcomeUnknown] = {}

    def feed(self, rec: dict[str, Any]) -> None:
        kind = rec.get("kind")
        action = rec.get("action")
        if kind not in ("write_ahead", "write_result") or not isinstance(action, dict):
            return
        key = (str(action.get("approval_id")), str(rec.get("tool")))
        if kind == "write_ahead":
            self._pending[key] = OutcomeUnknown(
                seq=int(rec["seq"]),
                ts=int(rec["ts"]),
                approval_id=key[0],
                tool=key[1],
                action_id=str(action.get("action_id")),
            )
        else:
            self._pending.pop(key, None)

    def result(self) -> list[OutcomeUnknown]:
        return sorted(self._pending.values(), key=lambda o: o.seq)


def find_outcome_unknown(records: Iterable[dict[str, Any]]) -> list[OutcomeUnknown]:
    """``write_ahead`` records never followed by a ``write_result`` for the same approval and tool."""
    tracker = _OutcomeTracker()
    for rec in records:
        tracker.feed(rec)
    return tracker.result()


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


def _read_sidecar(data_dir: Path, report: VerifyReport) -> list[tuple[int, str] | None] | None:
    """The sidecar reduced to what the checks need: ``(seq, hash)`` per parsed line, ``None`` for a parsed but
    malformed entry. Read after the chain, as the writer writes the chain first (a live log never looks truncated)."""
    path = data_dir / CHECKPOINT_SIDECAR
    if not path.is_file():
        return None
    entries: list[tuple[int, str] | None] = []
    n = 0
    try:
        for line in iter_lines(path):
            n += 1
            if not line.endswith(b"\n"):
                report.warnings.append("checkpoint sidecar has an unterminated final line (torn write), ignored")
                break
            try:
                entry = parse_record_line(line[:-1])
            except ValueError as exc:
                report.errors.append(f"checkpoint sidecar line {n} unreadable: {exc}")
                continue
            seq, digest = entry.get("seq"), entry.get("hash")
            entries.append((seq, digest) if isinstance(seq, int) and isinstance(digest, str) else None)
    except ValueError as exc:  # iter_lines: a line too long to be one the writer produced
        report.errors.append(f"checkpoint sidecar line {n + 1} unreadable: {exc}")
    return entries


def _stream_records(segments: list[tuple[int, Path]], report: VerifyReport) -> Generator[tuple[int, dict[str, Any]]]:
    """Yield ``(segment index, record)`` for every segment, strictly, one line at a time (known issue N6).

    Structural problems are added to ``report`` and end the stream; the caller validates what it was given.
    """
    last_index = len(segments) - 1
    for index, (first_seq, path) in enumerate(segments):
        n = 0
        try:
            for line in iter_lines(path):
                if not line.endswith(b"\n"):
                    report.warnings.append(f"{path.name}: unterminated final line (torn write), ignored")
                    break
                n += 1
                try:
                    rec = parse_record_line(line[:-1])
                except ValueError as exc:
                    report.errors.append(f"{path.name}:{n}: unreadable record: {exc}")
                    return
                if n == 1 and rec.get("seq") != first_seq:
                    report.errors.append(
                        f"{path.name}: file name says first seq {first_seq}, record says {rec.get('seq')}"
                    )
                    return
                yield index, rec
        except ValueError as exc:  # iter_lines: a line too long to be one the writer produced
            report.errors.append(f"{path.name}:{n + 1}: unreadable record: {exc}")
            return
        if n == 0 and index != last_index:
            report.errors.append(f"{path.name}: empty segment before the end of the chain")


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

    # Known issue N6: one pass, memory independent of the number of records. Only the hashes a later check looks
    # up are kept: every checkpoint record (all the sidecar ever names), expect_head's seq, and the last record of
    # each segment (a purge always ends on a segment boundary, so that is where a signed anchor_hash points).
    kept: dict[int, str] = {}
    segments = list_segments(data_dir)
    report.segments = len(segments)

    key_id = key_id_for(public_key)
    checkpoint_hashes: list[str] = []
    purges: list[dict[str, Any]] = []
    outcomes = _OutcomeTracker()
    first: dict[str, Any] | None = None
    prev_hash: Any = None
    prev_seq = -1
    last_cp_seq: int | None = None
    last_signed_to = -1
    segment_last: tuple[int, int, str] | None = None  # (segment index, seq, hash) of the newest record
    stream = _stream_records(segments, report)
    try:
        for index, rec in stream:
            problems = validate_record(rec)
            seq = rec.get("seq")
            if problems:
                report.errors.append(f"seq {seq}: schema violation: {problems[0]}")
                return report
            if first is None:
                first = rec
                report.first_seq = int(rec["seq"])
                prev_hash = rec["prev_hash"]
                prev_seq = report.first_seq - 1
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
                # to_seq is the previous record; when it is in the chain its hash is prev_hash (already linked)
                if seq != report.first_seq and cp["head_hash"] != prev_hash:
                    report.errors.append(f"seq {seq}: checkpoint head_hash does not match record {cp['to_seq']}")
                    return report
                if cp["key_id"] != key_id:
                    report.errors.append(f"seq {seq}: checkpoint signed with unknown key {cp['key_id']}")
                    return report
                if not verify_checkpoint(public_key, cp["to_seq"], cp["head_hash"], cp["signature"]):
                    report.errors.append(f"seq {seq}: checkpoint signature is invalid")
                    return report
                report.checkpoints_verified += 1
                checkpoint_hashes.append(rec["hash"])
                kept[seq] = rec["hash"]
                last_cp_seq = seq
                last_signed_to = cp["to_seq"]
            elif rec["kind"] == "purge":
                purges.append(rec)
            outcomes.feed(rec)
            if segment_last is not None and segment_last[0] != index:
                kept[segment_last[1]] = segment_last[2]
            segment_last = (index, seq, rec["hash"])
            if expect_head is not None and seq == expect_head[0]:
                kept[seq] = rec["hash"]
            prev_hash = rec["hash"]
            prev_seq = seq
            report.records += 1
    finally:
        stream.close()
    if segment_last is not None:
        kept[segment_last[1]] = segment_last[2]

    if first is None:
        if not report.errors:
            report.errors.append("no audit records found")
        return report

    report.last_seq = prev_seq
    report.head_hash = prev_hash if isinstance(prev_hash, str) else None
    covered = [p for p in purges if p["seq"] <= last_signed_to]  # a verified checkpoint commits to these

    report.purged_through = max((p["purge"]["to_seq"] for p in covered), default=-1)
    if first["seq"] != 0:
        # Anchored right after the purged range, or (known issue N2: a crash between the purge's unlinks) anywhere
        # inside it, as long as the surviving record at to_seq is the one whose hash the signed purge anchors.
        anchored = any(
            (p["purge"]["to_seq"] + 1 == first["seq"] and p["purge"]["anchor_hash"] == first["prev_hash"])
            or (
                p["purge"]["from_seq"] <= first["seq"] <= p["purge"]["to_seq"]
                and kept.get(p["purge"]["to_seq"]) == p["purge"]["anchor_hash"]
            )
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

    _check_tail(report, last_cp_seq, max_unsigned_records)
    _check_sidecar(data_dir, report, checkpoint_hashes, kept, covered, max_unsigned_records)
    if expect_head is not None:
        _check_expected_head(report, expect_head, kept, covered)
    report.outcome_unknown = outcomes.result()
    return report


def _check_tail(report: VerifyReport, last_cp_seq: int | None, max_records: int) -> None:
    last = report.last_seq if report.last_seq is not None else -1
    after = (last - last_cp_seq) if last_cp_seq is not None else report.records
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
    report: VerifyReport, expect_head: tuple[int, str], kept: dict[int, str], covered: list[dict[str, Any]]
) -> None:
    want_seq, want_hash = expect_head
    first = report.first_seq if report.first_seq is not None else 0
    last = report.last_seq if report.last_seq is not None else -1
    if want_seq > last:
        report.errors.append(f"chain is shorter than the expected head (seq {last} < {want_seq}): truncated")
    elif want_seq >= first:  # in the chain, so verify_dir kept its hash
        if kept.get(want_seq) != want_hash:
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
    checkpoint_hashes: list[str],
    kept: dict[int, str],
    covered: list[dict[str, Any]],
    max_records: int,
) -> None:
    sidecar = _read_sidecar(data_dir, report)
    if not sidecar:
        long_chain = report.records > max_records
        if checkpoint_hashes and (sidecar is None or len(checkpoint_hashes) > 1):
            report.errors.append(f"{CHECKPOINT_SIDECAR} is missing or empty")
        elif long_chain:
            report.errors.append(f"{CHECKPOINT_SIDECAR} is missing or empty on a chain long enough to have checkpoints")
        return
    first_seq = report.first_seq or 0
    last_seq = report.last_seq if report.last_seq is not None else -1
    sidecar_hashes: set[str] = set()
    for entry in sidecar:
        if entry is None:
            report.errors.append("checkpoint sidecar entry is malformed")
            continue
        seq, digest = entry
        sidecar_hashes.add(digest)
        if seq > last_seq:
            report.errors.append(
                f"chain ends at seq {last_seq} but checkpoint {seq} was recorded in the sidecar: log truncated"
            )
        elif seq < first_seq:
            if _covers(covered, seq) is None:
                report.errors.append(
                    f"sidecar records checkpoint {seq} below the chain start {first_seq} and no signed purge covers it"
                )
        elif kept.get(seq) != digest:  # kept holds every checkpoint record; a sidecar line names nothing else
            report.errors.append(f"checkpoint at seq {seq} differs between the chain and the sidecar")
    # Record hashes are unique (each commits to its seq and predecessor), so comparing hashes is comparing records.
    missing = [h for h in checkpoint_hashes if h not in sidecar_hashes]
    if len(missing) > 1 or (missing and missing[0] != checkpoint_hashes[-1]):
        report.errors.append("checkpoints in the chain are missing from the sidecar")
