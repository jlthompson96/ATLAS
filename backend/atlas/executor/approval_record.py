"""Approval record, version 1: what atlas-core signs after a human approves, and atlas-executor verifies.

This format is internal between atlas-core and atlas-executor (both on the Pi). It is not in
``contracts/`` today; docs/adr/0001-privilege-model.md records it and why.

Record (a JSON object, every member required, no others)::

    {
      "v": 1,
      "type": "atlas.approval",
      "approval_id": "apr-<32 hex>",
      "action_id": "<events.v1 Id>",
      "action_kind": "email.send" | "discord.post" | "research.schedule" | "portfolio.import",
      "payload_sha256": "<64 hex>",           SHA-256 of payload_canonical's UTF-8 bytes
      "origin_context": "interactive" | "unattended",   context that *proposed* the action
      "decision_context": "interactive",      approvals are always made by a present human
      "approved_via": "hud",                  the only channel that can approve (RT-13)
      "device_id": "<events.v1 Id>",
      "decision": "approve",
      "step_up": true,
      "action_created_at": <epoch ms>,
      "decided_at": <epoch ms>,               strictly after action_created_at
      "expires_at": <epoch ms>,               decided_at < expires_at <= decided_at + 10 min
      "nonce": "<32 hex>",                    single use, enforced by the executor's nonce store
      "key_id": "ak-<16 hex>"                 SHA-256 of the raw public key, first 16 hex
    }

Signature: Ed25519 (``approval-signing-key``, held by atlas-core only; the public key is in the
executor's config) over ``b"atlas-approval-v1\\n" + JCS(record)``, as unpadded base64url (86 chars).

Envelope on ``executor.sock`` (one JSON line)::

    {"op": "execute", "approval": <record>, "signature": "<86 chars>", "payload_canonical": "<JCS text>"}

Bumping the format means a new ``v`` and ``DOMAIN``; the executor accepts exactly one version.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from atlas.audit.jcs import canonicalize
from atlas.audit.keys import b64url, b64url_decode, public_key_raw

VERSION: Final = 1
RECORD_TYPE: Final = "atlas.approval"
DOMAIN: Final = b"atlas-approval-v1\n"
MAX_APPROVAL_TTL_MS: Final = 10 * 60 * 1000

_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_HEX32: Final = re.compile(r"[0-9a-f]{32}")
_SHA: Final = re.compile(r"[0-9a-f]{64}")
_KEY_ID: Final = re.compile(r"ak-[0-9a-f]{16}")
_APPROVAL_ID: Final = re.compile(r"apr-[0-9a-f]{32}")
_SIG: Final = re.compile(r"[A-Za-z0-9_-]{86}")
_KINDS: Final = frozenset({"email.send", "discord.post", "research.schedule", "portfolio.import"})
_MAX_MS: Final = 2**53 - 1


class RecordFormatError(ValueError):
    pass


class ApprovalKeyError(ValueError):
    """A signing or verification key is missing or malformed."""


def _int_ms(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_MS


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_id: str
    action_id: str
    action_kind: str
    payload_sha256: str
    origin_context: str
    decision_context: str
    approved_via: str
    device_id: str
    decision: str
    step_up: bool
    action_created_at: int
    decided_at: int
    expires_at: int
    nonce: str
    key_id: str
    v: int = VERSION
    type: str = RECORD_TYPE

    def to_json(self) -> dict[str, Any]:
        return {
            "v": self.v,
            "type": self.type,
            "approval_id": self.approval_id,
            "action_id": self.action_id,
            "action_kind": self.action_kind,
            "payload_sha256": self.payload_sha256,
            "origin_context": self.origin_context,
            "decision_context": self.decision_context,
            "approved_via": self.approved_via,
            "device_id": self.device_id,
            "decision": self.decision,
            "step_up": self.step_up,
            "action_created_at": self.action_created_at,
            "decided_at": self.decided_at,
            "expires_at": self.expires_at,
            "nonce": self.nonce,
            "key_id": self.key_id,
        }

    @classmethod
    def from_json(cls, data: Any) -> ApprovalRecord:
        """Strict parse: exact member set, exact types and formats. Semantic checks are the verifier's."""
        if not isinstance(data, dict):
            raise RecordFormatError("record is not an object")
        expected = set(FIELDS)
        if set(data) != expected:
            raise RecordFormatError("record members differ from version 1")
        checks: dict[str, bool] = {
            "v": data["v"] == VERSION and type(data["v"]) is int,
            "type": data["type"] == RECORD_TYPE,
            "approval_id": isinstance(data["approval_id"], str) and _APPROVAL_ID.fullmatch(data["approval_id"]) is not None,
            "action_id": isinstance(data["action_id"], str) and _ID.fullmatch(data["action_id"]) is not None,
            "action_kind": data["action_kind"] in _KINDS,
            "payload_sha256": isinstance(data["payload_sha256"], str) and _SHA.fullmatch(data["payload_sha256"]) is not None,
            "origin_context": data["origin_context"] in ("interactive", "unattended"),
            "decision_context": isinstance(data["decision_context"], str),
            "approved_via": isinstance(data["approved_via"], str),
            "device_id": isinstance(data["device_id"], str) and _ID.fullmatch(data["device_id"]) is not None,
            "decision": isinstance(data["decision"], str),
            "step_up": isinstance(data["step_up"], bool),
            "action_created_at": _int_ms(data["action_created_at"]),
            "decided_at": _int_ms(data["decided_at"]),
            "expires_at": _int_ms(data["expires_at"]),
            "nonce": isinstance(data["nonce"], str) and _HEX32.fullmatch(data["nonce"]) is not None,
            "key_id": isinstance(data["key_id"], str) and _KEY_ID.fullmatch(data["key_id"]) is not None,
        }
        bad = [name for name, ok in checks.items() if not ok]
        if bad:
            raise RecordFormatError(f"invalid member(s): {', '.join(bad)}")
        return cls(**{k: data[k] for k in FIELDS})


