"""Run atlas-core (fake LLM) + atlas-ui + the built HUD on this machine, for development.

    python -m atlas.ui_server.dev [--port 8443] [--dir .atlas-dev]

One process, but the two halves talk over a real Unix socket (the control socket) exactly as they will on the
Pi. Everything it creates (throwaway TLS CA, device key, PIN hash for ``0000``, paired-device registry) lives
under ``--dir`` (default ``backend/.atlas-dev``, gitignored). Demo data (weather, mail counts, ...) comes from
the mock scenarios' baseline fixtures; the orb, transcript, pending actions and approvals are real atlas-core.

Not for deployment: the model is scripted, "sending" an approved email only logs, and the control-socket
peer check is the same-user fallback (``SameUserPeers``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.core.approvals import ApprovalService, DeviceDirectory, ExecutorLink, StepUpRegistry
from atlas.core.auditing import RecordingAuditSink
from atlas.core.control import ControlServer, EventHub, PeerAuthenticator, SameUserPeers
from atlas.core.fake_llm import ScriptedBackend, Turn, call_turn, demo_responder, text_turn
from atlas.core.runtime import CoreRuntime, build_runtime
from atlas.core.taint import Source, SourceKind, UntrustedContent
from atlas.executor.approval_record import ApprovalSigner
from atlas.llm.interface import Message, Role, ToolSpec
from atlas.policy.engine import Allowlists
from atlas.ui_server.app import build_server
from atlas.ui_server.config import UiConfig
from atlas.ui_server.devtls import make_dev_tls
from atlas.ui_server.mock import scenario as sc
from atlas.ui_server.pin import hash_pin

log = logging.getLogger("atlas.dev")
REPO = Path(__file__).resolve().parents[3]
DEV_PIN = "0000"
DEMO_CONTACT = "dana@example.org"


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def dev_responder(
    messages: Sequence[Message], tools: Sequence[ToolSpec] | None, schema: Mapping[str, Any] | None
) -> Turn:
    """The CLI's demo model plus two requests that exercise approvals and the research queue."""
    if schema is None:
        last = messages[-1]
        if last.role is Role.TOOL and "pending the user's approval" in last.content:
            return text_turn("I've drafted that email and put it in front of you. Approve it in the pending panel.")
        if last.role is Role.USER:
            text = last.content.lower()
            if ("send" in text or "draft" in text) and ("email" in text or "mail" in text):
                return call_turn(
                    (
                        "propose_action",
                        {
                            "kind": "email.send",
                            "summary": "Email Dana: Saturday works",
                            "to": [DEMO_CONTACT],
                            "subject": "Saturday",
                            "body": ["Saturday at 2pm works for me. See you at the park!"],
                        },
                    )
                )
            if text.startswith("research "):
                return call_turn(("queue_research", {"question": last.content[9:200].strip() or "something"}))
    return demo_responder(messages, tools, schema)


