# atlas-hostd

The small agent on the Windows desktop (PLAN.md §5.4). It reports status and GPU telemetry, decides whether the
desktop may sleep, and holds keep-awake leases. `atlas-core` on the Pi is its only client. The API is
[`contracts/hostd.v1.yaml`](../contracts/hostd.v1.yaml) (fixed); addresses and ports come from
[`contracts/services.md`](../contracts/services.md).

Status: written and unit-tested on a Mac against a fake platform. **The Windows layer
(`atlas_hostd/platform/windows.py`) has never been run.** Nothing here is verified on real hardware until the owner
completes the checklist below.

## What is in the package

| Module | Role |
|---|---|
| `service.py` | Routes and handlers; glue between platform and decision logic |
| `policy.py` | Sleep decision logic (pure): every `RefusalReason`, wake-claim validation, resume grace |
| `leases.py`, `activity.py` | Lease table (TTL 30 to 1800 s); helper-report tracker (45 s staleness, conservative merge) |
| `http.py`, `server.py` | Minimal asyncio HTTP/1.1 server, source-address check before parsing, size and time limits |
| `validation.py`, `config.py` | Strict request validation mirroring the closed schemas; strict TOML config |
| `platform/base.py` | `Platform` and `HelperProbe` protocols |
| `platform/windows.py` | ctypes: NVML, `SetThreadExecutionState`, `SetSuspendState`, `GetLastInputInfo`, `SHQueryUserNotificationState`, WTS, ToolHelp32; `powercfg`, `wevtutil`, `typeperf` by absolute path |
| `platform/parsers.py` | Pure parsers for those tools' output (tested on the Mac) |
| `platform/fake.py` | `FakePlatform` and `FakeHelperProbe` for tests (controllable clock, idle, GPU, fullscreen, wake source) |
| `helper.py` | `atlas-hostd-helper`: per-user activity reporter |
| `wol.py` | Wake-on-LAN test tool; stdlib only, copyable as a single file |

No runtime dependencies (standard library only). Dev tooling comes from `backend/requirements-dev.lock`.

## Behaviour worth knowing (hostd.v1 1.1.0)

- **Wake claims.** The wake-cycle start is `last_resume_at` if the machine resumed since boot, else `booted_at` (a
  Wake-on-LAN from full shutdown is a boot, not a resume). A claim is accepted only if
  `first_packet_at - 5 s <= cycle start <= first_packet_at + 90 s` (both bounds inclusive) and the cycle's wake source
  is `network` or `unknown`; otherwise `not_atlas_woken`. `power.last_wake_source` describes the cycle start and is
  `unknown` for a cold boot.
- **No usable sleep state.** `power.preferred_sleep_state` is `null` exactly when `sleep_states_available` is empty, and
  `sleep_unsupported` is then a refusal reason.
- **Errors.** 400 `INVALID_BODY`, `BODY_TOO_LARGE`, `LEASE_LIMIT`, `INVALID_LEASE_ID` (malformed `lease_id` on PUT and
  DELETE; DELETE of a well-formed unknown id is 204); 403 `FORBIDDEN_SOURCE`; 404 `NOT_FOUND`; 405
  `METHOD_NOT_ALLOWED`; 500 `INTERNAL_ERROR` (unexpected failure); 503 `NVML_UNAVAILABLE` on `/v1/gpu` only.
- Tests load the contract from `ATLAS_CONTRACTS_DIR` if set, else the repo's `contracts/`.

## Run

```sh
# Windows (Python 3.13+)
python -m atlas_hostd config.toml            # service process, foreground
python -m atlas_hostd.helper config.toml     # per-user helper, in the signed-in session
# Mac, development only: simulated platform
python -m atlas_hostd config.example.toml --fake-platform   # edit listener.address to 127.0.0.1 first
```

Copy `config.example.toml` and edit it; it holds no secrets. The main listener refuses to start on `0.0.0.0` or `::`,
and the helper listener is always `127.0.0.1`.

Dev checks (from `hostd/`, venv built from the shared lock with `--require-hashes`):

```sh
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest
```

`mypy` is configured with `platform = "win32"` so the ctypes code is type-checked on the Mac.

## Desktop inventory (Phase 0)

`scripts/desktop-inventory.ps1` is read-only and needs nothing installed: it runs in the built-in Windows PowerShell 5.1. It records what Phase 0 and the Wake-on-LAN prerequisites need (PLAN A2, A3, A5, §0.4, §2.13):

- the Windows version and display language
- CPU, RAM, motherboard and firmware
- the GPU and its VRAM (`nvidia-smi`)
- disks
- the wired NIC and its wake settings
- the devices allowed to wake the PC
- `powercfg /a`
- Fast Startup, hibernate, Windows Update active hours and wake timers
- Python 3.13, the LM Studio version and WireGuard
- whether the Pi is on the same layer-2 segment (yes/no)

