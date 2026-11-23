# ATLAS services, v1

**Schema revision:** 1.5.0 · **Sources:** PLAN.md §0.2, §1.2, §2.9–2.16, §5.1–5.3 · Versioning rules: [README.md](README.md)

This file is the single source for service names, Unix users, listeners, secrets and egress allowlists. nftables rules, systemd units, the egress proxy config and the audit `actor` enum are all generated from or checked against it.

## 1. Services

Each Pi service runs as its own system user, named the same as the service (`atlas-<name>`), with no login shell and no home directory beyond its `StateDirectory=`.

| Service | Unix user | Host | Role | Status |
|---|---|---|---|---|
| `atlas-core` | `atlas-core` | Pi | Orchestrator, policy engine, task queue, state machine, power controller, approval signing | Phase 1 |
| `atlas-voice` | `atlas-voice` | Pi | Wake word, VAD, STT, TTS, AEC routing | Phase 3 |
| `atlas-ui` | `atlas-ui` | Pi | Serves the static HUD and `/ws/events`; TLS; device pairing | Phase 2 |
| `atlas-audit` | `atlas-audit` | Pi | Append-only hash-chained audit log | Phase 0 |
| `atlas-egress` | `atlas-egress` | Pi | **The only process allowed to reach the internet.** CONNECT proxy, research fetch/search API | Phase 0–1 |
| `atlas-google` | `atlas-google` | Pi | Gmail + Calendar **reader** (holds the read grant only). Called `atlas-mail` in older plan text. | Phase 4 |
| `atlas-discord` | `atlas-discord` | Pi | Discord **reader** bot | Phase 4 |
| `atlas-executor` | `atlas-executor` | Pi | Runs approved write actions only; holds the only send/post credentials | Phase 6 |
| `atlas-research` | `atlas-research` | Pi | Research sandbox, `PrivateNetwork=yes`; talks only to `atlas-egress` over a Unix socket | Phase 5 |
| `atlas-search` | `atlas-search` | Pi | SearXNG, private instance | Phase 5 |
| `atlas-panels` | `atlas-panels` | Pi | Weather (Open-Meteo) | Phase 4 |
| `atlas-market` | `atlas-market` | Pi | Quotes, portfolio ledger, encrypted portfolio file | Phase 4 |
| `atlas-fallback` | `atlas-fallback` | Pi | Pi fallback LLM for intent routing (`PiLocalBackend`), separate process so it can be preempted and memory-capped | Phase 1 (**new in C0**) |
| `atlas-kiosk` | `atlas-kiosk` | Pi | Chromium kiosk session (loopback only) | Phase 2 |
| `atlas-backup` | `atlas-backup` | Pi | Nightly encrypted restic backup of config, credentials and service state to a USB drive (ADR 0002). Oneshot unit started by `atlas-backup.timer`; no listener, no network, writes no audit records. Its root helper unit `atlas-backup-mount.service` mounts the allowlisted drive for the run (not a service user) | Phase 0 |
| `atlas-hostd` | Windows service account (`NT SERVICE\atlas-hostd`) | Desktop | Status, sleep, NVML telemetry, keep-awake leases | Phase 1 |
| `atlas-hostd-helper` | the signed-in user | Desktop | Per-user activity reporter, started at sign-in | Phase 1 |

**Reconciled in C0 (review these):**

- **Calendar belongs to `atlas-google`, not `atlas-panels`.** §5.2 lets `atlas-panels` reach the Calendar API, but §2.10 puts `calendar.readonly` in the same OAuth grant as `gmail.readonly`, and one grant means one refresh token. Giving that token to two services would double its exposure. So `atlas-google` fetches both mail and calendar and feeds both panels; `atlas-panels` does weather only.
- **Two Discord bot applications.** One bot token can both read and post, so a single bot breaks the rule that the reader physically cannot send (§2.9, rule 4). Plan: a *reader* bot (Message Content intent, **no** Send Messages permission in any channel) held by `atlas-discord`, and a *poster* bot (Send Messages only in allowlisted channels, no Message Content intent) held by `atlas-executor`. Discord enforces both through role permissions.
- **`atlas-fallback`** is a new service user. The plan names `PiLocalBackend` but not where it runs.

