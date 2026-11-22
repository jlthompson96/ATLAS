"""atlas-audit Unix-socket server: actor from peer credentials, never from the body."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from atlas.audit.client import AuditClient, AuditError
from atlas.audit.log import AuditLog
from atlas.audit.peercred import KernelPeerCredentials, PeerCredentialError, SockLike
from atlas.audit.segments import list_segments
from atlas.audit.server import AuditServer
from atlas.audit.verify import verify_dir
from tests.conftest import egress_body, service_event, tool_call, write_ahead

MakeLog = Callable[..., AuditLog]


class FakeCreds:
    """Stands in for SO_PEERCRED: reports whatever uid the test says the next peer has."""

    def __init__(self) -> None:
        self.uid = 1001

    def peer_uid(self, sock: SockLike) -> int:
        if self.uid < 0:
            raise PeerCredentialError("no credentials")
        return self.uid


class FakeUsers:
    TABLE: ClassVar[dict[int, str]] = {
        1001: "atlas-core",
        1002: "atlas-executor",
        1003: "atlas-egress",
        1004: "atlas-panels",
        1099: "mallory",
    }

    def user_name(self, uid: int) -> str | None:
        return self.TABLE.get(uid)


@pytest.fixture
def sock_path() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="atl") as directory:  # short path: AF_UNIX limit is ~104 bytes
        yield Path(directory) / "audit.sock"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def raw_exchange(path: Path, payload: bytes) -> list[dict[str, Any]]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(payload)
    await writer.drain()
    replies: list[dict[str, Any]] = []
    try:
        while True:
            line = await asyncio.wait_for(reader.readline(), 2)
            if not line:
                break
            replies.append(json.loads(line))
            if payload.count(b"\n") == len(replies):
                break
    finally:
        writer.close()
    return replies


def make_server(log: AuditLog, creds: Any | None = None) -> tuple[AuditServer, FakeCreds]:
    fake = FakeCreds()
    return AuditServer(log, creds if creds is not None else fake, FakeUsers(), tick_interval_s=3600), fake


def line(obj: Any) -> bytes:
    return json.dumps(obj).encode() + b"\n"


def stored_actor(log: AuditLog, seq: int) -> str:
    for _, path in list_segments(log.data_dir):
        for raw in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(raw)
            if rec["seq"] == seq:
                return str(rec["actor"])
    raise AssertionError("seq not found")


def test_actor_comes_from_peer_credentials(make_log: MakeLog, sock_path: Path) -> None:
    async def scenario() -> None:
        log = make_log()
        server, creds = make_server(log)
        await server.listen_path(sock_path)
        try:
            creds.uid = 1004  # atlas-panels: tool_call belongs to atlas-core, so send a record panels may write
            [reply] = await raw_exchange(sock_path, line({"op": "append", "record": service_event()}))
            assert reply["ok"] is True
            assert stored_actor(log, reply["seq"]) == "atlas-panels"
        finally:
            await server.close()

    run(scenario())


def test_body_claiming_another_actor_is_refused(make_log: MakeLog, sock_path: Path) -> None:
    async def scenario() -> None:
        log = make_log()
        server, creds = make_server(log)
        await server.listen_path(sock_path)
        try:
            creds.uid = 1004
            head = log.head
            [reply] = await raw_exchange(
                sock_path, line({"op": "append", "record": {**tool_call(), "actor": "atlas-core"}})
            )
            assert reply == {"ok": False, "code": "ACTOR_MISMATCH"}
            assert log.head == head
        finally:
            await server.close()

    run(scenario())


def test_per_kind_actor_constraints_over_the_wire(make_log: MakeLog, sock_path: Path) -> None:
    async def scenario() -> None:
        log = make_log()
        server, creds = make_server(log)
        await server.listen_path(sock_path)
        try:
            creds.uid = 1001  # atlas-core may not write executor or egress records
            for body in (write_ahead(), egress_body()):
                [reply] = await raw_exchange(sock_path, line({"op": "append", "record": body}))
                assert reply["ok"] is False
                assert reply["code"] == "BAD_RECORD"
            creds.uid = 1002
            [reply] = await raw_exchange(sock_path, line({"op": "append", "record": write_ahead()}))
            assert reply["ok"] is True
            assert reply["durable"] is True
        finally:
            await server.close()

    run(scenario())


@pytest.mark.parametrize("uid", [1099, 4242, -1])
def test_unknown_peers_are_rejected(make_log: MakeLog, sock_path: Path, uid: int) -> None:
    async def scenario() -> None:
        log = make_log()
        server, creds = make_server(log)
        await server.listen_path(sock_path)
        try:
            creds.uid = uid
            head = log.head
            [reply] = await raw_exchange(sock_path, line({"op": "append", "record": tool_call()}))
            assert reply == {"ok": False, "code": "PEER_REJECTED"}
            assert log.head == head
        finally:
            await server.close()

    run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        b"not json\n",
        b'{"op":"append","record":{"kind":"tool_call","kind":"tool_call"}}\n',  # duplicate key
        b'{"op":"append","record":[1,2]}\n',
        b'{"op":"append","record":{},"extra":1}\n',
        b'{"op":"drop_table"}\n',
        b'{"op":"append","record":{"kind":"tool_call","args_redacted":{"x":1.5}}}\n',
    ],
)
def test_malformed_requests_are_refused(make_log: MakeLog, sock_path: Path, payload: bytes) -> None:
    async def scenario() -> None:
        log = make_log()
        server, _ = make_server(log)
        await server.listen_path(sock_path)
        try:
            head = log.head
            [reply] = await raw_exchange(sock_path, payload)
            assert reply["ok"] is False
            assert log.head == head
        finally:
            await server.close()

    run(scenario())


def test_oversized_request_is_refused(make_log: MakeLog, sock_path: Path) -> None:
    async def scenario() -> None:
        log = make_log()
        server, _ = make_server(log)
        await server.listen_path(sock_path)
        try:
            reader, writer = await asyncio.open_unix_connection(str(sock_path))
            writer.write(b'{"op":"append","record":{"pad":"' + b"x" * 200_000)
            with contextlib.suppress(ConnectionError):  # the server may hang up while we are still sending
                await writer.drain()
            head = log.head
            try:  # the refusal may be lost if the server resets the connection with unread input
                reply = json.loads(await asyncio.wait_for(reader.readline(), 2))
                assert reply == {"ok": False, "code": "TOO_LARGE"}
            except (ConnectionError, ValueError):
                pass
            assert log.head == head
            writer.close()
        finally:
            await server.close()

    run(scenario())


def test_outcome_unknown_is_only_for_core_and_executor(make_log: MakeLog, sock_path: Path) -> None:
    async def scenario() -> None:
        log = make_log()
        server, creds = make_server(log)
        await server.listen_path(sock_path)
        try:
            creds.uid = 1002
            await raw_exchange(sock_path, line({"op": "append", "record": write_ahead("apr-5")}))
            creds.uid = 1001
            [reply] = await raw_exchange(sock_path, line({"op": "outcome_unknown"}))
            assert [i["approval_id"] for i in reply["outcome_unknown"]] == ["apr-5"]
            creds.uid = 1004
            [denied] = await raw_exchange(sock_path, line({"op": "outcome_unknown"}))
            assert denied == {"ok": False, "code": "FORBIDDEN"}
        finally:
            await server.close()

    run(scenario())


def test_client_roundtrip_and_failure_modes(make_log: MakeLog, sock_path: Path) -> None:
    async def scenario() -> None:
        log = make_log()
        server, _ = make_server(log)
        await server.listen_path(sock_path)
        client = AuditClient(sock_path, timeout_s=2)
        try:
            first = await client.append(tool_call(1))
            second = await client.append(tool_call(2))  # same pooled connection
            assert second.seq == first.seq + 1
            with pytest.raises(AuditError) as refused:
                await client.append({"kind": "genesis"})
            assert refused.value.code == "BAD_RECORD"
        finally:
            await client.close()
            await server.close()
        with pytest.raises(AuditError) as unavailable:
            await AuditClient(sock_path, timeout_s=1).append(tool_call())
        assert unavailable.value.code == "AUDIT_UNAVAILABLE"

    run(scenario())
    assert verify_dir(Path(sock_path.parent)).errors  # no log there: sanity that verify does not pass vacuously


def test_real_kernel_peer_credentials_report_this_process() -> None:
    """Native path (LOCAL_PEERCRED on macOS, SO_PEERCRED on Linux): the uid is ours."""
    with tempfile.TemporaryDirectory(prefix="atl") as directory:
        path = str(Path(directory) / "s.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(path)
        conn, _ = server.accept()
        try:
            assert KernelPeerCredentials().peer_uid(conn) == os.getuid()
        finally:
            for s in (conn, client, server):
                s.close()


def test_end_to_end_with_real_credentials_and_uid_mapping(make_log: MakeLog, sock_path: Path) -> None:
    class Me:
        def user_name(self, uid: int) -> str | None:
            return "atlas-core" if uid == os.getuid() else None

    async def scenario() -> None:
        log = make_log()
        server = AuditServer(log, KernelPeerCredentials(), Me(), tick_interval_s=3600)
        await server.listen_path(sock_path)
        client = AuditClient(sock_path, timeout_s=2)
        try:
            done = await client.append(tool_call())
            assert stored_actor(log, done.seq) == "atlas-core"
        finally:
            await client.close()
            await server.close()

    run(scenario())
