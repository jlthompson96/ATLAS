"""Wires the app to its two listeners and runs the lease sweeper."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from atlas_hostd.config import Config
from atlas_hostd.http import HttpServer
from atlas_hostd.platform.base import Platform
from atlas_hostd.service import HostdApp, Listener

log = logging.getLogger("atlas_hostd.server")

SWEEP_INTERVAL_S = 1.0


@dataclass(slots=True)
class RunningHostd:
    app: HostdApp
    main: HttpServer
    helper: HttpServer
    _sweeper: asyncio.Task[None]
    _platform: Platform

    async def stop(self) -> None:
        self._sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._sweeper
        await self.main.stop()
        await self.helper.stop()
        await self.app.shutdown()
        self._platform.close()


async def _sweep(app: HostdApp) -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_S)
        try:
            await app.tick()
        except Exception:
            log.exception("lease sweep failed")


async def start(config: Config, platform: Platform) -> RunningHostd:
    app = HostdApp(platform, config)
    main = HttpServer(
        "main",
        config.listener.address,
        config.listener.port,
        app.checker(Listener.MAIN),
        app.handler(Listener.MAIN),
    )
    helper = HttpServer(
        "helper",
        config.helper_listener.address,
        config.helper_listener.port,
        app.checker(Listener.HELPER),
        app.handler(Listener.HELPER),
    )
    await main.start()
    try:
        await helper.start()
    except OSError:
        await main.stop()
        raise
    sweeper = asyncio.get_running_loop().create_task(_sweep(app))
    return RunningHostd(app, main, helper, sweeper, platform)


async def run_forever(config: Config, platform: Platform) -> None:
    running = await start(config, platform)
    try:
        await asyncio.Event().wait()
    finally:
        await running.stop()
