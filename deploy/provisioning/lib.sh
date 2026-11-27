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

# --- atlas-core's link to the desktop (runbook core-link.md, ADR 0004) -----------------------------------------------
# The owner edits CORE_LINK_CONF; provision-pi.sh checks it and writes the flags into a drop-in (Environment=), so the
# unit needs no EnvironmentFile= (services take secrets from credentials only). Only these flags may be set: the URLs
# stay on 10.77.0.2 (their defaults), and the token file, sockets and directories keep the unit's values.
CORE_LINK_CONF=/etc/atlas/core-link.conf
CORE_LINK_DROPIN=/etc/systemd/system/atlas-core.service.d/link.conf
CORE_LINK_FLAGS=(--backend --model --reasoning-effort --no-hostd --mac --wol-broadcast --wol-port --model-vram-mb
  --idle-sleep)

# core_link_conf_template: the file provision-pi.sh creates once, everything commented out (core stays unlinked).
core_link_conf_template() {
  printf '%s\n' \
    "# atlas-core's link to the LLM desktop (runbook core-link.md, ADR 0004). Created by provision-pi.sh, never" \
    "# overwritten. ATLAS_CORE_ARGS is appended to atlas-core's command line; commented out or empty, core runs" \
    "# unlinked (--backend none). Allowed flags: ${CORE_LINK_FLAGS[*]}." \
    "# Not read by systemd: after a change, rerun provision-pi.sh (it checks this file and writes $CORE_LINK_DROPIN)," \
    "# then sudo systemctl restart atlas-core." \
    "#" \
    "# LM Studio only (before atlas-hostd runs on the desktop):" \
    "# ATLAS_CORE_ARGS=\"--backend lmstudio --model google/gemma-4-e4b --reasoning-effort none --no-hostd\"" \
    "# With atlas-hostd and Wake-on-LAN (the desktop's wired MAC; --model-vram-mb from B6):" \
    "# ATLAS_CORE_ARGS=\"--backend lmstudio --model google/gemma-4-e4b --reasoning-effort none --mac aa:bb:cc:dd:ee:ff\""
}

