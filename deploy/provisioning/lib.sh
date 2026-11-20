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
    local advice="disable it (systemctl disable --now systemd-resolved) and point /etc/resolv.conf at LAN_DNS from $site (docs/runbooks/provisioning.md)"
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

# service_list FILE: names of the Pi services in services.conf (comments and blanks skipped).
service_list() {
  awk '!/^[[:space:]]*(#|$)/ { print $1 }' "$1"
}

# enabled_services FILE: names whose second column is "yes".
enabled_services() {
  awk '!/^[[:space:]]*(#|$)/ && $2 == "yes" { print $1 }' "$1"
}
