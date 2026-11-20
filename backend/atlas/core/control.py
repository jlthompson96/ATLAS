"""The atlas-core control socket (``/run/atlas/core.sock``, contracts/services.md section 2).

Newline-delimited JSON over a Unix socket. Peer identity comes from the kernel (``SO_PEERCRED`` on
Linux, ``LOCAL_PEERCRED`` on macOS), never from anything the peer sends. Each peer may use only the
operations listed for it in ``PEER_OPS``; atlas-ui is the only peer that gets the HUD operations.

Requests are ``{"id": <int>, "op": "<name>", ...}`` and are answered with
``{"id": <same>, "ok": <bool>, "code"?: "<UPPER_SNAKE>", ...}``. After a ``subscribe`` the server also pushes
``{"event": "<events.v1 type>", "payload": {...}}`` frames: the events.v1 *payloads* atlas-core emits (atlas-ui
adds the envelope, the per-connection ``seq`` and the privacy filter).

Operations for atlas-ui::

    subscribe        -> {"ok": true, "snapshot": {state, host, panels, actions, tasks, alerts, transcript}}
    text.input       {text, device_id}
    action.decide    {device_id, action_id, decision, payload_sha256, step_up_token}
    stepup.register  {device_id, token, expires_at, action_id?}   (the PIN was verified by atlas-ui)
    device.announce  {device_id, role}                            (a paired device connected)
    task.kill        {task_id}
    killswitch.all
    ping

``decide`` is forwarded to ``ApprovalService``, which enforces the approval rules itself (device policy,
step-up token, hash, expiry, replay), so a confused or compromised atlas-ui cannot widen what a kiosk or
an unverified request can do beyond what step-up tokens it registered.

The model of "what is on screen" (``CoreModel``) is kept here so a (re)subscribing atlas-ui gets a full
snapshot, and so atlas-ui can restart without losing pending actions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import stat
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol

from atlas.audit.peercred import KernelPeerCredentials, PasswdDirectory, PeerCredentialError, SockLike
from atlas.core.approvals import ApprovalService, DeviceDirectory, ExecutorLink, StepUpRegistry
from atlas.core.orchestrator import Orchestrator, Phase
from atlas.core.pending import PendingActionStore
from atlas.core.prompt import PromptPart, TrustedText
from atlas.core.taint import VariableStore
from atlas.policy.types import DecisionChannel

log = logging.getLogger("atlas.core.control")
Json = dict[str, Any]

MAX_LINE: Final = 4 * 1024 * 1024
SUBSCRIBER_QUEUE: Final = 2000
EXPIRY_SWEEP_S: Final = 5.0
MAX_TEXT: Final = 2000
ID_RE_MAX: Final = 64

#: Which Unix users may connect, and the operations each may call (contracts/services.md section 2).
UI_OPS: Final = frozenset(
    {
        "ping",
        "subscribe",
        "text.input",
        "action.decide",
        "stepup.register",
        "device.announce",
        "task.kill",
        "killswitch.all",
    }
)
PEER_OPS: Final[dict[str, frozenset[str]]] = {
    "atlas-ui": UI_OPS,
    "atlas-voice": frozenset({"ping"}),
    "atlas-google": frozenset({"ping"}),
    "atlas-discord": frozenset({"ping"}),
    "atlas-panels": frozenset({"ping"}),
    "atlas-market": frozenset({"ping"}),
    "atlas-research": frozenset({"ping"}),
    "atlas-fallback": frozenset({"ping"}),
}

_DROP_ON: Final = frozenset({"denied", "expired", "executed", "failed", "withdrawn"})


# --- peer identity -----------------------------------------------------------------------------------


class PeerAuthenticator(Protocol):
    def identify(self, sock: SockLike) -> str:
        """The service name of the connecting process, or raise ``PeerCredentialError``."""
        ...


class KernelPeers:
    """Production: kernel-reported uid -> Unix user name -> must be one of the ``PEER_OPS`` services."""

    def __init__(self, allowed: frozenset[str] = frozenset(PEER_OPS)) -> None:
        self._creds = KernelPeerCredentials()
        self._users = PasswdDirectory()
        self._allowed = allowed

    def identify(self, sock: SockLike) -> str:
        uid = self._creds.peer_uid(sock)
        name = self._users.user_name(uid)
        if name is None or name not in self._allowed:
            raise PeerCredentialError("peer is not an ATLAS service user")
        return name


class SameUserPeers:
    """DEVELOPMENT FALLBACK (no ``atlas-ui`` Unix user exists on a Mac).

    Accepts a peer only if the kernel says it runs as the same uid as this process, and attributes it to a
    fixed service name. The socket is 0600, so this is no weaker than the dev machine's own user
    separation, but it cannot tell atlas-ui from any other process of the same user. Never used on the Pi.
    """

    def __init__(self, name: str = "atlas-ui") -> None:
        self._creds = KernelPeerCredentials()
        self._name = name

    def identify(self, sock: SockLike) -> str:
        if self._creds.peer_uid(sock) != os.getuid():
            raise PeerCredentialError("peer uid differs from the core's (dev fallback)")
        return self._name


# --- what is on screen -------------------------------------------------------------------------------


@dataclass
class CoreModel:
    """Core's view of the live data a HUD snapshot needs (full, unredacted payloads)."""

    state: Json = field(default_factory=lambda: {"state": "idle"})
    host: Json = field(default_factory=dict)
    panels: dict[str, Json] = field(default_factory=dict)
    actions: dict[str, Json] = field(default_factory=dict)
    tasks: dict[str, Json] = field(default_factory=dict)
    alerts: list[Json] = field(default_factory=list)
    transcript: list[Json] = field(default_factory=list)

    def apply(self, type_: str, p: Json) -> None:
        if type_ == "state.changed":
            self.state = p
        elif type_ == "host.status":
            self.host = p
        elif type_ == "panel.update":
            self.panels[p["panel_id"]] = p
        elif type_ == "action.pending":
            self.actions[p["action_id"]] = p
        elif type_ == "action.resolved" and p["outcome"] in _DROP_ON:
            self.actions.pop(p["action_id"], None)
        elif type_ == "task.progress":
            self.tasks[p["task_id"]] = p
        elif type_ == "alert":
            self.alerts = [*self.alerts, p][-20:]
        elif type_ == "transcript.final":
            self.transcript = [*self.transcript, p][-50:]

    def snapshot(self) -> Json:
        return {
            "state": self.state,
            "host": self.host,
            "panels": list(self.panels.values())[:8],
            "actions": list(self.actions.values())[:50],
            "tasks": list(self.tasks.values())[-50:],
            "alerts": self.alerts,
            "transcript": self.transcript,
        }


