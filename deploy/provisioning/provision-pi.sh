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
for unit in "$deploy_dir"/systemd/*.service "$deploy_dir"/systemd/*.socket; do
  install_file "$unit" "/etc/systemd/system/$(basename "$unit")" 644
done
install_file "$deploy_dir/systemd/atlas.tmpfiles.conf" /etc/tmpfiles.d/atlas.conf 644
run systemd-tmpfiles --create /etc/tmpfiles.d/atlas.conf
# Site values: the LAN as seen by the two units that need more than loopback (drop-ins are additive).
write_file /etc/systemd/system/atlas-ui.service.d/lan.conf 644 "[Service]
IPAddressAllow=$ATLAS_LAN_CIDR"
write_file /etc/systemd/system/atlas-core.service.d/lan.conf 644 "[Service]
IPAddressAllow=$ATLAS_LAN_BROADCAST"

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

log "== firewall (nftables)"
install_file "$deploy_dir/nftables/atlas.nft" /etc/atlas/atlas.nft 644
site=/etc/atlas/nftables-site.nft
if [ ! -f "$site" ]; then
  log "create  $site from the placeholder example: EDIT IT, then re-run"
  run install -m 640 -o root -g root "$deploy_dir/nftables/site.nft.example" "$site"
fi
write_file /etc/nftables.conf 755 '#!/usr/sbin/nft -f
include "/etc/atlas/atlas.nft"'
firewall_ready=1
if [ "$DRY_RUN" != "1" ] && cmp -s "$site" "$deploy_dir/nftables/site.nft.example"; then
  log "WARNING: $site still holds the placeholder addresses; the firewall is NOT loaded or enabled yet."
  firewall_ready=0
fi
if [ "$firewall_ready" = "1" ]; then
  run nft -c -f /etc/atlas/atlas.nft
  run systemctl enable --now nftables.service
fi

log "== enable units"
run systemctl daemon-reload
enable_args=()
for name in $(enabled_services "$SERVICES_CONF"); do
  enable_args+=("$name.service")
done
enable_args+=(atlas-audit.socket)
run systemctl enable "${enable_args[@]}"

log "done. Next: store credentials (store-credential.sh / gen-audit-keys.sh), deploy a release (deploy.sh), start units."
