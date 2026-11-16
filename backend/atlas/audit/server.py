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
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Final

from atlas.audit.log import AuditLog, AuditUnavailable, SubmissionRejected, load_body
from atlas.audit.peercred import PeerCredentialError, PeerCredentials, UserDirectory, actor_for_peer
from atlas.audit.verify import outcome_unknown_in_dir

log = logging.getLogger("atlas.audit.server")

MAX_REQUEST_BYTES: Final = 64 * 1024
IDLE_TIMEOUT_S: Final = 300.0
OUTCOME_UNKNOWN_READERS: Final = frozenset({"atlas-core", "atlas-executor"})
MAX_CONNECTIONS_PER_ACTOR: Final = 8  # one service must not use up the global limit of 64
# Per-actor append budget. 5 records/s sustained is far above any honest service (a few per second at peak)
# and bounds a flood: 5/s x ~400 B is ~170 MB/day per actor, so the log-size alarm (PLAN 3.5) still has to
# watch; the free-space floor in AuditLog.append stops it before the card fills.
APPEND_RATE_PER_S: Final = 5.0
APPEND_BURST: Final = 50.0


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
        max_connections_per_actor: int = MAX_CONNECTIONS_PER_ACTOR,
        rate_per_s: float = APPEND_RATE_PER_S,
        burst: float = APPEND_BURST,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fatal = asyncio.Event()  # set when the service must exit non-zero so systemd restarts it
        self._max_per_actor = max_connections_per_actor
        self._rate = rate_per_s
        self._burst = burst
        self._monotonic = monotonic
        self._actor_connections: dict[str, int] = {}
        self._buckets: dict[str, tuple[float, float]] = {}  # actor -> (tokens, last refill)
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
            except Exception:
                # Housekeeping (fsync, checkpoint) failed: the log can no longer be trusted to be durable.
                # Do not limp on: ask the service to exit so systemd restarts it and the log is re-validated.
                log.exception("audit housekeeping failed; requesting restart")
                self.fatal.set()
                return

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
        if self._connections >= self._max_connections or self._actor_connections.get(actor, 0) >= self._max_per_actor:
            writer.write(_reply(ok=False, code="BUSY"))
            await writer.drain()
            return
        self._connections += 1
        self._actor_connections[actor] = self._actor_connections.get(actor, 0) + 1
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
            self._actor_connections[actor] -= 1

    def _take_token(self, actor: str) -> bool:
        """Per-actor token bucket over all appends (durable kinds included)."""
        now = self._monotonic()
        tokens, last = self._buckets.get(actor, (self._burst, now))
        tokens = min(self._burst, tokens + (now - last) * self._rate)
        if tokens < 1.0:
            self._buckets[actor] = (tokens, now)
            return False
        self._buckets[actor] = (tokens - 1.0, now)
        return True

    async def _dispatch(self, actor: str, line: bytes) -> bytes:
        try:
            request = load_body(line.strip())
            op = request.get("op")
            if op == "append":
                record = request.get("record")
                if not isinstance(record, dict) or set(request) != {"op", "record"}:
                    raise SubmissionRejected("BAD_REQUEST")
                if not self._take_token(actor):
                    raise SubmissionRejected("RATE_LIMITED")
                async with self._lock:
                    done = await asyncio.to_thread(self._log.append, actor, record)
                return _reply(ok=True, seq=done.seq, hash=done.hash, durable=done.durable)
            if op == "outcome_unknown" and set(request) == {"op"}:
                if actor not in OUTCOME_UNKNOWN_READERS:
                    raise SubmissionRejected("FORBIDDEN")
                # Streamed, outside the append lock: the writer only appends, readers skip a partial last line.
                found = await asyncio.to_thread(outcome_unknown_in_dir, self._log.data_dir)
                return _reply(ok=True, outcome_unknown=[asdict(item) for item in found])
            raise SubmissionRejected("BAD_REQUEST")
        except SubmissionRejected as exc:
            log.info("rejected a record from %s: %s", actor, exc.code)
            return _reply(ok=False, code=exc.code)
        except (AuditUnavailable, OSError):
            log.exception("audit log unavailable")
            if self._log.failed:
                self.fatal.set()  # poisoned by an I/O error: exit so systemd restarts and re-validates
            return _reply(ok=False, code="AUDIT_UNAVAILABLE")


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