Subscriber = Callable[[str, Json], None]


class EventHub:
    """The ``EventSink`` handed to ``build_runtime``: updates the model, then fans out to subscribers."""

    def __init__(self, validate: Callable[[str, Mapping[str, Any]], None] | None = None) -> None:
        self.model = CoreModel()
        self._subscribers: set[Subscriber] = set()
        self._validate = validate

    async def emit(self, type_: str, payload: Mapping[str, Any]) -> None:
        body = json.loads(json.dumps(payload))  # a private copy: subscribers and the model never alias core data
        if self._validate is not None:
            self._validate(type_, body)
        self.model.apply(type_, body)
        for deliver in list(self._subscribers):
            deliver(type_, body)

    def subscribe(self, deliver: Subscriber) -> Json:
        """Register and return the snapshot atomically (no ``await`` between the two)."""
        self._subscribers.add(deliver)
        return self.model.snapshot()

    def unsubscribe(self, deliver: Subscriber) -> None:
        self._subscribers.discard(deliver)


# --- text requests -----------------------------------------------------------------------------------

_CONTROL_STRIP = {c: None for c in [*range(0, 32), 127] if c not in (9, 10)}


def reply_text(parts: tuple[PromptPart, ...], store: VariableStore) -> str:
    """The user-visible reply as plain text. Untrusted values appear as their descriptions only."""
    out = []
    for part in parts:
        out.append(part.text if isinstance(part, TrustedText) else f"[{store.describe(part)}]")
    return "".join(out).translate(_CONTROL_STRIP)


FAILURE_TEXT: Final = {
    Phase.FAILED: "I couldn't complete that request.",
    Phase.BUDGET_EXHAUSTED: "That took more steps than I'm allowed to use, so I stopped.",
}


def _chunks(text: str, size: int = 80) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


