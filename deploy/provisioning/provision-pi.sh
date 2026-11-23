#!/usr/bin/env bash
# Provision the Raspberry Pi for ATLAS. Idempotent: safe to re-run; it only changes what differs.
#
#   sudo ATLAS_ADMIN_USER=admin ATLAS_LAN_CIDR=192.0.2.0/24 ATLAS_LAN_BROADCAST=192.0.2.255 \
#        ./provision-pi.sh [--dry-run]
#
# Run it from a copy of this repository's deploy/ directory, in a maintenance window with the read-only root overlay
# disabled (it writes /etc and /usr/local/sbin). See docs/runbooks/provisioning.md. It does NOT start services, store
# credentials or ship code: those are store-credential.sh and deploy.sh.
#
# Placeholders only live in this repository; real addresses go in the environment and in /etc/atlas on the Pi.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
deploy_dir="$(cd "$here/.." && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h | --help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "unknown argument: $arg" ;;
  esac
done

ATLAS_ADMIN_USER="${ATLAS_ADMIN_USER:-admin}"
ATLAS_LAN_CIDR="${ATLAS_LAN_CIDR:-192.0.2.0/24}"
ATLAS_LAN_BROADCAST="${ATLAS_LAN_BROADCAST:-192.0.2.255}"
ATLAS_LAN_PREFIX6="${ATLAS_LAN_PREFIX6:-2001:db8:1234:5600::/64}" # RFC 3849 placeholder: the LAN's global IPv6 prefix
BASE=/var/lib/atlas
SERVICES_CONF="$here/services.conf"

need_root
[ -f "$SERVICES_CONF" ] || die "missing $SERVICES_CONF"
if [ "$DRY_RUN" != "1" ]; then
  user_exists "$ATLAS_ADMIN_USER" || die "admin user '$ATLAS_ADMIN_USER' does not exist (set ATLAS_ADMIN_USER)"
  case "$ATLAS_ADMIN_USER" in atlas-*) die "the admin account must not be named atlas-* (reserved for services)" ;; esac
fi
case "$ATLAS_LAN_CIDR$ATLAS_LAN_BROADCAST" in
  *[!0-9./]*) die "ATLAS_LAN_CIDR / ATLAS_LAN_BROADCAST must be plain IPv4 addresses" ;;
esac
case "$ATLAS_LAN_PREFIX6" in
  *[!0-9a-fA-F:/]*) die "ATLAS_LAN_PREFIX6 must be a plain IPv6 prefix such as 2001:db8::/64" ;;
esac
# The default is the RFC 3849 placeholder; with it the proxy's IPv6 LAN exclusion is a no-op (known issue D3).
if printf '%s\n' "$ATLAS_LAN_PREFIX6" | has_ipv6_placeholder -; then
  if [ "$DRY_RUN" = "1" ]; then
    log "WARNING: ATLAS_LAN_PREFIX6 is the 2001:db8:: placeholder; a real run refuses it"
  else
    die "ATLAS_LAN_PREFIX6 is the RFC 3849 placeholder: set your LAN's global IPv6 prefix, or fd00::/8 if the LAN has none"
  fi
fi

log "== groups and users"
ensure_group atlas-audit-clients # may write to /run/atlas/audit.sock
ensure_group atlas-core-clients  # may connect to /run/atlas/core.sock
for name in $(service_list "$SERVICES_CONF"); do
  ensure_service_user "$name"
done

log "== directory layout (contracts/services.md section 6)"
ensure_dir "$BASE" 0755 root:root
ensure_dir "$BASE/releases" 0755 root:root
ensure_dir "$BASE/incoming" 0750 "$ATLAS_ADMIN_USER":root # staging for uploaded release tarballs
ensure_dir "$BASE/credentials" 0700 root:root
ensure_dir /etc/atlas 0755 root:root
for name in $(service_list "$SERVICES_CONF"); do
  short="${name#atlas-}"
  ensure_dir "$BASE/credentials/$name" 0700 root:root
  ensure_dir "$BASE/$short" 0700 "$name:$name" # StateDirectory=atlas/<short>
done

