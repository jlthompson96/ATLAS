"""Every check atlas-executor runs before acting (PLAN.md §2.9 rule 4; UW-3, AP-1, AP-3).

``ApprovalVerifier.verify`` takes the raw envelope from ``executor.sock`` and either returns a
``Verified`` value or raises ``Rejected`` with a stable code. In order:

1. envelope shape (exact keys, size) and record format (``ApprovalRecord.from_json``, exact members);
2. ``key_id`` is the configured key, and the Ed25519 signature verifies over the canonical record
   (a forged or edited record stops here: ``UNKNOWN_KEY`` / ``BAD_SIGNATURE``);
3. SHA-256 of ``payload_canonical`` equals the signed ``payload_sha256`` (AP-1: ``HASH_MISMATCH``);
   the payload is exactly canonical JCS, its kind matches the signed kind, it passes the payload
   rules, and the executor supports the kind;
4. the record is an approval: ``decision`` approve, ``step_up`` true, ``approved_via`` hud,
   ``decision_context`` interactive (``NOT_APPROVED``);
5. context rules: an action proposed in the unattended context needs a human decision made strictly
   later than the proposal (``CONTEXT_REJECTED``);
6. time: not decided in the future (beyond a small skew), not expired, and a lifetime no longer than
   10 minutes (``EXPIRED`` / ``NOT_YET_VALID``).

``claim`` then records the nonce, approval ID and action ID in a durable store; any repeat of any of
the three is ``REPLAYED``. The claim happens before the write-ahead audit record, so a crash can't
turn one approval into two sends.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from atlas.executor.approval_record import (
    MAX_APPROVAL_TTL_MS,
    ApprovalRecord,
    RecordFormatError,
    key_id_for,
    verify_signature,
)
from atlas.policy.payloads import EXECUTOR_KINDS, parse_canonical, validate_payload

MAX_ENVELOPE_BYTES: Final = 192 * 1024
CLOCK_SKEW_MS: Final = 5_000
ENVELOPE_KEYS: Final = frozenset({"op", "approval", "signature", "payload_canonical"})
SUPPORTED_KINDS: Final = frozenset(kind.value for kind in EXECUTOR_KINDS)


class Rejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Verified:
    record: ApprovalRecord
    payload: dict[str, Any]
    payload_canonical: str


class NonceStore:
    """Single-use claims, durable across restarts (sqlite, synchronous=FULL)."""

    def __init__(self, path: Path | str) -> None:
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS claims ("
            "nonce TEXT PRIMARY KEY, approval_id TEXT NOT NULL UNIQUE, action_id TEXT NOT NULL UNIQUE, "
            "expires_at INTEGER NOT NULL, claimed_at INTEGER NOT NULL)"
        )
        self._db.commit()

    def claim(self, record: ApprovalRecord, now_ms: int) -> bool:
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO claims (nonce, approval_id, action_id, expires_at, claimed_at) VALUES (?, ?, ?, ?, ?)",
                    (record.nonce, record.approval_id, record.action_id, record.expires_at, now_ms),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def count(self) -> int:
        return int(self._db.execute("SELECT count(*) FROM claims").fetchone()[0])

    def close(self) -> None:
        self._db.close()


class ApprovalVerifier:
    def __init__(
        self,
        public_key: Ed25519PublicKey,
        nonces: NonceStore,
        clock_ms: Callable[[], int],
        *,
        supported_kinds: frozenset[str] = SUPPORTED_KINDS,
    ) -> None:
        self._key = public_key
        self._key_id = key_id_for(public_key)
        self._nonces = nonces
        self._clock = clock_ms
        self._kinds = supported_kinds

    def verify(self, envelope: Any) -> Verified:
        if not isinstance(envelope, dict) or set(envelope) != ENVELOPE_KEYS or envelope.get("op") != "execute":
            raise Rejected("BAD_REQUEST")
        raw_record = envelope["approval"]
        try:
            record = ApprovalRecord.from_json(raw_record)
        except RecordFormatError:
            raise Rejected("BAD_RECORD") from None
        if not hmac.compare_digest(record.key_id, self._key_id):
            raise Rejected("UNKNOWN_KEY")
        if not verify_signature(self._key, record.to_json(), envelope["signature"]):
            raise Rejected("BAD_SIGNATURE")

        canonical = envelope["payload_canonical"]
        if not isinstance(canonical, str):
            raise Rejected("BAD_PAYLOAD")
        digest = hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()
        if not hmac.compare_digest(digest, record.payload_sha256):
            raise Rejected("HASH_MISMATCH")
        try:
            payload = parse_canonical(canonical)
        except ValueError:
            raise Rejected("NON_CANONICAL_PAYLOAD") from None
        if payload.get("kind") != record.action_kind:
            raise Rejected("KIND_MISMATCH")
        if record.action_kind not in self._kinds:
            raise Rejected("UNSUPPORTED_KIND")
        if validate_payload(payload):
            raise Rejected("BAD_PAYLOAD")

        if (
            record.decision != "approve"
            or record.step_up is not True
            or record.approved_via != "hud"
            or record.decision_context != "interactive"
        ):
            raise Rejected("NOT_APPROVED")
        if record.decided_at <= record.action_created_at:
            # Covers UW-3's "created from an unattended context without a later human approval", and
            # the same for interactive proposals: the decision must follow the proposal.
            raise Rejected("CONTEXT_REJECTED")

        now = self._clock()
        if record.decided_at > now + CLOCK_SKEW_MS:
            raise Rejected("NOT_YET_VALID")
        if record.expires_at <= record.decided_at or record.expires_at - record.decided_at > MAX_APPROVAL_TTL_MS:
            raise Rejected("EXPIRED")
        if now >= record.expires_at:
            raise Rejected("EXPIRED")
        return Verified(record, payload, canonical)

    def claim(self, verified: Verified) -> None:
        if not self._nonces.claim(verified.record, self._clock()):
            raise Rejected("REPLAYED")
