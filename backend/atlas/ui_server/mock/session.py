"""One mock connection: state model, scenario player and client-message handling.

Transport-agnostic: the server passes a ``send`` coroutine; validation mode passes a collector and
a fake clock. Every outgoing frame goes through the same pipeline as the real server will use:
apply to state -> privacy filter -> envelope -> schema validation -> stream checks -> send.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from atlas.ui_server import privacy
from atlas.ui_server.mock import scenario as sc
from atlas.ui_server.protocol import Contract, Sequencer, StreamChecker, now_ms, redact_for_log

log = logging.getLogger("atlas.mock")
Json = dict[str, Any]

MOCK_PIN: Final = "0000"
MOCK_PASSPHRASE: Final = "atlas-mock"  # noqa: S105 - documented mock-only credential
MAX_FAILURES: Final = 5
STEP_UP_TTL_MS: Final = 60_000
RESTING: Final = frozenset({"idle", "llm_unreachable"})
_DROP_ON: Final = frozenset({"denied", "expired", "executed", "failed", "withdrawn"})


class Clock:
    """Wall clock. ``FakeClock`` replaces it for instant validation runs."""

    def now(self) -> int:
        return now_ms()

    async def sleep_ms(self, ms: float) -> None:
        if ms > 0:
            await asyncio.sleep(ms / 1000)


class FakeClock(Clock):
    def __init__(self, start: int) -> None:
        self.t = start

    def now(self) -> int:
        return self.t

    async def sleep_ms(self, ms: float) -> None:
        self.t += max(0, round(ms))


DEFAULT_LAYOUTS: Final[Json] = {
    "kiosk": {
        "top": "clock",
        "left": ["system", "email", "discord", "calendar"],
        "right": ["pending", "research", "egress_audit", "markets"],
        "bottom": "transcript",
    },
    "remote": {
        "top": "clock",
        "left": ["system", "weather", "email", "discord", "calendar"],
        "right": ["pending", "research", "markets", "portfolio", "egress_audit"],
        "bottom": "transcript",
    },
    "compact": {"left": ["pending", "research", "system", "portfolio"], "right": [], "bottom": "transcript"},
}


@dataclass
class Model:
    """Everything a sync.snapshot needs. Holds full (unredacted) payloads."""

    state: Json = field(default_factory=lambda: {"state": "idle"})
    host: Json = field(default_factory=dict)
    privacy: Json = field(default_factory=lambda: {"on": False, "reason": "user"})
    panels: dict[str, Json] = field(default_factory=dict)
    actions: dict[str, Json] = field(default_factory=dict)
    tasks: dict[str, Json] = field(default_factory=dict)
    alerts: list[Json] = field(default_factory=list)
    transcript: list[Json] = field(default_factory=list)
    layouts: Json = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_LAYOUTS)))

    def apply(self, type_: str, p: Json) -> None:
        if type_ == "state.changed":
            self.state = p
        elif type_ == "host.status":
            self.host = p
        elif type_ == "privacy.mode":
            self.privacy = p
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


class Session:
    def __init__(
        self,
        send: Callable[[str], Awaitable[None]],
        *,
        device_role: str = "remote",
        speed: float = 1.0,
        clock: Clock | None = None,
        contract: Contract | None = None,
    ) -> None:
        self.contract = contract or Contract.load()
        self.clock = clock or Clock()
        self.speed = speed
        self._send = send
        self.seq = Sequencer(clock=self.clock.now)
        self.checker = StreamChecker(self.contract)
        self.model = Model()
        self.device: Json = {
            "device_id": f"mock-{device_role}",
            "role": device_role,
            "can_approve": device_role == "remote",
            "can_unlock_portfolio": device_role == "remote",
        }
        self._lock = asyncio.Lock()
        self._tokens: dict[str, int] = {}  # step-up token -> expires_at
        self._failures = {"pin": 0, "passphrase": 0}
        self._client_seq = -1
        self.last_activity_ms: int | None = None
        self._busy = False  # an interactive reply is driving the orb
        self._resting: Json = {"state": "idle"}
        self._scenario_playing = False
        self._tasks: set[asyncio.Task[None]] = set()
        self.frames_sent = 0

    # --- outgoing -------------------------------------------------------------------------------

    async def emit(self, type_: str, payload: Mapping[str, Any]) -> None:
        async with self._lock:
            await self._emit_locked(type_, dict(payload))

    async def _emit_locked(self, type_: str, payload: Json) -> None:
        self.model.apply(type_, payload)
        out = privacy.filter_outgoing(type_, payload, self.model.privacy["on"], self.clock.now())
        if out is None:
            return
        await self._send_frame(self.seq.envelope(type_, out))
        if type_ == "privacy.mode":  # contract: every privacy change is followed by a fresh snapshot
            await self._send_frame(self.seq.envelope("sync.snapshot", self._snapshot_payload()))

    async def _send_frame(self, frame: Json) -> None:
        self.contract.validate("server", frame)
        self.checker.check(frame)
        self.frames_sent += 1
        await self._send(json.dumps(frame, separators=(",", ":")))

    async def snapshot(self) -> None:
        async with self._lock:
            await self._send_frame(self.seq.envelope("sync.snapshot", self._snapshot_payload()))

    def _snapshot_payload(self) -> Json:
        m, on, now = self.model, self.model.privacy["on"], self.clock.now()
        return {
            "server": {"version": "mock-0.1", "schema_revision": self.contract.revision, "boot_id": "mock-boot"},
            "device": self.device,
            "state": m.state,
            "host": m.host,
            "privacy": m.privacy,
            "panels": [privacy.redact_panel(p, now) if on else p for p in m.panels.values()],
            "actions": [privacy.redact_action(a) if on else a for a in m.actions.values()],
            "tasks": [privacy.redact_task(t) if on else t for t in list(m.tasks.values())[-50:]],
            "alerts": m.alerts,
            "transcript": [] if on else m.transcript,
            "layouts": m.layouts,
        }

    # --- scenario playback -----------------------------------------------------------------------

    def apply_setup(self, scenario: sc.Scenario) -> None:
        """Reset the model (keeping this device's privacy mode and layouts) and apply the setup."""
        self.model = Model(privacy=self.model.privacy, layouts=self.model.layouts)
        t0 = self.clock.now()
        for step in scenario.setup:
            payload = sc.resolve(step.payload, t0, self.speed)
            # Validate setup payloads as if they were frames, so fixtures can't drift from the schema.
            self.contract.validate("server", {"v": 1, "type": step.type, "seq": 0, "ts": t0, "payload": payload})
            self.model.apply(step.type, payload)

    async def play(self, scenario: sc.Scenario) -> None:
        self._scenario_playing = True
        try:
            await self.play_steps(scenario.steps)
        finally:
            self._scenario_playing = False

    async def play_steps(self, steps: Iterable[sc.Step]) -> None:
        t0 = self.clock.now()
        elapsed = 0.0
        for step in steps:
            target = step.at / self.speed
            await self.clock.sleep_ms(target - elapsed)
            elapsed = max(elapsed, target)
            await self.emit(step.type, sc.resolve(step.payload, t0, self.speed))

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
            log.warning("invalid client frame %s: %s", redact_for_log(frame), errors[:3])
            await self._result(seq if isinstance(seq, int) else -1, False, "INVALID_MESSAGE", errors[0][:200])
            return
        if seq <= self._client_seq:
            await self._result(seq, False, "SEQ_NOT_INCREASING")
            return
        self._client_seq = seq
        log.info("client %s", redact_for_log(frame))
        handler = getattr(self, "_on_" + frame["type"].replace(".", "_"))
        await handler(seq, frame["payload"])

    async def _result(self, ref_seq: int, ok: bool, code: str | None = None, message: str | None = None) -> None:
        payload: Json = {"ref_seq": max(ref_seq, 0), "ok": ok}
        if code:
            payload["code"] = code
        if message:
            payload["message"] = message
        await self.emit("request.result", payload)

    async def _on_clock_ping(self, seq: int, p: Json) -> None:
        await self.emit("clock.pong", {"client_t": p["client_t"], "server_t": self.clock.now()})

    async def _on_activity_ping(self, seq: int, p: Json) -> None:
        # Resets the device's privacy-inactivity timer; no reply (contracts/README.md, "Inactivity").
        # The mock has no inactivity timer, so it only records the time.
        self.last_activity_ms = self.clock.now()

    async def _on_sync_request(self, seq: int, p: Json) -> None:
        await self.snapshot()

    async def _on_layout_save(self, seq: int, p: Json) -> None:
        self.model.layouts[p["profile"]] = p["slots"]
        await self._result(seq, True)

    async def _on_stepup_verify(self, seq: int, p: Json) -> None:
        if self._failures["pin"] >= MAX_FAILURES:
            await self._result(seq, False, "RATE_LIMITED", "Too many attempts")
            return
        if p["pin"] != MOCK_PIN:
            self._failures["pin"] += 1
            await self._result(seq, False, "BAD_PIN")
            return
        self._failures["pin"] = 0
        token, expires = secrets.token_urlsafe(24), self.clock.now() + STEP_UP_TTL_MS
        self._tokens[token] = expires
        await self.emit("stepup.granted", {"token": token, "expires_at": expires})

    def _step_up_ok(self, token: str | None) -> bool:
        return token is not None and self._tokens.get(token, 0) > self.clock.now()

    async def _on_privacy_set(self, seq: int, p: Json) -> None:
        if not p["on"] and self.device["role"] == "remote" and not self._step_up_ok(p.get("step_up_token")):
            await self._result(seq, False, "STEP_UP_REQUIRED", "Enter your PIN to leave privacy mode")
            return
        await self._result(seq, True)
        await self.emit("privacy.mode", {"on": p["on"], "reason": "user"})

    async def _on_portfolio_unlock(self, seq: int, p: Json) -> None:
        if not self.device["can_unlock_portfolio"]:
            await self._result(seq, False, "FORBIDDEN_DEVICE", "This device can't unlock the portfolio")
        elif self._failures["passphrase"] >= MAX_FAILURES:
            await self._result(seq, False, "RATE_LIMITED", "Too many attempts")
        elif p["passphrase"] != MOCK_PASSPHRASE:
            self._failures["passphrase"] += 1
            await self._result(seq, False, "BAD_PASSPHRASE")
        else:
            self._failures["passphrase"] = 0
            unlocked = sc.resolve(sc.fixtures()["portfolio_unlocked"], self.clock.now(), 1.0)
            await self.emit("panel.update", unlocked)
            await self._result(seq, True)

    async def _on_action_decide(self, seq: int, p: Json) -> None:
        action = self.model.actions.get(p["action_id"])
        if not self.device["can_approve"]:
            await self._result(seq, False, "FORBIDDEN_DEVICE", "Approvals are disabled on this device")
        elif action is None:
            await self._result(seq, False, "UNKNOWN_ACTION")
        elif not self._step_up_ok(p["step_up_token"]):
            await self._result(seq, False, "STEP_UP_REQUIRED")
        elif p["payload_sha256"] != action["payload_sha256"]:
            await self._result(seq, False, "HASH_MISMATCH", "Payload changed; review it again")
        elif action["expires_at"] <= self.clock.now():
            await self._result(seq, False, "EXPIRED")
            await self.emit(
                "action.resolved", {"action_id": action["action_id"], "outcome": "expired", "at": self.clock.now()}
            )
        elif p["decision"] == "deny":
            await self._result(seq, True)
            await self.emit(
                "action.resolved", {"action_id": action["action_id"], "outcome": "denied", "at": self.clock.now()}
            )
        else:
            await self._result(seq, True)
            await self.emit(
                "action.resolved", {"action_id": action["action_id"], "outcome": "approved", "at": self.clock.now()}
            )
            self._spawn(self._execute_later(action["action_id"]))

    async def _execute_later(self, action_id: str) -> None:
        await self.clock.sleep_ms(1500)
        await self.emit("action.resolved", {"action_id": action_id, "outcome": "executed", "at": self.clock.now()})

    async def _on_task_kill(self, seq: int, p: Json) -> None:
        task = self.model.tasks.get(p["task_id"])
        if task is None:
            await self._result(seq, False, "UNKNOWN_TASK")
            return
        await self.emit("task.progress", {**task, "status": "killed"})
        await self._result(seq, True)

    async def _on_killswitch_all(self, seq: int, p: Json) -> None:
        for task in list(self.model.tasks.values()):
            if task["status"] in ("queued", "running", "blocked"):
                await self.emit("task.progress", {**task, "status": "killed"})
        await self.emit(
            "alert", {"alert_id": f"ks-{seq}", "level": "warn", "code": "KILLSWITCH", "message": "All research stopped"}
        )
        await self._result(seq, True)

    async def _interactive_guard(self, seq: int) -> bool:
        if self._scenario_playing:
            await self._result(seq, False, "MOCK_SCENARIO_PLAYING", "Run the mock with --scenario interactive")
            return False
        if self._busy or self.model.state["state"] not in RESTING:
            await self._result(seq, False, "BUSY")
            return False
        self._resting = dict(self.model.state)  # idle, or llm_unreachable with its code
        return True

    async def _on_text_input(self, seq: int, p: Json) -> None:
        if not await self._interactive_guard(seq):
            return
        await self._result(seq, True)
        uid = f"u{seq}"
        await self.emit("transcript.final", {"utterance_id": uid, "role": "user", "text": p["text"], "source": "text"})
        reply = f"Mock reply. You typed {len(p['text'])} characters; the real answer comes from atlas-core."
        steps = [
            sc.Step(0, "state.changed", {"state": "thinking"}),
            *sc.gen_assistant_reply(400, f"a{seq}", reply, source="text"),
        ]
        self._spawn(self._run_reply(steps, tail_ms=300))

    async def _on_ptt_start(self, seq: int, p: Json) -> None:
        if not await self._interactive_guard(seq):
            return
        self._busy = True
        await self._result(seq, True)
        await self.emit("state.changed", {"state": "listening"})
        self._spawn(self._mic_until_stopped())

    async def _mic_until_stopped(self) -> None:
        n = 0
        while self._busy and self.model.state["state"] == "listening" and n < 30_000 // 32:
            for step in sc.gen_mic_levels(0, 32, seed=n):
                await self.emit(step.type, sc.resolve(step.payload, self.clock.now(), 1.0))
            await self.clock.sleep_ms(32)
            n += 1

    async def _on_ptt_stop(self, seq: int, p: Json) -> None:
        if self.model.state["state"] != "listening":
            await self._result(seq, False, "NOT_LISTENING")
            return
        await self._result(seq, True)
        uid = f"u{seq}"
        await self.emit(
            "transcript.final",
            {"utterance_id": uid, "role": "user", "text": "(mock push-to-talk utterance)", "source": "voice"},
        )
        reply = "This is the mock voice reply, so the orb can show speaking."
        steps = [
            sc.Step(0, "state.changed", {"state": "thinking"}),
            *sc.gen_tts_envelope(900, 3200, seed=seq),
            sc.Step(900, "state.changed", {"state": "speaking"}),
            *sc.gen_assistant_reply(900, f"a{seq}", reply),
        ]
        # The last TTS batch is sent 150 ms before it plays; wait for it to finish playing.
        self._spawn(self._run_reply(sorted(steps, key=lambda s: s.at), tail_ms=150 + 8 * sc.FRAME_MS))

    async def _run_reply(self, steps: list[sc.Step], tail_ms: int) -> None:
        self._busy = True
        try:
            await self.play_steps(steps)
            await self.clock.sleep_ms(tail_ms)
            await self.emit("state.changed", self._resting)
        finally:
            self._busy = False

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for background replies/executions to finish (tests and validation)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks))

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