## 2. Listeners and ports

| Listener | Bound to | Who connects | Notes |
|---|---|---|---|
| `atlas-ui` HTTPS + WSS (`/ws/events`) | Pi LAN IP `:443` and `127.0.0.1:443` | LAN browsers, the kiosk | TLS from the ATLAS local CA; device token cookie + Origin check (§4.5). Binds 443 via `AmbientCapabilities=CAP_NET_BIND_SERVICE`. |
| `atlas-core` control socket | `/run/atlas/core.sock` | `atlas-ui`, `atlas-voice`, `atlas-google`, `atlas-discord`, `atlas-panels`, `atlas-market`, `atlas-research`, `atlas-fallback` | Unix socket; peer identity from `SO_PEERCRED`. |
| `atlas-executor` socket | `/run/atlas/executor.sock` | `atlas-core` only | Accepts signed approval records + payloads. |
| `atlas-audit` socket | `/run/atlas/audit.sock` | every Pi service that writes audit records | `actor` is taken from `SO_PEERCRED`, never from the record body. |
| `atlas-egress` CONNECT proxy | one loopback port **per caller** (table below) | the caller named for that port | The port identifies the caller: nftables lets only that caller's `skuid` connect to it (TCP has no `SO_PEERCRED`). Per-caller hostname allowlist; SNI must equal the CONNECT host. |
| `atlas-egress` research API | `/run/atlas/egress-research.sock` | `atlas-research` only | Search and fetch, budgets and provenance enforced here (§2.13). |
| `atlas-fallback` inference | `/run/atlas/fallback.sock` | `atlas-core` only | |
| SearXNG | `127.0.0.1:8888` | `atlas-egress` only (enforced by nftables `skuid`) | JSON format on, limiter off (§2.13). |
| WireGuard `wg0` | UDP `51820` on both hosts | Pi ↔ desktop | Pi `10.77.0.1`, desktop `10.77.0.2`. |
| LM Studio API | `10.77.0.2:1234` | `atlas-core`, from `10.77.0.1` only | API token required (§2.8). |
| `atlas-hostd` | `10.77.0.2:8770` | `atlas-core`, from `10.77.0.1` only | See `hostd.v1.yaml`. |
| `atlas-hostd` helper | desktop `127.0.0.1:8771` | `atlas-hostd-helper` | Loopback only. |
| SSH | Pi `:22` | the home LAN (`LAN_CIDR`); only the admin's key can log in (the development Mac, §7.0) | Key-only; the key has a passphrase. Deploys use this path. |
| Mock event server (dev only) | `127.0.0.1:8765` | HUD dev server | Never deployed. |

**CONNECT proxy ports** (all on `127.0.0.1`, owned by `atlas-egress`):

| Port | Only this user may connect |
|---|---|
| 3131 | `atlas-google` |
| 3132 | `atlas-discord` |
| 3133 | `atlas-executor` |
| 3134 | `atlas-panels` |
| 3135 | `atlas-market` |
| 3136 | `atlas-search` |

Each service gets `HTTPS_PROXY=http://127.0.0.1:<its port>` in its unit, and nftables drops loopback connections to that port from any other `skuid`.

Unix sockets live in `/run/atlas/` (tmpfs), created by systemd socket units with mode `0660`, so only members of the socket's group can connect. Clients get membership per unit with `SupplementaryGroups=`, never with `usermod`.

| Socket | Group | Members |
|---|---|---|
| `core.sock` | `atlas-core-clients` | the `atlas-core` clients listed above |
| `audit.sock` | `atlas-audit-clients` | every Pi service that writes audit records |
| `executor.sock` | `atlas-core` | `atlas-core` only |
| `fallback.sock` | `atlas-core` | `atlas-core` only |
| `egress-research.sock` | `atlas-research` | `atlas-research` only |

A group named `atlas-<consumer>` is used only for a socket with exactly one client, and then contains only that user. Sockets with several clients get an `atlas-<service>-clients` group.

