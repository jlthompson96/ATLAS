"""End to end: core (scripted model) <-> control socket <-> atlas-ui <-> a real WSS client.

Covers the HUD flows (type a message, approve a pending action with the PIN), AP-1/AP-4 through the whole
stack, PIN rate limiting, privacy mode, layouts and core outages.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from atlas.ui_server.protocol import Contract
from tests.ui_support import PIN, Stack, WsClient

Json = dict[str, Any]


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def propose_email(s: Stack, client: WsClient) -> Json:
    """Ask the scripted model to draft an email; return the ``action.pending`` payload."""
    assert (await client.request("text.input", {"text": "please send an email to Dana"}))["ok"]
    frame = await client.of_type("action.pending")
    await client.recv_until(lambda f: f["type"] == "state.changed" and f["payload"]["state"] == "idle")
    payload: Json = frame["payload"]
    return payload


def test_snapshot_reflects_core_and_the_device() -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair("remote"))
            snap = client.frames[0]
            assert snap["type"] == "sync.snapshot" and snap["seq"] == 0
            p = snap["payload"]
            assert p["state"] == {"state": "idle"}
            assert p["device"]["role"] == "remote" and p["device"]["can_approve"] is True
            assert p["server"]["schema_revision"] == Contract.load().revision
            assert p["privacy"] == {"on": False, "reason": "user"}
            await client.conn.close()

    run(go())


def test_typing_a_message_runs_the_real_orchestrator() -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair())
            assert (await client.request("text.input", {"text": "what time is it?"}))["ok"]
            user = await client.of_type("transcript.final")
            assert user["payload"]["role"] == "user" and user["payload"]["source"] == "text"
            assert (await client.of_type("state.changed"))["payload"]["state"] == "thinking"
            reply = await client.recv_until(
                lambda f: f["type"] == "transcript.final" and f["payload"]["role"] == "assistant"
            )
            assert "found" in reply["payload"]["text"].lower()
            assert (await client.of_type("state.changed"))["payload"]["state"] == "idle"
            # a second request is accepted again once the first finished
            assert (await client.request("text.input", {"text": "hello"}))["ok"]

    run(go())


def test_busy_while_the_orb_is_not_resting() -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair())
            await s.core.hub.emit("state.changed", {"state": "listening"})  # e.g. a voice turn in progress
            res = await client.request("text.input", {"text": "and now?"})
            assert res["ok"] is False and res["code"] == "BUSY"

    run(go())


def test_approve_with_pin_runs_the_executor_with_the_exact_payload() -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair("remote"))
            action = await propose_email(s, client)
            assert action["kind"] == "email.send" and action["redacted"] is False
            canonical = action["payload_canonical"]
            sha = hashlib.sha256(canonical.encode()).hexdigest()
            assert sha == action["payload_sha256"]

            token = await s.step_up(client, PIN)
            res = await client.request(
                "action.decide",
                {
                    "action_id": action["action_id"],
                    "decision": "approve",
                    "payload_sha256": sha,
                    "step_up_token": token,
                },
            )
            assert res["ok"] is True
            await client.wait_for(lambda f: f["type"] == "action.resolved" and f["payload"]["outcome"] == "executed")
            outcomes = [f["payload"]["outcome"] for f in client.frames if f["type"] == "action.resolved"]
            assert outcomes == ["approved", "executed"]
            await s.core.control.drain()
            assert len(s.executor.envelopes) == 1
            assert json.dumps(s.executor.envelopes[0]).count(action["payload_sha256"]) >= 1
            # A fresh snapshot no longer lists it.
            await client.send("sync.request")
            snap = await client.of_type("sync.snapshot")
            assert snap["payload"]["actions"] == []

    run(go())


def test_ap_1_wrong_hash_is_refused_and_the_action_stays_pending() -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair("remote"))
            action = await propose_email(s, client)
            token = await s.step_up(client)
            res = await client.request(
                "action.decide",
                {
                    "action_id": action["action_id"],
                    "decision": "approve",
                    "payload_sha256": "0" * 64,
                    "step_up_token": token,
                },
            )
            assert res["ok"] is False and res["code"] == "HASH_MISMATCH"
            assert s.executor.envelopes == []
            assert [a.action_id for a in s.core.runtime.pending.pending()] == [action["action_id"]]

    run(go())


def test_approval_without_step_up_is_refused() -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair("remote"))
            action = await propose_email(s, client)
            res = await client.request(
                "action.decide",
                {
                    "action_id": action["action_id"],
                    "decision": "approve",
                    "payload_sha256": action["payload_sha256"],
                    "step_up_token": "x" * 24,  # never issued
                },
            )
            assert res["ok"] is False and res["code"] == "STEP_UP_REQUIRED"
            assert s.executor.envelopes == []

    run(go())


def test_deny_resolves_without_executing() -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair("remote"))
            action = await propose_email(s, client)
            token = await s.step_up(client)
            res = await client.request(
                "action.decide",
                {
                    "action_id": action["action_id"],
                    "decision": "deny",
                    "payload_sha256": action["payload_sha256"],
                    "step_up_token": token,
                },
            )
            assert res["ok"] is True
            denied = await client.wait_for(lambda f: f["type"] == "action.resolved")
            assert denied["payload"]["outcome"] == "denied"
            assert s.executor.envelopes == []

    run(go())


def test_ap_4_kiosk_cannot_approve_and_cannot_get_a_step_up() -> None:
    async def go() -> None:
        async with Stack() as s:
            remote = await s.ws(await s.pair("remote"))
            kiosk = await s.ws(await s.pair("kiosk"))
            assert kiosk.frames[0]["payload"]["device"] == {
                "device_id": kiosk.frames[0]["payload"]["device"]["device_id"],
                "role": "kiosk",
                "can_approve": False,
                "can_unlock_portfolio": False,
            }
            action = await propose_email(s, remote)
            await kiosk.wait_for(
                lambda f: f["type"] == "action.pending"
            )  # the kiosk sees pending actions, it just can't decide them
            res = await kiosk.request("stepup.verify", {"pin": PIN})
            assert res["code"] == "FORBIDDEN_DEVICE"
            stolen = await s.step_up(remote)  # even with a valid token obtained elsewhere
            res = await kiosk.request(
                "action.decide",
                {
                    "action_id": action["action_id"],
                    "decision": "approve",
                    "payload_sha256": action["payload_sha256"],
                    "step_up_token": stolen,
                },
            )
            assert res["ok"] is False and res["code"] == "FORBIDDEN_DEVICE"
            assert s.executor.envelopes == []
            # The kiosk may keep its view-only role for privacy mode: a tap turns it back off.
            assert (await kiosk.request("privacy.set", {"on": True}))["ok"]
            assert (await kiosk.request("privacy.set", {"on": False}))["ok"]
            assert (await kiosk.request("portfolio.unlock", {"passphrase": "x"}))["code"] == "FORBIDDEN_DEVICE"

    run(go())


def test_pin_lockout_after_five_wrong_pins() -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair("remote"))
            codes = [(await client.request("stepup.verify", {"pin": "1111"}))["code"] for _ in range(5)]
            assert codes == ["BAD_PIN"] * 4 + ["RATE_LIMITED"]
            # Locked: even the right PIN is refused, and no token is granted.
            res = await client.request("stepup.verify", {"pin": PIN})
            assert res["ok"] is False and res["code"] == "RATE_LIMITED"
            assert "stepup.granted" not in client.types()
            # A different device is not locked by the first one's failures (only the global limit applies).
            other = await s.ws(await s.pair("remote"))
            assert (await other.request("stepup.verify", {"pin": "1111"}))["code"] == "BAD_PIN"

    run(go())


def test_pin_global_lockout_across_devices() -> None:
    async def go() -> None:
        async with Stack() as s:
            clients = [await s.ws(await s.pair("remote")) for _ in range(4)]
            for client in clients:
                for _ in range(4):
                    await client.request("stepup.verify", {"pin": "9999"})
            # 16 failures >= the global limit of 15: a fresh device with the right PIN is locked out too.
            fresh = await s.ws(await s.pair("remote"))
            res = await fresh.request("stepup.verify", {"pin": PIN})
            assert res["code"] == "RATE_LIMITED"

    run(go())


def test_step_up_token_is_single_use_across_decisions() -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair("remote"))
            action = await propose_email(s, client)
            token = await s.step_up(client)
            body = {
                "action_id": action["action_id"],
                "decision": "approve",
                "payload_sha256": action["payload_sha256"],
                "step_up_token": token,
            }
            assert (await client.request("action.decide", body))["ok"]
            replay = await client.request("action.decide", body)
            assert replay["ok"] is False  # already decided: a replay is refused

    run(go())


def test_privacy_mode_is_enforced_on_the_server() -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair("remote"))
            action = await propose_email(s, client)
            assert (await client.request("privacy.set", {"on": True}))["ok"]
            snap = await client.wait_for(  # every privacy change is followed by a snapshot
                lambda f: f["type"] == "sync.snapshot" and f["payload"]["privacy"]["on"]
            )
            assert snap["payload"]["privacy"]["on"] is True
            assert snap["payload"]["transcript"] == []
            assert snap["payload"]["actions"] == [
                {"action_id": action["action_id"], "expires_at": action["expires_at"], "redacted": True}
            ]
            before = len(client.frames)
            assert (await client.request("text.input", {"text": "what time is it?"}))["ok"]
            await client.recv_until(lambda f: f["type"] == "state.changed" and f["payload"]["state"] == "idle")
            leaked = [f for f in client.frames[before:] if f["type"] in ("transcript.final", "assistant.delta")]
            assert leaked == []
            # Leaving privacy mode from a remote device needs the PIN.
            res = await client.request("privacy.set", {"on": False})
            assert res["code"] == "STEP_UP_REQUIRED"
            token = await s.step_up(client)
            assert (await client.request("privacy.set", {"on": False, "step_up_token": token}))["ok"]

    run(go())


def test_invalid_client_frames_get_error_results_and_never_crash_the_session() -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair())
            await client.conn.send("not json")
            assert (await client.of_type("request.result"))["payload"]["code"] == "INVALID_JSON"
            await client.conn.send(json.dumps({"v": 1, "type": "text.input", "seq": 5, "ts": 0, "payload": {}}))
            assert (await client.of_type("request.result"))["payload"]["code"] == "INVALID_MESSAGE"
            await client.conn.send(json.dumps({"v": 1, "type": "bogus", "seq": 6, "ts": 0, "payload": {}}))
            assert (await client.of_type("request.result"))["payload"]["code"] == "INVALID_MESSAGE"
            await client.send("sync.request")  # seq 1: fine
            await client.of_type("sync.snapshot")
            client.seq = 0  # reuse of a sequence number is refused
            await client.send("sync.request")
            assert (await client.of_type("request.result"))["payload"]["code"] == "SEQ_NOT_INCREASING"

    run(go())


def test_sensitive_fields_never_reach_the_logs(caplog: Any) -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair("remote"))
            await client.send("stepup.verify", {"pin": "4321"})
            await client.of_type("request.result")
            token = await s.step_up(client)
            await client.send(
                "action.decide",
                {"action_id": "act-none", "decision": "deny", "payload_sha256": "0" * 64, "step_up_token": token},
            )
            await client.of_type("request.result")
            await client.send("portfolio.unlock", {"passphrase": "hunter2-passphrase"})
            await client.of_type("request.result")
            await client.send("text.input", {"text": "my private question about Dana"})  # events.v1 1.2.0
            await client.of_type("request.result")
            await client.conn.close()
            assert token  # for the assertions below

            # (the websockets *client* in this test logs its own frames; only the server side matters)
            text = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("websockets.client"))
            for secret in ("4321", "hunter2-passphrase", "my private question"):
                assert secret not in text
            assert token not in text

    import logging

    caplog.set_level(logging.DEBUG)
    run(go())


def test_layout_save_persists_across_restarts(tmp_path: Path) -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair())
            slots = {"left": ["system"], "right": ["pending"], "bottom": "transcript"}
            assert (await client.request("layout.save", {"profile": "compact", "slots": slots}))["ok"]
            assert json.loads((s.cfg.state_dir / "layouts.json").read_text())["compact"] == slots
            await client.send("sync.request")
            snap = await client.of_type("sync.snapshot")
            assert snap["payload"]["layouts"]["compact"] == slots

    run(go())


def test_core_outage_shows_llm_unreachable_then_recovers() -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair())
            await s.core.control.stop()
            offline = await client.recv_until(lambda f: f["type"] == "state.changed")
            assert offline["payload"] == {"state": "llm_unreachable", "code": "CORE_UNREACHABLE"}
            alert = await client.of_type("alert")
            assert alert["payload"]["code"] == "CORE_UNREACHABLE"
            res = await client.request("text.input", {"text": "hello?"})
            assert res["ok"] is False and res["code"] == "CORE_UNREACHABLE"

            await s.core.control.start(s.sock)
            snap = await client.of_type("sync.snapshot", wait_s=10)
            assert snap["payload"]["state"] == {"state": "idle"}
            recovered = await client.of_type("alert")
            assert recovered["payload"]["code"] == "CORE_RECOVERED"
            assert (await client.request("text.input", {"text": "what time is it?"}))["ok"]

    run(go())


def test_ui_starts_before_core_and_shows_core_unreachable() -> None:
    async def go() -> None:
        async with Stack(start_core=False) as s:
            client = await s.ws(await s.pair())
            assert client.frames[0]["payload"]["state"] == {"state": "llm_unreachable", "code": "CORE_UNREACHABLE"}
            await s.core.control.start(s.sock)
            snap = await client.of_type("sync.snapshot", wait_s=10)
            assert snap["payload"]["state"] == {"state": "idle"}
            assert "alert" not in [f["type"] for f in client.frames[1:]]  # first connection: not a "recovery"

    run(go())


def test_server_frames_are_schema_valid_and_ordered() -> None:
    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair("remote"))
            await propose_email(s, client)
            contract = Contract.load()
            seqs = [f["seq"] for f in client.frames]
            assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
            for frame in client.frames:
                contract.validate("server", frame)

    run(go())


def test_an_invalid_frame_is_logged_without_its_content(caplog: Any) -> None:
    """Redaction is by field name, so a malformed frame logs metadata only (2026-11-25)."""

    async def go() -> None:
        async with Stack() as s:
            client = await s.ws(await s.pair("remote"))
            await client.conn.send(
                json.dumps(
                    {"v": 1, "type": "text.input", "seq": 1, "ts": 1, "payload": {"txt": "a secret in the wrong key"}}
                )
            )
            assert (await client.of_type("request.result"))["payload"]["code"] == "INVALID_MESSAGE"
            await client.conn.close()
            text = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("websockets.client"))
            assert "a secret in the wrong key" not in text
            assert "invalid client frame" in text and "type text.input" in text and "schema error(s)" in text

    import logging

    caplog.set_level(logging.DEBUG)
    run(go())
