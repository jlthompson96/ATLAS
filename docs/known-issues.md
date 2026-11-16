# Known issues

Open problems in code already on `main`. Each has an ID, what it blocks, and the fix agreed so far.

**Read this before working on `backend/atlas/audit/`, `backend/atlas/egress/` or `deploy/`.** Close an issue by fixing it on a branch, with a regression test, and moving its row to "Closed" with the commit.

## Open

### From the security review of agent C's Pi foundations (merged 2026-11-16, `b6a90cc`)

The owner merged C's branch at `68d3945` before the second fix round. The reviewer's second pass found the original bypasses closed (F1–F4, F6, F9) but the regressions below, all reproduced. **Everything marked "Pi provisioning" must be fixed before the Pi is provisioned**, because FI-3 (power cuts) and PT-13 (audit tamper detection) depend on them.

The reproduction scripts (`exp4.py`, `exp5.py`) lived in a session scratch directory. Rebuild them as regression tests when fixing.

| ID | Severity | Blocks | Where | Problem | Agreed fix |
|---|---|---|---|---|---|
| N1 | High | Pi provisioning (FI-3, PT-13) | `backend/atlas/audit/verify.py` `_check_tail`; `backend/atlas/audit/log.py` open/recover | The verifier's "more than 1 h of records after the last verified checkpoint" rule uses wall-clock `ts`, but the writer's hourly checkpoint timer is monotonic and resets on restart. After any unclean stop (power cut, SIGKILL, NTP step forward), an honest log fails `verify` for up to an hour. On a purged chain, a second crash in that hour makes `AuditLog.open` refuse to start, and all egress then fails closed. | Sign a checkpoint in `open()` right after `_recover` whenever any record is unsigned. Drop the time-based tail rule; keep the 1,000-record rule. The time rule only constrains honest writers, because `ts` is attacker-chosen in a forged tail. |
| N2 | Medium | The retention job (nothing calls `purge_segments` yet) | `backend/atlas/audit/log.py` `purge_segments` | A crash after unlinking some but not all purged segments leaves a chain whose start doesn't match the purge's `to_seq + 1`, so `verify` fails and the service refuses to start. | The verifier accepts a chain start anywhere in a covered purge's `from_seq..to_seq+1`. `_recover` finishes an interrupted purge by unlinking segments wholly at or below a covered `to_seq`. |
| N3 | Medium | Pi provisioning | `backend/atlas/audit/server.py` (5 appends/s, burst 50, per actor); `backend/atlas/egress/proxy.py` | All proxy records are written by the single actor `atlas-egress`, and each allowed tunnel costs two records. One caller opening allowed tunnels to its own host (7,944 in 3 s in the reproduction) exhausts that budget. Other callers' tunnels are then refused, and close records with byte counts are dropped uncounted. Heavy honest use (SearXNG fan-out, Gmail sync) could hit the same limit. | Rate-limit tunnel opens per caller in the proxy, well below the egress audit budget, replying 429. Give `atlas-egress` a larger budget in the audit server. Count best-effort losses in the suppressed-denials aggregate. |
| N4 | Medium | Pi provisioning | `backend/atlas/audit/server.py` close; `backend/atlas/audit/__main__.py` | `Server.wait_closed()` waits for open connections, and `atlas-egress` always holds one idle pooled connection. Shutdown blocks past `TimeoutStopSec=15`, so systemd SIGKILLs: no `service_stop` record and no clean-stop checkpoint, which feeds N1. After an I/O failure the process lingers up to 5 minutes before exiting. | Track client writers and close them in `close()` (or `Server.close_clients()` on 3.13). Write the stop record and checkpoint before waiting for connections. |
| N5 | Low | Pi provisioning | `deploy/provisioning/atlas-release.sh` install | The sha256 check, the entry check and `tar -xf` each re-open `/var/lib/atlas/incoming/<v>.tar`, in an admin-owned directory, so the file can be swapped between steps. The top-level allowlist isn't re-checked on the extracted tree. No root execution was found; a pre-seeded venv would run only as the service user. A failed build leaves a world-readable `releases/.in.XXXXXX`. | Copy the tarball into root-owned `releases/` first and run every step on that copy. Re-check the allowlist on the extracted tree. Clean up on every failure path. |
| N6 | Low | — | `backend/atlas/audit/log.py` open; `backend/atlas/audit/verify.py` | Starting on a purged chain runs a full in-memory `verify_dir`. At 1–20 MB/day for 365 days that is slow, and may not fit in memory at the top of the range. Startup time is egress downtime. | Make the verifier stream records. |
| G1 | Medium | Pi provisioning (PT-13) | `backend/atlas/audit/log.py`; `backend/atlas/audit/verify.py`; `docs/runbooks/audit.md` | Someone with write access but without the signing key can delete everything and write a fresh genesis chain. With the sidecar deleted and no off-box anchor, it verifies, because it looks like a new install. | Sign a checkpoint right after genesis. With `--pubkey`, "0 verified checkpoints" is an error. Add `--expect-genesis <hash>`, recorded off-box with the public key at provisioning. Without `--expect-head`, `verify` prints a distinct `UNANCHORED` status. Rollback to a genuine prefix stays covered by `--expect-head`. |
| D1 | Medium | Phase 1 (WireGuard) | `docs/runbooks/` WireGuard and nftables notes | The runbook says `FwMark = 0x5182`, which is wg-quick syntax. services.md puts WireGuard under `systemd-networkd`, where the key is `FirewallMark=`. With the wrong key the nftables mark rule never matches, and the tunnel is silently blocked. | Use `FirewallMark=0x5182` in the `.netdev` file, and check it in the VM. |
| D2 | Low | Pi provisioning | `deploy/provisioning/provision-pi.sh` | A change to the site nftables file alone is neither validated nor reloaded. Only changes to `atlas.nft` trigger `nft -c` and the reload. | Treat both files as one ruleset: validate and reload if either changed. |
| D3 | Low | Pi provisioning | `deploy/nftables/` site include; `provision-pi.sh` | Nothing forces the owner to replace the RFC 3849 placeholder `LAN_PREFIX6`, so the IPv6 LAN exclusion (F6) can silently stay a no-op. | `provision-pi.sh` refuses to continue while any `2001:db8::` placeholder remains, as it does for the IPv4 site file. |
| D4 | Low | — | `deploy/nftables/atlas.nft` | A process that squats a proxy port (3131–3136) while `atlas-egress` is down can still stop it from binding: a denial of service, with no data exposure. `LAN_ROUTER6` is a spoofable link-local source, so it adds little. | Socket-activate the proxy listeners from systemd. |

