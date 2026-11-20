"""The atlas-core control socket: peer identity, per-peer operation tables, protocol errors, approvals."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from atlas.audit.peercred import PeerCredentialError, SockLike
from atlas.core.control import PEER_OPS, UI_OPS, ControlServer, KernelPeers, SameUserPeers
from atlas.core.control_client import ControlClient, ControlError
from atlas.ui_server.dev import CoreStack, build_core
from tests.ui_support import InstantExecutor

Json = dict[str, Any]


class FixedPeer:
    """Pretends the kernel identified every connection as ``name`` (or refuses it)."""

    def __init__(self, name: str | None) -> None:
        self.name = name

    def identify(self, sock: SockLike) -> str:
        if self.name is None:
            raise PeerCredentialError("nope")
        return self.name


def with_core(peers: Any, body: Callable[[CoreStack, Path, list[tuple[str, Json]]], Awaitable[None]]) -> None:
    async def go() -> None:
        base = Path(tempfile.mkdtemp(prefix="atlas-core-", dir="/tmp"))
        core = build_core(peers=peers, executor=InstantExecutor())
        sock = base / "core.sock"
        await core.control.start(sock)
        try:
            await body(core, sock, [])
        finally:
            await core.control.stop()
            shutil.rmtree(base, ignore_errors=True)

    asyncio.run(go())


async def connect(sock: Path, events: list[tuple[str, Json]]) -> ControlClient:
    client = ControlClient(sock, lambda t, p: events.append((t, p)))
    await client.connect()
    return client


def test_socket_is_not_world_accessible() -> None:
    async def body(core: CoreStack, sock: Path, events: list[tuple[str, Json]]) -> None:
        assert os.stat(sock).st_mode & 0o777 == 0o660  # noqa: PTH116 - group access only, as systemd socket units set it

    with_core(SameUserPeers(), body)


def test_same_user_peer_is_accepted_and_ping_works() -> None:
    async def body(core: CoreStack, sock: Path, events: list[tuple[str, Json]]) -> None:
        client = await connect(sock, events)
        reply = await client.call("ping")
        assert reply["ok"] is True and isinstance(reply["t"], int)
        await client.close()

    with_core(SameUserPeers(), body)


def test_kernel_peer_credentials_identify_this_process_uid() -> None:
    """The real SO_PEERCRED / LOCAL_PEERCRED path: our own uid is not an ATLAS user, so it is refused."""

    async def body(core: CoreStack, sock: Path, events: list[tuple[str, Json]]) -> None:
        client = await connect(sock, events)
        with pytest.raises(ControlError):
            await client.call("ping")  # the server closed the connection without answering
        await client.close()

    with_core(KernelPeers(), body)


def test_unidentifiable_peers_are_dropped_before_any_operation() -> None:
    async def body(core: CoreStack, sock: Path, events: list[tuple[str, Json]]) -> None:
        client = await connect(sock, events)
        with pytest.raises(ControlError):
            await client.call("subscribe")
        await client.close()
        assert core.hub._subscribers == set()  # type: ignore[attr-defined]

    with_core(FixedPeer(None), body)


def test_dev_fallback_rejects_a_different_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(core: CoreStack, sock: Path, events: list[tuple[str, Json]]) -> None:
        monkeypatch.setattr(os, "getuid", lambda: 99_999)
        client = await connect(sock, events)
        with pytest.raises(ControlError):
            await client.call("ping")
        await client.close()

    with_core(SameUserPeers(), body)


def test_non_ui_peers_can_only_ping() -> None:
    async def body(core: CoreStack, sock: Path, events: list[tuple[str, Json]]) -> None:
        client = await connect(sock, events)
        assert (await client.call("ping"))["ok"] is True
        for op in sorted(UI_OPS - {"ping"}):
            reply = await client.call(op, device_id="dev-x", text="hi")
            assert reply == {"id": reply["id"], "ok": False, "code": "FORBIDDEN_PEER"}, op
        assert core.runtime.pending.pending() == [] and core.hub.model.transcript == []
        await client.close()

    with_core(FixedPeer("atlas-voice"), body)


def test_peer_table_matches_services_md() -> None:
    text = (Path(__file__).resolve().parents[2] / "contracts" / "services.md").read_text()
    row = next(line for line in text.splitlines() if line.startswith("| `atlas-core` control socket"))
    listed = {part.strip(" `") for part in row.split("|")[3].split(",")}
    assert listed == set(PEER_OPS)
    assert PEER_OPS["atlas-ui"] == UI_OPS
    assert all(ops == {"ping"} for name, ops in PEER_OPS.items() if name != "atlas-ui")


def test_malformed_requests_get_errors_not_crashes() -> None:
    async def body(core: CoreStack, sock: Path, events: list[tuple[str, Json]]) -> None:
        reader, writer = await asyncio.open_unix_connection(str(sock))
        for raw in (b"not json\n", b"[1,2]\n", b'{"id":"x","op":"ping"}\n', b'{"id":1}\n', b'{"id":2,"op":"nope"}\n'):
            writer.write(raw)
            await writer.drain()
            reply = json.loads(await reader.readline())
            assert reply["ok"] is False
            assert reply["code"] in ("INVALID_MESSAGE", "FORBIDDEN_PEER")
        writer.write(b'{"id":3,"op":"ping"}\n')
        await writer.drain()
        assert json.loads(await reader.readline())["ok"] is True  # still alive
        writer.close()

    with_core(SameUserPeers(), body)


def test_field_validation_for_ui_operations() -> None:
    async def body(core: CoreStack, sock: Path, events: list[tuple[str, Json]]) -> None:
        client = await connect(sock, events)
        bad = [
            ("text.input", {"device_id": "d", "text": ""}),
            ("text.input", {"device_id": "d", "text": "x" * 2001}),
            ("text.input", {"text": "hi"}),
            ("device.announce", {"device_id": "d", "role": "admin"}),
            ("stepup.register", {"device_id": "d", "token": "short", "expires_at": 1}),
            (
                "action.decide",
                {"device_id": "d", "action_id": "a", "decision": "maybe", "payload_sha256": "x", "step_up_token": "y"},
            ),
            ("task.kill", {"task_id": "nope"}),
        ]
        for op, fields in bad:
            reply = await client.call(op, **fields)
            assert reply["ok"] is False and reply["code"] in ("INVALID_MESSAGE", "UNKNOWN_TASK"), (op, reply)
        await client.close()

    with_core(SameUserPeers(), body)


def test_stepup_tokens_cannot_outlive_sixty_seconds() -> None:
    async def body(core: CoreStack, sock: Path, events: list[tuple[str, Json]]) -> None:
        client = await connect(sock, events)
        far = int(asyncio.get_running_loop().time() * 1000) + 10**13
        reply = await client.call("stepup.register", device_id="d", token="t" * 24, expires_at=far)
        assert reply["ok"] is False and reply["code"] == "INVALID_MESSAGE"
        await client.close()

    with_core(SameUserPeers(), body)


def test_subscribe_returns_a_snapshot_then_streams_events() -> None:
    async def body(core: CoreStack, sock: Path, events: list[tuple[str, Json]]) -> None:
        await core.hub.emit("alert", {"alert_id": "a1", "level": "info", "code": "CORE_RECOVERED", "message": "m"})
        client = await connect(sock, events)
        reply = await client.call("subscribe")
        snap = reply["snapshot"]
        assert snap["state"] == {"state": "idle"} and [a["alert_id"] for a in snap["alerts"]] == ["a1"]
        await core.hub.emit("alert", {"alert_id": "a2", "level": "info", "code": "CORE_RECOVERED", "message": "m"})
        for _ in range(50):
            if events:
                break
            await asyncio.sleep(0.01)
        assert events == [("alert", {"alert_id": "a2", "level": "info", "code": "CORE_RECOVERED", "message": "m"})]
        await client.close()
        await asyncio.sleep(0.05)
        assert core.hub._subscribers == set()  # type: ignore[attr-defined]

    with_core(SameUserPeers(), body)


def test_core_enforces_the_kiosk_rule_itself_ap_4() -> None:
    """Even a (confused or compromised) atlas-ui that forwards a kiosk's decision gets refused by core."""

    async def body(core: CoreStack, sock: Path, events: list[tuple[str, Json]]) -> None:
        client = await connect(sock, events)
        await client.call("device.announce", device_id="kiosk-1", role="kiosk")
        out = await core.runtime.orchestrator.handle("please send an email to Dana", store=core.runtime.store)
        (action_id,) = out.pending_actions
        action = core.runtime.pending.get(action_id)
        assert action is not None
        far = int(__import__("time").time() * 1000) + 30_000
        await client.call("stepup.register", device_id="kiosk-1", token="k" * 24, expires_at=far)
        reply = await client.call(
            "action.decide",
            device_id="kiosk-1",
            action_id=action_id,
            decision="approve",
            payload_sha256=action.payload_sha256,
            step_up_token="k" * 24,
        )
        assert reply["ok"] is False and reply["code"] == "FORBIDDEN_DEVICE"
        # An unknown device is refused too, and a remote one succeeds only with its own token.
        await client.call("device.announce", device_id="hud-1", role="remote")
        reply = await client.call(
            "action.decide",
            device_id="hud-1",
            action_id=action_id,
            decision="approve",
            payload_sha256=action.payload_sha256,
            step_up_token="k" * 24,  # registered for the kiosk, not for hud-1
        )
        assert reply["ok"] is False and reply["code"] == "STEP_UP_REQUIRED"
        await client.call("stepup.register", device_id="hud-1", token="h" * 24, expires_at=far)
        reply = await client.call(
            "action.decide",
            device_id="hud-1",
            action_id=action_id,
            decision="approve",
            payload_sha256=action.payload_sha256,
            step_up_token="h" * 24,
        )
        assert reply["ok"] is True
        await core.control.drain()
        await client.close()

    with_core(SameUserPeers(), body)


def test_start_replaces_a_stale_socket_but_refuses_to_replace_a_file() -> None:
    async def go() -> None:
        base = Path(tempfile.mkdtemp(prefix="atlas-core-", dir="/tmp"))
        try:
            sock = base / "core.sock"
            with socket.socket(socket.AF_UNIX) as stale:  # a crashed core leaves its socket file behind
                stale.bind(str(sock))
            assert sock.exists()
            again = build_core(peers=SameUserPeers(), executor=InstantExecutor()).control
            await again.start(sock)
            client = ControlClient(sock, lambda t, p: None)
            await client.connect()
            assert (await client.call("ping"))["ok"] is True
            await client.close()
            await again.stop()
            (base / "regular").write_text("precious")
            with pytest.raises(RuntimeError, match="not a socket"):
                await again.start(base / "regular")
            assert (base / "regular").read_text() == "precious"
        finally:
            shutil.rmtree(base, ignore_errors=True)

    asyncio.run(go())


def test_control_server_type_is_exported() -> None:
    assert ControlServer.__name__ == "ControlServer"
