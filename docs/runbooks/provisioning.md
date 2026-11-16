# Runbook: provision the Pi (Phase 0)

Scope: users and groups, `/var/lib/atlas` layout, systemd units, nftables, the release helper. Source of truth for every name, port and socket: [contracts/services.md](../../contracts/services.md). Scripts: `deploy/provisioning/`. Tested here only statically and in `--dry-run`; the first real run happens in the ARM64 Debian VM, then on the Pi (checklist: [pi-verification.md](pi-verification.md)).

## Before you start

- Raspberry Pi OS 64-bit (Debian, Python **3.13**; the dependency locks are CPython 3.13 `aarch64`). Packages: `nftables`, `python3-venv`, `openssl`, `sudo`, `curl` (PT-1 script).
- A maintenance window with the **read-only root overlay disabled** (PLAN 2.7: `raspi-config nonint disable_overlayfs`, reboot, provision, re-enable). The script writes `/etc` and `/usr/local/sbin`.
- **Console access** (keyboard and screen or serial). The firewall loads with default-deny output and SSH only from the admin IP; a wrong `ADMIN_IP` locks you out of SSH.
- An admin account that is not named `atlas-*` (set `ATLAS_ADMIN_USER`); it uploads releases and may run exactly one root command (below).

## Steps

1. Copy this repository's `deploy/` directory to the Pi (for example `rsync -a deploy/ admin@atlas-pi.example.org:/tmp/atlas-deploy/`). No secrets are in it.
2. Dry run, read the output: `./provision-pi.sh --dry-run`.
3. Run it: `sudo ATLAS_ADMIN_USER=admin ATLAS_LAN_CIDR=192.0.2.0/24 ATLAS_LAN_BROADCAST=192.0.2.255 ./provision-pi.sh` (use your real LAN values; the script rejects anything but plain IPv4 text).
4. It creates `/etc/atlas/nftables-site.nft` from the placeholder example and **stops short of loading the firewall** while that file is unchanged. Edit it (LAN CIDR and broadcast, LAN DNS and NTP, admin IP, desktop LAN IP), then run the script again. It validates the ruleset with `nft -c` before `systemctl enable --now nftables`.
5. Store credentials on the Pi only. For audit: `sudo ./gen-audit-keys.sh`. For the others, as their phases arrive: `printf %s "$SECRET" | sudo ./store-credential.sh atlas-market twelvedata-api-key` (only credential names that the service's unit loads are accepted).
6. Deploy a release ([deploy.md](deploy.md)), then `sudo systemctl start atlas-audit.socket atlas-egress.service`.
7. Re-enable the overlay. Confirm what survives a reboot: `/var/lib/atlas` (data partition) yes; edits under `/etc` only if the overlay was off while you made them.

## What the script does (all idempotent)

| Area | Result |
|---|---|
| Users/groups | `atlas-<name>` system user and same-named group for each Pi service (no shell, no home); shared groups `atlas-audit-clients` and `atlas-core-clients` (see [the proposal](../../contracts/proposals/2026-11-16-socket-groups-and-reserved-names.md)) |
| Layout | `/var/lib/atlas/{releases,incoming,credentials}`, per-service state dirs `/var/lib/atlas/<name>` (0700, owned by the service), `/etc/atlas`, `/run/atlas` via tmpfiles |
| Units | `deploy/systemd/*.service|*.socket` into `/etc/systemd/system`; site drop-ins `lan.conf` for `atlas-ui` and `atlas-core`; only `atlas-audit` and `atlas-egress` are enabled, the rest are templates for later phases (`services.conf`, column 2) |
| Firewall | `/etc/atlas/atlas.nft` plus `/etc/nftables.conf` including it; site addresses in `/etc/atlas/nftables-site.nft` (never committed) |
| Releases | `/usr/local/sbin/atlas-release` and one sudoers line: the admin may run `atlas-release` as root, nothing else |

## Network policy in one paragraph

Output is default-drop. Only `atlas-egress` reaches the internet, TCP 443, never to a non-public address. Each of the six loopback proxy ports (3131-3136) accepts a connection only from its one caller's user. DNS only for `atlas-egress` and root, only to the LAN resolver, so DNS cannot carry exfiltrated data. NTP from the LAN source, WireGuard to the desktop, `atlas-core` alone over `wg0` to ports 1234 and 8770, and the Wake-on-LAN broadcast. Port 80 for research is a commented-out line until Phase 5. SSH in from the admin IP only. Units add `IPAddressDeny=any` underneath.

## Maintenance (OS updates, `apt`)

Not solved here: root has no internet path by design. Until the owner decides (open question in the handoff), do updates in the maintenance window by adding a temporary, timed nft rule for root by hand, and remove it afterwards. Do not edit `atlas.nft` for this.

## Rollback

Everything here is additive. To undo the firewall from the console: `sudo nft delete table inet atlas`. Units: `systemctl disable --now <unit>`; remove `/etc/systemd/system/atlas-*`.
