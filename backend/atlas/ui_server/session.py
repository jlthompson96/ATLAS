"""One authenticated browser connection to ``/ws/events``.

Transport-agnostic (the server passes a ``send`` coroutine; tests pass a collector). Every outgoing frame
goes through the pipeline the mock server uses: privacy filter -> envelope -> events.v1 validation ->
stream checks -> send. A frame that fails validation is logged and dropped, never sent; a frame that breaks
the state machine also triggers a fresh ``sync.snapshot`` so the HUD can't end up showing a wrong orb.

Privacy mode is per connection and enforced here, on the server (PLAN.md section 4.6).
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final, Protocol

from atlas.ui_server import privacy
from atlas.ui_server.devices import Device
from atlas.ui_server.model import Model
from atlas.ui_server.pin import PinVerifier
from atlas.ui_server.protocol import Contract, ContractViolation, Sequencer, StreamChecker, now_ms, redact_for_log

log = logging.getLogger("atlas.ui")
Json = dict[str, Any]

STEP_UP_TTL_MS: Final = 60_000
MAX_TOKENS: Final = 8
INACTIVITY_S: Final = {"remote": 300.0, "kiosk": 600.0}  # PLAN.md section 4.6
NO_ACTIVITY: Final = frozenset({"clock.ping", "sync.request"})


class Backend(Protocol):
    """What a session needs from the server: the shared model and a way to call atlas-core."""

    model: Model
    version: int  # bumped for every event applied to ``model``
    server_version: str
    boot_id: str

    async def core_call(self, op: str, **fields: Any) -> Json | None:
        """One control-socket call. ``None`` means atlas-core is unreachable."""
        ...

    def save_layouts(self) -> None: ...


class Session:
    def __init__(
        self,
        send: Callable[[str], Awaitable[None]],
        *,
        device: Device,
        backend: Backend,
        pin: PinVerifier,
        contract: Contract | None = None,
        wall_ms: Callable[[], int] = now_ms,
        monotonic: Callable[[], float] = time.monotonic,
        inactivity_s: float | None = None,
    ) -> None:
        self.device = device
        self.contract = contract or Contract.load()
        self._backend = backend
        self._pin = pin
        self._send = send
        self._wall = wall_ms
        self._mono = monotonic
        self._inactivity_s = INACTIVITY_S[device.role] if inactivity_s is None else inactivity_s
        self.seq = Sequencer(clock=wall_ms)
        self.checker = StreamChecker(self.contract)
        self.privacy: Json = {"on": False, "reason": "user"}
        self._lock = asyncio.Lock()
        self._tokens: dict[str, int] = {}
        self._client_seq = -1
        self._last_activity = monotonic()
        self.frames_sent = 0
        self.dropped = 0
        self._snapshot_version = -1

    # --- outgoing -------------------------------------------------------------------------------

    async def emit(self, type_: str, payload: Mapping[str, Any]) -> None:
        async with self._lock:
            await self._emit_locked(type_, dict(payload))

    async def push(self, type_: str, payload: Json, version: int) -> None:
        """An event from atlas-core. The shared model already includes it, so a snapshot sent since then does too."""
        async with self._lock:
            if version > self._snapshot_version:
                await self._emit_locked(type_, dict(payload))

    async def _emit_locked(self, type_: str, payload: Json) -> None:
        if type_ == "privacy.mode":
            self.privacy = payload
        try:
            out = privacy.filter_outgoing(type_, payload, self.privacy["on"], self._wall())
        except (ValueError, KeyError) as exc:
            self._drop(type_, f"privacy filter: {type(exc).__name__}")
            return
        if out is None:
            return
        if not await self._send_frame(type_, out):
            if type_ == "state.changed":  # the orb must not stay wrong: resend the whole picture
                await self._send_snapshot_locked()
            return
        if type_ == "privacy.mode":  # contract: every privacy change is followed by a fresh snapshot
            await self._send_snapshot_locked()

    async def _send_frame(self, type_: str, payload: Json) -> bool:
        """Build, validate and send one frame. ``False`` (and nothing sent) if it breaks events.v1."""
        saved_seq, saved_last = self.seq.next_seq, self.checker.last_seq
        saved_state, saved_stage = self.checker.state, self.checker.wake_stage_index
        frame = self.seq.envelope(type_, payload)
        try:
            self.contract.validate("server", frame)
            self.checker.check(frame)
        except ContractViolation as exc:
            self.seq.next_seq, self.checker.last_seq = saved_seq, saved_last
            self.checker.state, self.checker.wake_stage_index = saved_state, saved_stage
            self._drop(type_, str(exc)[:200])
            return False
        self.frames_sent += 1
        await self._send(json.dumps(frame, separators=(",", ":")))
        return True

    def _drop(self, type_: str, why: str) -> None:
        self.dropped += 1
        log.error("dropped outgoing %s for %s: %s", type_, self.device.device_id, why)

    async def snapshot(self) -> None:
        async with self._lock:
            await self._send_snapshot_locked()

    async def _send_snapshot_locked(self) -> None:
        self._snapshot_version = self._backend.version
        if not await self._send_frame("sync.snapshot", self._snapshot_payload()):
            raise ContractViolation("sync.snapshot failed validation")

    def _snapshot_payload(self) -> Json:
        m, on, now = self._backend.model, self.privacy["on"], self._wall()
        return {
            "server": {
                "version": self._backend.server_version,
                "schema_revision": self.contract.revision,
                "boot_id": self._backend.boot_id,
            },
            "device": {
                "device_id": self.device.device_id,
                "role": self.device.role,
                "can_approve": self.device.can_approve,
                "can_unlock_portfolio": self.device.can_unlock_portfolio,
            },
            "state": m.state,
            "host": m.host,
            "privacy": self.privacy,
            "panels": [privacy.redact_panel(p, now) if on else p for p in m.panels.values()][:8],
            "actions": [privacy.redact_action(a) if on else a for a in m.actions.values()][:50],
            "tasks": [privacy.redact_task(t) if on else t for t in list(m.tasks.values())[-50:]],
            "alerts": m.alerts,
            "transcript": [] if on else m.transcript,
            "layouts": m.layouts,
        }

    # --- inactivity ------------------------------------------------------------------------------

    async def check_inactivity(self) -> bool:
        """Turn privacy mode on if this device has been idle for its window. Returns True if it did."""
        if self.privacy["on"] or self._mono() - self._last_activity < self._inactivity_s:
            return False
        await self.emit("privacy.mode", {"on": True, "reason": "inactivity"})
        return True

    # --- incoming --------------------------------------------------------------------------------

    async def handle_raw(self, raw: str | bytes) -> None:
        try:
            frame = json.loads(raw)
        except ValueError:
            await self._result(-1, False, "INVALID_JSON")
            return
        errors = self.contract.errors("client", frame)
        seq = frame.get("seq", -1) if isinstance(frame, dict) else -1
        if errors:
            # Metadata only: redaction works by field name, so a malformed frame (typed text under a wrong key) would
            # slip through, and a schema error quotes the offending value. Type (if known), size and error count.
            kind = frame.get("type") if isinstance(frame, dict) else None
            log.warning(
                "invalid client frame from %s: type %s, %d bytes, %d schema error(s)",
                self.device.device_id,
                kind if kind in self.contract.client_types else "?",
                len(raw),
                len(errors),
            )
            await self._result(seq if isinstance(seq, int) else -1, False, "INVALID_MESSAGE", errors[0][:200])
            return
        if seq <= self._client_seq:
            await self._result(seq, False, "SEQ_NOT_INCREASING")
            return
        self._client_seq = seq
        if frame["type"] not in NO_ACTIVITY:
            self._last_activity = self._mono()
        # DEBUG, not INFO: on the Pi, INFO lines are journal writes on the SD card (budget: 100 MB/day), and a frame per
        # heartbeat adds up. Redacted either way (events.v1 x-atlas-sensitive: what you type is never logged).
        if frame["type"] != "clock.ping":
            log.debug("client %s %s", self.device.device_id, redact_for_log(frame))
        await getattr(self, "_on_" + frame["type"].replace(".", "_"))(seq, frame["payload"])

    async def _result(self, ref_seq: int, ok: bool, code: str | None = None, message: str | None = None) -> None:
        payload: Json = {"ref_seq": max(ref_seq, 0), "ok": ok}
        if code:
            payload["code"] = code
        if message:
            payload["message"] = message
        await self.emit("request.result", payload)

    async def _forward(self, seq: int, op: str, **fields: Any) -> Json | None:
        """Call atlas-core and answer the client's request from its reply. Returns the reply if ok."""
        reply = await self._backend.core_call(op, **fields)
        if reply is None:
            await self._result(seq, False, "CORE_UNREACHABLE", "ATLAS core is offline")
            return None
        if not reply.get("ok"):
            await self._result(seq, False, str(reply.get("code", "INTERNAL")))
            return None
        await self._result(seq, True)
        return reply

    async def _on_clock_ping(self, seq: int, p: Json) -> None:
        await self.emit("clock.pong", {"client_t": p["client_t"], "server_t": self._wall()})

    async def _on_activity_ping(self, seq: int, p: Json) -> None:
        return  # resets the inactivity timer in handle_raw; no reply (contracts/README.md, "Inactivity")

    async def _on_sync_request(self, seq: int, p: Json) -> None:
        await self.snapshot()

    async def _on_layout_save(self, seq: int, p: Json) -> None:
        self._backend.model.layouts[p["profile"]] = p["slots"]
        self._backend.save_layouts()
        await self._result(seq, True)

    async def _on_stepup_verify(self, seq: int, p: Json) -> None:
        if not self.device.can_approve:
            await self._result(seq, False, "FORBIDDEN_DEVICE", "This device can't approve actions")
            return
        outcome = await self._pin.verify(self.device.device_id, p["pin"])
        if outcome == "locked":
            await self._result(seq, False, "RATE_LIMITED", "Too many attempts; try again later")
            return
        if outcome == "bad":
            await self._result(seq, False, "BAD_PIN")
            return
        token, expires = secrets.token_urlsafe(24), self._wall() + STEP_UP_TTL_MS
        reply = await self._backend.core_call(
            "stepup.register", device_id=self.device.device_id, token=token, expires_at=expires
        )
        if reply is None or not reply.get("ok"):
            await self._result(seq, False, "CORE_UNREACHABLE" if reply is None else str(reply.get("code", "INTERNAL")))
            return
        now = self._wall()
        self._tokens = {t: e for t, e in self._tokens.items() if e > now}
        if len(self._tokens) < MAX_TOKENS:
            self._tokens[token] = expires
        await self.emit("stepup.granted", {"token": token, "expires_at": expires})

    def _step_up_ok(self, token: str | None) -> bool:
        return token is not None and self._tokens.get(token, 0) > self._wall()

    async def _on_privacy_set(self, seq: int, p: Json) -> None:
        if not p["on"] and self.device.role == "remote" and not self._step_up_ok(p.get("step_up_token")):
            await self._result(seq, False, "STEP_UP_REQUIRED", "Enter your PIN to leave privacy mode")
            return
        await self._result(seq, True)
        await self.emit("privacy.mode", {"on": p["on"], "reason": "user"})

    async def _on_portfolio_unlock(self, seq: int, p: Json) -> None:
        if not self.device.can_unlock_portfolio:
            await self._result(seq, False, "FORBIDDEN_DEVICE", "This device can't unlock the portfolio")
        else:
            await self._result(seq, False, "NOT_AVAILABLE", "The portfolio isn't available yet")

    async def _on_action_decide(self, seq: int, p: Json) -> None:
        if not self.device.can_approve:
            await self._result(seq, False, "FORBIDDEN_DEVICE", "Approvals are disabled on this device")
            return
        await self._forward(
            seq,
            "action.decide",
            device_id=self.device.device_id,
            action_id=p["action_id"],
            decision=p["decision"],
            payload_sha256=p["payload_sha256"],
            step_up_token=p["step_up_token"],
        )

    async def _on_task_kill(self, seq: int, p: Json) -> None:
        await self._forward(seq, "task.kill", task_id=p["task_id"])

    async def _on_killswitch_all(self, seq: int, p: Json) -> None:
        await self._forward(seq, "killswitch.all")

    async def _on_text_input(self, seq: int, p: Json) -> None:
        await self._forward(seq, "text.input", device_id=self.device.device_id, text=p["text"])

    async def _on_ptt_start(self, seq: int, p: Json) -> None:
        await self._result(seq, False, "VOICE_UNAVAILABLE", "Voice isn't running; type instead")

    async def _on_ptt_stop(self, seq: int, p: Json) -> None:
        await self._result(seq, False, "NOT_LISTENING")
