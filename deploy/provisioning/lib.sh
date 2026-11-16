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

# service_list FILE: names of the Pi services in services.conf (comments and blanks skipped).
service_list() {
  awk '!/^[[:space:]]*(#|$)/ { print $1 }' "$1"
}

# enabled_services FILE: names whose second column is "yes".
enabled_services() {
  awk '!/^[[:space:]]*(#|$)/ && $2 == "yes" { print $1 }' "$1"
}
