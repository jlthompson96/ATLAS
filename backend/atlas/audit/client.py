"""Async client for the atlas-audit socket, used by atlas-egress and the CLI stub.

The server derives the actor from this process's Unix user, so the body never carries one.
Fail closed: callers that must not proceed without an audit trail treat ``AuditError`` as "deny".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

DEFAULT_SOCKET: Final = Path("/run/atlas/audit.sock")
REPLY_LIMIT: Final = 1024 * 1024


class AuditError(Exception):
    """The audit service refused a record or could not be reached. ``code`` is UPPER_SNAKE."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AppendReply:
    seq: int
    hash: str
    durable: bool


class AuditClient:
    def __init__(self, socket_path: Path = DEFAULT_SOCKET, timeout_s: float = 5.0) -> None:
        self._path = socket_path
        self._timeout = timeout_s
        self._lock = asyncio.Lock()
        self._conn: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None

    async def append(self, record: dict[str, Any]) -> AppendReply:
        reply = await self._request({"op": "append", "record": record})
        try:
            return AppendReply(seq=int(reply["seq"]), hash=str(reply["hash"]), durable=bool(reply["durable"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise AuditError("BAD_REPLY") from exc

    async def outcome_unknown(self) -> list[dict[str, Any]]:
        reply = await self._request({"op": "outcome_unknown"})
        items = reply.get("outcome_unknown")
        if not isinstance(items, list):
            raise AuditError("BAD_REPLY")
        return items

    async def close(self) -> None:
        async with self._lock:
            await self._drop()

    async def _drop(self) -> None:
        if self._conn is not None:
            _, writer = self._conn
            self._conn = None
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _request(self, message: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        async with self._lock:
            for attempt in (1, 2):  # a stale pooled connection gets exactly one reconnect
                try:
                    return await asyncio.wait_for(self._roundtrip(payload), self._timeout)
                except (OSError, asyncio.IncompleteReadError, TimeoutError, asyncio.LimitOverrunError):
                    await self._drop()
                    if attempt == 2:
                        raise AuditError("AUDIT_UNAVAILABLE") from None
        raise AuditError("AUDIT_UNAVAILABLE")  # unreachable; keeps type checkers honest

    async def _roundtrip(self, payload: bytes) -> dict[str, Any]:
        if self._conn is None:
            self._conn = await asyncio.open_unix_connection(str(self._path), limit=REPLY_LIMIT)
        reader, writer = self._conn
        writer.write(payload)
        await writer.drain()
        line = await reader.readuntil(b"\n")
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise AuditError("BAD_REPLY") from exc
        if not isinstance(reply, dict):
            raise AuditError("BAD_REPLY")
        if reply.get("ok") is not True:
            raise AuditError(str(reply.get("code", "UNKNOWN")))
        return reply
