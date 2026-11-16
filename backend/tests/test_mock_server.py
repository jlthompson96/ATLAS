"""Mock event server: every scenario conforms to events.v1, and interactive flows behave like the
contract says the real server will."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from atlas.ui_server.mock import scenario as sc
from atlas.ui_server.mock.__main__ import VALIDATION_T0, validate_scenario
from atlas.ui_server.mock.server import Options, build_server
from atlas.ui_server.mock.session import MOCK_PASSPHRASE, MOCK_PIN, FakeClock, Session

Json = dict[str, Any]


@pytest.mark.parametrize("name", [sc.BASELINE, *sc.available()])
def test_scenario_conforms(name: str) -> None:
    frames, warnings = asyncio.run(validate_scenario(name))
    assert frames >= 1
    assert warnings == []


def test_scenarios_cover_every_orb_state() -> None:
    seen = set()
    for name in sc.available():
        seen |= {s.payload["state"] for s in sc.load(name).steps if s.type == "state.changed"}
    assert seen == {"idle", "listening", "thinking", "speaking", "waking_host", "error", "llm_unreachable"}


class Harness:
    """A session with a fake clock that records every frame it sends."""

    def __init__(self, device_role: str = "remote", scenario: str = sc.BASELINE) -> None:
        self.frames: list[Json] = []
        self.session = Session(self._collect, device_role=device_role, clock=FakeClock(VALIDATION_T0))
        self.session.apply_setup(sc.load(scenario))
        self._seq = 0

    async def _collect(self, raw: str) -> None:
        self.frames.append(json.loads(raw))

    async def send(self, type_: str, payload: Json) -> list[Json]:
        start = len(self.frames)
        self._seq += 1
        await self.session.handle_raw(
            json.dumps({"v": 1, "type": type_, "seq": self._seq, "ts": 0, "payload": payload})
        )
        await self.session.drain()
        return self.frames[start:]

    def result(self, frames: list[Json]) -> Json:
        return next(f["payload"] for f in frames if f["type"] == "request.result")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_clock_ping_and_snapshot() -> None:
    async def go() -> None:
        h = Harness()
        await h.session.snapshot()
        out = await h.send("clock.ping", {"client_t": 1234.5})
        assert out[0]["type"] == "clock.pong"
        assert out[0]["payload"] == {"client_t": 1234.5, "server_t": VALIDATION_T0}
        out = await h.send("sync.request", {})
        assert out[0]["type"] == "sync.snapshot"
        assert out[0]["payload"]["device"]["role"] == "remote"

    run(go())


def test_approval_flow_is_bound_to_hash_and_step_up() -> None:
    async def go() -> None:
        h = Harness(scenario="approvals")
        await h.session.snapshot()
        await h.session.play(sc.load("approvals"))
        action = h.session.model.actions["act-101"]
        decide = {
            "action_id": "act-101",
            "decision": "approve",
            "payload_sha256": action["payload_sha256"],
            "step_up_token": "x" * 32,
        }

        assert h.result(await h.send("action.decide", decide))["code"] == "STEP_UP_REQUIRED"
        assert h.result(await h.send("stepup.verify", {"pin": "9999"}))["code"] == "BAD_PIN"
        out = await h.send("stepup.verify", {"pin": MOCK_PIN})
        token = next(f for f in out if f["type"] == "stepup.granted")["payload"]["token"]

        tampered = {**decide, "step_up_token": token, "payload_sha256": "0" * 64}
        assert h.result(await h.send("action.decide", tampered))["code"] == "HASH_MISMATCH"

        out = await h.send("action.decide", {**decide, "step_up_token": token})
        outcomes = [f["payload"]["outcome"] for f in out if f["type"] == "action.resolved"]
        assert outcomes == ["approved", "executed"]
        assert "act-101" not in h.session.model.actions

    run(go())


def test_kiosk_cannot_approve_or_unlock() -> None:
    async def go() -> None:
        h = Harness(device_role="kiosk")
        await h.session.snapshot()
        out = await h.send("portfolio.unlock", {"passphrase": MOCK_PASSPHRASE})
        assert h.result(out)["code"] == "FORBIDDEN_DEVICE"
        decide = {"action_id": "act-101", "decision": "approve", "payload_sha256": "a" * 64, "step_up_token": "x" * 32}
        assert h.result(await h.send("action.decide", decide))["code"] == "FORBIDDEN_DEVICE"

    run(go())


def test_portfolio_unlock_and_rate_limit(caplog: pytest.LogCaptureFixture) -> None:
    async def go() -> None:
        h = Harness()
        await h.session.snapshot()
        with caplog.at_level(logging.INFO, logger="atlas.mock"):
            for _ in range(5):
                assert (
                    h.result(await h.send("portfolio.unlock", {"passphrase": "wrong-guess"}))["code"]
                    == "BAD_PASSPHRASE"
                )
        assert "wrong-guess" not in caplog.text  # never logged
        out = await h.send("portfolio.unlock", {"passphrase": MOCK_PASSPHRASE})
        assert h.result(out)["code"] == "RATE_LIMITED"

        h2 = Harness()
        await h2.session.snapshot()
        out = await h2.send("portfolio.unlock", {"passphrase": MOCK_PASSPHRASE})
        panel = next(f["payload"] for f in out if f["type"] == "panel.update")
        assert panel["data"]["locked"] is False
        assert h2.result(out)["ok"] is True

    run(go())


def test_privacy_mode_redacts_server_side() -> None:
    async def go() -> None:
        h = Harness()
        await h.session.snapshot()
        await h.session.play(sc.load("privacy"))
        types = [f["type"] for f in h.frames]
        assert "transcript.final" not in types
        snapshots = [f["payload"] for f in h.frames if f["type"] == "sync.snapshot"]
        assert len(snapshots) == 3  # initial + after each privacy change
        private = snapshots[1]
        assert private["privacy"]["on"] is True and private["transcript"] == []
        assert all(p["redacted"] for p in private["panels"] if p["sensitivity"] == "secret")
        email = next(
            f["payload"] for f in h.frames if f["type"] == "panel.update" and f["payload"]["panel_id"] == "email"
        )
        assert email == {**email, "redacted": True, "data": {"unread": 5}}
        task = next(f["payload"] for f in h.frames if f["type"] == "task.progress")
        assert "title" not in task
        assert any(t["role"] == "user" for t in snapshots[2]["transcript"])  # back once privacy is off

        assert h.result(await h.send("privacy.set", {"on": True}))["ok"] is True
        assert h.result(await h.send("privacy.set", {"on": False}))["code"] == "STEP_UP_REQUIRED"

    run(go())


def test_text_input_and_ptt_drive_legal_state_sequences() -> None:
    async def go() -> None:
        h = Harness()
        await h.session.snapshot()
        out = await h.send("text.input", {"text": "hello"})
        states = [f["payload"]["state"] for f in out if f["type"] == "state.changed"]
        assert states == ["thinking", "idle"]

        await h.send("ptt.start", {})
        assert h.session.model.state["state"] == "listening"
        out = await h.send("ptt.stop", {})
        states = [f["payload"]["state"] for f in out if f["type"] == "state.changed"]
        assert states == ["thinking", "speaking", "idle"]
        assert any(f["type"] == "audio.level" and f["payload"]["stream"] == "tts" for f in out)

    run(go())


def test_activity_ping_is_accepted_silently() -> None:
    async def go() -> None:
        h = Harness()
        await h.session.snapshot()
        assert await h.send("activity.ping", {}) == []
        assert h.session.last_activity_ms == VALIDATION_T0
        assert h.result(await h.send("activity.ping", {"x": 1}))["code"] == "INVALID_MESSAGE"

    run(go())


def test_invalid_client_frame_is_rejected() -> None:
    async def go() -> None:
        h = Harness()
        await h.session.snapshot()
        out = await h.send("ptt.start", {"unexpected": 1})
        assert h.result(out)["code"] == "INVALID_MESSAGE"
        await h.session.handle_raw("not json")
        assert h.frames[-1]["payload"]["code"] == "INVALID_JSON"

    run(go())


def test_websocket_end_to_end() -> None:
    async def go() -> None:
        async with build_server(Options(port=0, scenario="error", speed=20)) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}/ws/events") as ws:
                first = json.loads(await ws.recv())
                assert first["type"] == "sync.snapshot"
                await ws.send(json.dumps({"v": 1, "type": "clock.ping", "seq": 1, "ts": 0, "payload": {"client_t": 5}}))
                seen = []
                async with asyncio.timeout(5):
                    while "clock.pong" not in seen or "error" not in str(seen):
                        msg = json.loads(await ws.recv())
                        seen.append(msg["type"] if msg["type"] != "state.changed" else msg["payload"]["state"])
            with pytest.raises(InvalidStatus) as wrong_path:
                async with connect(f"ws://127.0.0.1:{port}/other"):
                    pass
            assert wrong_path.value.response.status_code == 404
            with pytest.raises(InvalidStatus) as foreign:
                async with connect(f"ws://127.0.0.1:{port}/ws/events", origin="https://evil.example"):  # type: ignore[arg-type]
                    pass
            assert foreign.value.response.status_code == 403

    run(go())
