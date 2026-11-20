"""Test harness for atlas-ui: a real core (scripted model) + control socket + TLS UiServer on a free port."""

from __future__ import annotations

import asyncio
import json
import shutil
import ssl
import tempfile
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any

from argon2 import PasswordHasher
from websockets.asyncio.client import ClientConnection, connect

from atlas.core.control import PeerAuthenticator, SameUserPeers
from atlas.ui_server.app import UiServer, build_server
from atlas.ui_server.config import UiConfig
from atlas.ui_server.dev import CoreStack, build_core
from atlas.ui_server.devtls import make_dev_tls
from atlas.ui_server.pin import hash_pin

Json = dict[str, Any]
FAST_HASHER = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)  # tests only
PIN = "0000"


class InstantExecutor:
    """Executor link that records what it was asked to run and succeeds at once."""

    def __init__(self) -> None:
        self.envelopes: list[Json] = []

    async def submit(self, envelope: Json) -> Json:
        self.envelopes.append(envelope)
        return {"ok": True}


class HttpReply:
    def __init__(self, raw: bytes) -> None:
        head, _, self.body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        self.status = int(lines[0].split()[1])
        self.headers: dict[str, str] = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            self.headers[key.strip().lower()] = value.strip()

    def cookie(self) -> str:
        """``name=value`` from Set-Cookie, ready to send back."""
        return self.headers["set-cookie"].split(";")[0]


class WsClient:
    """A WebSocket test client that numbers its frames and collects what the server sends."""

    def __init__(self, conn: ClientConnection) -> None:
        self.conn = conn
        self.frames: list[Json] = []
        self.seq = 0
        self._cursor = 0

    async def send(self, type_: str, payload: Json | None = None) -> int:
        self.seq += 1
        await self.conn.send(json.dumps({"v": 1, "type": type_, "seq": self.seq, "ts": 0, "payload": payload or {}}))
        return self.seq

    async def recv_until(self, pred: Callable[[Json], bool], wait_s: float = 5.0) -> Json:
        """Next unread frame matching ``pred`` (frames before it are kept in ``frames``)."""
        async with asyncio.timeout(wait_s):
            while True:
                while self._cursor < len(self.frames):
                    frame = self.frames[self._cursor]
                    self._cursor += 1
                    if pred(frame):
                        return frame
                self.frames.append(json.loads(await self.conn.recv()))

    async def wait_for(self, pred: Callable[[Json], bool], wait_s: float = 5.0) -> Json:
        """First frame anywhere in ``frames`` (read or not) matching ``pred``, waiting for more if needed."""
        async with asyncio.timeout(wait_s):
            seen = 0
            while True:
                while seen < len(self.frames):
                    frame = self.frames[seen]
                    seen += 1
                    if pred(frame):
                        return frame
                self.frames.append(json.loads(await self.conn.recv()))

    async def of_type(self, type_: str, wait_s: float = 5.0) -> Json:
        return await self.recv_until(lambda f: f["type"] == type_, wait_s)

    async def result(self, ref_seq: int) -> Json:
        frame = await self.recv_until(lambda f: f["type"] == "request.result" and f["payload"]["ref_seq"] == ref_seq)
        return frame["payload"]  # type: ignore[no-any-return]

    async def request(self, type_: str, payload: Json | None = None) -> Json:
        return await self.result(await self.send(type_, payload))

    def types(self) -> list[str]:
        return [f["type"] for f in self.frames]


class Stack:
    """core + atlas-ui. Use as ``async with Stack(tmp) as s``."""

    def __init__(
        self,
        base: Path | None = None,
        *,
        peers: PeerAuthenticator | None = None,
        start_core: bool = True,
        max_connections: int = 16,
    ) -> None:
        self._owns_dir = base is None
        # Unix socket paths are short on macOS (104 bytes): use a short directory of our own.
        self.dir = base or Path(tempfile.mkdtemp(prefix="atlas-ui-", dir="/tmp"))
        self.executor = InstantExecutor()
        self.core: CoreStack = build_core(peers=peers or SameUserPeers(), executor=self.executor)
        self.sock = self.dir / "core.sock"
        self._start_core = start_core
        self.static = self.dir / "hud"
        self.static.mkdir()
        (self.static / "index.html").write_text("<!doctype html><title>HUD</title><script src=/assets/app.js></script>")
        (self.static / "assets").mkdir()
        (self.static / "assets" / "app.js").write_text("console.log('hud')")
        (self.dir / "secret.txt").write_text("outside the static root")
        self.ca, cert, key = make_dev_tls(self.dir / "tls")
        self.cfg = UiConfig(
            port=0,
            tls_cert=cert,
            tls_key=key,
            device_key=b"k" * 32,
            pin_hash=hash_pin(PIN, FAST_HASHER),
            state_dir=self.dir / "state",
            static_dir=self.static,
            core_socket=self.sock,
            max_connections=max_connections,
        )
        self.server: UiServer = build_server(self.cfg, pin_hasher=lambda: FAST_HASHER)
        self.tls = ssl.create_default_context(cafile=str(self.ca))
        self.port = 0

    async def __aenter__(self) -> Stack:
        if self._start_core:
            await self.core.control.start(self.sock)
        await self.server.start(port=0)
        self.port = self.server.bound_port
        if self._start_core:
            await asyncio.wait_for(self.server.core_connected.wait(), 10)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.server.stop()
        await self.core.control.stop()
        if self._owns_dir:
            shutil.rmtree(self.dir, ignore_errors=True)

    @property
    def origin(self) -> str:
        return f"https://localhost:{self.port}"

    async def http(self, path: str, headers: dict[str, str] | None = None, host: str | None = None) -> HttpReply:
        hdrs = {"Host": host or f"localhost:{self.port}", "Connection": "close", **(headers or {})}
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.port, ssl=self.tls, server_hostname="localhost"
        )
        writer.write((f"GET {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs.items()) + "\r\n").encode())
        await writer.drain()
        raw = await reader.read()
        writer.close()
        return HttpReply(raw)

    async def pair(self, role: str = "remote") -> str:
        """Create a code, redeem it over HTTPS, return the cookie (``name=value``)."""
        code = self.server.pairing.create(role)  # type: ignore[arg-type]
        reply = await self.http("/api/pair", {"Origin": self.origin, "X-Atlas-Pairing-Code": code})
        assert reply.status == 200, reply.status
        return reply.cookie()

    async def ws(self, cookie: str | None, origin: str | None = "", wait_snapshot: bool = True) -> WsClient:
        """Open /ws/events. ``origin=""`` means this server's own origin; ``None`` sends no Origin."""
        headers = {"Cookie": cookie} if cookie else {}
        conn = await connect(
            f"wss://localhost:{self.port}/ws/events",
            ssl=self.tls,
            origin=self.origin if origin == "" else origin,  # type: ignore[arg-type]
            additional_headers=headers,
            open_timeout=5,
        )
        client = WsClient(conn)
        if wait_snapshot:
            await client.of_type("sync.snapshot")
        return client

    async def step_up(self, client: WsClient, pin: str = PIN) -> str:
        await client.send("stepup.verify", {"pin": pin})
        frame = await client.of_type("stepup.granted")
        return str(frame["payload"]["token"])
