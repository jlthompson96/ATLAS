"""Windows platform layer (ctypes, standard library only).

Never imported on macOS/Linux: the module raises on import anywhere but ``win32`` and callers import it lazily.
NOTHING IN THIS FILE HAS BEEN RUN: it was written on a Mac and is type-checked with ``mypy --platform win32``.
Every function is on the Windows test checklist in hostd/README.md.
"""

from __future__ import annotations

import sys

if sys.platform != "win32":  # pragma: no cover - guards accidental imports on the dev machine
    raise ImportError("atlas_hostd.platform.windows is Windows-only")

import contextlib
import ctypes
import logging
import os
import queue
import subprocess
import threading
import time
import winreg
from collections.abc import Mapping
from ctypes import wintypes
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from atlas_hostd.models import GpuSample, LlmServerKind, OsInfo, ResumeInfo, SleepState
from atlas_hostd.platform.base import tcp_port_listening
from atlas_hostd.platform.parsers import (
    classify_throttle,
    gpu_heavy_pids,
    parse_powercfg_available,
    parse_resume_event_xml,
)

log = logging.getLogger("atlas_hostd.platform.windows")

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001  # deliberately not ES_DISPLAY_REQUIRED (contract)
_MB = 1024 * 1024
_TOOL_TIMEOUT_S = 10.0
_NVML_NOT_AVAILABLE = 0xFFFFFFFFFFFFFFFF

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
_wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)

_kernel32.GetTickCount64.restype = ctypes.c_uint64
_kernel32.GetTickCount.restype = wintypes.DWORD
_kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
_kernel32.SetThreadExecutionState.restype = wintypes.DWORD
_kernel32.GetCurrentProcessId.restype = wintypes.DWORD
_kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_powrprof.SetSuspendState.argtypes = [wintypes.BOOLEAN, wintypes.BOOLEAN, wintypes.BOOLEAN]
_powrprof.SetSuspendState.restype = wintypes.BOOLEAN
_shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
_shell32.SHQueryUserNotificationState.restype = ctypes.c_long
_wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
_wtsapi32.WTSFreeMemory.restype = None


def _system_tool(name: str) -> str:
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return str(Path(root) / "System32" / name)


def _run_tool(args: list[str], timeout_s: float = _TOOL_TIMEOUT_S) -> str | None:
    """Run a built-in Windows tool by absolute path, no shell. ``None`` on any failure."""
    try:
        done = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout_s, check=False, creationflags=0x08000000
        )
    except (OSError, subprocess.TimeoutExpired):
        log.warning("tool failed to run: %s", Path(args[0]).name)
        return None
    if done.returncode != 0:
        log.warning("tool exited %d: %s", done.returncode, Path(args[0]).name)
        return None
    return done.stdout


# ---- process list (ToolHelp32) -----------------------------------------------------------------------------
class _ProcessEntry32(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    )


