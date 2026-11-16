"""The contract's golden chain through the real verifier (no private key needed: it has no checkpoints)."""

from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.audit.jcs import canonicalize
from atlas.audit.keys import load_public_key, public_key_b64
from atlas.audit.verify import verify_dir
from tests.test_contracts import golden_chain


def test_verifier_accepts_the_golden_chain_from_the_contract(tmp_path: Path) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    chain = golden_chain()
    (segments / "seg-0000000000.jsonl").write_bytes(b"".join(canonicalize(r) + b"\n" for r in chain))
    report = verify_dir(tmp_path, load_public_key(public_key_b64(Ed25519PrivateKey.generate())))
    assert report.ok, report.errors
    assert report.records == len(chain)
    assert report.first_seq == 0


def test_verifier_flags_a_modified_golden_record(tmp_path: Path) -> None:
    segments = tmp_path / "segments"
    segments.mkdir()
    chain = golden_chain()
    chain[3]["context"] = "unattended"
    (segments / "seg-0000000000.jsonl").write_bytes(b"".join(canonicalize(r) + b"\n" for r in chain))
    report = verify_dir(tmp_path, load_public_key(public_key_b64(Ed25519PrivateKey.generate())))
    assert not report.ok