### Accepted (no fix planned)

| ID | Note |
|---|---|
| N7 | Denial aggregation replaces the hostnames a caller probed with a count once its 10-token bucket is spent. Nothing leaves the Pi on a denial, so this affects forensic detail only. |

### Contract follow-ups (proposals, not bugs)

- `contracts/proposals/2026-11-17-audit-signed-purge-and-egress-count.md` (agent C). Proposes an optional signature on `purge` and an optional `count` on `egress`.
  - Reviewer note: include `chain_id` and the purge record's `seq` in the signed message.
  - Until `count` lands, anything reading `egress.task_id` must ignore the `suppressed-` prefix.
- Actor pinning for `auth`, `config`, `power`, `action_proposed` and `system` audit records. Today any service may write them, so a compromised reader could write `stepup_verified` or `killswitch`. Consumers must filter by actor until this is pinned. A proposal is still to be written.

### Needs the ARM64 Debian VM or the Pi

These are not bugs, just unverified. The full list is `docs/runbooks/pi-verification.md`.

- **nftables syntax.** Run `nft -c` on the rules: quoted skuid names, variables in anonymous sets, `limit … log`, `meta mark`, `th dport`, and the SYN-ACK behaviour of the proxy sport rule.
- **Release build.** GNU tar link and setuid handling. The `systemd-run -p DynamicUser=yes` venv build on the read-only-overlay root (`/var/lib/atlas-build` lands in RAM).
- **Python 3.13.** N1, N4 and F4, plus all tests, on Python 3.13. The Mac only has 3.15.

## Closed

| ID | Closed by | Note |
|---|---|---|
| F1–F4, F6, F9, MDWE | `2f7ba8e`..`68d3945` | First review round. F5, F7 and F8 partly fixed; the remainder is N3, D1–D4 above. |
