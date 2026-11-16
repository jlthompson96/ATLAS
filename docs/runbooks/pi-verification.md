# Checklist: what must be verified in the ARM64 Debian VM and on the Pi

Everything below was written and statically tested on the Mac (pytest structure tests, `bash -n`, `--dry-run`), but **not executed** on Linux. Tick each item in the ARM64 Debian VM first, then on the Pi, and note the result next to it. Nothing here is a benchmark: B10 and friends only count on the real card.

## Firewall (PT-1)

- [ ] `nft -c -f /etc/atlas/atlas.nft` accepts the ruleset (it was **not** run on the Mac: nftables is not installed). Syntax risks to look at first: `priority filter`, `meta skuid "name"` (users must exist), the `include`, interval sets, `limit rate ... log`.
- [ ] `sudo CANARY_HOST=<public ip> deploy/provisioning/pt1-egress-check.sh` exits 0: no ATLAS user (other than `atlas-egress` on 443) connects out on 53/80/443/22/8080/random; `atlas-egress` cannot reach 53/80/22 or private addresses; a caller cannot reach another caller's proxy port.
- [ ] UDP is not covered by that script (it uses `/dev/tcp`): check UDP/53 and UDP/123 from a non-egress user with `nc -u` or `dig @<LAN_DNS>` (should time out; NTP to `LAN_NTP` is allowed for everyone by design).
- [ ] SSH works from the admin IP only; the HUD port 443 only from the LAN; `nft list ruleset` counters show drops for stray traffic.
- [ ] With IPv6 on the LAN, `atlas-egress` reaches a public IPv6 host on 443 but not `fe80::`/`fc00::`.

## systemd

- [ ] `systemd-analyze verify /etc/systemd/system/atlas-*.service` is clean (templates for missing modules will fail on `ExecStart` only).
- [ ] `systemd-analyze security atlas-audit.service atlas-egress.service` scores are sane; compare with the baseline in services.md section 5.
- [ ] **`StateDirectory=atlas/<name>` ownership**: with the parent `/var/lib/atlas` root-owned, does systemd chown only `atlas/<name>`? Provisioning pre-creates the directories; confirm nothing changes the parent.
- [ ] **`SystemCallFilter=@system-service` allows the `chattr` ioctl** (`FS_IOC_SETFLAGS`) for `atlas-audit`; if not, add the minimal allowance and document it.
- [ ] **`MemoryDenyWriteExecute=yes` with `cryptography`/cffi** on `aarch64` (the audit service imports it). If it fails, relax with a `# RELAXED` comment and a test entry.
- [ ] `CapabilityBoundingSet=CAP_LINUX_IMMUTABLE` + `AmbientCapabilities` works with `NoNewPrivileges=yes` for a non-root `User=`.
- [ ] Socket units: `/run/atlas/*.sock` appear with the right owner, group and mode `0660`; `atlas-audit.socket` activates the service and `LISTEN_FDS` is honoured (`systemctl status atlas-audit`); a service outside `atlas-audit-clients` gets `EACCES`.
- [ ] `LoadCredentialEncrypted` works with files from `store-credential.sh` (`systemd-creds` host-key mode, no TPM); `$CREDENTIALS_DIRECTORY/audit-checkpoint-key` is a PEM the service parses.
- [ ] `ProtectSystem=strict` + `StateDirectory` leaves `/var/lib/atlas/audit` writable and everything else (including `releases/`) read-only for the services.
- [ ] `PrivateTmp`, `ProtectProc=invisible`, `RemoveIPC` do not break the Python runtime.

## Audit on real ext4 (PT-13, FI-3)

- [ ] On start the service `chattr +a`s the active segment (`lsattr` shows `a`), refuses to start if the probe fails, and sealed segments show `i` after rotation. Appends still work with `+a` (O_APPEND); a root-less `echo >> / sed -i` on the segment is refused.
- [ ] Day rollover and size rollover produce sealed segments; `verify` passes across them; `purge_segments` can unseal and unlink (needs the capability).
- [ ] `python -m atlas.audit verify` passes after a normal run, and fails (exit 1) when you edit a record with `chattr -a` first (PT-13 on the real card, with the HUD audit panel when it exists).
- [ ] **FI-3**: run `fi3-audit-load.sh`, cut power 20 times; after each boot the service starts, `verify` passes, torn tails are warnings only, and write-aheads without results are listed as OUTCOME_UNKNOWN. Also `fsck` clean (that part is the filesystem, not ours).
- [ ] Measure the audit write volume for the B10 budget (24 h): batching should keep it far below 20 MB/day.

## Egress proxy

- [ ] From each caller user, `curl --proxy http://127.0.0.1:<port> https://<allowed host>/` works against the real host, and a host on another caller's list is refused.
- [ ] Real clients' ClientHellos are accepted (Python `ssl`/`requests`, `discord.py`'s aiohttp, Google API clients): none sends a fragmented hello or `ech` that changes the SNI. If one does, the proxy refuses it (fail closed); decide with the owner.
- [ ] `getaddrinfo` through the LAN resolver returns public addresses for each allowlisted host; the proxy's private-address block does not reject a legitimate answer (split-horizon DNS would).
- [ ] 100 concurrent connections: per-caller limit (16) and global limit (64) behave; idle timeout 10 min closes dead tunnels; Discord's gateway stays up over a long run.
- [ ] Audit records appear in the audit log for allowed, denied and unreachable cases, and **stopping `atlas-audit` stops new tunnels** (fail closed).
- [ ] Run the audit and egress test suites under **Python 3.13** (the target); on the Mac they ran under 3.15, so a 3.13-only difference (for example in `asyncio`, `ipaddress.is_global`) would not have shown.

## Provisioning and deploy

- [ ] `provision-pi.sh` twice in a row: the second run changes nothing. Real `useradd`, `groupadd`, `install`, `visudo -cf`, `systemd-tmpfiles`.
- [ ] `shellcheck deploy/provisioning/*.sh` is clean (shellcheck is not installed on the Mac; the structure test skips itself there).
- [ ] `deploy.sh` end to end against the VM: upload, install, activate, health check, deliberate failure (break a unit) triggers automatic rollback, `rollback`, `prune` keeps 3. `python3 -m venv` and offline `pip install --require-hashes` from the shipped wheels.
- [ ] Re-lock inside the VM: `backend/scripts/relock-pi.sh audit cryptography==47.0.0 cffi==2.1.1 pycparser==3.12` yields identical hashes to `backend/requirements-audit.lock`.
- [ ] `exposure-inventory.sh` output matches the PLAN 2.7 table (PT-11).
- [ ] A deploy's write volume (for B10).
