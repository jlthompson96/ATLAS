"""The real `python -m atlas.audit serve` process: starts from a credential file, listens, refuses unknown peers,
shuts down cleanly and leaves a verifiable chain. (Peers here are not atlas-* users, so appends are refused;
accepted appends over a real socket are covered with a fake user directory in test_audit_server.py.)"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.audit.verify import verify_dir

BACKEND = Path(__file__).resolve().parents[1]


def pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def environment(root: Path, sock: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(BACKEND),
        "CREDENTIALS_DIRECTORY": str(root / "creds"),
        "ATLAS_AUDIT_DATA_DIR": str(root / "audit"),
        "ATLAS_AUDIT_SOCKET": sock,
        "ATLAS_AUDIT_REQUIRE_APPEND_ONLY": "0",  # chattr is Linux-only; verified on the VM
    }


def test_serve_refuses_unknown_peers_and_shuts_down_cleanly() -> None:
    with tempfile.TemporaryDirectory(prefix="atl") as tmp:
        root = Path(tmp)
        (root / "creds").mkdir()
        (root / "creds" / "audit-checkpoint-key").write_bytes(pem(Ed25519PrivateKey.generate()))
        sock_path = str(root / "a.sock")
        proc = subprocess.Popen(
            [sys.executable, "-m", "atlas.audit", "serve"],
            env=environment(root, sock_path),
            cwd=BACKEND,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not Path(sock_path).exists():
                assert proc.poll() is None, proc.stderr.read() if proc.stderr else b""
                assert time.monotonic() < deadline, "server did not start"
                time.sleep(0.05)
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(5)
                client.connect(sock_path)
                client.sendall(b'{"op":"append","record":{"kind":"tool_call"}}\n')
                reply = json.loads(client.makefile().readline())
            assert reply == {"ok": False, "code": "PEER_REJECTED"}  # this process is not an atlas-* user
        finally:
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=10) == 0
        report = verify_dir(root / "audit")
        assert report.ok, report.errors
        assert report.records == 5  # genesis, its checkpoint, service_start, service_stop, a checkpoint of the tail
        assert report.records_after_last_checkpoint == 0
        assert (root / "audit" / "checkpoint.pub").is_file()


def test_serve_refuses_to_start_without_a_usable_key() -> None:
    with tempfile.TemporaryDirectory(prefix="atl") as tmp:
        root = Path(tmp)
        (root / "creds").mkdir()
        (root / "creds" / "audit-checkpoint-key").write_bytes(b"not a key")
        done = subprocess.run(
            [sys.executable, "-m", "atlas.audit", "serve"],
            env=environment(root, str(root / "a.sock")),
            cwd=BACKEND,
            capture_output=True,
            timeout=20,
            check=False,
        )
        assert done.returncode == 2
        assert not (root / "a.sock").exists()


def test_serve_requires_the_credentials_directory() -> None:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(BACKEND)}
    done = subprocess.run(
        [sys.executable, "-m", "atlas.audit", "serve"],
        env=env,
        cwd=BACKEND,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert done.returncode == 2
    assert b"CREDENTIALS_DIRECTORY" in done.stderr


def test_serve_refuses_when_chattr_is_required_but_unavailable() -> None:
    """On macOS (and any non-Linux host) the production default must fail closed instead of running unprotected."""
    if sys.platform.startswith("linux"):
        return  # on Linux the probe may legitimately succeed; the VM checklist covers it
    with tempfile.TemporaryDirectory(prefix="atl") as tmp:
        root = Path(tmp)
        (root / "creds").mkdir()
        (root / "creds" / "audit-checkpoint-key").write_bytes(pem(Ed25519PrivateKey.generate()))
        env = environment(root, str(root / "a.sock"))
        del env["ATLAS_AUDIT_REQUIRE_APPEND_ONLY"]
        done = subprocess.run(
            [sys.executable, "-m", "atlas.audit", "serve"],
            env=env,
            cwd=BACKEND,
            capture_output=True,
            timeout=20,
            check=False,
        )
        assert done.returncode != 0
        assert b"chattr" in done.stderr