## 3. Internet egress allowlist (per caller)

nftables on the Pi: `output` policy **drop**. Only `meta skuid atlas-egress` may open TCP 443 (and 80 for research) to non-private addresses. Everything else gets loopback, the LAN resolver (DNS) and NTP, the WireGuard peer and the WoL broadcast only (§5.2).

Hostnames each caller may reach **through `atlas-egress`**:

| Caller | Mode | Allowed hosts | Status |
|---|---|---|---|
| `atlas-google` | CONNECT | `gmail.googleapis.com`, `oauth2.googleapis.com`, `www.googleapis.com` (Calendar API v3) | Initial; final set from Phase 4 packet capture |
| `atlas-discord` | CONNECT | `discord.com`, `gateway.discord.gg` | Initial; final set from Phase 4 capture |
| `atlas-executor` | CONNECT | `gmail.googleapis.com`, `oauth2.googleapis.com`, `discord.com` | Phase 6 |
| `atlas-panels` | CONNECT | `api.open-meteo.com` | Fixed URL template (§2.15) |
| `atlas-market` | CONNECT | `api.twelvedata.com` | Provider per D19; switching provider changes this row |
| `atlas-search` | CONNECT | Enabled SearXNG engine hosts only (3–5 engines + Wikipedia) | Chosen in Phase 5 by measured reliability |
| `atlas-research` | research API | Any public host, subject to the denylist, provenance rules, budgets and the private-address block (§2.13) | Phase 5 |
| all other services | — | **none** | |

The OAuth consent flow itself (weekly re-authorization) runs in the user's browser, not through the Pi's egress. Only the code exchange (`oauth2.googleapis.com`) goes out through the proxy.

**Desktop:** LM Studio, llmster, `llama-server` and `atlas-hostd` are blocked outbound by Windows Defender Firewall program rules, except in user-triggered maintenance mode (§5.1).

## 4. Secrets (systemd-creds, one set per service)

Credential names are the `LoadCredentialEncrypted=` IDs. A service can read only its own.

