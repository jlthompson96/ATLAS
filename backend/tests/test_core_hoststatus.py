"""host.status from real counters and the desktop link: measured or reported, never assumed (2026-11-25)."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Sequence
from pathlib import Path
from typing import Any

import pytest

from atlas.core.hoststatus import DAY_S, HostStatusPublisher, PiMetrics, gpu_json
from atlas.core.service import start_host_status
from atlas.llm.interface import Health
from atlas.power.hostd import GpuReport, HostdError
from atlas.power.interface import PowerState
from atlas.ui_server.protocol import Contract

EVENTS = Contract.load()


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


class Box:
    """A fake /proc and /sys, with a clock."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.t = 1000.0
        (root / "proc").mkdir()
        (root / "sys/class/thermal/thermal_zone0").mkdir(parents=True)
        self.set(cpu=(100, 0, 100, 700, 100, 0, 0, 0), mem=(8_000_000, 6_000_000), milli_c=57_600, sectors=0, uptime=60)

    def set(
        self,
        *,
        cpu: Sequence[int] | None = None,
        mem: tuple[int, int] | None = None,
        milli_c: int | None = None,
        sectors: int | None = None,
        uptime: float | None = None,
    ) -> None:
        proc = self.root / "proc"
        if cpu is not None:
            (proc / "stat").write_text("cpu  " + " ".join(map(str, cpu)) + " 0 0\ncpu0 1 2 3 4 5 6 7 8 0 0\n")
        if mem is not None:
            (proc / "meminfo").write_text(f"MemTotal: {mem[0]} kB\nMemFree: 1 kB\nMemAvailable: {mem[1]} kB\n")
        if milli_c is not None:
            (self.root / "sys/class/thermal/thermal_zone0/temp").write_text(f"{milli_c}\n")
        if sectors is not None:
            (proc / "diskstats").write_text(
                f" 179       0 mmcblk0 4915 9415 314525 7203 656 5791 {sectors} 29857 0 6056 37060 0 0 0 0 0 0\n"
                f" 179       3 mmcblk0p3 953 93 38762 1002 578 5790 1 28111 0 3596 29113 0 0 0 0 0 0\n"
            )
        if uptime is not None:
            (proc / "uptime").write_text(f"{uptime:.2f} 100.00\n")

    def metrics(self) -> PiMetrics:
        return PiMetrics(self.root, clock=lambda: self.t)


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


MB = 1_000_000 // 512  # sectors in 1 MB (10^6 bytes, as B10 counts)


# --- the Pi's counters --------------------------------------------------------------------------------------------


def test_reads_cpu_memory_and_temperature(box: Box) -> None:
    pi = box.metrics()
    assert pi.available()
    first = pi.read()
    assert first["cpu_pct"] == 20.0  # since boot: 200 busy of 1000
    assert first["ram_pct"] == 25.0
    assert first["temp_c"] == 57.6
    box.set(cpu=(150, 0, 150, 700, 100, 0, 0, 0))  # 100 more busy, nothing idle
    assert pi.read()["cpu_pct"] == 100.0  # since the last read


def test_card_writes_are_since_boot_during_the_first_day(box: Box) -> None:
    box.set(sectors=36 * MB, uptime=DAY_S - 1)
    assert box.metrics().read()["disk_write_mb_24h"] == pytest.approx(36.0, abs=0.1)


def test_card_writes_after_a_restart_are_the_average_since_boot(box: Box) -> None:
    box.set(sectors=72 * MB, uptime=2 * DAY_S)  # core started now, the Pi two days ago
    assert box.metrics().read()["disk_write_mb_24h"] == pytest.approx(36.0, abs=0.1)


def test_card_writes_over_a_full_day_are_the_real_delta(box: Box) -> None:
    pi = box.metrics()
    box.set(sectors=500 * MB, uptime=5 * DAY_S)
    pi.read()  # the day's first sample
    for step in range(1, 145):  # a day of 10-minute rounds, 1 MB each
        box.t += 600
        box.set(sectors=(500 + step) * MB, uptime=5 * DAY_S + 600 * step)
        reading = pi.read()["disk_write_mb_24h"]
    assert reading == pytest.approx(144.0, abs=0.5)
    box.t += 600  # one round later the window slides: still a day's worth
    box.set(sectors=645 * MB, uptime=6 * DAY_S + 600)
    assert pi.read()["disk_write_mb_24h"] == pytest.approx(144.0, abs=0.5)


@pytest.mark.parametrize(
    "missing", ["proc/stat", "proc/meminfo", "proc/diskstats", "proc/uptime", "sys/class/thermal/thermal_zone0/temp"]
)
def test_not_available_without_every_counter(box: Box, missing: str) -> None:
    (box.root / missing).unlink()
    assert not box.metrics().available()


def test_not_available_without_the_card(box: Box) -> None:
    (box.root / "proc/diskstats").write_text(" 8 0 sda 1 2 3 4 5 6 7 8 9 10 11\n")
    assert not box.metrics().available()


# --- the desktop half -----------------------------------------------------------------------------------------------


class Backend:
    def __init__(self, health: Health | Exception) -> None:
        self._health = health

    async def health(self, deadline: float) -> Health:
        if isinstance(self._health, Exception):
            raise self._health
        return self._health


class Controller:
    def __init__(self, state: PowerState) -> None:
        self._state = state

    async def state(self) -> PowerState:
        return self._state


FULL_GPU = GpuReport(8192, 6992, 4000, 0, vram_used_mb=1200, util_pct=3, temp_c=40)


