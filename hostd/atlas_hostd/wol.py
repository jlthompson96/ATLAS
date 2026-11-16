"""Wake-on-LAN test tool (standard library only, platform-neutral, no imports from the rest of the package).

Runs from the Pi side (or any machine on the desktop's L2 segment). It can be copied around as a single file::

    python3 wol.py 02:00:00:00:00:01 --broadcast 192.0.2.255
    python3 wol.py 02:00:00:00:00:01 --broadcast 192.0.2.255 --poll http://10.77.0.2:8770 --timeout 60

Packets follow the PLAN.md §5.4 schedule (0, 2, 5, 10 and 20 s) until the host answers ``/v1/health`` or the
timeout passes. With ``--poll`` the request must come from the Pi's WireGuard address, or hostd answers 403;
that is reported as "reachable but forbidden" and still counts as the host being up.

Exit codes: 0 host answered, 1 timed out, 2 usage error. ``--no-poll`` (the default without ``--poll``) only
sends packets and exits 0.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import socket
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_SCHEDULE = (0.0, 2.0, 5.0, 10.0, 20.0)
_MAC = re.compile(r"^[0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\1){4}[0-9A-Fa-f]{2}$")


def parse_mac(text: str) -> bytes:
    """``aa:bb:cc:dd:ee:ff`` or ``aa-bb-cc-dd-ee-ff`` to 6 bytes."""
    if _MAC.fullmatch(text) is None:
        raise ValueError("MAC must look like 02:00:00:00:00:01")
    return bytes(int(part, 16) for part in re.split(r"[:-]", text))


def build_magic_packet(mac: bytes) -> bytes:
    """6 x 0xFF followed by the MAC 16 times (102 bytes)."""
    if len(mac) != 6:
        raise ValueError("MAC must be 6 bytes")
    return b"\xff" * 6 + mac * 16


def send_magic_packet(mac: bytes, broadcast: str, port: int, source: str | None = None) -> None:
    packet = build_magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if source:
            sock.bind((source, 0))
        sock.sendto(packet, (broadcast, port))


@dataclass(frozen=True, slots=True)
class HealthResult:
    reachable: bool
    forbidden: bool = False
    boot_id: str | None = None
    uptime_s: int | None = None


def check_poll_url(base_url: str) -> None:
    parts = urlsplit(base_url)
    if parts.scheme != "http" or not parts.hostname:
        raise ValueError("--poll must be an http:// URL")


def probe_health(base_url: str, timeout_s: float = 1.0) -> HealthResult:
    """One ``GET /v1/health``. Connection errors mean "not up yet", not a failure of the tool."""
    check_poll_url(base_url)
    parts = urlsplit(base_url)
    assert parts.hostname is not None  # noqa: S101 - checked above
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout_s)
    try:
        conn.request("GET", "/v1/health")
        response = conn.getresponse()
        raw = response.read(4096)
    except (OSError, http.client.HTTPException):
        return HealthResult(False)
    finally:
        conn.close()
    if response.status == 403:
        return HealthResult(True, forbidden=True)
    if response.status != 200:
        return HealthResult(False)
    try:
        body = json.loads(raw)
        return HealthResult(True, boot_id=str(body.get("boot_id")), uptime_s=int(body.get("uptime_s")))
    except (ValueError, TypeError, AttributeError):
        return HealthResult(True)


def parse_schedule(text: str) -> tuple[float, ...]:
    try:
        values = tuple(float(p) for p in text.split(",") if p.strip())
    except ValueError as exc:
        raise ValueError("--schedule must be comma-separated seconds") from exc
    if not values or any(v < 0 for v in values):
        raise ValueError("--schedule needs at least one non-negative time")
    return tuple(sorted(values))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atlas-hostd-wol", description="Send Wake-on-LAN packets and time the wake")
    parser.add_argument("mac", help="target MAC address, e.g. 02:00:00:00:00:01")
    parser.add_argument("--broadcast", default="255.255.255.255", help="broadcast address of the desktop's subnet")
    parser.add_argument("--port", type=int, default=9, help="UDP port (default 9; some NICs use 7)")
    parser.add_argument("--source", help="local address to send from (selects the interface)")
    parser.add_argument("--schedule", default=",".join(f"{s:g}" for s in DEFAULT_SCHEDULE), help="packet times in s")
    parser.add_argument(
        "--poll", metavar="URL", help="poll URL/v1/health (e.g. http://10.77.0.2:8770) and time the wake"
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="give up after this many seconds (with --poll)")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    try:
        mac = parse_mac(args.mac)
        schedule = parse_schedule(args.schedule)
        if args.poll:
            check_poll_url(args.poll)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    start = time.monotonic()
    pending = list(schedule)
    sent = 0
    if not args.poll:
        # Fire-and-forget: send the whole schedule's worth of packets honouring the spacing.
        for at in pending:
            time.sleep(max(0.0, at - (time.monotonic() - start)))
            send_magic_packet(mac, args.broadcast, args.port, args.source)
            sent += 1
            print(f"t={time.monotonic() - start:6.1f}s magic packet {sent} sent")
        return 0

    while True:
        elapsed = time.monotonic() - start
        while pending and pending[0] <= elapsed:
            pending.pop(0)
            send_magic_packet(mac, args.broadcast, args.port, args.source)
            sent += 1
            print(f"t={elapsed:6.1f}s magic packet {sent} sent")
        try:
            result = probe_health(args.poll)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if result.reachable:
            note = " (reachable but 403: run this from the Pi's WireGuard address)" if result.forbidden else ""
            detail = f" boot_id={result.boot_id} uptime_s={result.uptime_s}" if result.boot_id else ""
            print(f"t={time.monotonic() - start:6.1f}s hostd answered /v1/health{detail}{note}")
            print(f"WAKE_TIME_S={time.monotonic() - start:.1f}")
            return 0
        if elapsed >= args.timeout:
            print(f"t={elapsed:6.1f}s timeout: no answer after {args.timeout:g} s and {sent} packet(s)")
            return 1
        time.sleep(args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())
