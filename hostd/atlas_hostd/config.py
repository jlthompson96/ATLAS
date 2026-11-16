"""TOML configuration (``tomllib``), strictly validated. Unknown keys are errors."""

from __future__ import annotations

import ipaddress
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atlas_hostd.models import LlmServerKind


class ConfigError(ValueError):
    """The configuration file is missing, malformed or unsafe."""


def _default_processes() -> dict[LlmServerKind, tuple[str, ...]]:
    return {
        LlmServerKind.LMSTUDIO: ("lm studio.exe",),
        LlmServerKind.LLMSTER: ("llmster.exe",),
        LlmServerKind.LLAMA_SERVER: ("llama-server.exe",),
    }


@dataclass(frozen=True, slots=True)
class ListenerConfig:
    """Main listener. ``address`` must be a concrete address (the ``wg0`` address in production)."""

    address: str = "10.77.0.2"
    port: int = 8770
    allowed_source: str = "10.77.0.1"


@dataclass(frozen=True, slots=True)
class HelperListenerConfig:
    """Helper listener. Loopback only."""

    address: str = "127.0.0.1"
    port: int = 8771


@dataclass(frozen=True, slots=True)
class SleepConfig:
    input_idle_s: int = 1200  # no keyboard/mouse input for this long (PLAN.md §5.4: 20 min)
    resume_grace_s: int = 120  # contract: resumed < 2 min ago
    sleep_delay_s: float = 2.0  # reply first, sleep after ~2 s
    allow_sleep_if_user_woke: bool = False  # opt-in: ATLAS may sleep a host the user woke themselves
    foreign_gpu_util_pct: int = 30  # NVML utilisation at/above this with no lease counts as foreign GPU load
    claim_clock_skew_s: int = 5  # contract: first_packet_at minus 5 s


@dataclass(frozen=True, slots=True)
class LlmServerConfig:
    port: int = 1234
    listen_address: str = "10.77.0.2"
    processes: dict[LlmServerKind, tuple[str, ...]] = field(default_factory=_default_processes)

    def all_process_names(self) -> frozenset[str]:
        return frozenset(name for names in self.processes.values() for name in names)


@dataclass(frozen=True, slots=True)
class HelperConfig:
    """Used by ``atlas-hostd-helper`` only."""

    hostd_port: int = 8771
    interval_s: int = 15
    gpu_heavy_pct: int = 20  # per-process GPU engine utilisation counted as "heavy"


@dataclass(frozen=True, slots=True)
class Config:
    listener: ListenerConfig = field(default_factory=ListenerConfig)
    helper_listener: HelperListenerConfig = field(default_factory=HelperListenerConfig)
    sleep: SleepConfig = field(default_factory=SleepConfig)
    llm_server: LlmServerConfig = field(default_factory=LlmServerConfig)
    helper: HelperConfig = field(default_factory=HelperConfig)
    max_leases: int = 16  # the contract's Status.leases maxItems


def _section(raw: dict[str, Any], name: str, allowed: set[str]) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"[{name}] unknown keys: {', '.join(sorted(unknown))}")
    return value


