"""atlas-core's client for ``/run/atlas/executor.sock`` (implements ``atlas.core.approvals.ExecutorLink``).

Kept in atlas-core, not in ``atlas.executor``: core imports nothing from the executor except the
approval-record format (UW-1).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any


class SocketExecutorLink:
    def __init__(self, path: Path, timeout_s: float = 30.0) -> None:
        self._path = path
        self._timeout = timeout_s

    async def submit(self, envelope: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(self._path)), self._timeout)
        except (OSError, TimeoutError):
            return {"ok": False, "code": "EXECUTOR_UNAVAILABLE"}
        try:
            writer.write(data)
            await writer.drain()
            line = await asyncio.wait_for(reader.readuntil(b"\n"), self._timeout)
            reply = json.loads(line)
        except (OSError, TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            return {"ok": False, "code": "EXECUTOR_UNAVAILABLE"}
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        return reply if isinstance(reply, dict) else {"ok": False, "code": "EXECUTOR_UNAVAILABLE"}
