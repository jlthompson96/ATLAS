"""Client for the atlas-core control socket (protocol: ``atlas.core.control``). Standard library only, so
atlas-ui can use it without importing any of atlas-core."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

Json = dict[str, Any]
MAX_LINE: Final = 4 * 1024 * 1024
CALL_TIMEOUT_S: Final = 10.0


class ControlError(Exception):
    pass


class ControlClient:
    """A small asyncio client for the control socket. ``events`` receives pushed ``(type, payload)`` frames."""

    def __init__(self, path: Path, on_event: Callable[[str, Json], None]) -> None:
        self._path = path
        self._on_event = on_event
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 0
        self._waiters: dict[int, asyncio.Future[Json]] = {}
        self._read_task: asyncio.Task[None] | None = None
        self.closed = asyncio.Event()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(str(self._path), limit=MAX_LINE)
        self.closed.clear()
        self._read_task = asyncio.ensure_future(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._reader is not None  # noqa: S101 - set by connect()
        try:
            while line := await self._reader.readline():
                try:
                    frame = json.loads(line)
                except ValueError:
                    continue
                if "event" in frame:
                    self._on_event(frame["event"], frame["payload"])
                elif (fut := self._waiters.pop(frame.get("id", -1), None)) is not None and not fut.done():
                    fut.set_result(frame)
        except (ConnectionError, asyncio.IncompleteReadError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            for fut in self._waiters.values():
                if not fut.done():
                    fut.set_exception(ControlError("control socket closed"))
            self._waiters.clear()
            self.closed.set()

    async def call(self, op: str, **fields: Any) -> Json:
        if self._writer is None or self.closed.is_set():
            raise ControlError("not connected")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Json] = asyncio.get_running_loop().create_future()
        self._waiters[rid] = fut
        try:
            self._writer.write(json.dumps({"id": rid, "op": op, **fields}, separators=(",", ":")).encode() + b"\n")
            await self._writer.drain()
            async with asyncio.timeout(CALL_TIMEOUT_S):
                return await fut
        except (TimeoutError, ConnectionError) as exc:
            raise ControlError(type(exc).__name__) from None
        finally:
            self._waiters.pop(rid, None)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
        if self._read_task is not None:
            self._read_task.cancel()
            await asyncio.gather(self._read_task, return_exceptions=True)
