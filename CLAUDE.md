# ATLAS: conventions for every agent

ATLAS is a single-user, privacy-first voice assistant with a HUD. A Raspberry Pi 5 is always on and holds all secrets; a Windows desktop runs the LLM and is woken on demand. **The plan is [docs/PLAN.md](docs/PLAN.md).** Read §0.5 (decisions), §7.0 (how work is split) and the sections your task cites before you change anything. Don't re-open decisions recorded in §0.5 or §10.

## Ground rules

- **Stay inside this repository.** Don't read, reference or copy from other projects on this machine.
- **Contracts are fixed.** `contracts/` and the two interface files (`backend/atlas/llm/interface.py`, `backend/atlas/power/interface.py`) are versioned contracts. Never edit them in a feature branch. To change one, write a proposal in `contracts/proposals/` (process in [contracts/README.md](contracts/README.md)).
- **One agent per git worktree and branch.** Merges need the owner's approval, and the reviewer agent's too for security-critical code (policy engine, executor, egress gateway, portfolio encryption).
- **Run the tests for your area before handing work back** (§8 of the plan lists the acceptance tests per phase).
- **Commit only when asked.** Commit messages are imperative and scoped: `hud: add orb lite renderer`, `contracts: bump events to 1.1.0`.

## No secrets in the repo

- Never commit tokens, keys, passphrases, OAuth client secrets, WireGuard keys, `.env` files, real email/Discord/calendar content, or real portfolio data. `.gitignore` blocks the common file types; that is a backstop, not permission.
- Use obviously fake values in examples and tests (`example.com`, `example.org`; the mock PIN `0000` and passphrase `atlas-mock`).
- Fidelity CSV fixtures (Phase 4) are **redacted** copies only: no account numbers, no real quantities.
- Never log secrets or private content. Log IDs, sizes, hashes and timings. Fields marked `x-atlas-sensitive` in `events.v1` go through `redact_for_log()`.
- Real secrets live only in systemd-creds on the Pi (contracts/services.md §4).
- **Claude Code sessions go to a cloud service.** Never paste real emails, Discord messages, calendar data, tokens or portfolio data into a session. Fidelity fixtures are redacted *before* they enter the repo or a session.

## Where things run (PLAN.md §7.0, Development environment)

| What | Where it's tested |
|---|---|
| Python backend, contracts, policy engine, portfolio ledger | Natively on this Mac |
| Pi-specific behaviour (systemd units and sandboxing, nftables, `systemd-creds`, `chattr +a`, PipeWire) | ARM64 Debian VM or container on the Mac; final check on the Pi |
| HUD | On the Mac against the mock server; kiosk performance only on the Pi (B8) |
| `atlas-hostd` | Written against a mocked platform layer (power, NVML, idle time); real tests only on the Windows desktop |
| Benchmarks B1–B12 | Real hardware only. Numbers measured on the Mac don't count. |

Pi deploys are versioned releases under `/var/lib/atlas/releases/` with a `current` symlink (contracts/services.md §6). Each deploy costs microSD writes (§3.5).

## Naming

| Thing | Convention |
|---|---|
| Services, Unix users, systemd units | `atlas-<name>` (user and service share the name); unit file `deploy/systemd/atlas-<name>.service` |
| Python package / modules | `atlas` (`backend/atlas/<area>/`), `atlas_hostd` (`hostd/atlas_hostd/`); snake_case |
| Frontend package | `@atlas/hud` in `frontend/hud/` |
| Event types | `noun.verb` (`state.changed`, `action.pending`) |
| Codes (alerts, errors, results) | `UPPER_SNAKE` (catalogue in contracts/README.md) |
| Audit resource IDs | `<namespace>:<path>` (`gmail:msg/…`, `web:src/42`) |
| Benchmarks | `bench/B<n>-<slug>/`; results in `bench/results/B<n>-<YYYY-MM-DD>.md` |
| ADRs | `docs/adr/NNNN-<slug>.md`, one per R# decision |

Service names, users, ports, sockets and allowlists come **only** from [contracts/services.md](contracts/services.md).

## Backend (Python 3.13 on the Pi; 3.13+ on the dev machine)

