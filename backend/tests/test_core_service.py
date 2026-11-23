"""atlas-core's production entry point (atlas.core.service): socket activation, no-desktop mode, key loading."""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from atlas.core import service
from atlas.core.auditing import RecordingAuditSink
from atlas.core.control import ControlServer, EventHub, SameUserPeers
from atlas.core.control_client import ControlClient
from atlas.llm.interface import BackendUnreachable, NotReady

Json = dict[str, Any]


def key_dir(tmp: Path) -> Path:
    pem = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    (tmp / service.SIGNING_KEY_CRED).write_bytes(pem)
    return tmp


def run_core(body: Callable[[ControlServer, EventHub, RecordingAuditSink, Path], Awaitable[None]]) -> None:
    async def go() -> None:
        base = Path(tempfile.mkdtemp(prefix="atlas-svc-", dir="/tmp"))
        hub, audit = EventHub(), RecordingAuditSink("atlas-core")
        control = service.build_control(
            backend=service.UnlinkedBackend(),
            hooks=service.UnlinkedHooks(hub),
            hub=hub,
            audit=audit,
            signer=service.load_signer(key_dir(base)),
            peers=SameUserPeers(),
            executor_socket=base / "executor.sock",
            state_dir=base,
        )
        await hub.emit("state.changed", dict(service.UNLINKED_STATE))
        await control.start(base / "core.sock")
        try:
            await body(control, hub, audit, base / "core.sock")
        finally:
            await control.stop()
            shutil.rmtree(base, ignore_errors=True)

    asyncio.run(go())


async def wait_for(events: list[tuple[str, Json]], pred: Callable[[list[tuple[str, Json]]], bool]) -> None:
    for _ in range(200):
        if pred(events):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out; events: {events}")


def assistant_finals(events: list[tuple[str, Json]]) -> list[str]:
    return [p["text"] for t, p in events if t == "transcript.final" and p["role"] == "assistant"]


def test_no_desktop_mode_answers_time_locally_and_refuses_the_rest() -> None:
    async def body(control: ControlServer, hub: EventHub, audit: RecordingAuditSink, sock: Path) -> None:
        events: list[tuple[str, Json]] = []
        client = ControlClient(sock, lambda t, p: events.append((t, p)))
        await client.connect()
        snap = (await client.call("subscribe"))["snapshot"]
        assert snap["state"] == service.UNLINKED_STATE  # the HUD shows "no desktop" from the first frame

        assert (await client.call("text.input", text="what time is it", device_id="d1"))["ok"]
        await wait_for(events, lambda e: len(assistant_finals(e)) == 1)
        assert assistant_finals(events)[0].startswith("It's ")

        assert (await client.call("text.input", text="send an email to dana", device_id="d1"))["ok"]
        await wait_for(events, lambda e: len(assistant_finals(e)) == 2)
        assert assistant_finals(events)[1] == service.UNLINKED_LINE
        states = [p for t, p in events if t == "state.changed"]
        assert states[-1] == service.UNLINKED_STATE  # back to resting "no desktop", never "idle"
        assert hub.model.state == service.UNLINKED_STATE
        # The planner never ran: no tool calls, no proposed actions.
        assert audit.records == []
        await client.close()

    run_core(body)


def test_unlinked_backend_refuses_everything() -> None:
    backend = service.UnlinkedBackend()
    with pytest.raises(BackendUnreachable):
        backend.chat([], max_tokens=1)
    assert isinstance(asyncio.run(backend.ensure_ready("m", 0.0)), NotReady)
    assert asyncio.run(backend.health(0.0)).reachable is False
    assert asyncio.run(backend.list_models(0.0)) == ()


def test_signing_key_must_be_an_ed25519_pem(tmp_path: Path) -> None:
    with pytest.raises(service.ServiceError, match="approval-signing-key"):
        service.load_signer(tmp_path)  # missing
    (tmp_path / service.SIGNING_KEY_CRED).write_text("not a key")
    with pytest.raises(service.ServiceError, match="approval-signing-key"):
        service.load_signer(tmp_path)
    assert service.load_signer(key_dir(tmp_path)).key_id


def test_systemd_socket_is_used_only_when_meant_for_this_process() -> None:
    assert service.systemd_socket({}, pid=10) is None
    assert service.systemd_socket({"LISTEN_PID": "11", "LISTEN_FDS": "1"}, pid=10) is None
    for count in ("0", "2", ""):
        with pytest.raises(service.ServiceError, match="exactly one socket"):
            service.systemd_socket({"LISTEN_PID": "10", "LISTEN_FDS": count}, pid=10)


def test_systemd_socket_takes_fd_3_and_rejects_the_wrong_kind() -> None:
    saved = os.dup(3) if _fd_open(3) else None
    try:
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(f"{d}/s")
            listener.listen()
            os.dup2(listener.fileno(), 3)
            listener.close()
            sock = service.systemd_socket({"LISTEN_PID": "10", "LISTEN_FDS": "1"}, pid=10)
            assert sock is not None and sock.family == socket.AF_UNIX and sock.getsockname() == f"{d}/s"
            sock.detach()
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            os.dup2(udp.fileno(), 3)
            udp.close()
            with pytest.raises(service.ServiceError, match="not a Unix stream socket"):
                service.systemd_socket({"LISTEN_PID": "10", "LISTEN_FDS": "1"}, pid=10)
    finally:
        if saved is not None:
            os.dup2(saved, 3)
            os.close(saved)
        else:
            os.close(3)


def _fd_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def test_control_start_needs_exactly_one_of_path_and_sock() -> None:
    async def body(control: ControlServer, hub: EventHub, audit: RecordingAuditSink, sock: Path) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            await control.start()
        with pytest.raises(ValueError, match="exactly one"):
            await control.start(sock, sock=socket.socket(socket.AF_UNIX))

    run_core(body)


def test_main_refuses_a_misconfiguration_with_status_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    common = ["--socket", str(tmp_path / "c.sock"), "--state-dir", str(tmp_path)]
    assert service.main([*common, "--credentials-dir", str(tmp_path)]) == 2  # no signing key
    assert "approval-signing-key" in capsys.readouterr().err
    key_dir(tmp_path)
    assert service.main([*common, "--credentials-dir", str(tmp_path), "--backend", "lmstudio"]) == 2
    assert "needs --model" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        service.parser().parse_args(["--backend", "fake"])  # no scripted model on the Pi
    assert service.parser().parse_args([]).backend == "none"


def test_main_without_a_socket_or_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LISTEN_PID", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    assert service.main([]) == 2
    assert "no socket from systemd" in capsys.readouterr().err
    assert service.main(["--socket", str(tmp_path / "x.sock")]) == 2
    assert "CREDENTIALS_DIRECTORY" in capsys.readouterr().err
