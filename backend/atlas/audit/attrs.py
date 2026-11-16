"""ext4 file attributes for segment protection: ``chattr +a`` on the active segment, ``+i`` once sealed.

Linux only (``FS_IOC_GETFLAGS``/``FS_IOC_SETFLAGS`` on a file descriptor). Changing these flags needs
``CAP_LINUX_IMMUTABLE``, which the unit grants to atlas-audit alone (deploy/systemd/atlas-audit.service).
The hash chain and signed checkpoints are the real tamper evidence; the attributes only stop an
unprivileged or root-less writer from rewriting a segment in place.

NOT testable on macOS or in CI: verify in the ARM64 Debian VM and on the Pi (docs/runbooks/audit.md).
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import platform
import struct
from pathlib import Path
from typing import Final, Protocol

FS_IOC_GETFLAGS: Final = 0x80086601  # _IOR('f', 1, long), LP64 Linux (aarch64, x86_64)
FS_IOC_SETFLAGS: Final = 0x40086602  # _IOW('f', 2, long)
FS_IMMUTABLE_FL: Final = 0x00000010
FS_APPEND_FL: Final = 0x00000020


class SegmentAttributes(Protocol):
    def make_append_only(self, path: Path) -> None: ...
    def seal(self, path: Path) -> None: ...
    def unseal(self, path: Path) -> None: ...


class NoAttributes:
    """Dev/test implementation: does nothing. Never used when ``require_append_only`` is set."""

    def make_append_only(self, path: Path) -> None:
        return None

    def seal(self, path: Path) -> None:
        return None

    def unseal(self, path: Path) -> None:
        return None


class LinuxChattr:
    """Real ``chattr`` via ioctl. Raises ``OSError`` if the filesystem or capability does not allow it."""

    def __init__(self) -> None:
        if platform.system() != "Linux":
            raise OSError("chattr attributes are only available on Linux")

    @staticmethod
    def _update(path: Path, set_bits: int, clear_bits: int) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            current = struct.unpack("l", fcntl.ioctl(fd, FS_IOC_GETFLAGS, struct.pack("l", 0)))[0]
            fcntl.ioctl(fd, FS_IOC_SETFLAGS, struct.pack("l", (current & ~clear_bits) | set_bits))
        finally:
            os.close(fd)

    def make_append_only(self, path: Path) -> None:
        self._update(path, FS_APPEND_FL, 0)

    def seal(self, path: Path) -> None:
        self._update(path, FS_IMMUTABLE_FL, FS_APPEND_FL)

    def unseal(self, path: Path) -> None:
        self._update(path, 0, FS_IMMUTABLE_FL | FS_APPEND_FL)


def probe_linux_chattr(directory: Path) -> bool:
    """True if ``chattr +a``/``-a`` works on a scratch file in ``directory`` (startup self-test)."""
    if platform.system() != "Linux":
        return False
    probe = directory / ".chattr-probe"
    attrs = LinuxChattr()
    try:
        probe.write_bytes(b"")
        attrs.make_append_only(probe)
        attrs.unseal(probe)
        return True
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            probe.unlink()
