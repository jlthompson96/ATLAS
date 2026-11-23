"""atlas-core as it runs on the Pi (``python -m atlas.core``; unit ``deploy/systemd/atlas-core.service``).

What it wires, compared with the development runner (``atlas.ui_server.dev``):

* the control socket is the one systemd created from ``atlas-core.socket`` (fd 3, owner ``atlas-core``, group
  ``atlas-core-clients``, mode 0660), and peers are identified by the kernel (``KernelPeers``: ``SO_PEERCRED`` uid ->
  ATLAS service user), never the same-user fallback;
* audit records go to atlas-audit (``/run/atlas/audit.sock``), which fails closed;
* approvals are signed with the ``approval-signing-key`` credential and handed to atlas-executor over
  ``/run/atlas/executor.sock``. Until atlas-executor is deployed an approved action fails with
  ``EXECUTOR_UNAVAILABLE``: nothing is sent;
* no demo mail, no demo contacts, no recipient allowlist.

Two model modes:

* ``--backend none`` (default): **no desktop linked yet**. Time, date and help are answered on the Pi by the local
  grammar; every other request gets a fixed line and the orb shows ``llm_unreachable`` / ``LINK_DOWN``. The
  planner's backend is ``UnlinkedBackend``, which refuses every call, as a backstop.
* ``--backend lmstudio``: the real link (LM Studio over WireGuard, optionally atlas-hostd and Wake-on-LAN), built
  by ``atlas.core.link.build_link`` as in the CLI. ``--model`` is required so that starting never depends on the
  desktop being awake.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from atlas.audit.client import DEFAULT_SOCKET as AUDIT_SOCKET
from atlas.audit.client import AuditClient
from atlas.core.approvals import ApprovalService, DeviceDirectory, StepUpRegistry
from atlas.core.auditing import AuditSink
from atlas.core.control import ControlServer, EventHub, KernelPeers, PeerAuthenticator, Readiness, TextHooks
from atlas.core.executor_link import SocketExecutorLink
from atlas.core.fallback import LocalResponder, parse_intent
from atlas.core.link import LinkTextHooks, add_link_arguments, build_link
from atlas.core.runtime import build_runtime
from atlas.executor.approval_record import ApprovalKeyError, ApprovalSigner, load_signing_key
from atlas.llm.interface import (
    BackendUnreachable,
    Capabilities,
    Delta,
    Health,
    LLMBackend,
    Message,
    ModelInfo,
    NotReady,
    ReadinessStage,
    Ready,
    ToolSpec,
)
from atlas.policy.engine import Allowlists

log = logging.getLogger("atlas.core")

STATE_DIR: Final = Path("/var/lib/atlas/core")  # the unit's StateDirectory=atlas/core
EXECUTOR_SOCKET: Final = Path("/run/atlas/executor.sock")
SIGNING_KEY_CRED: Final = "approval-signing-key"
SD_LISTEN_FDS_START: Final = 3

UNLINKED_STATE: Final[dict[str, Any]] = {"state": "llm_unreachable", "code": "LINK_DOWN", "detail": "No desktop linked"}
UNLINKED_LINE: Final = (
    "No desktop is linked to ATLAS yet, so I can only answer the time and the date here. "
    "Everything else needs the desktop."
)


class ServiceError(Exception):
    """A configuration problem: the service exits with status 2 and this message (never a secret)."""


def now_ms() -> int:
    return time.time_ns() // 1_000_000


# --- the socket from systemd ----------------------------------------------------------------------


def systemd_socket(environ: Mapping[str, str] | None = None, pid: int | None = None) -> socket.socket | None:
    """The one listening socket passed by socket activation (``sd_listen_fds``), or ``None`` without one.

    The LISTEN_* variables are removed so a child process can't mistake the socket for its own.
    """
    env = os.environ if environ is None else environ
    if env.get("LISTEN_PID") != str(os.getpid() if pid is None else pid):
        return None
    count = env.get("LISTEN_FDS", "")
    if count != "1":
        raise ServiceError(f"expected exactly one socket from systemd (LISTEN_FDS={count or 'unset'})")
    if environ is None:
        for key in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
            os.environ.pop(key, None)
    sock = socket.socket(fileno=SD_LISTEN_FDS_START)
    if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_STREAM:
        sock.detach()
        raise ServiceError("the socket from systemd is not a Unix stream socket")
    sock.setblocking(False)
    return sock


# --- no desktop linked ----------------------------------------------------------------------------


class UnlinkedBackend(LLMBackend):
    """The planner's backend while no desktop is linked: every call fails. ``UnlinkedHooks`` stops requests before
    they reach the planner; this is the backstop if one ever got through."""

    def id(self) -> str:
        return "unlinked"

    def capabilities(self) -> Capabilities:
        return Capabilities(tools=False, json_schema=False, streaming=False, max_ctx=0)

    async def health(self, deadline: float) -> Health:
        return Health(reachable=False, authenticated=False, model_loaded=None)

    async def ensure_ready(self, model: str, deadline: float) -> Ready | NotReady:
        return NotReady(ReadinessStage.PACKET, "no desktop linked")

    def chat(
        self,
        messages: Sequence[Message],
        *,
        max_tokens: int,
        tools: Sequence[ToolSpec] | None = None,
        response_schema: Mapping[str, Any] | None = None,
        deadline: float | None = None,
    ) -> AsyncIterator[Delta]:
        raise BackendUnreachable("no desktop linked")

    async def list_models(self, deadline: float) -> Sequence[ModelInfo]:
        return ()


class UnlinkedHooks:
    """``TextHooks`` without a desktop: the local grammar answers what it can; nothing else reaches the planner."""

    def __init__(self, hub: EventHub, now: Callable[[], datetime] = datetime.now) -> None:
        self._hub = hub
        self._responder = LocalResponder(now)

    async def local_answer(self, text: str) -> str | None:
        intent = parse_intent(text)
        return None if intent is None else await self._responder.respond(intent)

    async def ensure_ready(self, text: str) -> Readiness:
        await self._hub.emit("state.changed", dict(UNLINKED_STATE))
        return Readiness(UNLINKED_LINE)


# --- wiring ---------------------------------------------------------------------------------------


def load_signer(credentials: Path) -> ApprovalSigner:
    try:
        return ApprovalSigner(load_signing_key(credentials / SIGNING_KEY_CRED))
    except ApprovalKeyError as exc:
        raise ServiceError(str(exc)) from None


def build_control(
    *,
    backend: LLMBackend,
    hooks: TextHooks,
    hub: EventHub,
    audit: AuditSink,
    signer: ApprovalSigner,
    peers: PeerAuthenticator,
    executor_socket: Path,
    state_dir: Path,
    clock_ms: Callable[[], int] = now_ms,
) -> ControlServer:
    runtime = build_runtime(
        backend,
        audit=audit,
        events=hub,
        clock_ms=clock_ms,
        memory_path=state_dir / "memory.sqlite3",
        allowlists=Allowlists(),
    )
    devices, stepups = DeviceDirectory(), StepUpRegistry(clock_ms)
    approvals = ApprovalService(runtime.pending, devices, stepups, signer, audit, hub, clock_ms)
    return ControlServer(
        hub=hub,
        orchestrator=runtime.orchestrator,
        store=runtime.store,
        pending=runtime.pending,
        approvals=approvals,
        stepups=stepups,
        devices=devices,
        executor=SocketExecutorLink(executor_socket),
        peers=peers,
        clock_ms=clock_ms,
        text_hooks=hooks,
    )


def credentials_dir() -> Path:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise ServiceError("no $CREDENTIALS_DIRECTORY: run under systemd (LoadCredentialEncrypted=)")
    return Path(directory)


async def run(args: argparse.Namespace) -> int:
    sock = systemd_socket()
    if sock is None and args.socket is None:
        raise ServiceError("no socket from systemd and no --socket given")
    signer = load_signer(Path(args.credentials_dir) if args.credentials_dir else credentials_dir())
    audit = AuditClient(Path(args.audit_socket))
    hub = EventHub()
    hooks: TextHooks
    backend: LLMBackend
    if args.backend == "lmstudio":
        if not args.model:
            raise ServiceError("--backend lmstudio needs --model, so starting never waits for the desktop")
        link = await build_link(args, audit, sys.stderr, events=hub)
        backend, hooks = link.backend, LinkTextHooks(link)
        log.info("model link: %s, model %s", link.backend.id(), link.model)
    else:
        backend, hooks = UnlinkedBackend(), UnlinkedHooks(hub)
        log.info("no desktop linked: local answers only")
    control = build_control(
        backend=backend,
        hooks=hooks,
        hub=hub,
        audit=audit,
        signer=signer,
        peers=KernelPeers(),
        executor_socket=Path(args.executor_socket),
        state_dir=Path(args.state_dir),
    )
    if args.backend != "lmstudio":
        await hub.emit("state.changed", dict(UNLINKED_STATE))
    if sock is not None:
        await control.start(sock=sock)
    else:
        await control.start(Path(args.socket))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    await control.stop()
    await audit.close()
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m atlas.core", description="atlas-core on the Pi")
    # Production choices only: no scripted demo model on the Pi.
    add_link_arguments(
        p, ("none", "lmstudio"), "none: no desktop linked yet (default); lmstudio: the real link (needs --model)"
    )
    p.add_argument("--socket", default=None, help="listen here instead of the socket from systemd (testing)")
    p.add_argument("--credentials-dir", default=None, help="default: $CREDENTIALS_DIRECTORY")
    p.add_argument("--audit-socket", default=str(AUDIT_SOCKET))
    p.add_argument("--executor-socket", default=str(EXECUTOR_SOCKET))
    p.add_argument("--state-dir", default=str(STATE_DIR))
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    try:
        return asyncio.run(run(args))
    except ServiceError as exc:
        print(f"atlas-core: {exc}", file=sys.stderr)
        return 2
