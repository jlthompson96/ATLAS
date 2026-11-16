"""The atlas-audit Unix-socket server.

Wire format: newline-delimited JSON, one request per line, one reply line per request.

    request  {"op": "append", "record": {...record body without actor/seq/ts/prev_hash/hash...}}
    request  {"op": "outcome_unknown"}                       (atlas-core and atlas-executor only)
    reply    {"ok": true, "seq": 12, "hash": "...", "durable": true}
    reply    {"ok": false, "code": "ACTOR_MISMATCH"}

The ``actor`` is the service whose Unix user owns the connecting process (kernel peer credentials,
resolved once per connection). A body that names a different actor is refused. Reply codes are stable
UPPER_SNAKE strings; request content is never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import stat
from dataclasses import asdict
from pathlib import Path
from typing import Any, Final

from atlas.audit.log import AuditLog, AuditUnavailable, SubmissionRejected, load_body
from atlas.audit.peercred import PeerCredentialError, PeerCredentials, UserDirectory, actor_for_peer
from atlas.audit.verify import VerifyReport, find_outcome_unknown, load_records

log = logging.getLogger("atlas.audit.server")

MAX_REQUEST_BYTES: Final = 64 * 1024
IDLE_TIMEOUT_S: Final = 300.0
OUTCOME_UNKNOWN_READERS: Final = frozenset({"atlas-core", "atlas-executor"})


def _reply(**fields: Any) -> bytes:
    return json.dumps(fields, separators=(",", ":")).encode() + b"\n"


class AuditServer:
    def __init__(
        self,
        audit_log: AuditLog,
        creds: PeerCredentials,
        users: UserDirectory,
        *,
        max_connections: int = 64,
        idle_timeout_s: float = IDLE_TIMEOUT_S,
        tick_interval_s: float = 5.0,
    ) -> None:
        self._log = audit_log
        self._creds = creds
        self._users = users
        self._max_connections = max_connections
        self._idle_timeout = idle_timeout_s
        self._tick_interval = tick_interval_s
        self._lock = asyncio.Lock()
        self._connections = 0
        self._server: asyncio.Server | None = None
        self._ticker: asyncio.Task[None] | None = None

    # ---- listening ---------------------------------------------------------------------------------

    async def listen_path(self, path: Path, mode: int = 0o660) -> None:
        """Bind ``path`` (development and tests; on the Pi systemd's socket unit does this)."""
        _remove_stale_socket(path)
        self._server = await asyncio.start_unix_server(self._handle, path=str(path), limit=MAX_REQUEST_BYTES)
        _chmod(path, mode)
        self._start_ticker()

    async def listen_socket(self, sock: socket.socket) -> None:
        """Serve on an already-listening socket (systemd socket activation)."""
        self._server = await asyncio.start_unix_server(self._handle, sock=sock, limit=MAX_REQUEST_BYTES)
        self._start_ticker()

    def _start_ticker(self) -> None:
        self._ticker = asyncio.create_task(self._tick_loop())

    async def close(self) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self._tick_interval)
            try:
                async with self._lock:
                    await asyncio.to_thread(self._log.tick)
            except AuditUnavailable:
                log.error("audit log unavailable in housekeeping")
            except OSError:
                log.exception("audit housekeeping failed")

    # ---- connections -------------------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
            pass
        except Exception:
            log.exception("unexpected error serving a connection")
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock = writer.get_extra_info("socket")
        try:
            if sock is None:
                raise PeerCredentialError("no socket")
            actor = actor_for_peer(sock, self._creds, self._users)
        except PeerCredentialError:
            log.warning("refused a connection with unknown peer identity")
            writer.write(_reply(ok=False, code="PEER_REJECTED"))
            await writer.drain()
            return
        if self._connections >= self._max_connections:
            writer.write(_reply(ok=False, code="BUSY"))
            await writer.drain()
            return
        self._connections += 1
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readuntil(b"\n"), self._idle_timeout)
                except asyncio.LimitOverrunError:
                    writer.write(_reply(ok=False, code="TOO_LARGE"))
                    await writer.drain()
                    return
                except asyncio.IncompleteReadError:
                    return
                writer.write(await self._dispatch(actor, line))
                await writer.drain()
        finally:
            self._connections -= 1

    async def _dispatch(self, actor: str, line: bytes) -> bytes:
        try:
            request = load_body(line.strip())
            op = request.get("op")
            if op == "append":
                record = request.get("record")
                if not isinstance(record, dict) or set(request) != {"op", "record"}:
                    raise SubmissionRejected("BAD_REQUEST")
                async with self._lock:
                    done = await asyncio.to_thread(self._log.append, actor, record)
                return _reply(ok=True, seq=done.seq, hash=done.hash, durable=done.durable)
            if op == "outcome_unknown" and set(request) == {"op"}:
                if actor not in OUTCOME_UNKNOWN_READERS:
                    raise SubmissionRejected("FORBIDDEN")
                async with self._lock:
                    items = await asyncio.to_thread(self._outcome_unknown)
                return _reply(ok=True, outcome_unknown=items)
            raise SubmissionRejected("BAD_REQUEST")
        except SubmissionRejected as exc:
            log.info("rejected a record from %s: %s", actor, exc.code)
            return _reply(ok=False, code=exc.code)
        except (AuditUnavailable, OSError):
            log.exception("audit log unavailable")
            return _reply(ok=False, code="AUDIT_UNAVAILABLE")

    def _outcome_unknown(self) -> list[dict[str, Any]]:
        report = VerifyReport()
        records = load_records(self._log.data_dir, report)
        if report.errors:
            raise AuditUnavailable("log unreadable")
        return [asdict(item) for item in find_outcome_unknown(records)]


def _remove_stale_socket(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        if stat.S_ISSOCK(path.lstat().st_mode):
            path.unlink()


def _chmod(path: Path, mode: int) -> None:
    path.chmod(mode)


def listen_fds_socket() -> socket.socket | None:
    """The first socket-activation fd (``LISTEN_FDS``), or None when not started by systemd."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or int(os.environ.get("LISTEN_FDS", "0")) < 1:
        return None
    return socket.socket(fileno=3)
