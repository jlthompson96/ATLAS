# Runbook: nightly backup (restic to USB) and restore

Source: [ADR 0002](../adr/0002-backup-restic-usb.md), PLAN §2.7 (R-BAK), contracts/services.md 1.4.0 (`atlas-backup`). Acceptance test: PT-9 (restore onto the spare card).

**What it does.** At 05:30 every night, `atlas-backup.timer` starts `atlas-backup.service`. Its root helper, `atlas-backup-mount.service`, first mounts whichever of the two listed drives is plugged in at `/mnt/atlas-backup`. The backup then runs as `atlas-backup` (read everything, write only the drive, no network). It unlocks stale locks, backs up, forgets old snapshots (14 daily + 4 weekly), and checks the repository; on the 1st of the month it also reads back a quarter of the data. When it finishes, the drive is unmounted. The result is one line in the journal and in `/var/lib/atlas/backup/status`.

**What's in it:** `/etc/atlas`, `/var/lib/atlas/credentials`, every service's state directory under `/var/lib/atlas/` (the audit log today), the host key `/var/lib/systemd/credential.secret`, and `/etc/credstore.encrypted` (the Pi's WireGuard key). **Left out:** releases (rebuilt from git), `logs/` (the journal), the root filesystem (the golden image covers it).

**How long deleted data survives.** Anything deleted on the Pi, whether a "forget", a retention purge or audit crypto-shredding, stays in the backups for up to about 5 weeks on the plugged-in drive, and about 9 weeks counting the offline one.

**The password never goes into a Claude Code session.** Type it only on the Pi's own console (keyboard and screen) or on paper. Not over SSH, not in the shared tmux session.

## One-time setup

You need: two USB drives (any size above 32 GB, HDD or SSD), a maintenance window (overlay off: pi-bringup §11), and the console.

### 1. Install restic without opening the firewall

Root on the Pi has no internet path (provisioning.md, "Maintenance"). Download the package in the ARM64 VM, which runs the same Debian release and architecture, and check it against the Pi's own apt index. That index came from Debian's signed archive.

```sh
# Mac
limactl shell atlas -- bash -c 'cd /tmp && apt-get download restic=0.18.0-1+b4 && sha256sum restic_*_arm64.deb'
limactl copy atlas:/tmp/restic_0.18.0-1+b4_arm64.deb .
scp restic_0.18.0-1+b4_arm64.deb atlas-pi:/tmp/
# Pi: the two hashes must be identical. Name the version: with backports enabled, apt-cache lists more than one.
apt-cache show restic=0.18.0-1+b4 | grep '^SHA256:'
sha256sum /tmp/restic_0.18.0-1+b4_arm64.deb
# Offline: --no-install-recommends, or apt tries to download the recommended packages and fails (no internet for root).
# The file stays in /tmp: apt reads local packages as its unprivileged _apt user, which can't read a home directory.
sudo apt-get install --no-install-recommends /tmp/restic_0.18.0-1+b4_arm64.deb
```

The window rehearsal in the ARM64 VM (2026-11-23) found both: a plain `apt install` failed offline on restic's recommended documentation packages, and a package under `/root` was unreadable to `_apt`.

Checked on 2026-11-23: `3877c6ef8efe052e80a6417b0ac0a672e644fd8bf5ef14e8b803ab6744e43be2` for `0.18.0-1+b4`. The Pi's lists are only as fresh as its last `apt update`, so a newer package in the VM won't match. Use the version the Pi lists (`apt-get download restic=<version>`).

### 2. Format the drives (destroys what's on them)

Plug in **one** drive. Find it, and check the size, so you don't format the wrong disk:

```sh
lsblk -o NAME,SIZE,MODEL,FSTYPE,LABEL      # the USB drive is sda, its partition sda1; mmcblk0 is the card
sudo mkfs.ext4 -L ATLAS-BACKUP-A /dev/sda1   # drive B: -L ATLAS-BACKUP-B
sudo blkid -s UUID -o value /dev/sda1        # note the UUID
```

Repeat with the other drive. A drive without a partition table: create one partition first (`sudo parted /dev/sda mklabel gpt mkpart backup ext4 0% 100%`).

### 3. Create the password (at the console)

```sh
sudo ~/atlas-deploy/provisioning/gen-backup-key.sh
```

It shows 26 characters once. Write them on paper and type them back to confirm; it then stores them as the `atlas-backup` credential. It refuses to run over SSH or inside tmux. Keep the paper offline, away from the drives. Without it, nothing on the drives can be restored. With it, together with a drive, someone gets everything in the backup, including every credential.

### 4. List the drives, enable the timer

```sh
sudo nano /etc/atlas/backup.conf     # one line per drive: "A <uuid>" and "B <uuid>"
sudo ATLAS_ADMIN_USER=... ./provision-pi.sh    # same variables as always (provisioning.md); it now enables the timer
```

`provision-pi.sh` creates a commented template if the file doesn't exist, and never overwrites your copy. The timer is enabled only when the file lists a drive **and** the credential exists.

### 5. First run, then turn the overlay back on

```sh
sudo systemctl start atlas-backup.service        # a few seconds for the first, small backup
journalctl -u atlas-backup-mount -u atlas-backup -n 20 --no-pager
sudo ~/atlas-deploy/provisioning/base-os.sh check  # "backup: ok: last backup ..."
```

Then plug in the other drive instead, run it once more so both drives have a repository, and turn the overlay back on (pi-bringup §11).

## Monthly: swap the drives

Unplug the drive that's in, plug in the other, and put the first one somewhere else (offline; another building is better). The next night's run uses the drive that's plugged in. Never plug both in at once: the run refuses rather than guess.

## Is it working?

- `sudo base-os.sh check` fails if the last run failed or is more than 36 h old.
- `systemctl --failed` shows `atlas-backup.service` or `atlas-backup-mount.service` after a failed night.
- `journalctl -u atlas-backup-mount -u atlas-backup` has one summary line per run. It holds no file names.

| Message | Meaning, and what to do |
|---|---|
| `none of the backup drives ... is plugged in` | Plug in A or B. |
| `2 backup drives are plugged in` | Unplug one. |
| `... is 'vfat', not ext4` / `e2fsck found errors` | Wrong drive, or a damaged filesystem. Repair by hand (`sudo e2fsck /dev/sda1`) or reformat it and let the next run start a new repository. |
| `snapshot incomplete: N file(s) could not be read` | Something under the backed-up paths isn't readable even with `CAP_DAC_READ_SEARCH`. Run `journalctl -u atlas-backup` as root; the paths are deliberately not logged, so reproduce it with the restore commands below plus `restic backup --dry-run -vv`. |
| `restic exit 12` | Wrong password: the credential doesn't match this drive's repository. |
| `already mounted` | A run was killed mid-way. Check nothing is running, then `sudo umount /mnt/atlas-backup`. |

## Restore (PT-9 and the real thing)

Do this at the console. The password is typed into restic's own prompt and never written anywhere.

1. Flash the golden image onto the spare card (or set up a fresh one: pi-bringup steps 1–10, without `gen-audit-keys.sh`), boot it, overlay **off**.
2. Plug in a backup drive (the newest: the one that was plugged in, not the offline one), then mount it **read-only**:

   ```sh
   sudo mount -o ro /dev/sda1 /mnt/atlas-backup
   sudo restic -r /mnt/atlas-backup/restic --no-cache --no-lock snapshots
   ```

   `--no-lock` is required on a read-only mount: without it restic keeps retrying to write a lock file for many minutes (seen in the VM, 2026-11-23).
3. Stop the ATLAS units (`sudo systemctl stop 'atlas-*'`), then restore over the live paths:

   ```sh
   sudo restic -r /mnt/atlas-backup/restic --no-cache --no-lock restore latest --target /
   ```

   This writes `/etc/atlas`, `/var/lib/atlas/credentials`, the state directories and `/var/lib/systemd/credential.secret`. Audit segments come back with their `chattr` flags cleared; the audit service sets `+a`/`+i` again when it starts.

   restic restores owners by **number**. A freshly provisioned card can give the service users different UIDs, so hand every state directory back to its service:

   ```sh
   for s in $(awk '!/^#/ && NF {print $1}' /etc/atlas/services.conf); do
     d="/var/lib/atlas/${s#atlas-}"; [ -d "$d" ] && sudo chown -R "$s:$s" "$d"
   done
   ```
4. `sudo umount /mnt/atlas-backup`, then deploy a release from the Mac (`deploy.sh`), start the units, and turn the overlay back on (pi-bringup §11).
5. Verify against the **off-box** anchors (runbook audit.md): `atlas.audit verify ... --pubkey <your saved checkpoint.pub> --expect-genesis <saved genesis> --expect-head <saved SEQ:HASH>`. **PT-9 passes when the restored Pi verifies against them and the services start with their credentials.**

Restored on a different Pi board: the host key comes from the backup, so the credentials still decrypt. Records written after the last backup are gone. The off-box `--expect-head`, if newer than the backup, then fails, as it should. Record that in the audit runbook's incident notes.

## Changing the password

Only needed if the paper copy is lost or exposed. Do it in one sitting, with the overlay off, at the console, and with both drives at hand. restic repositories can hold several keys, so each drive gets the new one before the old one is removed.

1. Move the old credential aside and create the new password (on paper, as in setup step 3):

   ```sh
   sudo mv /var/lib/atlas/credentials/atlas-backup/backup-repo-password.cred{,.old}
   sudo ~/atlas-deploy/provisioning/gen-backup-key.sh
   ```

2. For each drive: plug in only that one, mount it read-write, add the new key (restic asks for the old password, then the new one twice), remove the old key, and hand the files back to `atlas-backup`:

   ```sh
   sudo mount /dev/sda1 /mnt/atlas-backup
   sudo restic -r /mnt/atlas-backup/restic --no-cache key add
   sudo restic -r /mnt/atlas-backup/restic --no-cache key list          # note the old key's ID (not marked *)
   sudo restic -r /mnt/atlas-backup/restic --no-cache key remove <old-id>
   sudo chown -R atlas-backup:atlas-backup /mnt/atlas-backup/restic
   sudo umount /mnt/atlas-backup
   ```

3. `sudo systemctl start atlas-backup.service` with each drive to confirm, then delete the `.old` credential and destroy the old paper.