class DevExecutor:
    """Stands in for atlas-executor: pretends to send, after a moment, and logs only the action ID."""

    async def submit(self, envelope: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(1.0)
        log.info("dev executor: pretended to run action %s", envelope.get("approval", {}).get("action_id", "?"))
        return {"ok": True}


@dataclass
class CoreStack:
    hub: EventHub
    runtime: CoreRuntime
    approvals: ApprovalService
    stepups: StepUpRegistry
    devices: DeviceDirectory
    audit: RecordingAuditSink
    control: ControlServer


def build_core(
    *,
    peers: PeerAuthenticator,
    executor: ExecutorLink,
    clock_ms: Callable[[], int] = now_ms,
    backend: ScriptedBackend | None = None,
) -> CoreStack:
    """atlas-core with the scripted model, demo mail, a throwaway approval key and the given executor link."""
    hub = EventHub()
    audit = RecordingAuditSink("atlas-core")
    runtime = build_runtime(
        backend or ScriptedBackend(responder=dev_responder),
        audit=audit,
        events=hub,
        clock_ms=clock_ms,
        allowlists=Allowlists(recipients=frozenset({DEMO_CONTACT})),
        contacts={DEMO_CONTACT: "Dana"},
    )
    t0 = clock_ms()
    runtime.index.add(
        UntrustedContent(
            b"Hi! Saturday works for me. How about 2pm at the park?",
            source=Source(SourceKind.EMAIL, "gmail:msg/demo1", t0 - 60_000),
            sender=f"Dana <{DEMO_CONTACT}>",
            subject="Re: Saturday plans?",
        )
    )
    devices, stepups = DeviceDirectory(), StepUpRegistry(clock_ms)
    approvals = ApprovalService(
        runtime.pending, devices, stepups, ApprovalSigner(Ed25519PrivateKey.generate()), audit, hub, clock_ms
    )
    control = ControlServer(
        hub=hub,
        orchestrator=runtime.orchestrator,
        store=runtime.store,
        pending=runtime.pending,
        approvals=approvals,
        stepups=stepups,
        devices=devices,
        executor=executor,
        peers=peers,
        clock_ms=clock_ms,
    )
    return CoreStack(hub, runtime, approvals, stepups, devices, audit, control)


def ensure_dev_files(directory: Path) -> tuple[Path, Path, bytes, str]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _, cert, tls_key = make_dev_tls(directory / "tls")
    key_file, pin_file = directory / "device-token-key", directory / "stepup-pin-hash"
    if not key_file.exists():
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(os.urandom(32))
    if not pin_file.exists():
        pin_file.write_text(hash_pin(DEV_PIN), encoding="utf-8")
        pin_file.chmod(0o600)
    return cert, tls_key, key_file.read_bytes(), pin_file.read_text(encoding="utf-8").strip()


async def run(args: argparse.Namespace) -> int:
    directory = Path(args.dir)
    cert, tls_key, device_key, pin_hash = ensure_dev_files(directory)
    static_dir = Path(args.static_dir)
    if not (static_dir / "index.html").is_file():
        print(f"The HUD is not built: run `npm ci --ignore-scripts && npm run build` in {static_dir.parent}")
    sock = directory / "core.sock"

    core = build_core(peers=SameUserPeers(), executor=DevExecutor())
    hub, control = core.hub, core.control
    await control.start(sock)
    for step in sc.load(sc.BASELINE).setup:  # demo panels and host status (not real data)
        if step.type in ("host.status", "panel.update"):
            await hub.emit(step.type, sc.resolve(step.payload, now_ms(), 1.0))

    # --- atlas-ui ---
    cfg = UiConfig(
        port=args.port,
        tls_cert=cert,
        tls_key=tls_key,
        device_key=device_key,
        pin_hash=pin_hash,
        state_dir=directory / "state",
        static_dir=static_dir,
        core_socket=sock,
    )
    server = build_server(cfg)
    await server.start()
    await asyncio.wait_for(server.core_connected.wait(), 10)
    url = f"https://localhost:{server.bound_port}"
    print(f"\nATLAS dev server: {url}")
    print(f"Pairing code (remote device, 10 min, single use): {server.pairing.create('remote')}")
    if args.kiosk_code:
        print(f"Pairing code (kiosk, view-only): {server.pairing.create('kiosk')}")
    print(
        f"Step-up PIN: {DEV_PIN}.  The browser will warn about the dev certificate; trust {directory}/tls/ca.pem or proceed."
    )
    print(
        "Try: 'what time is it', 'check my email', 'send an email to Dana', 'research solar panels'. Ctrl-C to stop.\n"
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await server.stop()
    await control.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--dir", default=str(REPO / "backend" / ".atlas-dev"))
    parser.add_argument("--static-dir", default=str(REPO / "frontend" / "hud" / "dist"))
    parser.add_argument("--kiosk-code", action="store_true", help="also print a pairing code for a kiosk device")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
