"""The hash-chained, segmented, append-only audit log writer (PLAN 2.12).

Single-writer, synchronous, not thread-safe: the server serializes calls (one lock) and runs them in a
worker thread so an fsync never blocks the event loop.

Durability policy (also the write-budget policy, PLAN 3.5):

* ``write()`` happens immediately for every record, so a process crash loses nothing acknowledged.
* Durable kinds (``record.is_durable``: write_ahead, write_result, approval, action_proposed, auth,
  config, checkpoint, purge, genesis, chain_broken/killswitch) are ``fsync``-ed before ``append``
  returns, so a ``write_ahead`` is on disk before the executor acts (FI-3).
* Everything else (tool_call, egress, power, other system events) is fsynced at most every
  ``flush_interval_s`` (30 s) or ``flush_bytes`` (64 KB) of unsynced data: a power cut loses at most
  that window of read-event records, never corrupts the chain (the unterminated tail is ignored).

Any I/O error poisons the log: every later call raises ``AuditUnavailable`` until restart, where
``open`` re-validates the active segment. A broken log never silently keeps going.

Phase 0 limits (documented, not hidden): zstd compression of sealed segments is not implemented (the
Pi's Python 3.13 has no stdlib zstd); purge deletes whole sealed segments but real crypto-shredding
(per-segment data keys wrapped by ``audit-segment-master-key``) is not implemented; the records hold
only IDs and hashes (D7) and are stored in the clear, as the PLAN 2.7 exposure table says.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.audit.attrs import NoAttributes, SegmentAttributes
from atlas.audit.jcs import canonicalize
from atlas.audit.keys import key_id_for, public_key_b64, sign_checkpoint
from atlas.audit.record import (
    ZERO_HASH,
    is_durable,
    record_hash,
    validate_record,
    validate_submission,
)
from atlas.audit.segments import (
    CHECKPOINT_SIDECAR,
    PUBLIC_KEY_FILE,
    SEGMENTS_DIR,
    list_segments,
    parse_record_line,
    read_lines,
    segment_filename,
    segment_id,
)
from atlas.audit.verify import verify_dir

DEFAULT_FLUSH_INTERVAL_S: Final = 30.0
DEFAULT_FLUSH_BYTES: Final = 64 * 1024
DEFAULT_CHECKPOINT_EVERY: Final = 1000
DEFAULT_CHECKPOINT_INTERVAL_S: Final = 3600.0
DEFAULT_MAX_SEGMENT_BYTES: Final = 16 * 1024 * 1024
DEFAULT_MIN_FREE_BYTES: Final = 200 * 1024 * 1024  # refuse client records below this much free space


class AuditUnavailable(Exception):
    """The log cannot accept records (I/O failure earlier, or closed). Callers must treat this as a refusal."""


class AuditCorrupt(Exception):
    """The existing log failed validation at startup; the service refuses to run on top of it."""


class SubmissionRejected(Exception):
    """A client record was refused. ``code`` is a stable UPPER_SNAKE reason; ``detail`` is for logs only."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AuditConfig:
    data_dir: Path
    chain_id: str = "atlas-pi-01"
    flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S
    flush_bytes: int = DEFAULT_FLUSH_BYTES
    checkpoint_every: int = DEFAULT_CHECKPOINT_EVERY
    checkpoint_interval_s: float = DEFAULT_CHECKPOINT_INTERVAL_S
    max_segment_bytes: int = DEFAULT_MAX_SEGMENT_BYTES
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES


@dataclass(frozen=True)
class Appended:
    seq: int
    hash: str
    durable: bool


def _utc_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=UTC).strftime("%Y%m%d")