FIELDS: Final = (
    "v",
    "type",
    "approval_id",
    "action_id",
    "action_kind",
    "payload_sha256",
    "origin_context",
    "decision_context",
    "approved_via",
    "device_id",
    "decision",
    "step_up",
    "action_created_at",
    "decided_at",
    "expires_at",
    "nonce",
    "key_id",
)


def signing_message(record: dict[str, Any]) -> bytes:
    return DOMAIN + canonicalize(record)


def key_id_for(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    return "ak-" + hashlib.sha256(public_key_raw(key)).hexdigest()[:16]


def new_nonce() -> str:
    return secrets.token_hex(16)


def new_approval_id() -> str:
    return "apr-" + secrets.token_hex(16)


@dataclass(frozen=True, slots=True)
class SignedApproval:
    record: ApprovalRecord
    signature: str

    def envelope(self, payload_canonical: str) -> dict[str, Any]:
        return {
            "op": "execute",
            "approval": self.record.to_json(),
            "signature": self.signature,
            "payload_canonical": payload_canonical,
        }


class ApprovalSigner:
    """Held by atlas-core only (``approval-signing-key`` in systemd-creds)."""

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key
        self.key_id = key_id_for(key)

    def sign(self, record: ApprovalRecord) -> SignedApproval:
        if record.key_id != self.key_id:
            raise ValueError("record names a different key")
        ApprovalRecord.from_json(record.to_json())  # never sign something the executor would refuse to parse
        return SignedApproval(record, b64url(self._key.sign(signing_message(record.to_json()))))


def verify_signature(key: Ed25519PublicKey, record: dict[str, Any], signature: Any) -> bool:
    if not isinstance(signature, str) or _SIG.fullmatch(signature) is None:
        return False
    try:
        key.verify(b64url_decode(signature), signing_message(record))
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


def load_signing_key(path: Path) -> Ed25519PrivateKey:
    """PKCS#8 PEM, as ``openssl genpkey -algorithm ed25519`` writes it (systemd-creds on the Pi)."""
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise ApprovalKeyError("approval-signing-key is missing or not a PEM private key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise ApprovalKeyError("approval-signing-key is not an Ed25519 key")
    return key


def load_verify_key(text: str) -> Ed25519PublicKey:
    """The executor's configured public key: unpadded base64url of the 32 raw bytes."""
    try:
        return Ed25519PublicKey.from_public_bytes(b64url_decode(text.strip()))
    except ValueError as exc:
        raise ApprovalKeyError("approval public key is malformed") from exc


def public_key_text(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    return b64url(public_key_raw(key))