It leaves out the computer and user names, MAC and IP addresses, serials and UUIDs, because the repository is public.

Copy the script to the desktop, open PowerShell **as administrator** (needed for wake timers and Secure Boot; the rest works without), and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop-inventory.ps1 -PiAddress <the Pi's LAN IP> -OutFile inventory.md
```

Read `inventory.md` before sharing it (wake timers can name scheduled tasks), then save it as `docs/inventory/desktop-<date>.md`. `-ExecutionPolicy Bypass` covers this one process; the machine's policy doesn't change. Checked statically by `tests/test_inventory_script.py` (read-only commands, nothing identifying, 5.1 syntax). First real run on the desktop 2026-11-25: [docs/inventory/desktop-2026-11-25.md](../docs/inventory/desktop-2026-11-25.md).

## Desktop firewall for the ATLAS link (Phase 1)

`scripts/desktop-firewall.ps1` creates the Windows Defender Firewall rules from PLAN §5.1/§5.3 in their own `ATLAS` group:
- WireGuard (UDP 51820) from the Pi's LAN address
- LM Studio (1234) and `atlas-hostd` (8770) only from `10.77.0.1` to `10.77.0.2` inside the tunnel
- a block rule on 1234 and 8770 for every other address, which overrides any broad "allow LM Studio" rule

Without `-Apply` it only shows the plan; `-Remove` takes the group away. Steps in [docs/runbooks/wireguard.md](../docs/runbooks/wireguard.md); checked statically only (`tests/test_firewall_script.py`).

## Wake-on-LAN test tool

```sh
python3 hostd/atlas_hostd/wol.py 02:00:00:00:00:01 --broadcast 192.0.2.255                       # send packets only
python3 hostd/atlas_hostd/wol.py 02:00:00:00:00:01 --broadcast 192.0.2.255 \
    --poll http://10.77.0.2:8770 --timeout 60                                                   # time the wake
```

Packets go out at 0, 2, 5, 10 and 20 s (PLAN.md §5.4; `--schedule` overrides) until `/v1/health` answers. It prints
`WAKE_TIME_S=` and the `boot_id`, so comparing it before and after tells a resume (same id) from a reboot (new id).
Run it from the Pi: the health poll only succeeds from `10.77.0.1`. Exit codes: 0 answered, 1 timed out, 2 usage.

## Service hosting: a Phase 1 decision (not made here)

hostd is a plain Python process; how Windows starts and supervises it is open. Options:

| Option | For | Against |
|---|---|---|
| **Service wrapper (WinSW, NSSM or similar) running `python -m atlas_hostd`** | Real service: starts at boot with nobody signed in, restart on failure, `NT SERVICE\atlas-hostd` virtual account (contracts/services.md), clean stop signal | A third-party binary to vet and hash-pin; the wrapper is a dependency of its own |
| **Native service via `pywin32`** | In-process service control, receives power events (resume notifications) directly | Adds a runtime dependency (Windows-only wheel, large); conflicts with "zero dependencies" |
| **Task Scheduler** ("At startup", run whether or not a user is logged on, restart on failure) | No extra software; easy to inspect and remove; can run as a dedicated account | Weaker supervision (restart limits), no `NT SERVICE` virtual account, no service-control stop semantics, log capture is manual |
| Own `ctypes` service shim (`StartServiceCtrlDispatcherW`) | Zero dependencies, real service | We would be writing and maintaining Windows service plumbing; high risk without a Windows test loop |

The helper must run **inside the user's session**, so it is always started at sign-in by Task Scheduler ("At log on of
any user") regardless of the choice above; the same applies to the helper in every option.

Things the chosen account/wrapper must provide, whichever way it goes:

- Python 3.13+ installed (or an embeddable zip) readable by the service account; hostd needs no packages.
- Read access to the System event log (`wevtutil`; the "Event Log Readers" group) for resume and wake-source detection.
- Permission to suspend: `SetSuspendState` may need `SeShutdownPrivilege`, which virtual service accounts may not hold. If sleep
  requests are accepted but the machine never sleeps, check the log line `SetSuspendState failed (error N)`.
- Windows Defender Firewall: inbound TCP 8770 allowed only from `10.77.0.1` on the WireGuard interface; outbound blocked
  for the program (contracts/services.md). The service itself also rejects other sources with 403.

## Windows test checklist (owner, on the desktop)

Everything below is **unverified**. Tick each item and note surprises in the Phase 1 report; failures here mean
`platform/windows.py` or `parsers.py` needs a fix, not the decision logic.

**Setup**
- [ ] `python -m atlas_hostd config.toml` starts with `listener.address` set to the desktop's `wg0` address; `netstat -an` shows 8770 bound to that address only, and 8771 to `127.0.0.1` only.
- [ ] From the Pi: `curl http://10.77.0.2:8770/v1/health` answers in < 50 ms. From the desktop itself (loopback and `10.77.0.2`): 403 `FORBIDDEN_SOURCE`.
- [ ] Firewall rule restricts 8770 to `10.77.0.1`; a request from another LAN host fails at the firewall.

