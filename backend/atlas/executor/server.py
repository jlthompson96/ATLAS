"""The ``/run/atlas/executor.sock`` handler (contracts/services.md §2: "atlas-core only").

Wire format: newline-delimited JSON, one request per line, one reply per request::

    request  {"op": "execute", "approval": {...}, "signature": "...", "payload_canonical": "..."}
    reply    {"ok": true, "status": "executed"}
    reply    {"ok": false, "code": "BAD_SIGNATURE"}

The peer must be ``atlas-core`` by kernel peer credentials (the socket's group already limits who can
connect; this is the second check). Requests are handled one at a time, so two copies of one approval
cannot race past the nonce claim. Request content is never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import Any, Final

from atlas.audit.peercred import PeerCredentialError, PeerCredentials, UserDirectory, actor_for_peer
from atlas.core.schema import strict_json_loads
from atlas.executor.service import Executor
from atlas.executor.verify import MAX_ENVELOPE_BYTES

log = logging.getLogger("atlas.executor.server")

ALLOWED_PEER: Final = "atlas-core"
IDLE_TIMEOUT_S: Final = 300.0


def _reply(**fields: Any) -> bytes:
    return json.dumps(fields, separators=(",", ":")).encode() + b"\n"


class ExecutorServer:
    def __init__(self, executor: Executor, creds: PeerCredentials, users: UserDirectory) -> None:
        self._executor = executor
        self._creds = creds
        self._users = users
        self._lock = asyncio.Lock()
        self._server: asyncio.Server | None = None

    async def listen_path(self, path: Path, mode: int = 0o660) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=str(path), limit=MAX_ENVELOPE_BYTES)
        await asyncio.to_thread(path.chmod, mode)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            sock = writer.get_extra_info("socket")
            try:
                if sock is None:
                    raise PeerCredentialError("no socket")
                actor = actor_for_peer(sock, self._creds, self._users)
            except PeerCredentialError:
                actor = None
            if actor != ALLOWED_PEER:
                log.warning("refused an executor connection from a peer other than atlas-core")
                writer.write(_reply(ok=False, code="PEER_REJECTED"))
                await writer.drain()
                return
            while True:
                try:
                    line = await asyncio.wait_for(reader.readuntil(b"\n"), IDLE_TIMEOUT_S)
                except asyncio.LimitOverrunError:
                    writer.write(_reply(ok=False, code="TOO_LARGE"))
                    await writer.drain()
                    return
                except asyncio.IncompleteReadError:
                    return
                writer.write(await self.dispatch(line))
                await writer.drain()
        except (ConnectionError, TimeoutError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def dispatch(self, line: bytes) -> bytes:
        try:
            envelope = strict_json_loads(line.decode("utf-8").strip(), max_bytes=MAX_ENVELOPE_BYTES)
        except (UnicodeDecodeError, ValueError):
            return _reply(ok=False, code="BAD_REQUEST")
        async with self._lock:
            result = await self._executor.execute(envelope)
        return json.dumps(result.to_json(), separators=(",", ":")).encode() + b"\n"
