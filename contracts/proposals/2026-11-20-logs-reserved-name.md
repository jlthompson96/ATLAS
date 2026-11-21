# Proposal: reserve `logs` under `/var/lib/atlas` for the persistent journal

**Target:** `contracts/services.md` (v1.1.0), section 6. **Bump:** MINOR (one new reserved name). **Author:** base OS work (`deploy/provisioning/base-os.sh`). **Status:** Approved by the owner 2026-11-20, applied in services.md 1.2.0.

## What

Add one entry to the reserved names under `/var/lib/atlas/`:

- `logs`: persistent logs, written only by root. `logs/journal` is journald's store, bind-mounted at `/var/log/journal` (fstab, `bind,nofail,x-systemd.requires-mounts-for=/var/lib/atlas`). It's capped at 256 MB and 30 days (`SystemMaxUse=256M`, `MaxRetentionSec=30day`).

No service gets write access. Services keep logging to stdout/stderr, and journald stores what they print. `ProtectSystem=strict` and each unit's single `StateDirectory=` already keep services out of `logs/`.

## Why

The owner wants ATLAS's logs kept across reboots, in one directory. Until now the plan used `Storage=volatile` (PLAN §2.7), so every reboot lost the logs, including the ones that would explain the reboot. The root filesystem becomes a read-only overlay in step 11, so the journal has to live on the data partition. A bind mount keeps `journalctl` and every tool unchanged.

## Costs and limits

- **Card writes:** a few MB/day (estimate). Entries below CRIT are synced at most every 5 minutes; the ext4 mount has `commit=60`. This counts against the 100 MB/day budget in PLAN §3.5, and B10 measures it.
- **Privacy (T10):** logs are now readable from a stolen card for up to 30 days. The existing rule already covers this: logs never hold private content or secrets (`redact_for_log()`, PT-5).
- **Failure mode:** if the data partition is missing, `nofail` lets the Pi boot with the journal in RAM, as before this change.

## Who is affected

- `deploy/provisioning/base-os.sh` creates the directory and the mount; its `check` verifies them.
- No service unit, event, audit record or allowlist changes.
- The name doesn't collide with any of the 14 service state directories.