**Platform reads** (compare each with `GET /v1/status` and the Windows tool)
- [ ] `os.name` / `os.build` match `winver` (Windows 11 reports "Windows 11 ...").
- [ ] `power.sleep_states_available` matches `powercfg /a` (S3 listed? Modern Standby? Hibernate?). Note the output on a non-English Windows.
- [ ] `power.booted_at` matches the real boot time (`systeminfo`, Task Manager uptime) within a few seconds.
- [ ] `boot_id` is stable across a hostd restart and across sleep/resume, and changes after a reboot. (Derived from `GetTickCount64`; confirm it keeps counting through sleep and restarts at boot with Fast Startup off.)
- [ ] After a sleep/resume: `power.last_resume_at` matches the Power-Troubleshooter event (`wevtutil qe System /q:"*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and (EventID=1)]]" /c:1 /rd:true /f:xml`). Check the `WakeTime` and wake-source field names match what `parsers.parse_resume_event_xml` expects.
- [ ] `power.last_wake_source`: wake by magic packet gives `network`; by power button `power_button`; by key press `input_device`; by scheduled task `timer`. Record what Windows reports when it cannot tell (`unknown`).
- [ ] `GET /v1/gpu`: values match `nvidia-smi` (VRAM total/used/free, util, temp, driver). Note whether per-process VRAM is reported under WDDM (`llm_server_vram_mb` is 0 if NVML says "not available"; `foreign_vram_mb` is then total used). Provoke throttling if safe and check `throttling`.
- [ ] `llm_server.kind` / `running` / `port_listening` match reality for LM Studio, llmster and `llama-server` (adjust `[llm_server.processes]` names).
- [ ] `activity.user_signed_in` is true while signed in, locked and disconnected; false at the sign-in screen (FI-9).

**Helper** (`python -m atlas_hostd.helper config.toml` in the user session)
- [ ] `GET /v1/activity` shows `helper_connected: true`, and `input_idle_s` tracks keyboard/mouse input (move the mouse: it drops to 0 within about 1 s, an out-of-cycle report).
- [ ] `fullscreen_app` is true for a full-screen game, a borderless-windowed game, a full-screen video and presentation mode; false for a maximised normal window.
- [ ] `gpu_heavy_process_count` rises when a GPU-heavy non-LLM app runs and ignores the LLM server (`typeperf` GPU Engine counters present on this Windows build; the run takes about 2 s).
- [ ] Kill the helper: `helper_connected` goes false after 45 s and `input_idle_s` becomes null.
- [ ] Helper started at sign-in by Task Scheduler runs in the interactive session (`session_id` not 0).

**Leases and keep-awake**
- [ ] `PUT /v1/leases/<id>` makes `powercfg /requests` list a SYSTEM request from python; it disappears after expiry (within about 1 s) and on `DELETE`. No DISPLAY request appears.
- [ ] With a lease held and Windows' sleep timer set short, the PC does not sleep; without a lease it does.

**Sleep (Phase 1 verification, B6)**
- [ ] `POST /v1/sleep` from the Pi (conditions satisfied) returns 202, then the desktop **sleeps, not hibernates and not shuts down** (power LED, `powercfg /a`, Power-Troubleshooter event shows the sleep state). Record S3 vs Modern Standby.
- [ ] Wake-on-LAN from that state with `wol.py --poll`: record the time to `/v1/health` (feeds CT-1..CT-4, B6). Repeat 30 times for the cold-start matrix.
- [ ] After the WoL resume, `last_wake_source` is `network` or `unknown`, and a sleep request with the wake claim is accepted after 2 minutes (not before: `resume_grace`).
- [ ] Cold-boot claim: shut the desktop down, wake it with `wol.py`, and after 2 minutes a sleep request with the Pi's claim is accepted (cycle start is `booted_at`; `last_wake_source` is `unknown` or `network`). Record what Windows reports.
- [ ] OW-1 support: wake the host with the power button or keyboard, then send a sleep request with `wake: null` or with the Pi's claim: refused `not_atlas_woken`. Set `allow_sleep_if_user_woke = true` and it is accepted.
- [ ] FI-9: reboot and stay at the sign-in screen; hostd (as a service) answers `/v1/health` and `/v1/status` with `helper_connected: false`, and a sleep request is judged on lease/GPU/claim only.
- [ ] FI-10: start a GPU-heavy app; `/v1/gpu` `vram_free_mb` drops accordingly and `foreign_gpu_busy` / sleep refusal `foreign_gpu_busy` follow.
- [ ] If sleep is accepted but nothing happens: check the log for `SetSuspendState failed (error N)` and the privilege note above.
