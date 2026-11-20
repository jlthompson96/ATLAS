# Checklist: what must be verified in the ARM64 Debian VM and on the Pi

Everything below was written and statically tested on the Mac (pytest structure tests, `bash -n`, `--dry-run`), but **not executed** on Linux. Tick each item in the ARM64 Debian VM first ([vm.md](vm.md)), then on the Pi, and note the result next to it. Nothing here is a benchmark: B10 and friends only count on the real card.

## Firewall (PT-1)

- [ ] `nft -c -f /etc/atlas/atlas.nft` accepts the ruleset (it was **not** run on the Mac: nftables is not installed). **VM 2026-11-20: OK** with `site.nft.example` and the ATLAS users created (docs/runbooks/vm.md); Pi still to do. Syntax risks to look at first (each was written from documentation, never loaded): `priority filter`; quoted `skuid` names (`meta skuid "atlas-egress"`, `meta skuid != "atlas-egress"`; the users must exist, and `$NTP_USER` is a quoted string defined in the site file); variables inside anonymous sets (`ip daddr { $LAN_BROADCAST, 255.255.255.255 }`); `limit rate 5/minute log prefix ...`; `meta mark $WG_FWMARK` (hex define); `meta l4proto { tcp, udp } th dport 53`; the `include` of the site file; interval sets; `icmpv6 type nd-router-advert ip6 saddr $LAN_ROUTER6`.
- [ ] **Hardening added after review (F7), check each really blocks:** a non-egress user cannot query a resolver on `127.0.0.53:53`; a process of another user that binds a proxy port (3131-3136) cannot answer its clients (its replies are dropped by the `tcp sport` rule); with `FirewallMark=0x5182` in the `[WireGuard]` section of `wg0.netdev` (systemd-networkd; `FwMark` is wg-quick syntax and is not used there, known issue D1) the tunnel still comes up while a user process sending UDP to `$DESKTOP_LAN_IP:51820` is dropped; NTP works for `systemd-timesync` only; IPv6 router advertisements are accepted from `$LAN_ROUTER6` only; with a global IPv6 LAN prefix set as `LAN_PREFIX6`, `atlas-egress` cannot reach a host inside it.
- [ ] Re-provisioning with a changed ruleset validates it before replacing the old one, keeps `/etc/atlas/atlas.nft.prev`, and `systemctl reload nftables` applies it without opening a gap.
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
- [ ] **`MemoryDenyWriteExecute` measurements (services.md allows relaxing it only once measured necessary).** Every unit sets `yes`. Four are expected to need a relaxation and carry a `TODO(VM): measure` comment: `atlas-voice` (ONNX Runtime), `atlas-fallback` (llama.cpp), `atlas-search` (CPython + lxml), `atlas-kiosk` (Chromium JIT). Run each under `yes`; relax in the unit with a `# RELAXED MemoryDenyWriteExecute` comment, add it to `ALLOWED_RELAXATIONS` in `backend/tests/test_deploy_structure.py`, and record the measurement.
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
- [ ] Real clients' ClientHellos are accepted (Python `ssl`/`requests`, `discord.py`'s aiohttp, Google API clients): none sends a fragmented hello. **ECH:** the proxy now refuses any hello carrying `encrypted_client_hello` (0xfe0d), its draft codepoints 0xfe08-0xfe0c, legacy ESNI 0xffce or `ech_outer_extensions` 0xfd00 (audit: `block_reason sni_mismatch`, `result.code ECH_REFUSED`), and any duplicate extension (`DUPLICATE_EXTENSION`). Confirm that none of the real clients above sends ECH by default (Chromium does when it finds an HTTPS DNS record; the Python clients do not); if one does, disable ECH in that client rather than relaxing the proxy.
- [ ] **Residual egress risks, accepted and documented (not fixed in code):** (1) *domain fronting*: SNI equals the CONNECT host, but the encrypted HTTP `Host` header is invisible; on a shared front-end (Google's, Cloudflare's) an allowed host can serve other tenants. The allowlist hosts are the mitigation, and the caller can only send what its own code sends. (2) *HelloRetryRequest*: the proxy inspects the first ClientHello only; a second one after an HRR is not re-checked (it must repeat the same SNI per RFC 8446, and a client that changes it only reaches the pinned, already-allowed address). (3) A caller can still encode data in what it sends to an allowed host; that is what the per-caller allowlist, approvals and the audit trail are for.
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
- [ ] **`atlas-release install` hardening (review F2), none of which could run on the Mac:** (a) GNU `tar -tvf` marks symlinks `l` and hard links `h`: confirm both are refused (the Mac test used bsdtar), and that setuid files in a tarball come out without the bit (`--no-same-permissions`, then `find -perm /6000` must be empty). (b) The `systemd-run -p DynamicUser=yes -p StateDirectory=atlas-build/<name> -p PrivateNetwork=yes ...` build works as written: `$STATE_DIRECTORY` is set, `/var/lib/atlas-build` resolves to `/var/lib/private/atlas-build`, the venv is created with `python3 -I -m venv` and pip installs the shipped wheels `--require-hashes --no-index`, and `cp -a --no-preserve=ownership` into the release plus `chown -R root:root` yields a venv that runs as the service user (`pyvenv.cfg` and `bin/python` are relocatable; console-script shebangs are not used). (c) Reproduce the reviewer's attack: a tarball with `venv/.../evil.pth` or an `--index-url` line in a lock is refused and nothing runs as root. (d) `.partial` and the `.in.*` input directory are removed on every failure path.
- [ ] **Audit hardening (review F1, F4, F5, F9) on the real card:** a purge writes the purge record, then a signed checkpoint, then unlinks (the `chattr -i/-a` calls need the capability); the service restarts cleanly on a purged chain (full `verify` at start) and refuses a forged one; after an injected I/O error (full disk, `chattr +i` on the active segment) the service exits non-zero and systemd restarts it; the free-space floor (default 200 MB) refuses client records with `NO_SPACE` while the checkpointing still works; per-actor limits (8 connections, 5 appends/s, burst 50) do not trip under honest load (check egress and executor in a busy session); steady-state audit writes against the 100 MB/day budget (see the audit runbook for the estimate).
