# ADR 0002: Nightly backup with restic to USB

- **Status:** Accepted 2026-11-23 (owner: restic, USB, yes to the three open questions). Implemented and tested end to end in the ARM64 VM; not yet installed on the Pi.
- **Date:** 2026-11-23
- **Decision record:** R-BAK (PLAN §2.7, "Backups"), threat T10, acceptance test PT-9.
- **Contract change:** [contracts/proposals/2026-11-23-atlas-backup.md](../../contracts/proposals/2026-11-23-atlas-backup.md) (services.md 1.4.0).
- **Code:** `backend/atlas/backup/` (the runner), `deploy/systemd/atlas-backup.{service,timer}`, `deploy/systemd/atlas-backup-mount.service`, `deploy/provisioning/atlas-backup-mount.sh`, `deploy/provisioning/gen-backup-key.sh`. Runbook: [backup.md](../runbooks/backup.md).

## Context

PLAN §2.7 requires a nightly (05:30), encrypted, deduplicated backup of config, encrypted credentials, the memory DB, the task queue, audit segments and checkpoints. It left the tool (restic or borg) and the target (a share on the desktop over `wg0`, or a USB drive on the Pi) open. Encryption is mandatory because the card isn't encrypted (D5, D17): once data leaves the Pi, the backup key is its only protection at rest. PT-9 (restore onto the spare card) is a Phase 0 acceptance test.

The owner chose **restic** and a **USB drive** on 2026-11-23. The `wg0` target depends on Phase 1 and would need a firewall change.

**restic on the Pi:** Debian trixie main ships `restic 0.18.0-1+b4` for arm64 (checked on the Pi 2026-11-23 with `apt-cache policy`). It's a single Go binary that depends only on `libc6`, and the Debian Go Packaging Team maintains it. Licence BSD-2-Clause. It encrypts with AES-256-CTR plus Poly1305-AES, and derives the key from the password with scrypt. This closes the "restic/borg ARM64 status" item in PLAN's list of unverified facts.

## Decision

### 1. Tool and installation

- restic from Debian's archive (`apt install restic`), not a downloaded binary. apt verifies Debian's archive signature, and `base-os.sh` adds `restic` to its package list and `check`.
- Root has no internet path after provisioning, so installing it is a maintenance-window step (provisioning.md, "Maintenance"). It isn't a Python dependency, so no lockfile is involved.

### 2. Target: two USB drives, one plugged in, one offline

- Two drives, `ATLAS-BACKUP-A` and `ATLAS-BACKUP-B` (ext4 labels). Each holds its own restic repository, and both use the same repository password.
- `/etc/atlas/backup.conf` lists the two filesystem UUIDs. The job backs up to whichever listed drive is plugged in. Any other drive, or none, fails the run loudly.
- **Rotation:** once a month the owner swaps the plugged drive for the offline one (PLAN: "a second, rotated USB copy is kept offline"). The offline copy is at most about a month old, and a drive that dies loses at most one month of history.
- The drive is mounted only while the job runs, by the root helper unit `atlas-backup-mount.service` (`/mnt/atlas-backup`, `nosuid,nodev,noexec,noatime`, `e2fsck -p` first, `StopWhenUnneeded=yes`). It doesn't sit in fstab mounted all day.
- **Why a helper unit (found in the VM).** An `ExecStartPre=+` mount in the backup unit runs inside that unit's own mount namespace on systemd 257: neither the sandboxed process nor the host sees it. The helper unit therefore sets no mount-namespacing option at all. That includes `PrivateNetwork=`, which on systemd 257 also creates a mount namespace.

### 3. Who runs it: its own user, read-only everywhere else

- `atlas-backup.service` (oneshot) runs as a new system user `atlas-backup`, started by `atlas-backup.timer` (05:30, `Persistent=true`).
- It gets **`CAP_DAC_READ_SEARCH` only**, which lets it read everything, and it can write only to `/mnt/atlas-backup`: the `services.md` §5 baseline plus `ReadWritePaths=/mnt/atlas-backup`.
- `PrivateNetwork=yes`, `IPAddressDeny=any`, `RestrictAddressFamilies=AF_UNIX`: a USB target needs no network.
- `--no-cache`, and a `StateDirectory` holding only a one-line JSON status of the last run (about 200 bytes a night), so `base-os.sh check` can fail when the last success is older than 36 h. systemd's own record of the last run is on the overlay root and is lost at every reboot.
- This replaces the "root timer" wording in services.md §4. A dedicated user follows the naming rules, and even if restic were compromised it could only write to the backup drive.

### 4. What is backed up

| Included | Why |
|---|---|
| `/etc/atlas/` | Service config, the site firewall file, `backup.conf` |
| `/var/lib/atlas/credentials/` | `LoadCredentialEncrypted` files, still encrypted with the host key |
| `/var/lib/atlas/<service>/` for every service | State: audit segments and `checkpoints.jsonl` now; memory DB, queue and portfolio file later |
| `/var/lib/systemd/credential.secret` | **Open question 1.** The host key that decrypts every credential above |

