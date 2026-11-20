"""The atlas-ui server: static HUD over HTTPS, ``/ws/events`` over WSS, device pairing.

Security model (PLAN.md sections 4.5, 5.1, threat T7):

* TLS only (certificate from the ATLAS local CA, key from a systemd credential).
* Every HTTP response carries the security headers in ``security.py`` (CSP ``default-src 'self'`` and more).
* The ``Host`` header must name this server and, for the WebSocket and pairing, the ``Origin`` must be one of
  the configured origins (cross-site WebSocket hijacking, DNS rebinding).
* Everything except the pairing page needs a valid device-token cookie; the WebSocket upgrade is refused
  without one (401) before any frame is exchanged.
* ``/api/pair`` redeems a one-time code (header ``X-Atlas-Pairing-Code``; it is a GET because the websockets
  HTTP parser only reads GET requests, and a custom header cannot be sent cross-origin without a CORS
  preflight that this server never answers). Failures are rate limited per client address and globally.

atlas-ui holds no secrets about the world: it relays to atlas-core over the control socket.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import mimetypes
import secrets
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Final

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from atlas.core.control_client import ControlClient, ControlError
from atlas.ui_server.config import UiConfig
from atlas.ui_server.devices import (
    DeviceRegistry,
    PairingStore,
    set_cookie_header,
)
from atlas.ui_server.model import CORE_OFFLINE_STATE, Model
from atlas.ui_server.pin import PinVerifier
from atlas.ui_server.protocol import Contract, now_ms
from atlas.ui_server.security import (
    SECURITY_HEADERS,
    FailureLimiter,
    host_allowed,
    origin_allowed,
)
from atlas.ui_server.session import Session

log = logging.getLogger("atlas.ui")
Json = dict[str, Any]

WS_PATH: Final = "/ws/events"
PAIR_PATH: Final = "/api/pair"
PAIR_HEADER: Final = "x-atlas-pairing-code"
WEB_DIR: Final = Path(__file__).resolve().parent / "web"
PUBLIC_FILES: Final = {"/pair": "pair.html", "/pair.js": "pair.js", "/pair.css": "pair.css"}
MAX_WS_BYTES: Final = 256 * 1024
TICK_S: Final = 5.0
SESSION_QUEUE: Final = 1000
SERVER_VERSION: Final = "atlas-ui-0.1"

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("image/svg+xml", ".svg")


def _response(
    status: HTTPStatus,
    body: bytes = b"",
    content_type: str = "text/plain; charset=utf-8",
    extra: dict[str, str] | None = None,
) -> Response:
    headers = Headers()
    headers["Content-Type"] = content_type
    headers["Content-Length"] = str(len(body))
    headers["Connection"] = "close"
    headers["Cache-Control"] = "no-store"
    for key, value in SECURITY_HEADERS.items():
        headers[key] = value
    for key, value in (extra or {}).items():
        if key in headers:
            del headers[key]
        headers[key] = value
    return Response(status.value, status.phrase, headers, body)


def _text(status: HTTPStatus, message: str) -> Response:
    return _response(status, (message + "\n").encode())


@dataclass
class _SessionSlot:
    session: Session
    queue: asyncio.Queue[tuple[str, Json, int] | None]
    ws: ServerConnection


@dataclass
class UiServer:
    cfg: UiConfig
    registry: DeviceRegistry
    pairing: PairingStore
    pin: PinVerifier
    contract: Contract = field(default_factory=Contract.load)
    pair_limiter: FailureLimiter = field(default_factory=lambda: FailureLimiter(5, 300, 300))
    pair_global: FailureLimiter = field(default_factory=lambda: FailureLimiter(20, 300, 300))
    model: Model = field(default_factory=Model)
    version: int = 0
    server_version: str = SERVER_VERSION
    boot_id: str = field(default_factory=lambda: "boot-" + secrets.token_hex(6))

    def __post_init__(self) -> None:
        self._slots: dict[int, _SessionSlot] = {}
        self._core: ControlClient | None = None
        self._core_task: asyncio.Task[None] | None = None
        self._core_online = False
        self._closers: set[asyncio.Future[None]] = set()
        self._ever_online = False
        self.core_connected = asyncio.Event()
        self._servers: list[Server] = []
        self._layouts_file = self.cfg.state_dir / "layouts.json"
        self._load_layouts()

    # --- lifecycle ---------------------------------------------------------------------------------

    def ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cfg.tls_cert, self.cfg.tls_key)
        return ctx

    async def start(self, *, port: int | None = None, ssl_context: ssl.SSLContext | None = None) -> Server:
        """Start listening (``port=0`` picks a free one, for tests) and connect to atlas-core in the background."""
        tls = ssl_context or self.ssl_context()
        for host in self.cfg.hosts:
            self._servers.append(
                await serve(
                    self._ws_handler,
                    host,
                    self.cfg.port if port is None else port,
                    ssl=tls,
                    process_request=self._process_request,
                    server_header=None,
                    max_size=MAX_WS_BYTES,
                    ping_interval=20,
                    ping_timeout=20,
                )
            )
        self._core_task = asyncio.ensure_future(self._core_loop())
        return self._servers[0]

    @property
    def bound_port(self) -> int:
        assert self._servers  # noqa: S101 - after start()
        return int(next(iter(self._servers[0].sockets)).getsockname()[1])

    async def stop(self) -> None:
        if self._core_task is not None:
            self._core_task.cancel()
            await asyncio.gather(self._core_task, return_exceptions=True)
        if self._core is not None:
            await self._core.close()
        for server in self._servers:
            server.close()
            await server.wait_closed()

    # --- HTTP --------------------------------------------------------------------------------------

    def _process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        origins = self.cfg.allowed_origins
        headers = request.headers
        if not host_allowed(headers.get("Host"), origins):
            return _text(HTTPStatus.MISDIRECTED_REQUEST, "Unknown host")
        path = request.path.split("?", 1)[0]
        if path == WS_PATH:
            return self._check_ws(connection, request)
        if path == PAIR_PATH:
            return self._pair(connection, request)
        if path in PUBLIC_FILES:
            return self._file(WEB_DIR / PUBLIC_FILES[path], immutable=False)
        if self.registry.authenticate_cookie(headers.get("Cookie")) is None:
            if "text/html" in (headers.get("Accept") or "") or path == "/":
                return _response(HTTPStatus.FOUND, extra={"Location": "/pair"})
            return _text(HTTPStatus.UNAUTHORIZED, "Not paired")
        return self._static(path)

    def _check_ws(self, connection: ServerConnection, request: Request) -> Response | None:
        headers = request.headers
        if (headers.get("Upgrade") or "").lower() != "websocket":
            return _text(HTTPStatus.UPGRADE_REQUIRED, "Use a WebSocket")
        if not origin_allowed(headers.get("Origin"), self.cfg.allowed_origins):
            log.warning("refused WebSocket from %s: bad Origin", connection.remote_address)
            return _text(HTTPStatus.FORBIDDEN, "Origin not allowed")
        if self.registry.authenticate_cookie(headers.get("Cookie")) is None:
            return _text(HTTPStatus.UNAUTHORIZED, "Not paired")
        if len(self._slots) >= self.cfg.max_connections:
            return _text(HTTPStatus.SERVICE_UNAVAILABLE, "Too many connections")
        return None

    def _pair(self, connection: ServerConnection, request: Request) -> Response:
        headers = request.headers
        if not origin_allowed(headers.get("Origin"), self.cfg.allowed_origins):
            return _text(HTTPStatus.FORBIDDEN, "Origin not allowed")
        code = headers.get(PAIR_HEADER)
        if code is None:
            return _text(HTTPStatus.BAD_REQUEST, "Missing pairing code")
        peer = connection.remote_address
        key = str(peer[0]) if peer else "?"
        if self.pair_limiter.locked(key) or self.pair_global.locked("*"):
            return _text(HTTPStatus.TOO_MANY_REQUESTS, "Too many attempts")
        role = self.pairing.redeem(code)
        if role is None:
            self.pair_limiter.failure(key)
            self.pair_global.failure("*")
            log.warning("pairing failed from %s", key)
            return _text(HTTPStatus.UNAUTHORIZED, "Wrong or expired code")
        try:
            device, token = self.registry.add(role)
        except OverflowError:
            return _text(HTTPStatus.CONFLICT, "Too many paired devices")
        self.pair_limiter.success(key)
        log.info("paired %s as %s", device.device_id, role)
        body = json.dumps({"ok": True, "role": role}).encode()
        return _response(HTTPStatus.OK, body, "application/json", {"Set-Cookie": set_cookie_header(token)})

    def _static(self, path: str) -> Response:
        root = self.cfg.static_dir
        rel = path.lstrip("/") or "index.html"
        target = (root / rel).resolve()
        if not target.is_relative_to(root.resolve()) or any(part.startswith(".") for part in Path(rel).parts):
            return _text(HTTPStatus.NOT_FOUND, "Not found")
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            if "." in Path(rel).name:  # a missing asset is a 404, not the app shell
                return _text(HTTPStatus.NOT_FOUND, "Not found")
            target = root.resolve() / "index.html"
            if not target.is_file():
                return _text(HTTPStatus.SERVICE_UNAVAILABLE, "The HUD is not built (npm run build in frontend/hud)")
        return self._file(target, immutable="/assets/" in path)

    @staticmethod
    def _file(path: Path, *, immutable: bool) -> Response:
        try:
            body = path.read_bytes()
        except OSError:
            return _text(HTTPStatus.NOT_FOUND, "Not found")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"
        cache = "private, max-age=31536000, immutable" if immutable else "no-cache"
        return _response(HTTPStatus.OK, body, ctype, {"Cache-Control": cache})

    # --- layouts (the one thing atlas-ui itself persists) -----------------------------------------

    def _load_layouts(self) -> None:
        try:
            saved = json.loads(self._layouts_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(saved, dict):
            self.model.layouts.update({k: v for k, v in saved.items() if k in self.model.layouts})

    def save_layouts(self) -> None:
        try:
            self._layouts_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = self._layouts_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.model.layouts), encoding="utf-8")
            tmp.chmod(0o600)
            tmp.replace(self._layouts_file)
        except OSError as exc:
            log.warning("could not save layouts: %s", exc.strerror)

    # --- WebSocket ---------------------------------------------------------------------------------

    async def _ws_handler(self, ws: ServerConnection) -> None:
        device = self.registry.authenticate_cookie(ws.request.headers.get("Cookie") if ws.request else None)
        if device is None:
            await ws.close(1008, "not paired")
            return
        session = Session(ws.send, device=device, backend=self, pin=self.pin, contract=self.contract)
        slot = _SessionSlot(session, asyncio.Queue(SESSION_QUEUE), ws)
        key = id(slot)
        self._slots[key] = slot
        log.info("%s (%s) connected", device.device_id, device.role)
        pump = asyncio.ensure_future(self._pump(slot))
        ticker = asyncio.ensure_future(self._tick(slot))
        try:
            await session.snapshot()
            await self.core_call("device.announce", device_id=device.device_id, role=device.role)
            async for raw in ws:
                await session.handle_raw(raw)
        except ConnectionClosed:
            pass
        except Exception as exc:  # one bad connection must not take the server down
            log.error("session %s failed: %s", device.device_id, type(exc).__name__)
            with contextlib.suppress(Exception):
                await ws.close(1011)
        finally:
            self._slots.pop(key, None)
            pump.cancel()
            ticker.cancel()
            await asyncio.gather(pump, ticker, return_exceptions=True)
            log.info("%s disconnected (%d frames sent)", device.device_id, session.frames_sent)

    async def _pump(self, slot: _SessionSlot) -> None:
        while (item := await slot.queue.get()) is not None:
            type_, payload, version = item
            await slot.session.push(type_, payload, version)

    async def _tick(self, slot: _SessionSlot) -> None:
        while True:
            await asyncio.sleep(TICK_S)
            if not self.registry.is_live(slot.session.device.device_id):
                log.info("%s was revoked; closing", slot.session.device.device_id)
                await slot.ws.close(1008, "revoked")
                return
            await slot.session.check_inactivity()

    def _fanout(self, type_: str, payload: Json) -> None:
        for key, slot in list(self._slots.items()):
            try:
                slot.queue.put_nowait((type_, payload, self.version))
            except asyncio.QueueFull:
                log.error("%s is too slow; dropping it", slot.session.device.device_id)
                self._slots.pop(key, None)
                self._closers.add(task := asyncio.ensure_future(slot.ws.close(1013, "slow consumer")))
                task.add_done_callback(self._closers.discard)

    # --- atlas-core link ---------------------------------------------------------------------------

    async def core_call(self, op: str, **fields: Any) -> Json | None:
        core = self._core
        if core is None or not self._core_online:
            return None
        try:
            return await core.call(op, **fields)
        except ControlError:
            return None

    def _apply(self, type_: str, payload: Json) -> None:
        self.model.apply(type_, payload)
        self.version += 1
        self._fanout(type_, payload)

    async def _core_loop(self) -> None:
        """Stay connected to atlas-core; show ``llm_unreachable`` / CORE_UNREACHABLE while it is away."""
        backoff = 0.25
        while True:
            gate = _EventGate(self._apply)
            client = ControlClient(self.cfg.core_socket, gate.on_event)
            try:
                await client.connect()
                reply = await client.call("subscribe")
                if not reply.get("ok"):
                    raise ControlError("subscribe refused")
                self._core = client
                self.model.load_core_snapshot(reply["snapshot"])
                self.version += 1
                self._core_online = True
                gate.open()
                await self._core_came_up()
                backoff = 0.25
                self.core_connected.set()
                await client.closed.wait()
            except (OSError, ControlError):
                pass
            finally:
                await client.close()
            self._core_online = False
            self.core_connected.clear()
            if self._ever_online:
                self._core_went_down()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 5.0)

    async def _core_came_up(self) -> None:
        recovered = self._ever_online
        self._ever_online = True
        log.info("connected to atlas-core")
        for slot in list(self._slots.values()):
            await self.core_call(
                "device.announce", device_id=slot.session.device.device_id, role=slot.session.device.role
            )
        for slot in list(self._slots.values()):
            await slot.session.snapshot()
        if recovered:
            self._apply(
                "alert",
                {
                    "alert_id": f"core-{now_ms()}",
                    "level": "info",
                    "code": "CORE_RECOVERED",
                    "message": "ATLAS core is back",
                },
            )

    def _core_went_down(self) -> None:
        log.warning("lost atlas-core")
        self._apply("state.changed", dict(CORE_OFFLINE_STATE))
        self._apply(
            "alert",
            {
                "alert_id": f"core-{now_ms()}",
                "level": "critical",
                "code": "CORE_UNREACHABLE",
                "message": "ATLAS core is offline",
            },
        )


class _EventGate:
    """Holds core events that arrive before the subscribe snapshot is applied, then replays them in order."""

    def __init__(self, apply: Callable[[str, Json], None]) -> None:
        self._apply = apply
        self._backlog: list[tuple[str, Json]] | None = []

    def on_event(self, type_: str, payload: Json) -> None:
        if self._backlog is None:
            self._apply(type_, payload)
        else:
            self._backlog.append((type_, payload))

    def open(self) -> None:
        backlog, self._backlog = self._backlog or [], None
        for type_, payload in backlog:
            self._apply(type_, payload)


def build_server(cfg: UiConfig, *, pin_hasher: Callable[[], Any] | None = None) -> UiServer:
    """Wire the registry, pairing store and PIN verifier from a config."""
    from argon2 import PasswordHasher

    registry = DeviceRegistry(cfg.state_dir, cfg.device_key)
    pairing = PairingStore(cfg.state_dir)
    hasher = pin_hasher() if pin_hasher else PasswordHasher()
    return UiServer(cfg, registry, pairing, PinVerifier(cfg.pin_hash, hasher=hasher))
