"""``python -m atlas.audit``: ``serve`` (the service), ``verify``, ``outcome-unknown``, ``append`` (CLI stub).

    python -m atlas.audit verify /var/lib/atlas/audit [--pubkey FILE] [--expect-head SEQ:HASH] [--expect-genesis HASH]
    python -m atlas.audit outcome-unknown /var/lib/atlas/audit
    python -m atlas.audit serve                      # reads its configuration from the environment
    echo '{"kind":"system","context":"system","system":{"event":"service_start"}}' | python -m atlas.audit append

Exit codes: 0 verified, 1 tampering or corruption detected, 2 usage or configuration error. A verified chain prints
``OK:`` when an off-box ``--expect-head`` was checked, and ``UNANCHORED:`` (still exit 0) without one: then a rollback
to an earlier genuine state, or a rewritten tail after the last checkpoint, cannot be ruled out.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
from pathlib import Path

from atlas.audit.attrs import LinuxChattr, NoAttributes, SegmentAttributes, probe_linux_chattr
from atlas.audit.client import DEFAULT_SOCKET, AuditClient, AuditError
from atlas.audit.keys import KeyFileError, load_private_key, load_public_key
from atlas.audit.log import AuditConfig, AuditCorrupt, AuditLog, AuditUnavailable
from atlas.audit.peercred import KernelPeerCredentials, PasswdDirectory
from atlas.audit.server import AuditServer, listen_fds_socket
from atlas.audit.verify import verify_dir

DEFAULT_DATA_DIR = Path("/var/lib/atlas/audit")
CREDENTIAL_NAME = "audit-checkpoint-key"


def _cmd_verify(args: argparse.Namespace) -> int:
    public_key = None
    if args.pubkey:
        try:
            public_key = load_public_key(Path(args.pubkey).read_text(encoding="ascii"))
        except (OSError, KeyFileError) as exc:
            print(f"cannot read public key: {exc}", file=sys.stderr)
            return 2
    expect = None
    if args.expect_head:
        seq_text, _, hash_text = args.expect_head.partition(":")
        if not seq_text.isdigit() or len(hash_text) != 64:
            print("--expect-head must be SEQ:HASH", file=sys.stderr)
            return 2
        expect = (int(seq_text), hash_text)
    if args.expect_genesis is not None and not re.fullmatch(r"[0-9a-f]{64}", args.expect_genesis):
        print("--expect-genesis must be the 64-hex-digit hash of the genesis record", file=sys.stderr)
        return 2
    report = verify_dir(
        Path(args.directory),
        public_key,
        expect,
        expect_genesis=args.expect_genesis,
        require_checkpoint=public_key is not None,  # G1: with a trusted key, an unsigned chain is a forged one
    )
    for warning in report.warnings:
        print(f"warning: {warning}")
    for item in report.outcome_unknown:
        print(
            f"OUTCOME_UNKNOWN: seq {item.seq} tool {item.tool} approval {item.approval_id} (write_ahead without write_result)"
        )
    for error in report.errors:
        print(f"ERROR: {error}")
    if report.ok:
        summary = (
            f"{report.records} records in {report.segments} segments, seq {report.first_seq}..{report.last_seq}, "
            f"{report.checkpoints_verified} checkpoints verified, head {report.head_hash}"
        )
        if report.genesis_hash is not None:
            summary += f", genesis {report.genesis_hash}"
        if report.anchored:
            print(f"OK: {summary}")
        else:
            trust = "" if public_key is not None else "; key: checkpoint.pub next to the data, not an off-box copy"
            print(
                f"UNANCHORED: {summary}{trust}. No --expect-head: a rollback to an earlier genuine state, or a "
                "rewritten tail after the last checkpoint, cannot be ruled out"
            )
        return 0
    print("FAILED: audit chain is broken (AUDIT_CHAIN_BROKEN)")
    return 1


def _cmd_outcome_unknown(args: argparse.Namespace) -> int:
    report = verify_dir(Path(args.directory))
    for item in report.outcome_unknown:
        print(
            json.dumps(
                {"seq": item.seq, "tool": item.tool, "approval_id": item.approval_id, "action_id": item.action_id}
            )
        )
    return 0 if report.ok else 1


async def _append_stub() -> int:
    body = json.load(sys.stdin)
    client = AuditClient(Path(os.environ.get("ATLAS_AUDIT_SOCKET", str(DEFAULT_SOCKET))))
    try:
        reply = await client.append(body)
    except AuditError as exc:
        print(f"refused: {exc.code}", file=sys.stderr)
        return 1
    finally:
        await client.close()
    print(json.dumps({"seq": reply.seq, "hash": reply.hash, "durable": reply.durable}))
    return 0


def _runtime_attributes(data_dir: Path) -> SegmentAttributes:
    if os.environ.get("ATLAS_AUDIT_REQUIRE_APPEND_ONLY", "1") == "0":
        logging.getLogger("atlas.audit").warning("chattr +a is DISABLED (development only)")
        return NoAttributes()
    data_dir.mkdir(parents=True, exist_ok=True)
    if not probe_linux_chattr(data_dir):
        raise SystemExit(
            "chattr +a is required but not available here (set ATLAS_AUDIT_REQUIRE_APPEND_ONLY=0 for development)"
        )
    return LinuxChattr()


async def _serve() -> int:
    data_dir = Path(os.environ.get("ATLAS_AUDIT_DATA_DIR", str(DEFAULT_DATA_DIR)))
    credentials = os.environ.get("CREDENTIALS_DIRECTORY")
    if not credentials:
        print("CREDENTIALS_DIRECTORY is not set (run under systemd with LoadCredentialEncrypted=)", file=sys.stderr)
        return 2
    try:
        key = load_private_key((Path(credentials) / CREDENTIAL_NAME).read_bytes())
        audit_log = AuditLog.open(
            AuditConfig(data_dir=data_dir, chain_id=os.environ.get("ATLAS_AUDIT_CHAIN_ID", "atlas-pi-01")),
            key,
            attributes=_runtime_attributes(data_dir),
        )
    except (OSError, KeyFileError, AuditCorrupt) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 2
    server = AuditServer(audit_log, KernelPeerCredentials(), PasswdDirectory())
    sock = listen_fds_socket()
    if sock is not None:
        await server.listen_socket(sock)
    else:
        await server.listen_path(Path(os.environ.get("ATLAS_AUDIT_SOCKET", str(DEFAULT_SOCKET))))
    audit_log.append_system("service_start")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    stopper = asyncio.create_task(stop.wait())
    failed = asyncio.create_task(server.fatal.wait())
    await asyncio.wait({stopper, failed}, return_when=asyncio.FIRST_COMPLETED)
    for task in (stopper, failed):
        task.cancel()
    healthy = not (server.fatal.is_set() or audit_log.failed)

    def final_records() -> None:
        audit_log.append_system("service_stop")
        audit_log.checkpoint_if_needed()  # sign the tail so a clean stop leaves nothing unsigned

    # The stop record and checkpoint are written BEFORE client connections are dropped and waited for (known issue
    # N4): an idle pooled client must not hold shutdown past TimeoutStopSec into a SIGKILL.
    try:
        await server.close(on_stop=final_records if healthy else None)
    except (AuditUnavailable, OSError):
        healthy = False
    if not healthy or audit_log.failed:
        # Poisoned by an I/O error: exit non-zero (systemd restarts us) and let startup re-validate the log.
        print("audit log failed; exiting so systemd restarts and re-validates it", file=sys.stderr)
        audit_log.close()
        return 1
    audit_log.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m atlas.audit", description=__doc__.split("\n\n")[0] if __doc__ else ""
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_verify = sub.add_parser("verify", help="verify the hash chain, checkpoints and sidecar")
    p_verify.add_argument("directory")
    p_verify.add_argument("--pubkey", help="trusted checkpoint public key file (default: <directory>/checkpoint.pub)")
    p_verify.add_argument("--expect-head", help="off-box anchor SEQ:HASH; fails if the log is shorter or differs")
    p_verify.add_argument(
        "--expect-genesis",
        help="off-box genesis record hash (recorded at provisioning); fails if the chain was replaced",
    )
    p_unknown = sub.add_parser("outcome-unknown", help="list write_ahead records without a write_result")
    p_unknown.add_argument("directory")
    sub.add_parser("serve", help="run the service")
    sub.add_parser("append", help="CLI stub: submit one record body from stdin through the socket")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    if args.command == "verify":
        return _cmd_verify(args)
    if args.command == "outcome-unknown":
        return _cmd_outcome_unknown(args)
    if args.command == "serve":
        return asyncio.run(_serve())
    return asyncio.run(_append_stub())


if __name__ == "__main__":
    sys.exit(main())
