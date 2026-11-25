"""Client for ``atlas-hostd`` (contracts/hostd.v1.yaml), the agent on the Windows desktop.

Plain HTTP to ``10.77.0.2:8770`` through WireGuard. There is no HTTP credential: access control is
the tunnel, the Windows firewall and hostd's source-address check. A 403 therefore means the Pi is
not coming from ``10.77.0.1``, which is a configuration fault, and is reported as such.

Only the fields the Pi acts on are parsed; hostd may add fields in later 1.x revisions.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

from atlas.net.http import Endpoint, HttpDeadlineError, HttpTransportError, fetch, parse_http_url

DEFAULT_HOSTD_URL: Final = "http://10.77.0.2:8770"
HEALTH_TIMEOUT_S: Final = 2.0
CALL_TIMEOUT_S: Final = 5.0
MAX_BODY: Final = 64 * 1024


class HostdError(Exception):
    """hostd unreachable, forbidden or answering nonsense. Never contains response text."""


class HostdUnreachable(HostdError):
    pass


class HostdForbidden(HostdError):
    """403: the request did not come from the allowed Pi address."""


@dataclass(frozen=True, slots=True)
class HostdHealth:
    api_version: str
    boot_id: str
    uptime_s: int


@dataclass(frozen=True, slots=True)
class LlmServerState:
    kind: str
    running: bool
    port_listening: bool


@dataclass(frozen=True, slots=True)
class HostdStatus:
    boot_id: str
    user_signed_in: bool
    llm_server: LlmServerState
    sleep_eligible: bool
    sleep_blockers: tuple[str, ...]
    last_wake_source: str | None


@dataclass(frozen=True, slots=True)
class GpuReport:
    vram_total_mb: int
    vram_free_mb: int
    llm_server_vram_mb: int
    foreign_vram_mb: int
    # For the HUD's System panel (core/hoststatus.py); None if hostd leaves one out.
    vram_used_mb: int | None = None
    util_pct: int | None = None
    temp_c: int | None = None


@dataclass(frozen=True, slots=True)
class SleepOk:
    sleep_state: str


@dataclass(frozen=True, slots=True)
class SleepNo:
    reasons: tuple[str, ...]


class HostdApi(Protocol):
    """What the power controller and wake flow need from hostd (a fake in tests)."""

    async def health(self) -> HostdHealth: ...
    async def status(self) -> HostdStatus: ...
    async def gpu(self) -> GpuReport | None: ...
    async def sleep(
        self, *, request_id: str, reason: str, atlas_idle_s: int, wake: Mapping[str, str] | None
    ) -> SleepOk | SleepNo: ...


def _obj(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except ValueError:
        raise HostdError("response is not JSON") from None
    if not isinstance(value, dict):
        raise HostdError("response is not an object")
    return value


def _opt_int(body: dict[str, Any], key: str, low: int, high: int | None) -> int | None:
    """An optional integer in range, or None (absent, not an integer, or out of range: never guessed)."""
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        return None
    return value


class HostdClient:
    def __init__(self, base_url: str = DEFAULT_HOSTD_URL) -> None:
        self._endpoint: Endpoint = parse_http_url(base_url)

    async def _call(
        self, method: str, path: str, *, limit_s: float, body: Mapping[str, Any] | None = None
    ) -> tuple[int, bytes]:
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, raw = await fetch(
                self._endpoint,
                method,
                path,
                headers=headers,
                body=data,
                deadline=time.monotonic() + limit_s,
                max_body=MAX_BODY,
            )
        except (HttpDeadlineError, HttpTransportError):
            raise HostdUnreachable("hostd did not answer") from None
        if status == 403:
            raise HostdForbidden("hostd refused this source address")
        return status, raw

    async def health(self) -> HostdHealth:
        status, raw = await self._call("GET", "/v1/health", limit_s=HEALTH_TIMEOUT_S)
        if status != 200:
            raise HostdError(f"health: HTTP {status}")
        body = _obj(raw)
        try:
            return HostdHealth(str(body["api_version"]), str(body["boot_id"]), int(body["uptime_s"]))
        except (KeyError, TypeError, ValueError):
            raise HostdError("bad health response") from None

    async def status(self) -> HostdStatus:
        status, raw = await self._call("GET", "/v1/status", limit_s=CALL_TIMEOUT_S)
        if status != 200:
            raise HostdError(f"status: HTTP {status}")
        body = _obj(raw)
        try:
            llm = body["llm_server"]
            elig = body["sleep_eligibility"]
            source = body["power"].get("last_wake_source")
            return HostdStatus(
                boot_id=str(body["boot_id"]),
                user_signed_in=bool(body["activity"]["user_signed_in"]),
                llm_server=LlmServerState(str(llm["kind"]), bool(llm["running"]), bool(llm["port_listening"])),
                sleep_eligible=bool(elig["eligible"]),
                sleep_blockers=tuple(str(r) for r in elig["reasons"]),
                last_wake_source=str(source) if source else None,
            )
        except (KeyError, TypeError, AttributeError):
            raise HostdError("bad status response") from None

    async def gpu(self) -> GpuReport | None:
        """``None`` when NVML is unavailable (503): the VRAM check is then skipped, not failed."""
        status, raw = await self._call("GET", "/v1/gpu", limit_s=CALL_TIMEOUT_S)
        if status == 503:
            return None
        if status != 200:
            raise HostdError(f"gpu: HTTP {status}")
        body = _obj(raw)
        try:
            return GpuReport(
                int(body["vram_total_mb"]),
                int(body["vram_free_mb"]),
                int(body["llm_server_vram_mb"]),
                int(body["foreign_vram_mb"]),
                vram_used_mb=_opt_int(body, "vram_used_mb", 0, None),
                util_pct=_opt_int(body, "util_pct", 0, 100),
                temp_c=_opt_int(body, "temp_c", -50, 150),
            )
        except (KeyError, TypeError, ValueError):
            raise HostdError("bad gpu response") from None

    async def sleep(
        self, *, request_id: str, reason: str, atlas_idle_s: int, wake: Mapping[str, str] | None
    ) -> SleepOk | SleepNo:
        payload = {"request_id": request_id, "reason": reason, "atlas_idle_s": atlas_idle_s, "wake": wake}
        status, raw = await self._call("POST", "/v1/sleep", limit_s=CALL_TIMEOUT_S, body=payload)
        body = _obj(raw)
        try:
            if status == 202:
                return SleepOk(str(body["sleep_state"]))
            if status == 409:
                return SleepNo(tuple(str(r) for r in body["reasons"]))
        except (KeyError, TypeError):
            raise HostdError("bad sleep response") from None
        raise HostdError(f"sleep: HTTP {status}")
