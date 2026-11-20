"""Wake-on-LAN magic packets (PLAN.md §5.4).

The packet is 6 bytes of ``0xFF`` followed by the target MAC 16 times, sent as a UDP broadcast
(port 9). nftables on the Pi allows exactly this broadcast and nothing else outbound to the LAN
(§5.2); the address comes from configuration, never from model output.
"""

from __future__ import annotations

import re
import socket
from typing import Final, Protocol

DEFAULT_BROADCAST: Final = "255.255.255.255"
DEFAULT_PORT: Final = 9
_MAC_RE: Final = re.compile(r"^[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}$")


def parse_mac(text: str) -> bytes:
    """``aa:bb:cc:dd:ee:ff`` or ``aa-bb-...`` to 6 bytes."""
    if not _MAC_RE.match(text):
        raise ValueError("MAC address must look like aa:bb:cc:dd:ee:ff")
    return bytes.fromhex(re.sub(r"[:-]", "", text))


def magic_packet(mac: bytes) -> bytes:
    if len(mac) != 6:
        raise ValueError("MAC address must be 6 bytes")
    return b"\xff" * 6 + mac * 16


class PacketSender(Protocol):
    def send_magic(self) -> None: ...


class UdpWolSender:
    """Sends the magic packet as a UDP broadcast. One tiny non-blocking datagram, so it runs inline."""

    def __init__(self, mac: str, *, broadcast: str = DEFAULT_BROADCAST, port: int = DEFAULT_PORT) -> None:
        self._packet = magic_packet(parse_mac(mac))
        self._target = (broadcast, port)

    def send_magic(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(1.0)
            sock.sendto(self._packet, self._target)
