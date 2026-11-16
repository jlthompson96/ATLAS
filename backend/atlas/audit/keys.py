"""Ed25519 checkpoint keys (the only use of the ``cryptography`` dependency)."""

from __future__ import annotations

import base64
import hashlib
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from atlas.audit.record import checkpoint_message

SIGNATURE_CHARS: Final = 86  # 64 raw bytes, unpadded base64url


class KeyFileError(Exception):
    """A key file is missing, malformed or of the wrong type."""


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    if not text.isascii() or "=" in text:
        raise ValueError("expected unpadded base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def load_private_key(pem: bytes) -> Ed25519PrivateKey:
    """Parse an unencrypted PKCS#8 PEM Ed25519 key (the format ``openssl genpkey -algorithm ed25519`` emits)."""
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError) as exc:
        raise KeyFileError("audit-checkpoint-key is not a valid PEM private key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise KeyFileError("audit-checkpoint-key is not an Ed25519 key")
    return key


def public_key_raw(key: Ed25519PrivateKey | Ed25519PublicKey) -> bytes:
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def public_key_b64(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    return b64url(public_key_raw(key))


def load_public_key(text: str) -> Ed25519PublicKey:
    """Parse the unpadded-base64url raw public key written to ``checkpoint.pub``."""
    try:
        return Ed25519PublicKey.from_public_bytes(b64url_decode(text.strip()))
    except ValueError as exc:
        raise KeyFileError("checkpoint public key is malformed") from exc


def key_id_for(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    """Stable identifier matching the schema's Id pattern: ``ck-`` + first 16 hex of SHA-256(public key)."""
    return "ck-" + hashlib.sha256(public_key_raw(key)).hexdigest()[:16]


def sign_checkpoint(key: Ed25519PrivateKey, to_seq: int, head_hash: str) -> str:
    return b64url(key.sign(checkpoint_message(to_seq, head_hash)))


def verify_checkpoint(key: Ed25519PublicKey, to_seq: int, head_hash: str, signature: str) -> bool:
    try:
        raw = b64url_decode(signature)
        key.verify(raw, checkpoint_message(to_seq, head_hash))
    except (InvalidSignature, ValueError):
        return False
    return True