```sh
cd backend
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.lock   # first time, or after the lock changes

.venv/bin/ruff check .            # lint
.venv/bin/ruff format --check .   # formatting (use `ruff format .` to fix)
.venv/bin/mypy                    # strict type check of the atlas package
.venv/bin/pytest                  # tests (includes contract tests)
```

Run all four before handing work back. The package is used in place (`pytest` sets `pythonpath`; run modules from `backend/` with `python -m`), so there's no editable install.

Style: type hints everywhere (mypy strict), `from __future__ import annotations`, frozen dataclasses for value types, asyncio for I/O. Security-relevant rules live in code, never only in prompts (§2.9).

## Mock event server (HUD development)

```sh
cd backend
.venv/bin/python -m atlas.ui_server.mock --list                         # scenarios
.venv/bin/python -m atlas.ui_server.mock                                # interactive (default) on ws://127.0.0.1:8765/ws/events
.venv/bin/python -m atlas.ui_server.mock --scenario all --loop          # replay every scenario
.venv/bin/python -m atlas.ui_server.mock --scenario wake_timeout --speed 4
.venv/bin/python -m atlas.ui_server.mock --device kiosk                 # kiosk permissions (no approve/unlock)
.venv/bin/python -m atlas.ui_server.mock --validate                     # check all scenarios against events.v1
```

Mock credentials: step-up PIN `0000`, portfolio passphrase `atlas-mock`. Every frame the mock sends is validated against `events.v1` and the state machine, so if the HUD and the mock disagree, the HUD is wrong or the contract needs a proposal. Scenarios live in `backend/atlas/ui_server/mock/scenarios/` (format in `scenario.py`). Adding scenarios is fine; they don't change the contract.

## Frontend (`frontend/hud`, Svelte + TypeScript + Vite)

Not scaffolded yet (agent A). Requirements:

- `package-lock.json` committed; install with `npm ci --ignore-scripts` only.
- Scripts that must exist: `npm run lint`, `npm run check` (svelte-check / tsc), `npm test`, `npm run build`.
- All assets local, no CDN, CSP `default-src 'self'`; self-hosted OFL fonts.
- Types for events come from `contracts/events.v1.schema.json` (generated, not hand-copied).
- The Vite dev server proxies `/ws/events` to the mock at `127.0.0.1:8765`.
- Built on the Mac into static files and shipped as-is. There is no Node on the Pi.

## Dependencies and lockfiles (threat T6)

- **Every dependency is hash-pinned.**
  - Python: `backend/requirements-dev.lock` (pip-tools `--generate-hashes`), installed with `--require-hashes`.
  - npm: `package-lock.json` integrity hashes, `npm ci --ignore-scripts`.
  - Models: SHA-256 recorded in config, GGUF/ONNX/safetensors only, never pickle.
- **Adding or upgrading a dependency is its own reviewed change.** Edit `backend/pyproject.toml`, run `backend/scripts/relock.sh`, and explain the new package (why, maintainer, licence, transitive deps) in the handoff. Prefer the standard library. Minimal dependencies is a security control here.
- `backend/pyproject.toml` runtime `dependencies` is empty on purpose. The `mock` and `dev` extras are dev-machine only and never installed on the Pi.
- `requirements-dev.lock` is resolved for this Mac and is **never shipped**. Locks for what ships resolve for the target, with hashes:
  - Pi runtime locks (one per service, arriving with each service): Linux `aarch64`, Python 3.13. Generate them inside the ARM64 Debian VM, not on macOS.
  - `atlas-hostd` lock: Windows `x86_64`.
- Development tooling stays pinned and separate from shipped dependencies. The Mac builds everything the Pi runs, so a bad dev dependency is a supply-chain risk.

## Layout

```text
contracts/      versioned interfaces (+ examples/, proposals/, CHANGELOG.md)
docs/           PLAN.md, adr/, runbooks/, threat-model/
backend/atlas/  core/ voice/ llm/ power/ integrations/ research/ egress/ audit/ memory/ ui_server/ policy/
backend/tests/  unit + contract tests
hostd/          atlas_hostd: Windows service + per-user helper (agent B)
frontend/hud/   @atlas/hud (agent A)
deploy/         systemd/, nftables/, wireguard/, provisioning/, kiosk/
bench/          B1–B12 harnesses; results/
tests/          system-level: privacy/ redteam/ failure/ latency/ ui-perf/
```