def _int(value: Any, name: str, *, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ConfigError(f"{name} must be an integer in {lo}..{hi}")
    return value


def _number(value: Any, name: str, *, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not lo <= value <= hi:
        raise ConfigError(f"{name} must be a number in {lo}..{hi}")
    return float(value)


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be a boolean")
    return value


def _ip(value: Any, name: str) -> str:
    try:
        return str(ipaddress.ip_address(value))
    except ValueError as exc:
        raise ConfigError(f"{name} must be an IP address literal") from exc


def parse_config(raw: dict[str, Any]) -> Config:
    unknown_top = set(raw) - {"listener", "helper_listener", "sleep", "llm_server", "helper", "max_leases"}
    if unknown_top:
        raise ConfigError(f"unknown top-level keys: {', '.join(sorted(unknown_top))}")

    lst = _section(raw, "listener", {"address", "port", "allowed_source"})
    d_l = ListenerConfig()
    address = _ip(lst.get("address", d_l.address), "listener.address")
    if ipaddress.ip_address(address).is_unspecified:
        raise ConfigError("listener.address must be a concrete address, never 0.0.0.0 or ::")
    listener = ListenerConfig(
        address=address,
        port=_int(lst.get("port", d_l.port), "listener.port", lo=0, hi=65535),
        allowed_source=_ip(lst.get("allowed_source", d_l.allowed_source), "listener.allowed_source"),
    )

    hl = _section(raw, "helper_listener", {"port"})
    d_h = HelperListenerConfig()
    helper_listener = HelperListenerConfig(
        address=d_h.address,
        port=_int(hl.get("port", d_h.port), "helper_listener.port", lo=0, hi=65535),
    )

    sl = _section(
        raw,
        "sleep",
        {
            "input_idle_s",
            "resume_grace_s",
            "sleep_delay_s",
            "allow_sleep_if_user_woke",
            "foreign_gpu_util_pct",
            "claim_clock_skew_s",
        },
    )
    d_s = SleepConfig()
    sleep = SleepConfig(
        input_idle_s=_int(sl.get("input_idle_s", d_s.input_idle_s), "sleep.input_idle_s", lo=60, hi=86400),
        resume_grace_s=_int(sl.get("resume_grace_s", d_s.resume_grace_s), "sleep.resume_grace_s", lo=0, hi=3600),
        sleep_delay_s=_number(sl.get("sleep_delay_s", d_s.sleep_delay_s), "sleep.sleep_delay_s", lo=0, hi=30),
        allow_sleep_if_user_woke=_bool(
            sl.get("allow_sleep_if_user_woke", d_s.allow_sleep_if_user_woke), "sleep.allow_sleep_if_user_woke"
        ),
        foreign_gpu_util_pct=_int(
            sl.get("foreign_gpu_util_pct", d_s.foreign_gpu_util_pct), "sleep.foreign_gpu_util_pct", lo=1, hi=100
        ),
        claim_clock_skew_s=_int(
            sl.get("claim_clock_skew_s", d_s.claim_clock_skew_s), "sleep.claim_clock_skew_s", lo=0, hi=60
        ),
    )

    ll = _section(raw, "llm_server", {"port", "listen_address", "processes"})
    d_m = LlmServerConfig()
    processes = _default_processes()
    raw_procs = ll.get("processes", {})
    if not isinstance(raw_procs, dict):
        raise ConfigError("[llm_server.processes] must be a table")
    for key, names in raw_procs.items():
        try:
            kind = LlmServerKind(key)
        except ValueError as exc:
            raise ConfigError(f"llm_server.processes: unknown kind {key!r}") from exc
        if kind is LlmServerKind.NONE:
            raise ConfigError("llm_server.processes: 'none' is not a process kind")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
            raise ConfigError(f"llm_server.processes.{key} must be a non-empty list of process names")
        processes[kind] = tuple(n.lower() for n in names)
    llm_server = LlmServerConfig(
        port=_int(ll.get("port", d_m.port), "llm_server.port", lo=1, hi=65535),
        listen_address=_ip(ll.get("listen_address", d_m.listen_address), "llm_server.listen_address"),
        processes=processes,
    )

    hp = _section(raw, "helper", {"hostd_port", "interval_s", "gpu_heavy_pct"})
    d_p = HelperConfig()
    helper = HelperConfig(
        hostd_port=_int(hp.get("hostd_port", helper_listener.port), "helper.hostd_port", lo=1, hi=65535),
        interval_s=_int(hp.get("interval_s", d_p.interval_s), "helper.interval_s", lo=1, hi=30),
        gpu_heavy_pct=_int(hp.get("gpu_heavy_pct", d_p.gpu_heavy_pct), "helper.gpu_heavy_pct", lo=1, hi=100),
    )

    return Config(
        listener=listener,
        helper_listener=helper_listener,
        sleep=sleep,
        llm_server=llm_server,
        helper=helper,
        max_leases=_int(raw.get("max_leases", 16), "max_leases", lo=1, hi=16),
    )


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except OSError as exc:
        raise ConfigError(f"cannot read config {path}: {exc.strerror}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc
    return parse_config(raw)