log "== systemd units"
for unit in "$deploy_dir"/systemd/*.service "$deploy_dir"/systemd/*.socket "$deploy_dir"/systemd/*.timer; do
  install_file "$unit" "/etc/systemd/system/$(basename "$unit")" 644
done
install_file "$deploy_dir/systemd/atlas.tmpfiles.conf" /etc/tmpfiles.d/atlas.conf 644
run systemd-tmpfiles --create /etc/tmpfiles.d/atlas.conf
# Site values: the LAN as seen by the two units that need more than loopback (drop-ins are additive).
write_file /etc/systemd/system/atlas-ui.service.d/lan.conf 644 "[Service]
IPAddressAllow=$ATLAS_LAN_CIDR"
write_file /etc/systemd/system/atlas-core.service.d/lan.conf 644 "[Service]
IPAddressAllow=$ATLAS_LAN_BROADCAST"
# The proxy refuses connections into the LAN's own prefixes even when they look public (global IPv6 prefix, review F6).
write_file /etc/systemd/system/atlas-egress.service.d/lan.conf 644 "[Service]
Environment=ATLAS_EGRESS_LAN_PREFIXES=$ATLAS_LAN_CIDR,$ATLAS_LAN_PREFIX6"

log "== release helper and sudoers"
install_file "$here/atlas-release.sh" /usr/local/sbin/atlas-release 755
install_file "$here/services.conf" /etc/atlas/services.conf 644
sudoers="$ATLAS_ADMIN_USER ALL=(root) NOPASSWD: /usr/local/sbin/atlas-release"
if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] would install /etc/sudoers.d/atlas-release: $sudoers"
else
  tmp="$(mktemp)"
  printf '%s\n' "$sudoers" >"$tmp"
  visudo -cf "$tmp" >/dev/null || die "generated sudoers entry does not validate"
  install -m 440 -o root -g root "$tmp" /etc/sudoers.d/atlas-release
  rm -f "$tmp"
fi

log "== backup (ADR 0002)"
# Root-only helper the unit runs as ExecStartPre=+/ExecStopPost=+: mounts the one allowlisted drive, then unmounts it.
install_file "$here/atlas-backup-mount.sh" /usr/local/sbin/atlas-backup-mount 755
ensure_dir /mnt/atlas-backup 0755 root:root
BACKUP_CONF=/etc/atlas/backup.conf
BACKUP_CRED="$BASE/credentials/atlas-backup/backup-repo-password.cred"
if [ -e "$BACKUP_CONF" ]; then
  log "ok      $BACKUP_CONF (the owner's drive list, never overwritten)"
else
  write_file "$BACKUP_CONF" 644 "$(printf '%s\n' \
    "# ATLAS backup drives (ADR 0002, runbook backup.md): one line per drive, '<name> <ext4 filesystem UUID>'." \
    "# Find the UUID with: sudo blkid -s UUID -o value /dev/sdX1. Unlisted drives are never mounted." \
    "# A 0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0" \
    "# B 1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d")"
fi

log "== firewall (nftables)"
write_file /etc/nftables.conf 755 '#!/usr/sbin/nft -f
include "/etc/atlas/atlas.nft"'
# atlas.nft and the site file are one ruleset: a change to either is validated, then reloaded (lib.sh, known issue D2).
# The site file is created from the placeholder example on the first run; the firewall is not loaded until it is edited,
# and an edited site file that still holds the 2001:db8:: placeholder is refused (known issue D3).
install_firewall /etc/atlas "$deploy_dir/nftables"
# The time servers come from the same site file as the firewall rule that allows them. Before the first run creates
# it (dry run), the example stands in.
site_file=/etc/atlas/nftables-site.nft
[ -f "$site_file" ] || site_file="$deploy_dir/nftables/site.nft.example"
install_timesync "$site_file" /etc/systemd/timesyncd.conf.d/50-atlas.conf
# Known issue P2: the ATLAS units wait for time-sync.target. With systemd-time-wait-sync enabled that means "the clock
# is synced", but its default wait is infinite: 90 s lets a Pi without internet start anyway, and the failed wait
# then shows in systemctl --failed.
write_file /etc/systemd/system/systemd-time-wait-sync.service.d/50-atlas.conf 644 "$(printf '%s\n' \
  "# ATLAS: wait at most 90 s for the first clock sync (known issue P2). Managed by provision-pi.sh." \
  "[Service]" "TimeoutStartSec=90s")"
run systemctl enable systemd-time-wait-sync.service

log "== enable units"
run systemctl daemon-reload
enable_args=()
for name in $(enabled_services "$SERVICES_CONF"); do
  enable_args+=("$name.service")
done
enable_args+=(atlas-audit.socket)
run systemctl enable "${enable_args[@]}"
# The nightly backup only once there is somewhere to write and a password to write with; until then a timer would
# only fail every night.
if grep -qE '^[[:space:]]*[A-Za-z0-9-]+[[:space:]]+[0-9a-fA-F-]{36}' "$BACKUP_CONF" 2>/dev/null && [ -f "$BACKUP_CRED" ]; then
  run systemctl enable --now atlas-backup.timer
else
  log "note    atlas-backup.timer not enabled: needs a drive in $BACKUP_CONF and $BACKUP_CRED (gen-backup-key.sh)"
fi

log "done. Next: store credentials (store-credential.sh / gen-audit-keys.sh), deploy a release (deploy.sh), start units."