# _core_link_words FILE: the ATLAS_CORE_ARGS words, one per line; on a problem, "!" and the reason, and status 1.
# Accepted: comments, blank lines and at most one ATLAS_CORE_ARGS= line (optionally in double quotes) holding no quote,
# backslash, $, backtick or % (a systemd specifier in Environment=), whose --flags are all in CORE_LINK_FLAGS.
_core_link_words() {
  local line value word name seen=0 f ok words
  # In variables: a regex written inline in [[ =~ ]] with quotes and backslashes parses differently across bash versions.
  local blank='^[[:space:]]*(#|$)' quoted='^ATLAS_CORE_ARGS="([^"\\$`%]*)"$' bare='^ATLAS_CORE_ARGS=([^"'\''\\$`%]*)$'
  while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ $blank ]] && continue
    if [[ "$line" =~ $quoted ]] || [[ "$line" =~ $bare ]]; then
      value="${BASH_REMATCH[1]}"
    else
      echo "!only comments and one ATLAS_CORE_ARGS=\"...\" line are allowed (no quotes, \\, \$, \` or % inside)"
      return 1
    fi
    seen=$((seen + 1))
    [ "$seen" -eq 1 ] || { echo "!ATLAS_CORE_ARGS is set more than once"; return 1; }
    read -ra words <<<"$value" # split at whitespace like systemd, without glob expansion
    for word in ${words[@]+"${words[@]}"}; do # (bash < 4.4 calls an empty array unbound under set -u)
      if [[ "$word" == --* ]]; then
        name="${word%%=*}"
        ok=0
        for f in "${CORE_LINK_FLAGS[@]}"; do [ "$name" = "$f" ] && ok=1; done
        [ "$ok" = 1 ] || { echo "!$name is not an allowed flag (allowed: ${CORE_LINK_FLAGS[*]})"; return 1; }
      fi
      printf '%s\n' "$word"
    done
  done <"$1"
}

# core_link_problem FILE: why FILE isn't an acceptable core-link.conf; nothing when it is.
core_link_problem() {
  local out
  if out="$(_core_link_words "$1")"; then return 0; fi
  printf '%s\n' "${out##*!}"
}

# core_link_dropin FILE: the drop-in for an accepted FILE (check it with core_link_problem first).
core_link_dropin() {
  local args
  args="$(_core_link_words "$1" | paste -sd' ' -)"
  printf '%s\n' \
    "# ATLAS: atlas-core's link flags. Generated by provision-pi.sh from $CORE_LINK_CONF; edit that and rerun." \
    "[Service]" "Environment=\"ATLAS_CORE_ARGS=$args\""
}

# service_list FILE: names of the Pi services in services.conf (comments and blanks skipped).
service_list() {
  awk '!/^[[:space:]]*(#|$)/ { print $1 }' "$1"
}

# enabled_services FILE: names whose second column is "yes".
enabled_services() {
  awk '!/^[[:space:]]*(#|$)/ && $2 == "yes" { print $1 }' "$1"
}

# --- consoles, overlay and credentials ------------------------------------------------------------------------------

# ancestor_comms: the command names of this script's ancestor processes, one per line, read from /proc (never from the
# environment, which sudo resets: $TMUX and $STY are gone under sudo).
ancestor_comms() {
  local pid=$$ stat ppid
  while [ "$pid" -gt 1 ]; do
    cat "/proc/$pid/comm" 2>/dev/null || return 0
    stat="$(cat "/proc/$pid/stat" 2>/dev/null)" || return 0
    read -r _ ppid _ <<<"${stat##*) }" # after the "(comm)" field, which may contain spaces ("tmux: server")
    pid="$ppid"
  done
}

# in_shared_terminal: true under tmux or screen, whose panes can be read from another session (Claude Code reads the
# shared tmux pane on the Pi).
in_shared_terminal() {
  ancestor_comms | grep -qE '^(tmux|screen)'
}

# need_local_console: refuse unless this runs at the Pi's own keyboard and screen. For secrets a person chooses or must
# write down (backup password, approval PIN): a Claude Code session can read a tmux pane, and SSH sessions can be logged.
# Checked through what the kernel and logind report, because under sudo (use_pty) `tty` names sudo's own pty and the
# environment is reset (security review of feat/ui-on-pi, item 1):
#  - stdin and stdout are the same terminal (no redirection, no pipes);
#  - no tmux, screen or sshd among the ancestor processes;
#  - the login session (systemd's session-N.scope, which sudo keeps) is local, on a virtual console: TTY=ttyN, Remote=no.
need_local_console() {
  local session props
  if ! { [ -t 0 ] && [ -t 1 ] && [ "/proc/$$/fd/0" -ef "/proc/$$/fd/1" ]; }; then
    die "run this on the Pi's own console: stdin and stdout must be the terminal (no redirection or pipes)"
  fi
  if ancestor_comms | grep -qE '^(tmux|screen|sshd)'; then
    die "run this on the Pi's own console, not over SSH or in tmux or screen (their screens can be read from elsewhere)"
  fi
  session="$(sed -n 's#.*/session-\([^/]*\)\.scope$#\1#p' /proc/self/cgroup | head -1)"
  [ -n "$session" ] || die "run this on the Pi's own console: this process is not in a login session"
  props="$(loginctl show-session "$session" -p Remote -p TTY 2>/dev/null | sort | tr '\n' ' ')"
  [[ "$props" =~ ^Remote=no\ TTY=tty[0-9]+\ $ ]] ||
    die "run this on the Pi's own console (this login session: ${props:-unknown}), not over SSH or in tmux"
}

# root_is_overlay: true while the read-only root overlay is on; anything written to /etc then vanishes at the next boot.
root_is_overlay() {
  [ "$(findmnt -n -o FSTYPE / 2>/dev/null)" = overlay ]
}

# missing_credentials UNIT_FILE: the LoadCredentialEncrypted= files that don't exist, one per line. A credential with a
# SetCredential= default is optional (atlas-core's LM Studio token before the desktop is linked; tests allow only a
# blank default).
missing_credentials() {
  local unit="$1" line id path
  while IFS= read -r line; do
    id="${line#LoadCredentialEncrypted=}"
    path="${id#*:}"
    id="${id%%:*}"
    if grep -q "^SetCredential=$id:" "$unit"; then
      continue
    fi
    [ -f "$path" ] || printf '%s\n' "$path"
  done < <(grep '^LoadCredentialEncrypted=' "$unit")
}

# --- atlas-ui (Phase 2; contracts/services.md sections 2 and 4) ------------------------------------------------------
# Used by provision-pi.sh, gen-ui-keys.sh and gen-ui-pin.sh, which source this file.
# shellcheck disable=SC2034
UI_TLS_DIR=/etc/atlas/tls # ca.crt and atlas-ui.crt: public; the CA keeps no private key
# shellcheck disable=SC2034
UI_CRED_DIR=/var/lib/atlas/credentials/atlas-ui
# shellcheck disable=SC2034
UI_CERT_DAYS=800 # Apple accepts at most 825 days for a TLS server certificate

# ui_python ARGS...: run the active release's atlas-ui Python as atlas-ui in a sandbox like atlas-ui.service's, never as
# root and never on the caller's terminal (review item 2): the caller must give it pipes or files for stdin, stdout and
# stderr, so release code can't push keystrokes into an admin's shell (TIOCSTI) or outlive the command. Extra -p
# properties (ReadWritePaths=) go first.
ui_python() {
  systemd-run --quiet --pipe --wait --collect --uid=atlas-ui --gid=atlas-ui \
    -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes -p PrivateDevices=yes \
    -p PrivateNetwork=yes -p RestrictAddressFamilies=AF_UNIX -p ProtectKernelTunables=yes -p ProtectKernelModules=yes \
    -p ProtectKernelLogs=yes -p ProtectControlGroups=yes -p ProtectClock=yes -p ProtectHostname=yes \
    -p RestrictNamespaces=yes -p RestrictSUIDSGID=yes -p RestrictRealtime=yes -p LockPersonality=yes \
    -p MemoryDenyWriteExecute=yes -p SystemCallFilter=@system-service -p SystemCallArchitectures=native \
    -p CapabilityBoundingSet= -p ProtectProc=invisible -p ProcSubset=pid -p UMask=0077 -p WorkingDirectory=/ \
    -p RuntimeMaxSec=60 "$@"
}

# pi_lan_ip SITE: the Pi's LAN address (`define PI_LAN_IP` in the nftables site file); nothing if unset or still the
# documentation placeholder. atlas-ui binds it and its certificate names it.
pi_lan_ip() {
  local ip
  ip="$(site_define PI_LAN_IP "$1")"
  case "$ip" in
    '' | 192.0.2.*) return 0 ;;
    0.* | 127.* | 169.254.* | 224.* | 255.*) die "$1: PI_LAN_IP must be the Pi's own LAN address, not $ip" ;;
  esac
  [[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "$1: PI_LAN_IP must be a plain IPv4 address"
  printf '%s\n' "$ip"
}