class AuditLog:
    def __init__(
        self,
        config: AuditConfig,
        private_key: Ed25519PrivateKey,
        *,
        attributes: SegmentAttributes | None = None,
        wall_clock_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = config
        self._key = private_key
        self._key_id = key_id_for(private_key)
        self._attrs: SegmentAttributes = attributes if attributes is not None else NoAttributes()
        self._now_ms = wall_clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._monotonic = monotonic
        self._fd: int | None = None
        self._active_path: Path | None = None
        self._active_day = ""
        self._active_size = 0
        self._head_seq = -1
        self._head_hash = ZERO_HASH
        self._last_cp_record_seq = -1
        self._last_cp_to_seq = -1
        self._unsynced = 0
        self._last_sync = monotonic()
        self._last_checkpoint_time = monotonic()
        self._failed = False
        self._closed = False

    # ---- lifecycle ---------------------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        config: AuditConfig,
        private_key: Ed25519PrivateKey,
        *,
        attributes: SegmentAttributes | None = None,
        wall_clock_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> AuditLog:
        log = cls(config, private_key, attributes=attributes, wall_clock_ms=wall_clock_ms, monotonic=monotonic)
        log._prepare_directories()
        log._write_public_key()
        log._recover()
        if log._head_seq < 0:
            log._append_internal(
                "genesis", {"genesis": {"chain_id": config.chain_id, "note": "chain created"}, "context": "system"}
            )
        # Sign whatever is unsigned before appending anything (known issue N1): after an unclean stop (power cut,
        # SIGKILL) the previous run's tail would otherwise wait for a cadence that restarts from zero. A fresh chain
        # gets its genesis signed the same way (G1), so even a new log carries a signature the off-box key can check.
        if log._head_seq > log._last_cp_record_seq:
            log.checkpoint_now()
        return log

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError):
            self._sync()
        self._close_active(seal=False)

    @property
    def failed(self) -> bool:
        """True after an I/O error: the service must exit so systemd restarts it and the log is re-validated."""
        return self._failed

    @property
    def head(self) -> tuple[int, str]:
        return self._head_seq, self._head_hash

    @property
    def data_dir(self) -> Path:
        return self._cfg.data_dir

    # ---- appending ---------------------------------------------------------------------------------

    def append(self, actor: str, body: dict[str, Any]) -> Appended:
        """Validate a client body and append it. ``actor`` MUST come from the socket peer credentials."""
        self._ensure_usable()
        if shutil.disk_usage(self._cfg.data_dir).free < self._cfg.min_free_bytes:
            raise SubmissionRejected("NO_SPACE", "free space below the configured floor")
        errors = validate_submission(body, actor)
        if errors:
            code = "ACTOR_MISMATCH" if "ACTOR_MISMATCH" in errors else "BAD_RECORD"
            raise SubmissionRejected(code, "; ".join(errors))
        candidate = {k: v for k, v in body.items() if k != "actor"}
        return self._append_record(actor, candidate)

    def _append_internal(self, kind: str, fields: dict[str, Any]) -> Appended:
        return self._append_record("atlas-audit", {"kind": kind, **fields})

    def append_system(self, event: str, code: str | None = None) -> Appended:
        system: dict[str, Any] = {"event": event}
        if code:
            system["code"] = code
        return self._append_internal("system", {"system": system, "context": "system"})

    def _append_record(self, actor: str, fields: dict[str, Any]) -> Appended:
        self._ensure_usable()
        record, line = self._prepare(actor, fields)  # nothing is written yet: a bad record never poisons the log
        try:
            return self._commit(record, line)
        except Exception:
            self._failed = True  # an error once the write has started: fail closed until a restart re-validates
            raise

    def _append_unchecked(self, actor: str, fields: dict[str, Any]) -> Appended:
        record, line = self._prepare(actor, fields)
        return self._commit(record, line)

    def _prepare(self, actor: str, fields: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        """Build, validate, hash and serialize one record. Raises only ``SubmissionRejected`` (no side effects)."""
        try:
            ts = self._now_ms()
            record: dict[str, Any] = {
                **fields,
                "v": 1,
                "seq": self._head_seq + 1,
                "ts": ts,
                "actor": actor,
                "prev_hash": self._head_hash,
            }
            problems = validate_record({**record, "hash": ZERO_HASH})  # shape first: hashing needs a canonical form
            if problems:
                raise SubmissionRejected("BAD_RECORD", "; ".join(problems[:5]))
            record["hash"] = record_hash(record)
            return record, canonicalize(record) + b"\n"
        except SubmissionRejected:
            raise
        except (RecursionError, ValueError, TypeError, KeyError, OverflowError) as exc:
            raise SubmissionRejected("BAD_RECORD", type(exc).__name__) from exc

    def _commit(self, record: dict[str, Any], line: bytes) -> Appended:
        ts = record["ts"]
        self._ensure_segment(record["seq"], ts, len(line))
        self._write_all(line)
        self._head_seq = record["seq"]
        self._head_hash = record["hash"]
        self._unsynced += len(line)
        durable = is_durable(record["kind"], record)
        if durable or self._flush_due():
            self._sync()
        if record["kind"] == "checkpoint":
            self._last_cp_record_seq = record["seq"]
            self._last_cp_to_seq = record["checkpoint"]["to_seq"]
        elif self._checkpoint_due():
            self._write_checkpoint()
        return Appended(seq=record["seq"], hash=record["hash"], durable=durable)

    # ---- durability and checkpoints ----------------------------------------------------------------

    def _flush_due(self) -> bool:
        return self._unsynced >= self._cfg.flush_bytes or (
            self._unsynced > 0 and self._monotonic() - self._last_sync >= self._cfg.flush_interval_s
        )

    def _sync(self) -> None:
        if self._fd is not None and self._unsynced:
            os.fsync(self._fd)
        self._unsynced = 0
        self._last_sync = self._monotonic()

    def tick(self) -> None:
        """Periodic housekeeping (called by the server timer): time-based fsync and checkpoint."""
        self._ensure_usable()
        try:
            if self._flush_due():
                self._sync()
            if self._checkpoint_due():
                self._write_checkpoint()
        except Exception:
            self._failed = True
            raise

    def _checkpoint_due(self) -> bool:
        since = self._head_seq - self._last_cp_record_seq
        if since <= 0 or self._head_seq < 0:
            return False
        if since >= self._cfg.checkpoint_every:
            return True
        return self._monotonic() - self._last_checkpoint_time >= self._cfg.checkpoint_interval_s

    def checkpoint_if_needed(self) -> None:
        """Sign a checkpoint if any record sits after the last one (used at clean shutdown)."""
        if self._head_seq > self._last_cp_record_seq:
            self.checkpoint_now()

    def checkpoint_now(self) -> Appended:
        self._ensure_usable()
        try:
            return self._write_checkpoint()
        except Exception:
            self._failed = True
            raise

    def _write_checkpoint(self) -> Appended:
        to_seq, head_hash = self._head_seq, self._head_hash
        checkpoint = {
            "from_seq": self._last_cp_to_seq + 1,
            "to_seq": to_seq,
            "head_hash": head_hash,
            "sig_alg": "ed25519",
            "key_id": self._key_id,
            "signature": sign_checkpoint(self._key, to_seq, head_hash),
        }
        self._last_checkpoint_time = self._monotonic()
        result = self._append_unchecked(
            "atlas-audit", {"kind": "checkpoint", "context": "system", "checkpoint": checkpoint}
        )
        # Sidecar copy: evidence of truncation that the chain itself cannot give.
        # (Written after the chain record is durable; a crash in between is repaired in _recover.)
        self._append_sidecar(result.seq, result.hash, checkpoint)
        return result

    def _append_sidecar(self, seq: int, record_hash_: str, checkpoint: dict[str, Any]) -> None:
        path = self._cfg.data_dir / CHECKPOINT_SIDECAR
        created = not path.exists()
        line = canonicalize({"seq": seq, "hash": record_hash_, "checkpoint": checkpoint}) + b"\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o640)
        try:
            if created:
                self._attrs.make_append_only(path)
            self._write_fd(fd, line)
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            self._fsync_dir(self._cfg.data_dir)

    # ---- segments ----------------------------------------------------------------------------------

    def _ensure_segment(self, seq: int, ts: int, incoming: int) -> None:
        day = _utc_day(ts)
        if self._fd is not None and (
            day != self._active_day or self._active_size + incoming > self._cfg.max_segment_bytes
        ):
            self._sync()
            self._close_active(seal=True)
        if self._fd is None:
            path = self._cfg.data_dir / SEGMENTS_DIR / segment_filename(seq)
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
            try:
                self._attrs.make_append_only(path)
            except OSError:
                os.close(fd)
                raise
            self._fd = fd
            self._active_path = path
            self._active_day = day
            self._active_size = 0
            self._fsync_dir(path.parent)

    def _close_active(self, *, seal: bool) -> None:
        fd, path = self._fd, self._active_path
        self._fd = None
        self._active_path = None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if seal and path is not None:
            self._attrs.seal(path)

    def _write_all(self, data: bytes) -> None:
        if self._fd is None:
            raise AuditUnavailable("no active segment")
        self._write_fd(self._fd, data)
        self._active_size += len(data)

    @staticmethod
    def _write_fd(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _ensure_usable(self) -> None:
        if self._closed:
            raise AuditUnavailable("audit log is closed")
        if self._failed:
            raise AuditUnavailable("audit log failed earlier; restart to re-validate")

    # ---- startup -----------------------------------------------------------------------------------

    def _prepare_directories(self) -> None:
        (self._cfg.data_dir / SEGMENTS_DIR).mkdir(parents=True, exist_ok=True, mode=0o750)

    def _write_public_key(self) -> None:
        path = self._cfg.data_dir / PUBLIC_KEY_FILE
        mine = public_key_b64(self._key)
        if path.exists():
            if path.read_text(encoding="ascii").strip() != mine:
                raise AuditCorrupt("checkpoint.pub does not match the configured checkpoint key")
            return
        path.write_text(mine + "\n", encoding="ascii")
        path.chmod(0o644)
        self._fsync_dir(self._cfg.data_dir)

    def _recover(self) -> None:
        """Find the chain head from the last segment (verifying it) and repair the checkpoint sidecar."""
        segments = list_segments(self._cfg.data_dir)
        # A last segment with no complete record (crash right after create, or a torn first record) was
        # never acknowledged: set it aside so its name can be reused.
        while segments:
            lines, _ = read_lines(segments[-1][1])
            if lines:
                break
            self._set_aside(segments.pop()[1])
        sidecar = self._cfg.data_dir / CHECKPOINT_SIDECAR
        if not segments:
            if sidecar.is_file() and sidecar.stat().st_size > 0:
                raise AuditCorrupt("no segments found but the checkpoint sidecar exists: log deleted?")
            return
        if segments[0][0] > 0:
            # A chain that does not start at genesis lives on a purge anchor: prove it end to end (signed coverage,
            # sidecar, tail) before appending to it. Review F1: a forged purge must not be resumed on.
            report = verify_dir(self._cfg.data_dir, self._key.public_key())
            if not report.ok:
                raise AuditCorrupt(f"purged chain does not verify: {report.errors[0]}")
        first_seq, path = segments[-1]
        lines, torn = read_lines(path)
        prev_hash: str | None = None
        if len(segments) > 1:
            prev_hash = self._last_hash_of(segments[-2][1])
            if prev_hash is None:
                raise AuditCorrupt(f"{segments[-2][1].name} is empty or unreadable")
        expected_seq = first_seq
        records: list[dict[str, Any]] = []
        for n, line in enumerate(lines, 1):
            try:
                rec = parse_record_line(line)
            except ValueError as exc:
                raise AuditCorrupt(f"{path.name}:{n}: {exc}") from exc
            if validate_record(rec) or rec["seq"] != expected_seq:
                raise AuditCorrupt(f"{path.name}:{n}: invalid record or seq gap")
            if prev_hash is not None and rec["prev_hash"] != prev_hash:
                raise AuditCorrupt(f"{path.name}:{n}: broken chain link")
            if rec["hash"] != record_hash(rec):
                raise AuditCorrupt(f"{path.name}:{n}: hash mismatch")
            prev_hash = rec["hash"]
            expected_seq += 1
            records.append(rec)
        self._head_seq = records[-1]["seq"]
        self._head_hash = records[-1]["hash"]
        for rec in records:
            if rec["kind"] == "checkpoint":
                self._last_cp_record_seq = rec["seq"]
                self._last_cp_to_seq = rec["checkpoint"]["to_seq"]
        if self._last_cp_record_seq < 0:
            self._load_last_checkpoint_from_sidecar()
        if not torn:
            self._reopen_last_segment(path, records[0], len(b"\n".join(lines)) + 1)
        # With a torn tail the file stays as it is (append-only); new records go to a fresh segment.
        self._cross_check_sidecar(records)
        self._repair_sidecar(records)
        self._last_sync = self._monotonic()

    def _set_aside(self, path: Path) -> None:
        self._attrs.unseal(path)
        path.rename(path.with_name(path.name + ".discarded"))
        self._fsync_dir(path.parent)

    def _reopen_last_segment(self, path: Path, first: dict[str, Any], size: int) -> None:
        self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        self._active_path = path
        self._active_day = _utc_day(int(first["ts"]))
        self._active_size = size

    def _last_hash_of(self, path: Path) -> str | None:
        lines, _ = read_lines(path)
        if not lines:
            return None
        try:
            rec = parse_record_line(lines[-1])
        except ValueError:
            return None
        value = rec.get("hash")
        return value if isinstance(value, str) else None

    def _load_last_checkpoint_from_sidecar(self) -> None:
        path = self._cfg.data_dir / CHECKPOINT_SIDECAR
        if not path.is_file():
            return
        lines, _ = read_lines(path)
        if lines:
            try:
                entry = parse_record_line(lines[-1])
                self._last_cp_record_seq = int(entry["seq"])
                self._last_cp_to_seq = int(entry["checkpoint"]["to_seq"])
            except (ValueError, KeyError, TypeError) as exc:
                raise AuditCorrupt("checkpoint sidecar is unreadable") from exc

    def _cross_check_sidecar(self, tail_records: list[dict[str, Any]]) -> None:
        """Refuse to resume on a chain that disagrees with the checkpoint sidecar (replaced or shortened log)."""
        path = self._cfg.data_dir / CHECKPOINT_SIDECAR
        if not path.is_file():
            if self._head_seq > self._cfg.checkpoint_every:
                raise AuditCorrupt("checkpoint sidecar is missing but the chain is long: log replaced?")
            return
        lines, _ = read_lines(path)
        if not lines:
            if self._head_seq > self._cfg.checkpoint_every:
                raise AuditCorrupt("checkpoint sidecar is empty but the chain is long: log replaced?")
            return
        try:
            entry = parse_record_line(lines[-1])
            seq, digest = int(entry["seq"]), str(entry["hash"])
        except (ValueError, KeyError, TypeError) as exc:
            raise AuditCorrupt("checkpoint sidecar is unreadable") from exc
        if seq > self._head_seq:
            raise AuditCorrupt(f"checkpoint {seq} is recorded in the sidecar but the chain ends at {self._head_seq}")
        for rec in tail_records:
            if rec["seq"] == seq and rec["hash"] != digest:
                raise AuditCorrupt(f"chain record {seq} differs from the checkpoint recorded in the sidecar")

    def _repair_sidecar(self, tail_records: list[dict[str, Any]]) -> None:
        """If a crash fell between a checkpoint reaching the chain and the sidecar, add the missing line."""
        checkpoints = [r for r in tail_records if r["kind"] == "checkpoint"]
        if not checkpoints:
            return
        last = checkpoints[-1]
        path = self._cfg.data_dir / CHECKPOINT_SIDECAR
        known: set[str] = set()
        if path.is_file():
            lines, _ = read_lines(path)
            for line in lines:
                with contextlib.suppress(ValueError, KeyError, TypeError):
                    known.add(str(parse_record_line(line)["hash"]))
        if last["hash"] not in known:
            self._append_sidecar(last["seq"], last["hash"], last["checkpoint"])

    # ---- purge (retention / user deletion) ---------------------------------------------------------

    def purge_segments(self, segment_ids: list[str], reason: str) -> Appended:
        """Delete the oldest sealed segments and record a ``purge`` that re-anchors the chain.

        Order matters: the purge record (with the anchor hash) is appended and fsynced *first*, then the
        files are removed, so a crash in between leaves a verifiable chain with extra segments. The
        ``segments`` in the record are the IDs whose data was destroyed.

        NOT crypto-shredding yet (Phase 0): the plaintext files are unlinked. When per-segment data keys
        arrive (audit-segment-master-key), destroying the key replaces the unlink.
        """
        self._ensure_usable()
        if reason not in ("retention", "user_request"):
            raise ValueError("reason must be retention or user_request")
        segments = list_segments(self._cfg.data_dir)
        by_id = {segment_id(first): (first, path) for first, path in segments}
        if not segment_ids or any(s not in by_id for s in segment_ids):
            raise ValueError("unknown segment id")
        oldest = [segment_id(first) for first, _ in segments]
        if segment_ids != oldest[: len(segment_ids)] or len(segment_ids) >= len(oldest):
            raise ValueError("purge must cover the oldest segments in order and never the active segment")
        last_path = by_id[segment_ids[-1]][1]
        lines, _ = read_lines(last_path)
        last_record = parse_record_line(lines[-1])
        from_seq = by_id[segment_ids[0]][0]
        purge = {
            "reason": reason,
            "from_seq": from_seq,
            "to_seq": last_record["seq"],
            "anchor_hash": last_record["hash"],
            "segments": list(segment_ids),
        }
        result = self._append_record("atlas-audit", {"kind": "purge", "context": "system", "purge": purge})
        # A purge record is trusted only once a signed checkpoint covers it (verify.py): sign one before any unlink.
        self.checkpoint_now()
        for sid in segment_ids:
            path = by_id[sid][1]
            self._attrs.unseal(path)
            path.unlink()
        self._fsync_dir(self._cfg.data_dir / SEGMENTS_DIR)
        return result


def load_body(raw: bytes) -> dict[str, Any]:
    """Parse a submitted body strictly (duplicate keys, floats and NaN are rejected)."""
    try:
        return parse_record_line(raw)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SubmissionRejected("BAD_JSON", str(exc)) from exc