class Hostd:
    def __init__(self, gpu: GpuReport | Exception | None) -> None:
        self._gpu = gpu

    async def gpu(self) -> GpuReport | None:
        if isinstance(self._gpu, Exception):
            raise self._gpu
        return self._gpu


def status(box: Box, tmp_path: Path, *, wg: bool = True, **kw: Any) -> dict[str, Any]:
    wg_path = tmp_path / "wg0"
    if wg:
        wg_path.mkdir(exist_ok=True)
    emitted: list[tuple[str, dict[str, Any]]] = []

    async def emit(t: str, p: dict[str, Any]) -> None:
        emitted.append((t, p))

    publisher = HostStatusPublisher(emit, box.metrics(), wg_interface=wg_path, **kw)
    payload = run(publisher.snapshot())
    EVENTS.validate("server", {"v": 1, "type": "host.status", "seq": 1, "ts": 1, "payload": payload})
    return payload


def test_without_a_desktop_link_nothing_is_claimed(box: Box, tmp_path: Path) -> None:
    s = status(box, tmp_path)
    assert s["llm"] == {"power": "unknown", "link": "down", "service": "unknown"}
    assert s["voice"] == {"status": "unavailable", "wake_word": "off"}  # no voice service yet (Phase 3)


def test_a_server_that_answers_through_the_tunnel(box: Box, tmp_path: Path) -> None:
    s = status(box, tmp_path, backend=Backend(Health(True, True, "google/gemma-4-e4b")))
    assert s["llm"] == {"power": "up", "link": "up", "service": "ready", "model_loaded": "google/gemma-4-e4b"}


def test_answering_without_wg0_is_not_a_tunnel(box: Box, tmp_path: Path) -> None:
    s = status(box, tmp_path, wg=False, backend=Backend(Health(True, True, None)))
    assert (s["llm"]["link"], s["llm"]["service"]) == ("down", "ready")


@pytest.mark.parametrize(
    ("health", "power", "service", "link"),
    [
        (Health(False, False, None), PowerState.ASLEEP, "unknown", "down"),  # asleep is not an outage
        (Health(False, False, None), PowerState.UP, "down", "down"),
        (Health(True, False, None), PowerState.UP, "down", "up"),  # the tunnel carried the refusal of the token
        (OSError("boom"), PowerState.UP, "down", "down"),  # a failed health check counts as unreachable
    ],
)
def test_service_and_power(
    box: Box, tmp_path: Path, health: Health | Exception, power: PowerState, service: str, link: str
) -> None:
    s = status(box, tmp_path, backend=Backend(health), controller=Controller(power))
    assert (s["llm"]["power"], s["llm"]["service"], s["llm"]["link"]) == (power.value, service, link)


def test_gpu_from_hostd_only_when_the_host_is_up_and_complete(box: Box, tmp_path: Path) -> None:
    up = {"backend": Backend(Health(True, True, None)), "controller": Controller(PowerState.UP)}
    assert status(box, tmp_path, hostd=Hostd(FULL_GPU), **up)["llm"]["gpu"] == {
        "util_pct": 3,
        "vram_used_mb": 1200,
        "vram_total_mb": 8192,
        "temp_c": 40,
    }
    partial = GpuReport(8192, 6992, 4000, 0)  # an older hostd, or NVML fields missing
    for hostd in (Hostd(partial), Hostd(None), Hostd(HostdError("x"))):
        assert "gpu" not in status(box, tmp_path, hostd=hostd, **up)["llm"]
    asleep = {"backend": Backend(Health(False, False, None)), "controller": Controller(PowerState.ASLEEP)}
    assert "gpu" not in status(box, tmp_path, hostd=Hostd(FULL_GPU), **asleep)["llm"]
    assert gpu_json(None) is None


# --- running ------------------------------------------------------------------------------------------------------


def test_run_emits_until_stopped_and_survives_a_bad_round(box: Box, tmp_path: Path) -> None:
    emitted: list[str] = []
    three = asyncio.Event()

    async def emit(t: str, p: dict[str, Any]) -> None:
        emitted.append(t)
        if len(emitted) == 3:
            three.set()
        if len(emitted) == 1:
            raise RuntimeError("first round fails")

    async def body() -> None:
        stop = asyncio.Event()
        task = asyncio.ensure_future(HostStatusPublisher(emit, box.metrics(), interval_s=0.01).run(stop))
        await asyncio.wait_for(three.wait(), 5)
        stop.set()
        await asyncio.wait_for(task, 1)

    run(body())
    assert emitted[:3] == ["host.status"] * 3


def test_core_publishes_only_where_the_counters_exist(box: Box, tmp_path: Path) -> None:
    class Hub:
        def __init__(self) -> None:
            self.types: list[str] = []
            self.got = asyncio.Event()

        async def emit(self, t: str, p: Any) -> None:
            self.types.append(t)
            self.got.set()

    async def body(pi: PiMetrics) -> list[str]:
        hub, stop = Hub(), asyncio.Event()
        task = start_host_status(hub, None, stop, pi=pi)  # type: ignore[arg-type]
        if task is None:
            return hub.types
        await asyncio.wait_for(hub.got.wait(), 5)
        stop.set()
        await asyncio.wait_for(task, 1)
        return hub.types

    assert run(body(box.metrics())) == ["host.status"]
    assert run(body(PiMetrics(tmp_path / "a-mac-has-no-proc"))) == []
