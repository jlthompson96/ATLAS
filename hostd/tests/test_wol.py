"""WoL tool: packet format, argument parsing, UDP send on loopback, and health polling against a real server."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from test_e2e import make_config, running

from atlas_hostd import wol
from atlas_hostd.platform.fake import FakePlatform

MAC = "02:00:00:00:00:01"


def test_magic_packet_layout() -> None:
    packet = wol.build_magic_packet(wol.parse_mac(MAC))
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes([2, 0, 0, 0, 0, 1]) * 16


@pytest.mark.parametrize("text", ["02-00-00-00-00-01", "02:00:00:00:00:01", "AA:bb:CC:dd:EE:ff"])
def test_mac_parsing_accepts_common_forms(text: str) -> None:
    assert len(wol.parse_mac(text)) == 6


@pytest.mark.parametrize("text", ["", "02:00:00:00:00", "02:00:00:00:00:0g", "0200.0000.0001", "02:00-00:00:00:01"])
def test_mac_parsing_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError, match="MAC"):
        wol.parse_mac(text)


def test_schedule_parsing() -> None:
    assert wol.parse_schedule("10,0,5") == (0.0, 5.0, 10.0)
    for bad in ("", "a,b", "-1"):
        with pytest.raises(ValueError, match="schedule"):
            wol.parse_schedule(bad)


@contextmanager
def udp_listener() -> Iterator[tuple[int, list[bytes]]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.05)
    received: list[bytes] = []
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                received.append(sock.recvfrom(2048)[0])
            except TimeoutError:
                continue
            except OSError:
                return

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1], received
    finally:
        stop.set()
        thread.join(timeout=3)
        sock.close()


def test_send_magic_packet_over_udp() -> None:
    with udp_listener() as (port, received):
        wol.send_magic_packet(wol.parse_mac(MAC), "127.0.0.1", port)
        for _ in range(100):
            if received:
                break
            threading.Event().wait(0.02)
    assert received == [wol.build_magic_packet(wol.parse_mac(MAC))]


def test_cli_send_only(capsys: pytest.CaptureFixture[str]) -> None:
    with udp_listener() as (port, received):
        code = wol.main([MAC, "--broadcast", "127.0.0.1", "--port", str(port), "--schedule", "0,0.05"])
        threading.Event().wait(0.1)
    assert code == 0 and len(received) == 2
    assert "magic packet 2 sent" in capsys.readouterr().out


def test_cli_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert wol.main(["nonsense"]) == 2
    assert wol.main([MAC, "--poll", "https://example.org"]) == 2
    assert "error:" in capsys.readouterr().err


def test_probe_health_not_up_yet() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens here now
    assert wol.probe_health(f"http://127.0.0.1:{port}", 0.2) == wol.HealthResult(False)


async def test_probe_health_against_hostd() -> None:
    import asyncio

    fake = FakePlatform()
    async with running(fake) as server:
        result = await asyncio.to_thread(wol.probe_health, f"http://127.0.0.1:{server.main.port}")
    assert result.reachable and not result.forbidden
    assert result.boot_id == "boot-fake-0001"


async def test_probe_health_reports_forbidden_source() -> None:
    import asyncio

    async with running(FakePlatform(), make_config(allowed_source="10.77.0.1")) as server:
        result = await asyncio.to_thread(wol.probe_health, f"http://127.0.0.1:{server.main.port}")
    assert result.reachable and result.forbidden


async def test_cli_poll_times_wake_when_host_already_up(capsys: pytest.CaptureFixture[str]) -> None:
    import asyncio

    async with running(FakePlatform()) as server:
        with udp_listener() as (udp_port, received):
            code = await asyncio.to_thread(
                wol.main,
                [
                    MAC,
                    "--broadcast",
                    "127.0.0.1",
                    "--port",
                    str(udp_port),
                    "--poll",
                    f"http://127.0.0.1:{server.main.port}",
                    "--timeout",
                    "5",
                ],
            )
            threading.Event().wait(0.1)
    out = capsys.readouterr().out
    assert code == 0 and "WAKE_TIME_S=" in out and "boot_id=boot-fake-0001" in out
    assert len(received) == 1  # answered right away, so only the first packet of the schedule went out


def test_cli_poll_timeout_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    code = wol.main(
        [
            MAC,
            "--broadcast",
            "127.0.0.1",
            "--port",
            "9",
            "--poll",
            f"http://127.0.0.1:{port}",
            "--timeout",
            "0.3",
            "--poll-interval",
            "0.05",
            "--schedule",
            "0",
        ]
    )
    assert code == 1
    assert "timeout" in capsys.readouterr().out
