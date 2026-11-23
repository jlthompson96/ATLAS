# Proposal: `atlas-backup`, the nightly restic backup to USB

**Target:** `contracts/services.md` (v1.3.0), sections 1, 4 and 5. **Bump:** MINOR (new service user, credential and relaxations; nothing existing changes meaning). **Author:** Phase 0 backup work, [ADR 0002](../../docs/adr/0002-backup-restic-usb.md). **Status:** Proposed 2026-11-23.

## What

**§1 Services:** add a row.

| Service | Unix user | Host | Role | Status |
|---|---|---|---|---|
| `atlas-backup` | `atlas-backup` | Pi | Nightly encrypted restic backup of config, credentials and service state to a USB drive. Oneshot unit started by `atlas-backup.timer`; no listener, no network | Phase 0 |

**§4 Secrets:** add a row, and replace the paragraph under the table.

| Service | Credentials |
|---|---|
| `atlas-backup` | `backup-repo-password` (restic repository password; the owner keeps a paper copy offline) |

> The WireGuard private key is owned by root (`systemd-networkd`), not by any ATLAS service. The backup runs as `atlas-backup` from a timer and writes only to a USB drive mounted at `/mnt/atlas-backup` for the length of the run (ADR 0002). Its repository password is a credential on the Pi and a paper copy offline; it never enters a Claude Code session.

**§5 Sandbox:** `atlas-backup` adds `PrivateNetwork=yes` (like `atlas-research`), `ReadWritePaths=/mnt/atlas-backup`, and has no `StateDirectory`. Change the capabilities line to:

> **Capabilities:** `atlas-audit` gets `CAP_LINUX_IMMUTABLE` (`chattr +a` on the active segment, `+i` when sealed); `atlas-ui` gets `CAP_NET_BIND_SERVICE`; `atlas-backup` gets `CAP_DAC_READ_SEARCH` (read every file it backs up, write none). No other service gets a capability.

It gets no address-family relaxation, no socket, no egress allowlist entry and no audit `actor` value. It doesn't write audit records in Phase 0. If a later phase wants backup runs audited, that's a separate audit-record proposal.

## Why

PLAN §2.7 requires the nightly encrypted backup, and PT-9 (restore onto the spare card) is a Phase 0 acceptance test. The owner chose restic and USB on 2026-11-23. The current §4 text says "root timer". A dedicated user with only `CAP_DAC_READ_SEARCH` follows the one-user-per-service rule, and confines a compromised restic to writing the backup drive.

## Costs and limits

- **Confidentiality:** `CAP_DAC_READ_SEARCH` reads everything, including every service's state and the encrypted credentials. That's what a backup has to do. `PrivateNetwork=yes` means there's no path out except the USB drive.
- **Card writes:** none beyond journal lines (`--no-cache`, no state directory).
- **Deletion:** data deleted on the Pi stays in backups for the retention period (ADR 0002 §5; about 5 weeks proposed).

## Who is affected

- `deploy/provisioning/lib.sh` / `services.conf` (new user), `provision-pi.sh` (units, mount unit), `base-os.sh` (`restic` package), `store-credential.sh` (new credential name).
- Tests that pin the service list, the capability list and the `PrivateNetwork` services.
- The audit `actor` enum is unaffected: `atlas-backup` never connects to the audit socket.
- No event, `llm`, `power` or HUD contract changes.
