"""The hostd application: routing, handlers and the glue between the platform layer and the decision logic."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Final

from atlas_hostd import API_VERSION, __version__
from atlas_hostd.activity import ActivitySnapshot, ActivityTracker
from atlas_hostd.config import Config
from atlas_hostd.http import Request, Response, error_response, normalize_ip
from atlas_hostd.leases import LeaseLimitError, LeaseTable, LeaseView
from atlas_hostd.models import (
    GpuSample,
    LlmServerKind,
    OsInfo,
    ResumeInfo,
    SleepState,
    WakeClaim,
    fmt_ts,
)
from atlas_hostd.platform.base import Platform
from atlas_hostd.policy import (
    CycleInfo,
    PolicyInputs,
    current_cycle,
    host_refusals,
    preferred_sleep_state,
    sleep_refusals,
)
from atlas_hostd.validation import (
    ValidationError,
    parse_helper_report,
    parse_lease_request,
    parse_sleep_request,
    valid_id,
)

log = logging.getLogger("atlas_hostd.service")

SLEEP_STATES_TTL_S: Final = 600.0
RESUME_CACHE_TTL_S: Final = 5.0
REPLAY_CACHE_SIZE: Final = 64


class Listener(StrEnum):
    MAIN = "main"
    HELPER = "helper"


@dataclass(frozen=True, slots=True)
class Route:
    method: str
    template: str
    listener: Listener
    operation_id: str


#: Every implemented route. A contract test compares this table with hostd.v1.yaml in both directions.
ROUTES: Final = (
    Route("GET", "/v1/health", Listener.MAIN, "getHealth"),
    Route("GET", "/v1/status", Listener.MAIN, "getStatus"),
    Route("GET", "/v1/gpu", Listener.MAIN, "getGpu"),
    Route("GET", "/v1/activity", Listener.MAIN, "getActivity"),
    Route("POST", "/v1/sleep", Listener.MAIN, "requestSleep"),
    Route("PUT", "/v1/leases/{lease_id}", Listener.MAIN, "putLease"),
    Route("DELETE", "/v1/leases/{lease_id}", Listener.MAIN, "deleteLease"),
    Route("POST", "/v1/helper/activity", Listener.HELPER, "postHelperActivity"),
)


@dataclass(frozen=True, slots=True)
class _Facts:
    """One blocking read of the platform."""

    now: datetime
    mono: float
    sleep_states: tuple[SleepState, ...]
    resume: ResumeInfo | None
    gpu: GpuSample | None
    llm_kind: LlmServerKind
    port_listening: bool
    signed_in: bool


def _match(template: str, path: str) -> dict[str, str] | None:
    t_parts = template.strip("/").split("/")
    p_parts = path.strip("/").split("/")
    if len(t_parts) != len(p_parts):
        return None
    params: dict[str, str] = {}
    for t, p in zip(t_parts, p_parts, strict=True):
        if t.startswith("{") and t.endswith("}"):
            if not p:
                return None
            params[t[1:-1]] = p
        elif t != p:
            return None
    return params


class HostdApp:
    def __init__(self, platform: Platform, config: Config) -> None:
        self._p = platform
        self._cfg = config
        self._leases = LeaseTable(config.max_leases)
        self._activity = ActivityTracker()
        self._started_mono = platform.monotonic()
        self._boot_id = platform.boot_id()
        self._boot_time = platform.boot_time()
        self._os: OsInfo = platform.os_info()
        self._llm_names = config.llm_server.all_process_names()
        self._states: tuple[SleepState, ...] | None = None
        self._states_at = 0.0
        self._resume_cache: tuple[float, ResumeInfo | None] | None = None
        self._keep_awake_on = False
        self._power_lock = asyncio.Lock()
        self._sleep_lock = asyncio.Lock()
        self._replay: OrderedDict[str, Response] = OrderedDict()
        self._pending_sleep: tuple[asyncio.Task[None], datetime, SleepState] | None = None
        self._handlers: dict[str, Callable[..., Awaitable[Response]]] = {
            "getHealth": self._get_health,
            "getStatus": self._get_status,
            "getGpu": self._get_gpu,
            "getActivity": self._get_activity,
            "requestSleep": self._request_sleep,
            "putLease": self._put_lease,
            "deleteLease": self._delete_lease,
            "postHelperActivity": self._post_helper_activity,
        }

    # ---- access control and dispatch -------------------------------------------------------------------------
    def source_allowed(self, listener: Listener, peer: str) -> bool:
        """Main listener: only the configured Pi address. Helper listener: loopback only."""
        try:
            addr = ipaddress.ip_address(normalize_ip(peer))
        except ValueError:
            return False
        if listener is Listener.HELPER:
            return addr.is_loopback
        return addr == ipaddress.ip_address(self._cfg.listener.allowed_source)

    def checker(self, listener: Listener) -> Callable[[str], bool]:
        return lambda peer: self.source_allowed(listener, peer)

    def handler(self, listener: Listener) -> Callable[[Request], Awaitable[Response]]:
        async def handle(request: Request) -> Response:
            return await self.handle(listener, request)

        return handle

    async def handle(self, listener: Listener, request: Request) -> Response:
        if not self.source_allowed(listener, request.peer):
            return error_response(403, "FORBIDDEN_SOURCE", "source address not allowed")
        path = request.target.split("?", 1)[0]
        allowed: list[str] = []
        for route in ROUTES:
            if route.listener is not listener:
                continue
            params = _match(route.template, path)
            if params is None:
                continue
            if route.method != request.method:
                allowed.append(route.method)
                continue
            return await self._handlers[route.operation_id](request, **params)
        if allowed:
            return error_response(405, "METHOD_NOT_ALLOWED", "method not allowed", {"Allow": ", ".join(allowed)})
        return error_response(404, "NOT_FOUND", "no such route")

    # ---- platform facts --------------------------------------------------------------------------------------
    def _read_facts(self, *, fresh_resume: bool) -> _Facts:
        """Blocking; runs in a worker thread."""
        mono = self._p.monotonic()
        if self._states is None or mono - self._states_at > SLEEP_STATES_TTL_S:
            self._states = self._p.sleep_states()
            self._states_at = mono
        cache = self._resume_cache
        if fresh_resume or cache is None or mono - cache[0] > RESUME_CACHE_TTL_S:
            resume = self._p.last_resume()
            self._resume_cache = (mono, resume)
        else:
            resume = cache[1]
        return _Facts(
            now=self._p.now(),
            mono=mono,
            sleep_states=self._states,
            resume=resume,
            gpu=self._p.gpu_sample(self._llm_names),
            llm_kind=self._p.llm_server_kind(self._cfg.llm_server.processes),
            port_listening=self._p.port_listening(self._cfg.llm_server.listen_address, self._cfg.llm_server.port),
            signed_in=self._p.user_signed_in(),
        )

    async def _facts(self, *, fresh_resume: bool = False) -> _Facts:
        return await asyncio.to_thread(self._read_facts, fresh_resume=fresh_resume)

    def _policy_inputs(self, facts: _Facts) -> tuple[PolicyInputs, ActivitySnapshot, CycleInfo]:
        snap = self._activity.snapshot(facts.mono)
        cycle = current_cycle(self._boot_time, facts.resume)
        leases = len(self._leases.active(facts.mono))
        # Utilisation only counts as foreign load while the LLM server is idle, i.e. while no lease is held.
        gpu_busy = snap.gpu_heavy_process_count > 0 or (
            leases == 0 and facts.gpu is not None and facts.gpu.util_pct >= self._cfg.sleep.foreign_gpu_util_pct
        )
        inputs = PolicyInputs(
            now=facts.now,
            sleep_states=facts.sleep_states,
            cycle=cycle,
            helper_connected=snap.helper_connected,
            input_idle_s=snap.input_idle_s,
            fullscreen_app=snap.fullscreen_app,
            foreign_gpu_busy=gpu_busy,
            active_leases=leases,
        )
        return inputs, snap, cycle

    # ---- JSON builders ---------------------------------------------------------------------------------------
    @staticmethod
    def _gpu_json(gpu: GpuSample, sampled_at: datetime) -> dict[str, Any]:
        return {
            "name": gpu.name[:80],
            "driver_version": gpu.driver_version[:40],
            "vram_total_mb": gpu.vram_total_mb,
            "vram_used_mb": gpu.vram_used_mb,
            "vram_free_mb": gpu.vram_free_mb,
            "util_pct": gpu.util_pct,
            "temp_c": gpu.temp_c,
            "throttling": sorted(set(gpu.throttling)),
            "llm_server_vram_mb": gpu.llm_server_vram_mb,
            "foreign_vram_mb": gpu.foreign_vram_mb,
            "foreign_process_count": gpu.foreign_process_count,
            "sampled_at": fmt_ts(sampled_at),
        }

    @staticmethod
    def _lease_json(lease: LeaseView) -> dict[str, Any]:
        return {"lease_id": lease.lease_id, "reason": lease.reason.value, "expires_at": fmt_ts(lease.expires_at)}

    def _activity_json(self, snap: ActivitySnapshot, signed_in: bool, gpu_busy: bool) -> dict[str, Any]:
        return {
            "user_signed_in": snap.helper_connected or signed_in,
            "helper_connected": snap.helper_connected,
            "input_idle_s": snap.input_idle_s,
            "fullscreen_app": snap.fullscreen_app,
            "foreign_gpu_busy": gpu_busy,
            "reported_at": None if snap.reported_at is None else fmt_ts(snap.reported_at),
        }

    # ---- handlers --------------------------------------------------------------------------------------------
    async def _get_health(self, request: Request) -> Response:
        return Response(
            200,
            {
                "status": "ok",
                "api_version": API_VERSION,
                "boot_id": self._boot_id,
                "uptime_s": max(int(self._p.monotonic() - self._started_mono), 0),
            },
        )

    async def _get_status(self, request: Request) -> Response:
        facts = await self._facts()
        inputs, snap, cycle = self._policy_inputs(facts)
        reasons = host_refusals(inputs, self._cfg.sleep)
        preferred = preferred_sleep_state(facts.sleep_states)
        resume = facts.resume
        body = {
            "hostd_version": __version__,
            "api_version": API_VERSION,
            "boot_id": self._boot_id,
            "os": {"name": self._os.name[:60], "build": self._os.build[:40]},
            "power": {
                "sleep_states_available": [s.value for s in facts.sleep_states],
                "preferred_sleep_state": None if preferred is None else preferred.value,
                "booted_at": fmt_ts(self._boot_time),
                "last_resume_at": None if resume is None else fmt_ts(resume.at),
                "last_wake_source": cycle.source.value,
            },
            "llm_server": {
                "kind": facts.llm_kind.value,
                "running": facts.llm_kind is not LlmServerKind.NONE,
                "port_listening": facts.port_listening,
            },
            "gpu": None if facts.gpu is None else self._gpu_json(facts.gpu, facts.now),
            "activity": self._activity_json(snap, facts.signed_in, inputs.foreign_gpu_busy),
            "leases": [self._lease_json(lease) for lease in self._leases.active(facts.mono)],
            "sleep_eligibility": {"eligible": not reasons, "reasons": [r.value for r in reasons]},
        }
        return Response(200, body)

    async def _get_gpu(self, request: Request) -> Response:
        facts = await self._facts()
        if facts.gpu is None:
            return error_response(503, "NVML_UNAVAILABLE", "GPU telemetry is unavailable")
        return Response(200, self._gpu_json(facts.gpu, facts.now))

    async def _get_activity(self, request: Request) -> Response:
        facts = await self._facts()
        inputs, snap, _ = self._policy_inputs(facts)
        return Response(200, self._activity_json(snap, facts.signed_in, inputs.foreign_gpu_busy))

    async def _post_helper_activity(self, request: Request) -> Response:
        if not _is_json(request):
            return error_response(400, "INVALID_BODY", "Content-Type must be application/json")
        try:
            report = parse_helper_report(request.body)
        except ValidationError as exc:
            return error_response(400, "INVALID_BODY", str(exc))
        self._activity.record(report, now_mono=self._p.monotonic(), now_wall=self._p.now())
        return Response(204)

    async def _put_lease(self, request: Request, lease_id: str) -> Response:
        if not valid_id(lease_id):
            return error_response(400, "INVALID_LEASE_ID", "lease_id is not a valid id")
        if not _is_json(request):
            return error_response(400, "INVALID_BODY", "Content-Type must be application/json")
        try:
            lease_request = parse_lease_request(request.body)
        except ValidationError as exc:
            return error_response(400, "INVALID_BODY", str(exc))
        try:
            lease = self._leases.put(
                lease_id,
                lease_request.reason,
                lease_request.ttl_s,
                now_mono=self._p.monotonic(),
                now_wall=self._p.now(),
            )
        except LeaseLimitError:
            return error_response(400, "LEASE_LIMIT", "too many active leases")
        await self._sync_keep_awake()
        return Response(200, self._lease_json(lease))

    async def _delete_lease(self, request: Request, lease_id: str) -> Response:
        if not valid_id(lease_id):
            return error_response(400, "INVALID_LEASE_ID", "lease_id is not a valid id")
        # Releasing a well-formed lease id that does not exist is a no-op by contract.
        self._leases.delete(lease_id)
        await self._sync_keep_awake()
        return Response(204)

    async def _request_sleep(self, request: Request) -> Response:
        if not _is_json(request):
            return error_response(400, "INVALID_BODY", "Content-Type must be application/json")
        try:
            sleep_request = parse_sleep_request(request.body)
        except ValidationError as exc:
            return error_response(400, "INVALID_BODY", str(exc))
        async with self._sleep_lock:
            replay = self._replay.get(sleep_request.request_id)
            if replay is not None:
                return replay  # idempotent per request_id
            facts = await self._facts(fresh_resume=True)
            inputs, _, _ = self._policy_inputs(facts)
            reasons = sleep_refusals(inputs, self._cfg.sleep, sleep_request.wake)
            request_id = sleep_request.request_id
            if reasons:
                log.info("sleep refused id=%s reasons=%s", request_id, ",".join(r.value for r in reasons))
                response = Response(
                    409, {"request_id": request_id, "accepted": False, "reasons": [r.value for r in reasons]}
                )
            else:
                state, sleep_at = self._schedule_sleep(facts, sleep_request.wake)
                log.info("sleep accepted id=%s state=%s", request_id, state.value)
                response = Response(
                    202,
                    {
                        "request_id": request_id,
                        "accepted": True,
                        "sleep_state": state.value,
                        "sleep_at": fmt_ts(sleep_at),
                    },
                )
            self._replay[request_id] = response
            while len(self._replay) > REPLAY_CACHE_SIZE:
                self._replay.popitem(last=False)
            return response

    # ---- sleeping --------------------------------------------------------------------------------------------
    def _schedule_sleep(self, facts: _Facts, claim: WakeClaim | None) -> tuple[SleepState, datetime]:
        if self._pending_sleep is not None and not self._pending_sleep[0].done():
            _, sleep_at, state = self._pending_sleep
            return state, sleep_at
        chosen = preferred_sleep_state(facts.sleep_states)
        if chosen is None:  # unreachable: SLEEP_UNSUPPORTED would have refused
            raise RuntimeError("no sleep state")
        delay = self._cfg.sleep.sleep_delay_s
        sleep_at = facts.now + timedelta(seconds=delay)
        task = asyncio.get_running_loop().create_task(self._sleep_after(delay, chosen, claim))
        self._pending_sleep = (task, sleep_at, chosen)
        return chosen, sleep_at

    async def _sleep_after(self, delay: float, state: SleepState, claim: WakeClaim | None) -> None:
        await asyncio.sleep(delay)
        # Re-check just before sleeping: a lease or user input may have arrived during the reply delay.
        facts = await self._facts(fresh_resume=True)
        inputs, _, _ = self._policy_inputs(facts)
        reasons = sleep_refusals(inputs, self._cfg.sleep, claim)
        if reasons:
            log.info("sleep cancelled at fire time reasons=%s", ",".join(r.value for r in reasons))
            return
        log.info("suspending state=%s", state.value)
        await asyncio.to_thread(self._p.suspend, state)  # returns when the machine resumes
        log.info("resumed from suspend")

    async def wait_for_pending_sleep(self) -> None:
        """Test hook: wait until the scheduled sleep (if any) has run or been cancelled."""
        if self._pending_sleep is not None:
            await self._pending_sleep[0]

    # ---- leases and keep-awake -------------------------------------------------------------------------------
    async def _sync_keep_awake(self) -> None:
        async with self._power_lock:
            want = bool(self._leases.active(self._p.monotonic()))
            if want != self._keep_awake_on:
                await asyncio.to_thread(self._p.set_keep_awake, want)
                self._keep_awake_on = want
                log.info("keep-awake %s", "held" if want else "released")

    async def tick(self) -> None:
        """Expire leases and release the power request when the last one goes. Called about once a second."""
        await self._sync_keep_awake()

    async def shutdown(self) -> None:
        if self._pending_sleep is not None:
            self._pending_sleep[0].cancel()
        self._leases.clear()
        await self._sync_keep_awake()


def _is_json(request: Request) -> bool:
    ctype = (request.content_type or "").split(";", 1)[0].strip().lower()
    return ctype == "application/json"
