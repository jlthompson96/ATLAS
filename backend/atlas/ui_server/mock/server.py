"""Mock /ws/events server for HUD development (dev machine only, never deployed).

No TLS, no device tokens: it binds to loopback and accepts browser Origins from localhost only.
Each connection gets its own session, snapshot and scenario playback.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from http import HTTPStatus

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from atlas.ui_server.mock import scenario as sc
from atlas.ui_server.mock.session import Session

log = logging.getLogger("atlas.mock")

WS_PATH = "/ws/events"
INTERACTIVE = "interactive"
ALL = "all"
LOCAL_ORIGIN = re.compile(r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$")


@dataclass(frozen=True)
class Options:
    host: str = "127.0.0.1"
    port: int = 8765
    scenario: str = INTERACTIVE
    loop: bool = False
    speed: float = 1.0
    device_role: str = "remote"


def playlist(name: str) -> list[str]:
    if name == ALL:
        return sc.available()
    if name == INTERACTIVE:
        return []
    sc.load(name)  # fail fast on a bad name
    return [name]


async def run_session(session: Session, names: Sequence[str], loop: bool) -> None:
    """Play each scenario after a fresh setup + snapshot. With no scenarios, just the baseline."""
    if not names:
        session.apply_setup(sc.load(sc.BASELINE))
        await session.snapshot()
        return
    while True:
        for name in names:
            scenario = sc.load(name)
            session.apply_setup(scenario)
            await session.snapshot()
            log.info("playing %s (%.1f s)", name, scenario.duration_ms / 1000 / session.speed)
            await session.play(scenario)
            await session.clock.sleep_ms(1500)
        if not loop:
            return


def _check_path(connection: ServerConnection, request: Request) -> Response | None:
    if request.path != WS_PATH:
        return connection.respond(HTTPStatus.NOT_FOUND, f"Use {WS_PATH}\n")
    return None


def build_server(opts: Options) -> Server:
    """The websockets server as an async context manager (port 0 picks a free port)."""
    names = playlist(opts.scenario)

    async def handler(ws: ServerConnection) -> None:
        session = Session(ws.send, device_role=opts.device_role, speed=opts.speed)
        log.info("client connected from %s", ws.remote_address)
        player = asyncio.create_task(run_session(session, names, opts.loop))
        try:
            async for raw in ws:
                await session.handle_raw(raw)
        except ConnectionClosed:
            pass
        finally:
            player.cancel()
            await session.close()
            log.info("client disconnected (%d frames sent)", session.frames_sent)

    return serve(
        handler,
        opts.host,
        opts.port,
        process_request=_check_path,
        origins=[None, LOCAL_ORIGIN],
        max_size=256 * 1024,
    )


async def serve_forever(opts: Options) -> None:
    async with build_server(opts) as server:
        log.info("mock event server on ws://%s:%d%s (scenario: %s)", opts.host, opts.port, WS_PATH, opts.scenario)
        await server.serve_forever()
