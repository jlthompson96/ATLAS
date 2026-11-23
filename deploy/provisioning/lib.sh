#!/usr/bin/env bash
# Shared helpers for the provisioning scripts. Source it; do not execute it.
# shellcheck shell=bash

DRY_RUN="${DRY_RUN:-0}"

log() { printf '%s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

# run CMD...: print it in dry-run mode, execute it otherwise.
run() {
  if [ "$DRY_RUN" = "1" ]; then
    log "[dry-run] $*"
  else
    "$@"
  fi
}

need_root() {
  if [ "$DRY_RUN" != "1" ] && [ "$(id -u)" -ne 0 ]; then
    die "must run as root (or use --dry-run)"
  fi
}

user_exists() { id -u "$1" >/dev/null 2>&1; }

group_exists() {
  if command -v getent >/dev/null 2>&1; then
    getent group "$1" >/dev/null 2>&1
  else
    grep -q "^$1:" /etc/group 2>/dev/null
  fi
}

ensure_group() {
  if group_exists "$1"; then
    log "ok      group $1"
  else
    log "create  group $1"
    run groupadd --system "$1"
  fi
}

# ensure_service_user NAME: system user + same-named group, no login, no home directory.
ensure_service_user() {
  if user_exists "$1"; then
    log "ok      user $1"
  else
    log "create  user $1"
    run useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$1"
  fi
}

# ensure_dir PATH MODE OWNER:GROUP
ensure_dir() {
  local path="$1" mode="$2" owner="$3"
  log "dir     $path ($mode $owner)"
  run install -d -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$path"
}

# install_file SRC DST MODE: copy only when the content or mode differs (idempotent, no needless SD-card writes).
install_file() {
  local src="$1" dst="$2" mode="$3"
  if [ -f "$dst" ] && cmp -s "$src" "$dst" && [ "$(stat -c %a "$dst" 2>/dev/null || stat -f %Lp "$dst")" = "$mode" ]; then
    log "ok      $dst"
  else
    log "install $dst"
    run install -D -m "$mode" -o root -g root "$src" "$dst"
  fi
}

# write_file DST MODE CONTENT: like install_file for generated content.
write_file() {
  local dst="$1" mode="$2" content="$3" tmp
  if [ -f "$dst" ] && [ "$(cat "$dst")" = "$content" ]; then
    log "ok      $dst"
    return 0
  fi
  log "write   $dst"
  if [ "$DRY_RUN" = "1" ]; then
    return 0
  fi
  tmp="$(mktemp)"
  printf '%s\n' "$content" >"$tmp"
  install -D -m "$mode" -o root -g root "$tmp" "$dst"
  rm -f "$tmp"
}

# has_ipv6_placeholder FILE: true if any non-comment text still holds the RFC 3849 documentation prefix 2001:db8::/32
# (FILE may be - for stdin). Left in place, the IPv6 LAN exclusion (review F6) is silently a no-op (known issue D3).
has_ipv6_placeholder() {
  grep -qiE '^[^#]*2001:0{0,2}db8:' "$1"
}

# install_firewall ETC SRC: install, validate and (re)load the ATLAS nftables ruleset. ETC is /etc/atlas on the Pi; SRC is
# this repository's deploy/nftables. The ruleset is atlas.nft PLUS the owner's site file ETC/nftables-site.nft, so a
# change to EITHER is validated with `nft -c` before it replaces anything, then reloaded (known issue D2). The site file
# that was last loaded is kept as nftables-site.nft.applied to detect edits; the previous one as nftables-site.nft.prev.
# resolver_problem: prints why the system resolver can't work under atlas.nft, or nothing. Only atlas-egress and root
# may send DNS, straight to LAN_DNS (PLAN 5.7). A local stub such as systemd-resolved sends its upstream queries as
# its own user, which the output policy drops, so every lookup fails (known issue V1, found in the ARM64 VM).
resolver_problem() {
  local conf="${RESOLV_CONF:-/etc/resolv.conf}" servers
  if systemctl is-active --quiet systemd-resolved.service; then
    echo "systemd-resolved is active"
    return 0
  fi
  servers="$(awk '$1 == "nameserver" { print $2 }' "$conf" 2>/dev/null || true)"
  if [ -z "$servers" ]; then
    echo "$conf names no nameserver"
  elif ! printf '%s\n' "$servers" | grep -qvE '^(127\.|::1$)'; then
    echo "$conf names only loopback resolvers ($(printf '%s' "$servers" | tr '\n' ' '))"
  fi
}

install_firewall() {
  local etc="$1" src="$2"
  local site="$etc/nftables-site.nft" applied="$etc/nftables-site.nft.applied" nft_changed=0 site_changed=0
  if [ ! -f "$site" ]; then
    log "create  $site from the placeholder example: EDIT IT, then re-run"
    run install -m 640 -o root -g root "$src/site.nft.example" "$site"
  fi
  if [ "$DRY_RUN" != "1" ] && cmp -s "$site" "$src/site.nft.example"; then
    log "WARNING: $site still holds the placeholder addresses; the firewall is NOT loaded or enabled yet."
    install_file "$src/atlas.nft" "$etc/atlas.nft" 644
    return 0
  fi
  if [ "$DRY_RUN" != "1" ] && has_ipv6_placeholder "$site"; then
    die "$site still holds the RFC 3849 placeholder 2001:db8:: (LAN_PREFIX6): set your LAN's global IPv6 prefix, or fd00::/8 if the LAN has none (docs/runbooks/provisioning.md)"
  fi
  local problem
  problem="$(resolver_problem)"
  if [ -n "$problem" ]; then
    local advice="stop systemd-resolved if it runs (systemctl disable --now systemd-resolved) and make /etc/resolv.conf a plain file naming LAN_DNS from $site (docs/runbooks/provisioning.md)"
    if [ "$DRY_RUN" = "1" ]; then
      log "WARNING: $problem: DNS would fail under the firewall; a real run refuses. Fix: $advice"
    else
      die "$problem: DNS would fail under the ATLAS firewall (only atlas-egress and root may query LAN_DNS). Fix: $advice"
    fi
  fi
  if [ -f "$etc/atlas.nft" ] && ! cmp -s "$src/atlas.nft" "$etc/atlas.nft"; then
    nft_changed=1
  fi
  if [ ! -f "$applied" ] || ! cmp -s "$site" "$applied"; then
    site_changed=1
  fi
  if [ ! -f "$etc/atlas.nft" ] || [ "$nft_changed" = "1" ] || [ "$site_changed" = "1" ]; then
    # A bad file must never be what the next boot loads: validate the candidate (with the current site file) first,
    # keep the last known-good copies for rollback, then replace (review F8).
    run install -m 644 -o root -g root "$src/atlas.nft" "$etc/atlas.nft.new"
    run nft -c -f "$etc/atlas.nft.new"
    if [ "$nft_changed" = "1" ]; then
      run cp -p "$etc/atlas.nft" "$etc/atlas.nft.prev"
    fi
    if [ "$site_changed" = "1" ] && [ -f "$applied" ]; then
      run cp -p "$applied" "$etc/nftables-site.nft.prev"
    fi
    run mv -f "$etc/atlas.nft.new" "$etc/atlas.nft"
  else
    log "ok      $etc/atlas.nft and $site"
  fi
  run nft -c -f "$etc/atlas.nft"
  run systemctl enable --now nftables.service
  if [ "$nft_changed" = "1" ] || [ "$site_changed" = "1" ]; then
    log "ruleset changed: reloading nftables (previous kept as atlas.nft.prev and nftables-site.nft.prev)"
    run systemctl reload nftables.service
    run install -m 640 -o root -g root "$site" "$applied"
  fi
}

# ntp_servers SITE: the addresses in SITE's NTP_SERVERS set, space-separated. Fails unless there is at least one and
# each is plain IPv4 text, because timesyncd gets the same list.
ntp_servers() {
  local set addr out=""
  set="$(sed -n 's/^[[:space:]]*define[[:space:]][[:space:]]*NTP_SERVERS[[:space:]]*=[[:space:]]*{\([^}]*\)}.*/\1/p' "$1" |
    head -1)"
  for addr in ${set//,/ }; do
    [[ "$addr" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
    out="$out $addr"
  done
  [ -n "$out" ] || return 1
  printf '%s\n' "${out# }"
}

# install_timesync SITE DST: point systemd-timesyncd at SITE's NTP_SERVERS, the only time servers the firewall lets it
# reach (PLAN 5.7), via the drop-in DST. Restarts timesyncd only when the list changes.
install_timesync() {
  local site="$1" dst="$2" servers content
  servers="$(ntp_servers "$site")" ||
    die "$site: NTP_SERVERS must be a set of plain IPv4 addresses, such as { 198.51.100.123, 198.51.100.124 }"
  content="$(printf '%s\n' "# ATLAS: the time servers the firewall allows (NTP_SERVERS in $site). Managed by provision-pi.sh." \
    "[Time]" "NTP=$servers" "FallbackNTP=")"
  if [ -f "$dst" ] && [ "$(cat "$dst")" = "$content" ]; then
    log "ok      $dst"
    return 0
  fi
  write_file "$dst" 644 "$content"
  run systemctl try-restart systemd-timesyncd.service
}

# site_define NAME SITE: the value of `define NAME = VALUE` in the nftables site file (first match, comment stripped).
site_define() {
  sed -n "s/^[[:space:]]*define[[:space:]][[:space:]]*$1[[:space:]]*=[[:space:]]*\([^#[:space:]]*\).*/\1/p" "$2" | head -1
}

# --- WireGuard to the LLM desktop (contracts/services.md section 2, PLAN 5.3) ---------------------------------------
WG_CRED_NAME=network.wireguard.private.wg0 # systemd-networkd imports network.wireguard.* credentials (systemd >= 256)
WG_CRED_DIR=/etc/credstore.encrypted
WG_PI_IP=10.77.0.1
WG_DESKTOP_IP=10.77.0.2
WG_PORT=51820

# wg_key_ok KEY: a WireGuard public key is 32 bytes, base64: 43 characters and one '='.
wg_key_ok() {
  [[ "$1" =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$ ]]
}

# wg_netdev_content DESKTOP_LAN_IP DESKTOP_PUBKEY FWMARK: the wg0 .netdev. The private key is a systemd-creds
# credential (never in this file); FirewallMark= is what atlas.nft matches to let only the tunnel's own packets out
# to the desktop's UDP 51820 (known issue D1: `FwMark` is wg-quick syntax and networkd ignores it).
wg_netdev_content() {
  printf '%s\n' \
    "# ATLAS: WireGuard to the LLM desktop. Generated by provision-pi.sh from /etc/atlas/nftables-site.nft and" \
    "# /etc/atlas/wireguard-desktop.pub; edit those and rerun provision-pi.sh instead." \
    "[NetDev]" "Name=wg0" "Kind=wireguard" "Description=ATLAS link to the LLM desktop" \
    "" "[WireGuard]" "PrivateKey=@$WG_CRED_NAME" "ListenPort=$WG_PORT" "FirewallMark=$3" \
    "" "[WireGuardPeer]" "PublicKey=$2" "AllowedIPs=$WG_DESKTOP_IP/32" "Endpoint=$1:$WG_PORT"
}

# install_wireguard SITE SRC: networkd's wg0 next to NetworkManager. Installs the static pieces always; the .netdev
# and systemd-networkd only once the Pi's key (gen-wg-key.sh) and the desktop's public key exist.
install_wireguard() {
  local site="$1" src="$2" ip mark wgip pub="/etc/atlas/wireguard-desktop.pub" key
  install_file "$src/wg0.network" /etc/systemd/network/50-atlas-wg0.network 644
  if [ -d /etc/NetworkManager ]; then
    install_file "$src/nm-unmanaged-wg0.conf" /etc/NetworkManager/conf.d/50-atlas-wg0-unmanaged.conf 644
  fi
  ip="$(site_define DESKTOP_LAN_IP "$site")"
  mark="$(site_define WG_FWMARK "$site")"
  wgip="$(site_define DESKTOP_WG_IP "$site")"
  [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "$site: DESKTOP_LAN_IP must be a plain IPv4 address"
  [[ "$mark" =~ ^0x[0-9a-fA-F]{1,8}$ ]] || die "$site: WG_FWMARK must be hexadecimal, such as 0x5182"
  [ "$wgip" = "$WG_DESKTOP_IP" ] || die "$site: DESKTOP_WG_IP must be $WG_DESKTOP_IP (contracts/services.md section 2)"
  if [ ! -f "$WG_CRED_DIR/$WG_CRED_NAME" ] || [ ! -f "$pub" ]; then
    log "note    wg0 not configured yet: needs $WG_CRED_DIR/$WG_CRED_NAME (gen-wg-key.sh) and $pub (runbook wireguard.md)"
    return 0
  fi
  case "$ip" in 192.0.2.*) die "$site: DESKTOP_LAN_IP is still the documentation placeholder" ;; esac
  key="$(tr -d '[:space:]' <"$pub")"
  wg_key_ok "$key" || die "$pub does not hold a WireGuard public key (44 characters of base64)"
  write_file /etc/systemd/network/50-atlas-wg0.netdev 644 "$(wg_netdev_content "$ip" "$key" "$mark")"
  log "wg0     $WG_PI_IP <-> $WG_DESKTOP_IP (desktop endpoint $ip:$WG_PORT, mark $mark)"
  # networkd's wait-online would wait for a link it doesn't manage at boot; NetworkManager's provides network-online.
  run systemctl enable systemd-networkd.service
  run systemctl disable systemd-networkd-wait-online.service
  run systemctl restart systemd-networkd.service
}

# service_list FILE: names of the Pi services in services.conf (comments and blanks skipped).
service_list() {
  awk '!/^[[:space:]]*(#|$)/ { print $1 }' "$1"
}

# enabled_services FILE: names whose second column is "yes".
enabled_services() {
  awk '!/^[[:space:]]*(#|$)/ && $2 == "yes" { print $1 }' "$1"
}