| Excluded | Why |
|---|---|
| `releases/`, `current`, `previous`, `incoming` | Rebuilt from git by `deploy.sh` |
| `logs/` (the journal) | Diagnostic only, up to 256 MB a night of churn, and it would keep logs past their 30 days |
| The root filesystem | The golden image covers it; under the overlay it holds no state |

**Consistency.** In Phase 0 the only state is the audit log, which is append-only. restic reads each file once, so a backup taken during a write ends at a torn tail, which the verifier already treats as a warning. Services that add SQLite databases later (memory, queue) must give the backup a consistent copy, for example a `sqlite3 .backup` snapshot in their own state directory before 05:30. That requirement goes to each of those phases, and this ADR doesn't solve it now.

### 5. Retention bounds how long deleted data survives

restic keeps deleted files until their snapshots are forgotten and pruned. **Backups extend every deletion**: "forget X", the 30-day memory retention, the 7-day transcript retention, and audit crypto-shredding all stay recoverable from a backup until its retention has passed.

- **Proposed:** `forget --keep-daily 14 --keep-weekly 4 --prune` after each run. The plugged drive then holds nothing older than about 5 weeks. The offline drive can be one rotation older, so about 9 weeks at worst. **Open question 2.**
- The runbook and PLAN §2.7 will state that bound, and PT-12 will check it.

### 6. Integrity and failure

- After each backup: `restic check` (repository structure). On the 1st of the month: `restic check --read-data-subset=1/4`, which reads a quarter of the data back.
- Exit status and a summary line (snapshot ID, files, bytes added, duration) go to the journal. restic's per-file errors are counted, never printed, because paths under the state directories can carry private names. A snapshot with any unreadable file counts as a failure, and old snapshots aren't pruned after it.
- A failed or missing nightly run shows as a failed unit (`systemctl --failed`, `base-os.sh check`). Showing it on the HUD needs an alert code. That's a Phase 1 contract proposal, not this one.

### 7. The repository password

- 32 random bytes, base64-encoded, generated on the Pi by `gen-backup-key.sh`. It's stored as the `atlas-backup` credential `backup-repo-password` (systemd-creds, host key) and shown **once** so the owner can write it down.
- **It must never pass through a Claude Code session.** The owner runs the script on the physical console (tty1), not in the shared tmux session: the pane is readable from SSH. The script refuses to print the key unless stdin and stdout are a local VT (`/dev/tty[0-9]*`).
- Stored this way, anyone holding the card can decrypt the password with the host key, just as they can read the data itself. The password protects the drives when they're away from the Pi: the offline drive, or a drive taken without the Pi.

### 8. Restore (PT-9)

`docs/runbooks/backup.md` covers it. Flash the golden image onto the spare card, run steps 7–11 of pi-bringup (layout, base, provisioning), plug in a backup drive, and `restic restore latest --target /` with the paper password. Then `deploy.sh` (code comes from git), start the units, and run `atlas.audit verify` against the off-box anchors. PT-9 passes when the restored Pi verifies against those anchors and its credentials decrypt.

## Questions settled by the owner (2026-11-23)

1. **Back up the host key (`/var/lib/systemd/credential.secret`)?** **Yes.** With it, a restore is complete: every credential decrypts and the audit checkpoint key keeps signing the same chain. Without it, every credential has to be stored again after a restore (Google re-consent, Discord tokens, a new audit key with a `credential_rotated` record). The cost: a stolen backup drive plus the paper password then opens every credential, not just the data.
2. **Retention of 14 daily + 4 weekly** (about 5 weeks on the plugged drive, about 9 weeks worst case with the offline one)? **Yes**, because it keeps backups close to the 30-day memory retention. A longer history (for example 12 monthly snapshots) would keep deleted data for a year.
3. **Hardware:** two USB drives. Any size above 32 GB is plenty for years of state; HDD or SSD. **The owner has them.**

## Tested in the ARM64 VM (2026-11-23)

Two loop-device "drives" (ext4, listed by UUID), the real units and helper, restic 0.18.0 from trixie, a mock password:

- First run initialises drive A and backs up; the second run is incremental (1 new file, 3.6 KB added).
- A root-only restore is byte-identical for the audit files and the host key. Credentials are included; `logs/` and `releases/` aren't.
- No drive plugged in, or both: the helper refuses before restic runs, and the status keeps the last success.
- Drive B alone gets its own new repository.
- The drive is unmounted when the run ends.
- `systemd-analyze security atlas-backup.service`: 0.8 SAFE. `systemd-analyze verify` and shellcheck are clean.
- **Found:** restic on a read-only mount retries writing its lock file for many minutes, so restores use `--no-lock` (runbook).
- **Found:** a different board or card can number the service users differently, so restore hands every state directory back to its service (runbook).

## Consequences

- One new service user and its two units, a credential, and a mount point (services.md 1.4.0).
- The backup takes nothing from the 100 MB/day card budget: it only reads the card, and `--no-cache` keeps restic's cache off it.
- Deleted data stays recoverable for the retention period; the runbook and PLAN §2.7 state the bound.
- The owner has a monthly chore (swap the drives) and a paper secret to keep safe.