class ControlServer:
    def __init__(
        self,
        *,
        hub: EventHub,
        orchestrator: Orchestrator,
        store: VariableStore,
        pending: PendingActionStore,
        approvals: ApprovalService,
        stepups: StepUpRegistry,
        devices: DeviceDirectory,
        executor: ExecutorLink,
        peers: PeerAuthenticator,
        clock_ms: Callable[[], int],
    ) -> None:
        self._hub = hub
        self._orchestrator = orchestrator
        self._store = store
        self._pending = pending
        self._approvals = approvals
        self._stepups = stepups
        self._devices = devices
        self._executor = executor
        self._peers = peers
        self._clock = clock_ms
        self._server: asyncio.Server | None = None
        self._busy = False
        self._utterance = 0
        self._background: set[asyncio.Task[None]] = set()
        self._sweeper: asyncio.Task[None] | None = None

    # --- lifecycle ---

    async def start(self, path: Path) -> None:
        _remove_stale_socket(path)
        old = os.umask(0o117)  # the socket is created 0660: group access is the contract's access control
        try:
            self._server = await asyncio.start_unix_server(self._client, path=str(path), limit=MAX_LINE)
        finally:
            os.umask(old)
        self._sweeper = asyncio.ensure_future(self._sweep_expired())
        log.info("control socket listening on %s", path)

    async def stop(self) -> None:
        if self._sweeper is not None:
            self._sweeper.cancel()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*self._background, *([self._sweeper] if self._sweeper else []), return_exceptions=True)

    async def _sweep_expired(self) -> None:
        while True:
            await asyncio.sleep(EXPIRY_SWEEP_S)
            try:
                await self._approvals.expire_due()
            except Exception as exc:  # the sweeper must outlive one bad pass
                log.error("expiry sweep failed: %s", type(exc).__name__)

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain(self) -> None:
        """Wait for background requests and executions (tests)."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    # --- connections ---

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        try:
            peer = self._peers.identify(sock)
        except PeerCredentialError as exc:
            log.warning("refused control connection: %s", exc)
            writer.close()
            return
        allowed = PEER_OPS[peer]
        outbox: asyncio.Queue[bytes | None] = asyncio.Queue(SUBSCRIBER_QUEUE)

        def put(frame: Json) -> None:
            try:
                outbox.put_nowait(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
            except asyncio.QueueFull:
                log.error("control peer %s too slow; dropping it", peer)
                outbox.put_nowait(None)

        def deliver(type_: str, payload: Json) -> None:
            put({"event": type_, "payload": payload})

        async def pump() -> None:
            while (data := await outbox.get()) is not None:
                writer.write(data)
                await writer.drain()
            writer.close()

        pump_task = asyncio.ensure_future(pump())
        subscribed = False
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    break
                if not line:
                    break
                reply, subscribed_now = await self._handle(peer, allowed, line, deliver)
                subscribed = subscribed or subscribed_now
                put(reply)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if subscribed:
                self._hub.unsubscribe(deliver)
            outbox.put_nowait(None)
            await asyncio.gather(pump_task, return_exceptions=True)
            with contextlib.suppress(Exception):
                writer.close()

    async def _handle(self, peer: str, allowed: frozenset[str], line: bytes, deliver: Subscriber) -> tuple[Json, bool]:
        try:
            req = json.loads(line)
            if not isinstance(req, dict) or not isinstance(req.get("id"), int) or not isinstance(req.get("op"), str):
                raise ValueError
        except ValueError:
            return {"id": -1, "ok": False, "code": "INVALID_MESSAGE"}, False
        rid, op = req["id"], req["op"]
        if op not in allowed:
            log.warning("peer %s may not call %r", peer, op)
            return {"id": rid, "ok": False, "code": "FORBIDDEN_PEER"}, False
        try:
            if op == "subscribe":
                return {"id": rid, "ok": True, "snapshot": self._hub.subscribe(deliver)}, True
            handler = getattr(self, "_op_" + op.replace(".", "_"))
            result: Json = await handler(req)
        except _Refuse as refusal:
            return {"id": rid, "ok": False, "code": refusal.code}, False
        except Exception as exc:
            log.error("control op %s failed: %s", op, type(exc).__name__)
            return {"id": rid, "ok": False, "code": "INTERNAL"}, False
        return {"id": rid, **result}, False

    # --- operations ---

    async def _op_ping(self, req: Json) -> Json:
        return {"ok": True, "t": self._clock()}

    async def _op_device_announce(self, req: Json) -> Json:
        device_id = _id(req, "device_id")
        role = req.get("role")
        if role not in ("kiosk", "remote"):
            raise _Refuse("INVALID_MESSAGE")
        known = self._devices.get(device_id)
        if known is None or known.role != role:
            self._devices.add(device_id, role)
        return {"ok": True}

    async def _op_stepup_register(self, req: Json) -> Json:
        device_id, token = _id(req, "device_id"), req.get("token")
        expires = req.get("expires_at")
        action_id = req.get("action_id")
        if not isinstance(token, str) or not 16 <= len(token) <= 128 or not isinstance(expires, int):
            raise _Refuse("INVALID_MESSAGE")
        if action_id is not None and not isinstance(action_id, str):
            raise _Refuse("INVALID_MESSAGE")
        try:
            self._stepups.register(device_id, token, expires, action_id=action_id)
        except ValueError:
            raise _Refuse("INVALID_MESSAGE") from None
        except OverflowError:
            raise _Refuse("RATE_LIMITED") from None
        return {"ok": True}

    async def _op_action_decide(self, req: Json) -> Json:
        device_id = _id(req, "device_id")
        action_id, decision = req.get("action_id"), req.get("decision")
        sha, token = req.get("payload_sha256"), req.get("step_up_token")
        if not all(isinstance(v, str) for v in (action_id, sha, token)) or decision not in ("approve", "deny"):
            raise _Refuse("INVALID_MESSAGE")
        result = await self._approvals.decide(
            channel=DecisionChannel.HUD,
            device_id=device_id,
            action_id=str(action_id),
            decision=str(decision),
            payload_sha256=str(sha),
            step_up_token=str(token),
        )
        if not result.ok:
            return {"ok": False, "code": result.code or "INTERNAL"}
        if result.signed is not None:
            signed = result.signed
            self._spawn(self._execute(signed))
        return {"ok": True}

    async def _execute(self, signed: Any) -> None:
        try:
            await self._approvals.execute(signed, self._executor)
        except Exception as exc:
            log.error("execution of an approved action failed: %s", type(exc).__name__)
            await self._hub.emit(
                "action.resolved",
                {
                    "action_id": signed.record.action_id,
                    "outcome": "failed",
                    "code": "EXECUTOR_REJECTED",
                    "at": self._clock(),
                },
            )

    async def _op_task_kill(self, req: Json) -> Json:
        task = self._hub.model.tasks.get(str(req.get("task_id")))
        if task is None:
            raise _Refuse("UNKNOWN_TASK")
        await self._hub.emit("task.progress", {**task, "status": "killed"})
        return {"ok": True}

    async def _op_killswitch_all(self, req: Json) -> Json:
        for task in list(self._hub.model.tasks.values()):
            if task["status"] in ("queued", "running", "blocked"):
                await self._hub.emit("task.progress", {**task, "status": "killed"})
        await self._hub.emit(
            "alert",
            {
                "alert_id": f"ks-{self._clock()}",
                "level": "warn",
                "code": "KILLSWITCH",
                "message": "All research stopped",
            },
        )
        return {"ok": True}

    async def _op_text_input(self, req: Json) -> Json:
        text = req.get("text")
        _id(req, "device_id")
        if not isinstance(text, str) or not 1 <= len(text) <= MAX_TEXT:
            raise _Refuse("INVALID_MESSAGE")
        if self._busy or self._hub.model.state["state"] not in ("idle", "llm_unreachable"):
            raise _Refuse("BUSY")
        self._busy = True
        self._spawn(self._run_text(text))
        return {"ok": True}

    async def _run_text(self, text: str) -> None:
        self._utterance += 1
        uid = f"u{self._utterance}"
        resting = dict(self._hub.model.state)
        try:
            emit = self._hub.emit
            await emit("transcript.final", {"utterance_id": uid, "role": "user", "text": text, "source": "text"})
            await emit("state.changed", {"state": "thinking"})
            outcome = await self._orchestrator.handle(text, store=self._store)
            reply = reply_text(outcome.reply, self._store) if outcome.reply else ""
            if not reply:
                reply = FAILURE_TEXT.get(outcome.phase, "Done.") + (f" ({outcome.code})" if outcome.code else "")
            aid = f"a{self._utterance}"
            reply = reply[:4000]
            for chunk in _chunks(reply, 80):
                await emit("assistant.delta", {"utterance_id": aid, "text": chunk})
            await emit("transcript.final", {"utterance_id": aid, "role": "assistant", "text": reply, "source": "text"})
        except Exception as exc:
            log.error("text request failed: %s", type(exc).__name__)
            await self._hub.emit(
                "alert",
                {"alert_id": f"e{self._utterance}", "level": "warn", "code": "LLM_ERROR", "message": "Request failed"},
            )
        finally:
            if self._hub.model.state != resting:
                await self._hub.emit("state.changed", resting)
            self._busy = False


def _remove_stale_socket(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise RuntimeError(f"{path} exists and is not a socket")
        path.unlink()


class _Refuse(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _id(req: Json, key: str) -> str:
    value = req.get(key)
    if not isinstance(value, str) or not 1 <= len(value) <= ID_RE_MAX:
        raise _Refuse("INVALID_MESSAGE")
    return value
