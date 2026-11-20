# Known issues

Open problems in code already on `main`. Each has an ID, what it blocks, and the fix agreed so far.

**Read this before working on `backend/atlas/audit/`, `backend/atlas/egress/` or `deploy/`.** Close an issue by fixing it on a branch, with a regression test, and moving its row to "Closed" with the commit.

## Open

### From the security review of agent C's Pi foundations (merged 2026-11-16, `b6a90cc`)

The owner merged C's branch at `68d3945` before the second fix round. The reviewer's second pass found the original bypasses closed (F1–F4, F6, F9) but a set of regressions, all reproduced. The ones that blocked Pi provisioning (N1, N3, N4, N5, G1, D2, D3, S1) are fixed on branch `worktree-agent-acde43c00fdfbd144` (see "Closed"), together with N2; the rows below remain.

| ID | Severity | Blocks | Where | Problem | Agreed fix |
|---|---|---|---|---|---|
| N6 | Low | — | `backend/atlas/audit/log.py` open; `backend/atlas/audit/verify.py` | Starting on a purged chain runs a full in-memory `verify_dir`. At 1–20 MB/day for 365 days that is slow, and may not fit in memory at the top of the range. Startup time is egress downtime. | Make the verifier stream records. |
| D1 | Medium | Phase 1 (WireGuard) | `docs/runbooks/` WireGuard and nftables notes | The runbook said `FwMark = 0x5182`, which is wg-quick syntax. services.md puts WireGuard under `systemd-networkd`, where the key is `FirewallMark=`. With the wrong key the nftables mark rule never matches, and the tunnel is silently blocked. **Docs fixed in `5d09422`** (runbook, site example, checklist; test `test_d1_the_wireguard_mark_is_documented_with_the_networkd_key`). | Use `FirewallMark=0x5182` in the `.netdev` file, and check it in the VM. **Still to do:** the VM check (it needs a `wg0` netdev and a loaded ruleset, which would reconfigure the VM's networking). |
| D4 | Low | — | `deploy/nftables/atlas.nft` | A process that squats a proxy port (3131–3136) while `atlas-egress` is down can still stop it from binding: a denial of service, with no data exposure. `LAN_ROUTER6` is a spoofable link-local source, so it adds little. | Socket-activate the proxy listeners from systemd. |

### From the first VM run (provisioning, deploy and services in the ARM64 VM, 2026-11-20)

Found by provisioning a disposable copy of the VM (`atlas-test`) with `provision-pi.sh`, deploying with `deploy.sh`, and running the services. Details: [pi-verification.md](runbooks/pi-verification.md#vm-run-2026-11-20).

| ID | Severity | Blocks | Where | Problem | Proposed fix |
|---|---|---|---|---|---|
| V1 | Medium | Pi provisioning, if the Pi runs `systemd-resolved` | `deploy/nftables/atlas.nft`; `provision-pi.sh` | With `systemd-resolved` active (Debian's default; `nsswitch.conf` has `resolve [!UNAVAIL=return]`), every lookup goes to resolved, whose upstream queries run as `systemd-resolve` and are dropped by the output policy. `atlas-egress` then fails every request with `DNS_FAILED`. Raspberry Pi OS normally points `resolv.conf` at the router without resolved, but nothing checks that. | `provision-pi.sh` refuses to load the firewall while `systemd-resolved` is active, and tells you to disable it and point `/etc/resolv.conf` at `LAN_DNS`. The alternative, allowing the resolver's user to `LAN_DNS:53`, keeps a second process able to send DNS, which PLAN 5.7 avoids. |
| V4 | Low | — | `backend/atlas/audit/server.py` (socket-activated listener) | On stop, asyncio tries to unlink `/run/atlas/audit.sock`, which systemd owns. The service user can't, so it only logs `PermissionError`. If it ever could, socket activation would break until the socket unit restarts. | Start the server with `cleanup_socket=False` (Python 3.13) when the socket comes from `LISTEN_FDS`. |
| V5 | Medium | — (safety net for deploys) | `deploy/provisioning/atlas-release.sh` `health_check` | The check passes as soon as each unit is `active` once. A release that starts and crashes within seconds is briefly `active` between `Restart=` attempts, so it passes and isn't rolled back. Seen in the VM: `activate` reported success while `atlas-audit` was failing on missing credentials. | Require each unit to stay `active` for 10 s with `NRestarts` unchanged before declaring the release healthy. |
| V6 | Low | — | `atlas-release.sh` `build_venv` | pip warns that `/~/.cache/pip` isn't writable (the DynamicUser has no home). Cosmetic. | Pass `--no-cache-dir` to pip in the builder. |

### Accepted (no fix planned)

| ID | Note |
|---|---|
| N7 | Denial aggregation replaces the hostnames a caller probed with a count once its 10-token bucket is spent. Nothing leaves the Pi on a denial, so this affects forensic detail only. |

### Contract follow-ups (proposals, not bugs)

- `contracts/proposals/2026-11-17-audit-signed-purge-and-egress-count.md` (agent C). Proposes an optional signature on `purge` and an optional `count` on `egress`.
  - Reviewer note: include `chain_id` and the purge record's `seq` in the signed message.
  - Until `count` lands, anything reading `egress.task_id` must ignore the `suppressed-` prefix. Since N3 it is used by two aggregates: `DENIALS_SUPPRESSED` (decision `blocked`) and `AUDIT_RECORDS_LOST` (decision `allowed`, with summed `bytes_in`/`bytes_out`).
- Actor pinning for `auth`, `config`, `power`, `action_proposed` and `system` audit records. Today any service may write them, so a compromised reader could write `stepup_verified` or `killswitch`. Consumers must filter by actor until this is pinned. A proposal is still to be written.

### Needs the ARM64 Debian VM or the Pi

These are not bugs, just unverified. The full list is `docs/runbooks/pi-verification.md`.

- **nftables behaviour.** Load the rules and check the SYN-ACK behaviour of the proxy sport rule, `meta mark`, and the PT-1 script.
- **Release build.** GNU tar link and setuid handling. The `systemd-run -p DynamicUser=yes` venv build on the read-only-overlay root (`/var/lib/atlas-build` lands in RAM). The N5 install logic (copy, re-check, cleanup) ran in the VM unprivileged, with `chown`, `python3` and `systemd-run` stubbed; a real root run is still to do.
- **Python 3.13.** Done 2026-11-20 in the VM: the full suite passes on 3.13.5, including the shellcheck test (S1) and the N1, N2, N3, N4, G1 and N5 regression tests.
- **nftables syntax.** `nft -c` passes in the VM (2026-11-20) with the example site file. Loading the rules and checking that they block traffic is still to do.
- **Audit shutdown under systemd (N4).** The fix is tested with a real `serve` process and SIGTERM; `systemctl stop atlas-audit` with `atlas-egress` connected, against `TimeoutStopSec=15`, is still to do.

## Closed

| ID | Closed by | Note |
|---|---|---|
| F1–F4, F6, F9, MDWE | `2f7ba8e`..`68d3945` | First review round. F5, F7 and F8 partly fixed; the remainder was N3 and D1–D4. |
| N1 | `336ca7f` | `AuditLog.open` signs a checkpoint whenever the recovered (or new) chain has unsigned records; the verifier's wall-clock tail rule is gone, the 1,000-record rule stays. Tests: `test_known_issues_audit.py::test_n1_*`. |
| G1 | `464936e` | Genesis is signed at once. With `--pubkey`, a chain with no verified checkpoint is an error. New `--expect-genesis HASH` (recorded off-box with the public key at provisioning). Without `--expect-head`, `verify` prints `UNANCHORED:` instead of `OK:` (exit code still 0). Tests: `test_known_issues_audit.py::test_g1_*`. |
| N4 | `756e139` | `AuditServer.close(on_stop=...)` stops accepting, writes `service_stop` and the checkpoint under the append lock, refuses later appends (`AUDIT_UNAVAILABLE`), closes every client and waits at most 5 s. Tests: `test_known_issues_audit.py::test_n4_*` (one runs the real `serve` process with an idle pooled client and SIGTERM). |
| N3 | `3a73b29` | Proxy: tunnel opens per caller limited to 2/s, burst 30, `429` beyond (audited `block_reason limit`, `RATE_LIMITED`). Audit server: per-actor budgets, `atlas-egress` 30/s, burst 300. Refused best-effort records are counted in the aggregates (`DENIALS_SUPPRESSED`, new `AUDIT_RECORDS_LOST`). Tests: `test_known_issues_egress.py::test_n3_*` (end to end through a real audit socket). |
| V2 | `baa4d78` | `deploy.sh` built the tarball with macOS tar, which adds AppleDouble `._*` entries (macOS `tar -t` hides them). `atlas-release` refused the first real deploy (`unexpected top-level entry: ._.`). Now `COPYFILE_DISABLE=1 tar --no-xattrs --no-mac-metadata`. Test: `test_deploy_structure.py::test_deploy_tarball_carries_no_macos_metadata` (it proves the old command produces the entries). |
| V3 | `baa4d78` | The `verify` command in `audit.md` and in the FI-3 script header lacked `PYTHONPATH=/var/lib/atlas/current/backend` (the unit sets it), so it failed with `No module named 'atlas'` on the box. |
| S1 | `c37c4f6` | Globs instead of `ls \| grep` (a newline in a directory name could make `prune` delete a real release), real `if`s instead of `&& \|\|`, quoted lock list in `deploy.sh`, SC2016 kept with a reasoned directive. `test_shellcheck_if_available` runs `shellcheck -x --source-path=SCRIPTDIR` and passes in the VM. Tests: `test_known_issues_deploy.py::test_s1_*`. |
| N5 | `d962b9c` | `atlas-release install` copies the upload (bounded to 512 MiB) into a 0700 work directory under root-owned `releases/` and runs every step on the copy, re-checks the top-level allowlist on the extracted tree, and cleans up on every failure path (EXIT trap). Tests: `test_known_issues_deploy.py::test_n5_*` (GNU tar: run in the VM). |
| D2 | `50caa3b` | The firewall step moved into `lib.sh` (`install_firewall`); a change to `atlas.nft` or to the site file is validated with `nft -c`, then reloaded; `nftables-site.nft.applied` and `.prev` are kept. Tests: `test_known_issues_deploy.py::test_d2_*`. |
| D3 | `50caa3b` | A real `provision-pi.sh` run refuses an edited site file that still holds `2001:db8::`, and a placeholder `ATLAS_LAN_PREFIX6` (dry runs warn). Without IPv6 on the LAN, `fd00::/8` is the documented value. Tests: `test_known_issues_deploy.py::test_d3_*`. |
| N2 | `e2cdb73` | The verifier accepts a chain start anywhere in a covered purge's `from_seq..to_seq+1` when the record at `to_seq` has the anchored hash; `_recover` finishes the purge (only after full verification). Tests: `test_known_issues_audit.py::test_n2_*`. |