| Service | Credentials |
|---|---|
| `atlas-core` | `lmstudio-api-token`, `approval-signing-key` (Ed25519 private; the public key is in `atlas-executor`'s config) |
| `atlas-ui` | `tls-key`, `device-token-key` (HMAC key for device tokens), `stepup-pin-hash` (Argon2id) |
| `atlas-audit` | `audit-checkpoint-key` (Ed25519 private), `audit-segment-master-key` (wraps per-segment data keys for crypto-shredding) |
| `atlas-google` | `google-oauth-client`, `google-read-refresh-token` (`gmail.readonly` + `calendar.readonly`) |
| `atlas-discord` | `discord-reader-bot-token` |
| `atlas-executor` | `google-oauth-client`, `google-send-refresh-token` (`gmail.send`), `discord-poster-bot-token` |
| `atlas-market` | `twelvedata-api-key`. The portfolio key is **not** a credential: it is derived from the passphrase and held in RAM only (D21). |
| `atlas-backup` | `backup-repo-password` (restic repository password; the owner keeps a paper copy offline) |
| `atlas-egress`, `atlas-voice`, `atlas-research`, `atlas-search`, `atlas-panels`, `atlas-fallback`, `atlas-kiosk` | none |

The WireGuard private key is owned by root (`systemd-networkd`), not by any ATLAS service. The nightly backup runs as `atlas-backup` from a timer and writes only to a USB drive that root (`atlas-backup-mount.service`) mounts at `/mnt/atlas-backup` for the length of the run (ADR 0002). Its repository password is a credential on the Pi plus a paper copy offline; it never enters a Claude Code session.

`atlas-ui`'s certificate is signed by the ATLAS local CA, which is created on the Pi and keeps **no private key**: the CA key signs the one server certificate and is then deleted, so a stolen card can't mint certificates that the owner's devices trust. Renewal makes a new CA and a new certificate, and each device trusts the new CA once. The CA certificate and the server certificate are public and live in `/etc/atlas/tls/` (root-owned, world-readable); only the server key is a credential (`tls-key`).

## 5. Per-unit sandbox baseline

Every Pi unit starts from this baseline and relaxes only what it needs (documented in the unit file):

```ini
DynamicUser=no
User=atlas-<name>
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes            # atlas-voice relaxes this for audio devices
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictNamespaces=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes    # relaxed for services that load ONNX/llama.cpp JITs, if measured necessary
SystemCallFilter=@system-service
SystemCallArchitectures=native
IPAddressDeny=any             # atlas-egress: IPAddressAllow=any; atlas-ui: LAN + loopback; see "Address-family relaxations"
RestrictAddressFamilies=AF_UNIX   # see "Address-family relaxations"
StateDirectory=atlas/<name>   # on the data partition, /var/lib/atlas/<name>
```

`atlas-research` adds `PrivateNetwork=yes`. `atlas-backup` adds `PrivateNetwork=yes` and `ReadWritePaths=/mnt/atlas-backup`; its `StateDirectory` holds only a one-line status file of the last run. `atlas-voice` and `atlas-fallback` set `MemoryMax=` from the §3.1 budgets.

**Address-family relaxations** (the only services that may have IP address families; tests pin this list):

| Service | Adds | `IPAddressAllow=` |
|---|---|---|
| `atlas-egress` | `AF_INET AF_INET6` | `any` |
| `atlas-ui` | `AF_INET AF_INET6` | LAN + loopback |
| `atlas-google`, `atlas-discord`, `atlas-executor`, `atlas-panels`, `atlas-market`, `atlas-search` | `AF_INET` | `localhost` (their `HTTPS_PROXY` port only; nftables enforces the port) |
| `atlas-core` | `AF_INET` | `localhost`, `10.77.0.2` (LM Studio, `atlas-hostd`), the LAN broadcast address (Wake-on-LAN) |
| `atlas-kiosk` | `AF_INET` | `localhost` (`https://127.0.0.1`) |

**Capabilities:** `atlas-audit` gets `CAP_LINUX_IMMUTABLE` (`chattr +a` on the active segment, `+i` when sealed); `atlas-ui` gets `CAP_NET_BIND_SERVICE`; `atlas-backup` gets `CAP_DAC_READ_SEARCH` (read every file it backs up, write none). No other service gets a capability.

## 6. Code location and releases (§7.0, Development environment)

- ATLAS code lives on the data partition as versioned releases: `/var/lib/atlas/releases/<version>/`, with `/var/lib/atlas/current` a symlink to the active one. Deploys don't need the read-only root turned off.
- Releases are owned by `root`, mode `0755`/`0644`, and are read-only to every service (`ProtectSystem=strict` already enforces this; only each service's `StateDirectory` is writable).
- A release holds `backend/`, `contracts/`, `deploy/`, `venv/<service>/` and `hud/`. `hud/` is the built HUD: static files built on the admin device from the committed `frontend/hud` (`npm ci --ignore-scripts`, `npm run build`), served read-only by `atlas-ui` from `/var/lib/atlas/current/hud`. There is no Node on the Pi. `atlas-release` accepts `hud/` like the other entries: regular files and directories only.
- Unit files start code through the symlink (`ExecStart=/var/lib/atlas/current/...`), never through a versioned path.
- A deploy copies a new release over SSH from the admin device, then switches the symlink and restarts units. Rollback switches the symlink back. Keep the last 3 releases.
- Reserved names under `/var/lib/atlas/`; no service may be named after them:
  - `releases`: the versioned releases;
  - `current`: symlink to the active release;
  - `previous`: symlink to the release `current` last replaced, for rollback;
  - `incoming`: upload staging for deploys, owned by the admin account;
  - `credentials`: `LoadCredentialEncrypted=` files, root-only, `credentials/<service>/<name>.cred`;
  - `logs`: persistent logs, written only by root. `logs/journal` is journald's store, bind-mounted at `/var/log/journal` and capped at 256 MB and 30 days (PLAN §2.7). No service gets write access; services log to stdout/stderr.
- Each service's runtime dependencies are installed into its release from a lock resolved for Linux `aarch64`, Python 3.13, with `--require-hashes`.