def list_processes() -> dict[int, str]:
    """PID to lower-case executable name."""
    snap = _kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snap is None or snap == ctypes.c_void_p(-1).value:
        return {}
    out: dict[int, str] = {}
    try:
        entry = _ProcessEntry32()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32)
        first = _kernel32.Process32FirstW
        first.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32)]
        first.restype = wintypes.BOOL
        nxt = _kernel32.Process32NextW
        nxt.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32)]
        nxt.restype = wintypes.BOOL
        ok = first(snap, ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = entry.szExeFile.lower()
            ok = nxt(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return out


# ---- NVML --------------------------------------------------------------------------------------------------
class _NvmlMemory(ctypes.Structure):
    _fields_ = (("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong))


class _NvmlUtilization(ctypes.Structure):
    _fields_ = (("gpu", ctypes.c_uint), ("memory", ctypes.c_uint))


class _NvmlProcessInfo(ctypes.Structure):
    # nvmlProcessInfo_t as used by the *_v2 and *_v3 calls.
    _fields_ = (
        ("pid", ctypes.c_uint),
        ("usedGpuMemory", ctypes.c_ulonglong),
        ("gpuInstanceId", ctypes.c_uint),
        ("computeInstanceId", ctypes.c_uint),
    )


class _NvmlError(Exception):
    pass


class _Nvml:
    """Thin NVML binding for device 0. Lazily initialised; re-initialised after a failure (driver reset)."""

    _RETRY_AFTER_S = 30.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lib: ctypes.WinDLL | None = None
        self._handle = ctypes.c_void_p()
        self._failed_at: float | None = None

    def _fn(self, name: str) -> Any:
        assert self._lib is not None  # noqa: S101
        return getattr(self._lib, name)

    def _call(self, name: str, *args: object) -> None:
        rc = self._fn(name)(*args)
        if rc != 0:
            raise _NvmlError(f"{name} returned {rc}")

    def _load(self) -> None:
        candidates = [
            "nvml.dll",
            str(
                Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvml.dll"
            ),
        ]
        last: Exception | None = None
        for candidate in candidates:
            try:
                self._lib = ctypes.WinDLL(candidate)
                break
            except OSError as exc:
                last = exc
        if self._lib is None:
            raise _NvmlError("nvml.dll not found") from last
        self._call("nvmlInit_v2")
        self._call("nvmlDeviceGetHandleByIndex_v2", 0, ctypes.byref(self._handle))

    def _processes(self, names: tuple[str, ...]) -> dict[int, int | None]:
        """PID to used VRAM in MB (``None`` when NVML reports it unavailable, common under WDDM)."""
        found: dict[int, int | None] = {}
        for name in names:
            for suffix in ("_v3", "_v2"):
                if not hasattr(self._lib, name + suffix):
                    continue
                count = ctypes.c_uint(64)
                buf = (_NvmlProcessInfo * 64)()
                try:
                    self._call(name + suffix, self._handle, ctypes.byref(count), buf)
                except _NvmlError:
                    break
                for info in buf[: min(count.value, 64)]:
                    used = None if info.usedGpuMemory == _NVML_NOT_AVAILABLE else int(info.usedGpuMemory // _MB)
                    previous = found.get(int(info.pid))
                    found[int(info.pid)] = used if previous is None else previous
                break
        return found

    def sample(self, llm_names: frozenset[str]) -> GpuSample | None:
        with self._lock:
            now = datetime.now(UTC).timestamp()
            if self._lib is None:
                if self._failed_at is not None and now - self._failed_at < self._RETRY_AFTER_S:
                    return None
                try:
                    self._load()
                except (_NvmlError, OSError):
                    self._failed_at = now
                    self._lib = None
                    return None
            try:
                return self._read(llm_names)
            except _NvmlError:
                log.warning("NVML read failed; will re-initialise")
                self._lib = None
                self._failed_at = now
                return None

    def _read(self, llm_names: frozenset[str]) -> GpuSample:
        name_buf = ctypes.create_string_buffer(96)
        drv_buf = ctypes.create_string_buffer(96)
        self._call("nvmlDeviceGetName", self._handle, name_buf, 96)
        self._call("nvmlSystemGetDriverVersion", drv_buf, 96)
        mem = _NvmlMemory()
        self._call("nvmlDeviceGetMemoryInfo", self._handle, ctypes.byref(mem))
        util = _NvmlUtilization()
        self._call("nvmlDeviceGetUtilizationRates", self._handle, ctypes.byref(util))
        temp = ctypes.c_uint()
        self._call("nvmlDeviceGetTemperature", self._handle, 0, ctypes.byref(temp))  # 0 = NVML_TEMPERATURE_GPU
        mask = ctypes.c_ulonglong(0)
        for fn in ("nvmlDeviceGetCurrentClocksEventReasons", "nvmlDeviceGetCurrentClocksThrottleReasons"):
            if hasattr(self._lib, fn):
                try:
                    self._call(fn, self._handle, ctypes.byref(mask))
                except _NvmlError:
                    mask = ctypes.c_ulonglong(0)
                break

        procs = self._processes(("nvmlDeviceGetComputeRunningProcesses", "nvmlDeviceGetGraphicsRunningProcesses"))
        names = list_processes()
        llm_pids = {pid for pid in procs if names.get(pid, "") in llm_names}
        llm_mb = sum(used or 0 for pid, used in procs.items() if pid in llm_pids)
        used_mb = int(mem.used // _MB)
        total_mb = int(mem.total // _MB)
        return GpuSample(
            name=name_buf.value.decode("ascii", "replace"),
            driver_version=drv_buf.value.decode("ascii", "replace"),
            vram_total_mb=total_mb,
            vram_used_mb=used_mb,
            vram_free_mb=int(mem.free // _MB),
            util_pct=min(int(util.gpu), 100),
            temp_c=int(temp.value),
            throttling=classify_throttle(int(mask.value)),
            llm_server_vram_mb=min(llm_mb, used_mb),
            # Under WDDM per-process VRAM is often unavailable, so "foreign" is everything the LLM server is
            # not known to hold. vram_free_mb (the number the Pi acts on) is unaffected.
            foreign_vram_mb=max(used_mb - llm_mb, 0),
            foreign_process_count=len([pid for pid in procs if pid not in llm_pids]),
        )

    def close(self) -> None:
        with self._lock:
            if self._lib is not None:
                with contextlib.suppress(_NvmlError):
                    self._call("nvmlShutdown")
                self._lib = None


# ---- keep-awake thread -------------------------------------------------------------------------------------
class _KeepAwake:
    """SetThreadExecutionState is per thread and ES_CONTINUOUS lasts only while the thread lives, so one
    dedicated thread owns the state and receives commands through a queue."""

    def __init__(self) -> None:
        self._commands: queue.Queue[bool | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="atlas-hostd-keepawake", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            command = self._commands.get()
            if command is None:
                _kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
                return
            flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if command else 0)
            if _kernel32.SetThreadExecutionState(flags) == 0:
                log.error("SetThreadExecutionState failed")

    def set(self, active: bool) -> None:
        self._commands.put(active)

    def close(self) -> None:
        self._commands.put(None)
        self._thread.join(timeout=2.0)


# ---- platform ----------------------------------------------------------------------------------------------
class WindowsPlatform:
    def __init__(self) -> None:
        self._nvml = _Nvml()
        self._keep_awake = _KeepAwake()
        # Boot time from the tick counter. Assumes GetTickCount64 keeps counting through sleep and restarts at
        # boot (Fast Startup off). Windows checklist item.
        uptime = timedelta(milliseconds=int(_kernel32.GetTickCount64()))
        self._boot_time = (datetime.now(UTC) - uptime).replace(microsecond=0)
        self._boot_id = f"boot-{int(self._boot_time.timestamp()) // 60:x}"

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def boot_time(self) -> datetime:
        return self._boot_time

    def boot_id(self) -> str:
        return self._boot_id

    def os_info(self) -> OsInfo:
        build = sys.getwindowsversion().build
        edition = ""
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as key:
                edition = str(winreg.QueryValueEx(key, "EditionID")[0])
        except OSError:
            pass
        # The registry ProductName still says "Windows 10" on Windows 11, so derive the major name from the build.
        major = "Windows 11" if build >= 22000 else "Windows 10"
        return OsInfo(name=f"{major} {edition}".strip()[:60], build=str(build))

    def sleep_states(self) -> tuple[SleepState, ...]:
        out = _run_tool([_system_tool("powercfg.exe"), "/a"])
        return () if out is None else parse_powercfg_available(out)

    def last_resume(self) -> ResumeInfo | None:
        query = "*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and (EventID=1)]]"
        out = _run_tool([_system_tool("wevtutil.exe"), "qe", "System", f"/q:{query}", "/c:1", "/rd:true", "/f:xml"])
        if not out:
            return None
        info = parse_resume_event_xml(out)
        if info is None or info.at < self._boot_time - timedelta(seconds=5):
            return None  # no resume since this boot
        return info

    def gpu_sample(self, llm_process_names: frozenset[str]) -> GpuSample | None:
        return self._nvml.sample(llm_process_names)

    def llm_server_kind(self, processes: Mapping[LlmServerKind, tuple[str, ...]]) -> LlmServerKind:
        running = set(list_processes().values())
        for kind in (LlmServerKind.LMSTUDIO, LlmServerKind.LLMSTER, LlmServerKind.LLAMA_SERVER):
            if any(name in running for name in processes.get(kind, ())):
                return kind
        return LlmServerKind.NONE

    def port_listening(self, host: str, port: int) -> bool:
        return tcp_port_listening(host, port)

    def user_signed_in(self) -> bool:
        count = wintypes.DWORD()
        sessions = ctypes.c_void_p()
        enum = _wtsapi32.WTSEnumerateSessionsW
        enum.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.DWORD),
        ]
        enum.restype = wintypes.BOOL

        class _WtsSessionInfo(ctypes.Structure):
            _fields_ = (("SessionId", wintypes.DWORD), ("pWinStationName", wintypes.LPWSTR), ("State", ctypes.c_int))

        if not enum(None, 0, 1, ctypes.byref(sessions), ctypes.byref(count)) or not sessions.value:
            return False
        try:
            array = (_WtsSessionInfo * count.value).from_address(sessions.value)
            # State 0 = WTSActive, 4 = WTSDisconnected; session 0 is the services session.
            return any(s.SessionId != 0 and s.State in (0, 4) for s in array)
        finally:
            _wtsapi32.WTSFreeMemory(sessions)

    def set_keep_awake(self, active: bool) -> None:
        self._keep_awake.set(active)

    def suspend(self, state: SleepState) -> None:
        # SetSuspendState(Hibernate, ForceCritical, DisableWakeEvent). Wake events stay enabled so WoL works.
        # Whether this sleeps rather than hibernates on this hardware is a Phase 1 verification item.
        hibernate = state is SleepState.HIBERNATE
        if not _powrprof.SetSuspendState(hibernate, False, False):
            log.error("SetSuspendState failed (error %d)", ctypes.get_last_error())

    def close(self) -> None:
        self._keep_awake.close()
        self._nvml.close()


# ---- helper (runs in the signed-in user's session) ---------------------------------------------------------
class _LastInputInfo(ctypes.Structure):
    _fields_ = (("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD))


class WindowsHelperProbe:
    def __init__(self) -> None:
        _user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LastInputInfo)]
        _user32.GetLastInputInfo.restype = wintypes.BOOL

    def session_id(self) -> int:
        session = wintypes.DWORD()
        _kernel32.ProcessIdToSessionId(_kernel32.GetCurrentProcessId(), ctypes.byref(session))
        return int(session.value)

    def input_idle_s(self) -> int:
        info = _LastInputInfo()
        info.cbSize = ctypes.sizeof(_LastInputInfo)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return 0  # cannot tell: report "active", the conservative answer
        elapsed_ms = (int(_kernel32.GetTickCount()) - int(info.dwTime)) & 0xFFFFFFFF  # 32-bit tick wraps
        return elapsed_ms // 1000

    def fullscreen_app(self) -> bool:
        state = ctypes.c_int()
        if _shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
        # 2 QUNS_BUSY (full-screen app), 3 QUNS_RUNNING_D3D_FULL_SCREEN, 4 QUNS_PRESENTATION_MODE
        return state.value in (2, 3, 4)

    def gpu_heavy_process_count(self, exclude_process_names: frozenset[str], threshold_pct: int) -> int:
        out = _run_tool(
            [_system_tool("typeperf.exe"), r"\GPU Engine(*)\Utilization Percentage", "-sc", "2", "-si", "1"],
            timeout_s=15.0,
        )
        if not out:
            return 0
        heavy = gpu_heavy_pids(out, float(threshold_pct))
        names = list_processes()
        return len([pid for pid in heavy if names.get(pid, "") not in exclude_process_names])
