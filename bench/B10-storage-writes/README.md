# B10: storage writes

PLAN §3.5 sets a hard budget of **≤ 100 MB/day of host writes to the microSD card**, and Phase 0 exits only when a 24 h measurement on the real Pi is inside it (PLAN Phase 0, exit criteria). B10 measures that from `/proc/diskstats`.

## Run

On the Pi, as the admin user (no root):

```sh
scp bench/B10-storage-writes/sample.sh atlas-pi:/tmp/b10-sample.sh
ssh atlas-pi 'setsid nohup sh /tmp/b10-sample.sh --out /tmp/b10-run.log >/tmp/b10-run.err 2>&1 </dev/null &'
```

It samples every 600 s for 24 h, then writes an `# end` line. Afterwards, on the Mac:

```sh
scp atlas-pi:/tmp/b10-run.log "$TMPDIR/"
python3 bench/B10-storage-writes/report.py "$TMPDIR/b10-run.log"   # paste into bench/results/B10-<date>.md
```

## Rules for a valid run

- Samples go to `/tmp` (tmpfs), so measuring writes nothing to the card. Running `scp` and `ssh` during the run is fine: an SSH login adds a few journal lines.
- **No deploy, provisioning or maintenance window during the run.** One release is about 15 MB of tarball, and more once unpacked into venvs. Deploy volume is a separate number (pi-verification.md, "A deploy's write volume").
- A reboot clears `/tmp` and resets the counters, and the report refuses a run whose counters go down. Start again.
- Record the configuration from the run header: overlay on or off, the release, and which `atlas-*` units were active. The result is valid only for that configuration. Rerun when a phase adds services that write (core, memory DB, queue).

## What the numbers mean

- MB is 10^6 bytes. `/proc/diskstats` counts 512-byte sectors whatever the device's sector size.
- These are **host writes**: what the kernel sent to the card. The card's internal write amplification can't be seen from the host. This is why the budget is a design target, not an endurance figure.
- `mmcblk0` is the whole card: `p1` boot, `p2` root (read-only under the overlay, so it should be near zero), `p3` data (the audit log, the journal, releases).
- The journal line is `du` of `/var/log/journal` (readable through group `adm`). It shows the journal's share. The audit directory is root-only, so its share needs root: `sudo du -sk /var/lib/atlas/audit` at the start and end.
