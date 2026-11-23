# Runbook: provision the Pi (Phase 0)

Scope: users and groups, `/var/lib/atlas` layout, systemd units, nftables, the release helper. Source of truth for every name, port and socket: [contracts/services.md](../../contracts/services.md). Scripts: `deploy/provisioning/`. Tested here only statically and in `--dry-run`; the first real run happens in the ARM64 Debian VM, then on the Pi (checklist: [pi-verification.md](pi-verification.md)).

## Before you start

- Raspberry Pi OS 64-bit (Debian, Python **3.13**; the dependency locks are CPython 3.13 `aarch64`). Packages: `nftables`, `python3-venv`, `openssl`, `sudo`, `curl` (PT-1 script).
- The base setup from [pi-bringup.md](pi-bringup.md) (card layout, data partition on `/var/lib/atlas`, SSH key-only). On later runs, a maintenance window with the **read-only root overlay disabled**: use the commands in [pi-bringup.md §11](pi-bringup.md#11-make-the-root-filesystem-read-only), not raspi-config, whose switch doesn't handle the `recurse=0` setting ATLAS needs. The script writes `/etc` and `/usr/local/sbin`.
- **The system resolver must be the LAN's DNS server, not a local stub.** Under the firewall only `atlas-egress` and root may send DNS, and only to `LAN_DNS`. With `systemd-resolved` running, lookups go through its own user and are dropped. Raspberry Pi OS doesn't run it by default; if it's active, `sudo systemctl disable --now systemd-resolved` and make `/etc/resolv.conf` a plain file with `nameserver <LAN_DNS>`. `provision-pi.sh` refuses to load the firewall otherwise (known issue V1).
- **Console access** (keyboard and screen or serial). The firewall loads with default-deny output and SSH only from the LAN (`LAN_CIDR`); a wrong `LAN_CIDR` locks you out of SSH. **SSH sessions that are open while it loads freeze**, even from the LAN: the firewall doesn't know they're already established, so it drops their replies. Open a new connection afterwards (seen on the Pi, 2026-11-20). A `tmux` session on the Pi survives this, and you can attach to it from the console.
- An admin account that is not named `atlas-*` (set `ATLAS_ADMIN_USER`); it uploads releases and may run exactly one root command (below).

## Steps

1. Copy this repository's `deploy/` directory to the Pi (for example `rsync -a deploy/ admin@atlas-pi.example.org:/tmp/atlas-deploy/`). No secrets are in it.
2. Dry run, read the output: `./provision-pi.sh --dry-run`.
3. Run it: `sudo ATLAS_ADMIN_USER=admin ATLAS_LAN_CIDR=192.0.2.0/24 ATLAS_LAN_BROADCAST=192.0.2.255 ATLAS_LAN_PREFIX6=<your prefix> ./provision-pi.sh` (use your real LAN values; the script rejects anything but plain IPv4 text). `ATLAS_LAN_PREFIX6` is the LAN's global IPv6 prefix (from the router); a real run refuses the RFC 3849 placeholder `2001:db8::` (known issue D3). If the LAN has no global IPv6 prefix, use `fd00::/8`: it is already non-public, so the exclusion is a harmless no-op.
4. It creates `/etc/atlas/nftables-site.nft` from the placeholder example and **stops short of loading the firewall** while that file is unchanged. Edit it (LAN CIDR and broadcast, LAN DNS, the time servers (`NTP_SERVERS`, Google's by default), desktop LAN IP, `LAN_PREFIX6` with the same value as `ATLAS_LAN_PREFIX6`), then run the script again. It refuses an edited file that still holds the `2001:db8::` placeholder. It validates the ruleset with `nft -c` before `systemctl enable --now nftables`. Later edits to the site file alone are picked up the same way: re-run the script and it validates and reloads the ruleset (known issue D2).
5. Store credentials on the Pi only. For audit: `sudo ./gen-audit-keys.sh`. For `atlas-core` and `atlas-ui`: [ui-on-pi.md](ui-on-pi.md). For the others, as their phases arrive: `printf %s "$SECRET" | sudo ./store-credential.sh atlas-market twelvedata-api-key` (only credential names that the service's unit loads are accepted). Then **run `provision-pi.sh` again**: it enables a service only once every credential its unit loads exists, so a deploy's health check never waits on a service that can't start.
6. Deploy a release ([deploy.md](deploy.md)), then `sudo systemctl start atlas-audit.socket atlas-egress.service`.
   Then record the audit trust anchors off the Pi (G1, [audit.md](audit.md#off-box-anchor-do-this)): copy `/var/lib/atlas/audit/checkpoint.pub`, and note the `genesis` hash that `python -m atlas.audit verify /var/lib/atlas/audit` prints. Keep both where the Pi cannot write.
7. Re-enable the overlay. Confirm what survives a reboot: `/var/lib/atlas` (data partition) yes; edits under `/etc` only if the overlay was off while you made them.

## What the script does (all idempotent)

| Area | Result |
|---|---|
| Users/groups | `atlas-<name>` system user and same-named group for each Pi service (no shell, no home); shared groups `atlas-audit-clients` and `atlas-core-clients` (see [the proposal](../../contracts/proposals/2026-11-16-socket-groups-and-reserved-names.md)) |
| Layout | `/var/lib/atlas/{releases,incoming,credentials}`, per-service state dirs `/var/lib/atlas/<name>` (0700, owned by the service), `/etc/atlas`, `/run/atlas` via tmpfiles |
| Units | `deploy/systemd/*.service|*.socket` into `/etc/systemd/system`; site drop-ins `lan.conf` for `atlas-ui` (with `PI_LAN_IP` from the site file) and `atlas-core`; `atlas-audit`, `atlas-egress`, `atlas-core` and `atlas-ui` are enabled once their credentials exist (and, for `atlas-ui`, `PI_LAN_IP` and its certificate), the rest are templates for later phases (`services.conf`, column 2) |
| Firewall | `/etc/atlas/atlas.nft` plus `/etc/nftables.conf` including it; site addresses in `/etc/atlas/nftables-site.nft` (never committed). The two are one ruleset: a change to either is validated and reloaded. `nftables-site.nft.applied` is the site file last loaded; `atlas.nft.prev` and `nftables-site.nft.prev` the previous ones |
| Releases | `/usr/local/sbin/atlas-release` and one sudoers line: the admin may run `atlas-release` as root, nothing else |

## Network policy in one paragraph

Output is default-drop. Only `atlas-egress` reaches the internet, TCP 443, never to a non-public address. Each of the six loopback proxy ports (3131-3136) accepts a connection only from its one caller's user. DNS only for `atlas-egress` and root, only to the LAN resolver, so DNS cannot carry exfiltrated data. NTP only from the time daemon's user to the fixed addresses in `NTP_SERVERS` (no DNS needed; `provision-pi.sh` points timesyncd at the same list), WireGuard to the desktop, `atlas-core` alone over `wg0` to ports 1234 and 8770, and the Wake-on-LAN broadcast. Port 80 for research is a commented-out line until Phase 5. SSH in from the LAN only. Units add `IPAddressDeny=any` underneath.

## Maintenance (OS updates, `apt`)

Not solved here: root has no internet path by design. Until the owner decides (open question in the handoff), do updates in the maintenance window by adding a temporary, timed nft rule for root by hand, and remove it afterwards. Do not edit `atlas.nft` for this.

## Rollback

Everything here is additive. Do **not** remove the firewall while ATLAS services run: with the `atlas` table gone the kernel's policy is accept, and every service could reach the internet.

- *A bad or unwanted ruleset change:* from the console, stop the ATLAS units first (`sudo systemctl stop 'atlas-*.service'`), then load the last known-good ruleset (`sudo cp /etc/atlas/atlas.nft.prev /etc/atlas/atlas.nft` if the ruleset changed, `sudo cp /etc/atlas/nftables-site.nft.prev /etc/atlas/nftables-site.nft` if the site file changed, then `sudo nft -c -f /etc/atlas/atlas.nft && sudo systemctl reload nftables.service`), then start the units again. `provision-pi.sh` keeps `atlas.nft.prev` whenever it replaces the ruleset and `nftables-site.nft.prev` whenever it loads a changed site file; it validates (`nft -c`) either change before replacing anything, then reloads. An edit to the site file that fails `nft -c` is not loaded, but it is already on disk and the next boot would load it: fix it, or restore `nftables-site.nft.applied`, before rebooting.
- *Units:* `systemctl disable --now <unit>`; remove `/etc/systemd/system/atlas-*`.
- *WireGuard note:* the ruleset only lets the tunnel's own packets reach the desktop's UDP 51820, identified by fwmark. WireGuard runs under `systemd-networkd` (contracts/services.md), so put `FirewallMark=0x5182` (the `WG_FWMARK` in `/etc/atlas/nftables-site.nft`) in the `[WireGuard]` section of `/etc/systemd/network/wg0.netdev`. `FwMark =` is wg-quick syntax: networkd does not use it, the mark rule never matches, and the tunnel is silently blocked under the default-drop output policy (known issue D1).
