"""Peer identity of a Unix-socket client: ``SO_PEERCRED`` (Linux, the Pi) or ``LOCAL_PEERCRED`` (macOS).

The audit ``actor`` is derived from the kernel-reported uid, never from the request body
(contracts/services.md section 2). Production maps uid -> Unix user name -> service name; tests inject
fakes through the two small interfaces below.
"""

from __future__ import annotations

import pwd
import socket
import struct
import sys
from asyncio.trsock import TransportSocket
from typing import Final, Protocol

from atlas.audit.record import ACTORS

SockLike = socket.socket | TransportSocket


class PeerCredentialError(Exception):
    """The peer's identity could not be established; the connection must be refused."""


class PeerCredentials(Protocol):
    def peer_uid(self, sock: SockLike) -> int: ...


class UserDirectory(Protocol):
    def user_name(self, uid: int) -> str | None: ...


class KernelPeerCredentials:
    """Linux: ``struct ucred {pid, uid, gid}``. Darwin: ``struct xucred`` via ``LOCAL_PEERCRED``."""

    _SOL_LOCAL: Final = 0  # Darwin SOL_LOCAL
    _LOCAL_PEERCRED: Final = 1
    _XUCRED_SIZE: Final = 76  # u32 version, u32 uid, i16 ngroups (+pad), 16 x u32 groups

    def peer_uid(self, sock: SockLike) -> int:
        try:
            if sys.platform.startswith("linux"):
                raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _pid, uid, _gid = struct.unpack("3i", raw)
                return int(uid)
            # mypy narrows sys.platform per OS: on Linux it sees this branch as dead code, on macOS it doesn't.
            if sys.platform == "darwin":  # type: ignore[unreachable, unused-ignore]
                raw = sock.getsockopt(self._SOL_LOCAL, self._LOCAL_PEERCRED, self._XUCRED_SIZE)
                _version, uid = struct.unpack_from("II", raw)
                return int(uid)
        except OSError as exc:
            raise PeerCredentialError(f"peer credentials unavailable: {exc}") from exc
        raise PeerCredentialError(f"no peer-credential support on {sys.platform}")


class PasswdDirectory:
    """uid -> user name from the system user database."""

    def user_name(self, uid: int) -> str | None:
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return None


def actor_for_peer(sock: SockLike, creds: PeerCredentials, users: UserDirectory) -> str:
    """Resolve the connecting process to an audit actor, or raise ``PeerCredentialError``."""
    uid = creds.peer_uid(sock)
    name = users.user_name(uid)
    if name is None or name not in ACTORS:
        raise PeerCredentialError("peer is not an ATLAS service user")
    return name
