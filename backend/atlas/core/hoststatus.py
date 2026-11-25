"""``host.status`` for the HUD's System panel and status badges (events.v1 ``HostStatus``, PLAN.md §4.5).

Before this module nothing produced the event: the HUD only ever saw the mock's samples. Every value here is
measured or reported, never assumed:

- ``pi``: this machine's own counters (``/proc``, ``/sys``). The contract requires them in every update, so the
  publisher only runs where they can be read: the Pi, not the development Mac (owner's decision, 2026-11-25).
  ``atlas-core.service`` drops ``ProcSubset=pid`` for this (RELAXED there); other processes stay hidden.
- ``llm``: what the desktop link reports. ``power`` from the power controller (hostd) when there is one; without
  hostd only "up" when the LLM server answers, otherwise "unknown". ``service`` "ready" only when the server answers
  an authenticated request; an unanswered server is "down" only when the host is known to be up (asleep is not an
  outage). ``link`` (the WireGuard tunnel) is "up" only when ``wg0`` exists and the server answered through it.
  ``gpu`` from hostd's NVML report when it has every field.
- ``voice``: "unavailable": the voice service doesn't exist yet (Phase 3, ADR 0003).

Card writes over 24 h come from ``/proc/diskstats`` like benchmark B10 (MB = 10^6 bytes, 512-byte sectors): since
boot while the Pi has been up less than a day, else the delta to a sample about 24 h old, else (atlas-core restarted
within the last day) the average daily rate since boot.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atlas.llm.interface import Health, LLMBackend
from atlas.power.hostd import GpuReport, HostdApi, HostdError
from atlas.power.interface import PowerController, PowerState

log = logging.getLogger("atlas.core.hoststatus")

Json = dict[str, Any]
DAY_S = 86_400.0
SAMPLE_EVERY_S = 600.0  # one disk sample per 10 minutes is enough for a 24 h window (144 kept)
SECTOR_BYTES = 512  # /proc/diskstats counts 512-byte sectors whatever the device's sector size
VOICE = {"status": "unavailable", "wake_word": "off"}


@dataclass(frozen=True, slots=True)
class _Cpu:
    busy: int
    total: int


class PiMetrics:
    """This machine's CPU, memory, temperature and card writes, read from ``root``'s /proc and /sys."""

    def __init__(self, root: Path = Path("/"), disk: str = "mmcblk0", clock: Callable[[], float] = time.monotonic):
        self._proc = root / "proc"
        self._sys = root / "sys"
        self._disk = disk
        self._clock = clock
        self._last_cpu: _Cpu | None = None
        self._disk_samples: deque[tuple[float, int]] = deque()

    def available(self) -> bool:
        """True when every required counter can be read (False on a Mac, or inside a ``ProcSubset=pid`` sandbox)."""
        try:
            self._cpu()
            self._memory()
            self._temp()
            self._sectors()
            self._uptime()
        except (OSError, ValueError, LookupError):
            return False
        return True

    def read(self) -> Json:
        cpu = self._cpu()
        last, self._last_cpu = self._last_cpu, cpu
        busy, total = (cpu.busy - last.busy, cpu.total - last.total) if last else (cpu.busy, cpu.total)
        mem_total, mem_available = self._memory()
        return {
            "cpu_pct": round(100.0 * busy / total, 1) if total > 0 else 0.0,
            "ram_pct": round(100.0 * (mem_total - mem_available) / mem_total, 1),
            "temp_c": round(self._temp(), 1),
            "disk_write_mb_24h": round(self._writes_24h() * SECTOR_BYTES / 1e6, 1),
        }

    def _cpu(self) -> _Cpu:
        fields = (self._proc / "stat").read_text().splitlines()[0].split()
        if fields[0] != "cpu":
            raise ValueError("no aggregate cpu line")
        values = [int(v) for v in fields[1:9]]  # user nice system idle iowait irq softirq steal
        idle = values[3] + values[4]
        return _Cpu(sum(values) - idle, sum(values))

    def _memory(self) -> tuple[int, int]:
        info: dict[str, int] = {}
        for line in (self._proc / "meminfo").read_text().splitlines():
            name, _, rest = line.partition(":")
            if name in ("MemTotal", "MemAvailable"):
                info[name] = int(rest.split()[0])
        if info["MemTotal"] <= 0:
            raise ValueError("MemTotal")
        return info["MemTotal"], info["MemAvailable"]

    def _temp(self) -> float:
        return int((self._sys / "class/thermal/thermal_zone0/temp").read_text().strip()) / 1000.0

    def _sectors(self) -> int:
        for line in (self._proc / "diskstats").read_text().splitlines():
            fields = line.split()
            if len(fields) > 9 and fields[2] == self._disk:
                return int(fields[9])  # sectors written
        raise LookupError(self._disk)

    def _uptime(self) -> float:
        return float((self._proc / "uptime").read_text().split()[0])

    def _writes_24h(self) -> int:
        sectors, uptime, now = self._sectors(), self._uptime(), self._clock()
        if not self._disk_samples or now - self._disk_samples[-1][0] >= SAMPLE_EVERY_S:
            self._disk_samples.append((now, sectors))
        # Keep exactly one sample at least a day old, so the window always has its start.
        while len(self._disk_samples) > 1 and now - self._disk_samples[1][0] >= DAY_S:
            self._disk_samples.popleft()
        if uptime <= DAY_S:
            return sectors  # the counters start at boot: this is the whole window
        start_t, start_sectors = self._disk_samples[0]
        if now - start_t >= DAY_S:
            return sectors - start_sectors
        return round(sectors * DAY_S / uptime)  # restarted within the day: average rate since boot


class HostStatusPublisher:
    """Builds ``host.status`` from :class:`PiMetrics` and the desktop link, and emits it every ``interval_s``."""

    def __init__(
        self,
        emit: Callable[[str, Json], Awaitable[None]],
        pi: PiMetrics,
        *,
        backend: LLMBackend | None = None,
        controller: PowerController | None = None,
        hostd: HostdApi | None = None,
        wg_interface: Path = Path("/sys/class/net/wg0"),
        interval_s: float = 30.0,
        health_timeout_s: float = 3.0,
    ) -> None:
        self._emit = emit
        self._pi = pi
        self._backend = backend
        self._controller = controller
        self._hostd = hostd
        self._wg = wg_interface
        self._interval = interval_s
        self._health_timeout = health_timeout_s

    async def snapshot(self) -> Json:
        return {"pi": self._pi.read(), "llm": await self._llm(), "voice": dict(VOICE)}

    async def run(self, stop: asyncio.Event) -> None:
        """Emit until ``stop`` is set. A failed round is logged (no content) and skipped, never fatal."""
        while not stop.is_set():
            try:
                await self._emit("host.status", await self.snapshot())
            except Exception as exc:  # a status round must never take atlas-core down
                log.warning("host.status round failed: %s", type(exc).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self._interval)

    async def _llm(self) -> Json:
        health = await self._health()
        power = await self._power(health)
        out: Json = {"power": power.value, "link": self._link(health), "service": self._service(health, power)}
        if health is not None and health.model_loaded:
            out["model_loaded"] = health.model_loaded[:100]
        gpu = await self._gpu(power)
        if gpu is not None:
            out["gpu"] = gpu
        return out

    async def _health(self) -> Health | None:
        if self._backend is None:
            return None
        try:
            return await self._backend.health(time.monotonic() + self._health_timeout)
        except Exception as exc:  # treat as unreachable: the badge then says so
            log.info("health check failed: %s", type(exc).__name__)
            return Health(False, False, None)

    async def _power(self, health: Health | None) -> PowerState:
        if self._controller is not None:
            try:
                return await self._controller.state()
            except Exception as exc:
                log.info("power state unavailable: %s", type(exc).__name__)
                return PowerState.UNKNOWN
        return PowerState.UP if health is not None and health.reachable else PowerState.UNKNOWN

    def _link(self, health: Health | None) -> str:
        return "up" if self._wg.exists() and health is not None and health.reachable else "down"

    @staticmethod
    def _service(health: Health | None, power: PowerState) -> str:
        if health is None:
            return "unknown"
        if health.reachable and health.authenticated:
            return "ready"
        if health.reachable:
            return "down"  # answers but refuses the token: an outage until fixed (LLM_AUTH_FAILED)
        return "down" if power is PowerState.UP else "unknown"

    async def _gpu(self, power: PowerState) -> Json | None:
        if self._hostd is None or power is not PowerState.UP:
            return None
        try:
            report = await self._hostd.gpu()
        except HostdError as exc:
            log.info("gpu report unavailable: %s", type(exc).__name__)
            return None
        return gpu_json(report)


def gpu_json(report: GpuReport | None) -> Json | None:
    """events.v1 ``HostStatus.llm.gpu``, or None when hostd lacks a field (NVML down, or an older hostd)."""
    if report is None or report.util_pct is None or report.temp_c is None or report.vram_used_mb is None:
        return None
    return {
        "util_pct": report.util_pct,
        "vram_used_mb": report.vram_used_mb,
        "vram_total_mb": report.vram_total_mb,
        "temp_c": report.temp_c,
    }
