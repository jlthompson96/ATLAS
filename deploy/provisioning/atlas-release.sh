#!/usr/bin/env bash
# atlas-release: install, activate, roll back and prune ATLAS releases on the Pi. Installed as /usr/local/sbin/atlas-release
# by provision-pi.sh and run as root through one narrow sudoers entry (deploy.sh calls it over SSH).
#
#   atlas-release install  <version> <sha256>   verify and unpack /var/lib/atlas/incoming/<version>.tar into releases/
#   atlas-release activate <version>            switch the `current` symlink, restart enabled units, health-check
#   atlas-release rollback                      switch back to the previous release
#   atlas-release prune                         keep the newest 3 releases (plus current and previous)
#   atlas-release list
#
# Layout (contracts/services.md section 6): /var/lib/atlas/releases/<version>/{backend,contracts,venv/<service>,...},
# /var/lib/atlas/current -> releases/<version>, /var/lib/atlas/previous -> the release `current` replaced.
# Releases are root-owned and read-only to every service. Arguments are untrusted (they arrive via sudo): every
# one is validated against a strict pattern before use. No network access is needed or used: Python dependencies
# come from wheels shipped in the tarball and are installed with --require-hashes --no-index.
set -euo pipefail

BASE=/var/lib/atlas
KEEP=3
HEALTH_TIMEOUT=30
VERSION_RE='^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{7,40}$'
CONF=/etc/atlas/services.conf

log() { printf '%s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

[ "$(id -u)" -eq 0 ] || die "must run as root"

valid_version() { [[ "$1" =~ $VERSION_RE ]] || die "bad version string"; }

enabled_units() {
  # services.conf column 2 == yes, then only those actually enabled in systemd.
  awk '!/^[[:space:]]*(#|$)/ && $2 == "yes" { print $1 }' "$CONF" | while read -r name; do
    if systemctl is-enabled --quiet "$name.service" 2>/dev/null; then
      echo "$name"
    fi
  done
}

current_version() {
  [ -L "$BASE/current" ] || return 0
  basename "$(readlink "$BASE/current")"
}

switch_symlink() { # switch_symlink NAME VERSION: atomic replace
  local name="$1" version="$2"
  ln -sfn "releases/$version" "$BASE/.$name.new"
  mv -T "$BASE/.$name.new" "$BASE/$name"
}

health_check() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) name
  for name in $(enabled_units); do
    while ! systemctl is-active --quiet "$name.service"; do
      [ "$SECONDS" -lt "$deadline" ] || { log "unhealthy: $name"; return 1; }
      sleep 1
    done
  done
}

restart_units() {
  local name
  for name in $(enabled_units); do
    systemctl restart "$name.service"
  done
}

cmd_install() {
  local version="${1:?version}" sha="${2:?sha256}"
  valid_version "$version"
  [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || die "bad sha256"
  local tarball="$BASE/incoming/$version.tar" dest="$BASE/releases/$version" partial="$BASE/releases/.$version.partial"
  [ -f "$tarball" ] && [ ! -L "$tarball" ] || die "missing $tarball"
  [ ! -e "$dest" ] || die "release $version already exists"
  echo "$sha  $tarball" | sha256sum --check --status || die "sha256 mismatch: refusing to unpack"

  # Refuse absolute paths, parent traversal and links before extracting anything.
  if tar -tf "$tarball" | grep -E '(^/|(^|/)\.\.(/|$))' >/dev/null; then die "unsafe path in tarball"; fi
  rm -rf "$partial"
  install -d -m 755 "$partial"
  tar -xf "$tarball" -C "$partial" --no-same-owner --no-same-permissions --no-overwrite-dir
  if [ -n "$(find "$partial" \( -type l -o -type b -o -type c -o -type p -o -type s \) -print -quit)" ]; then
    rm -rf "$partial"; die "tarball contains links or special files"
  fi
  [ -d "$partial/backend/atlas" ] || { rm -rf "$partial"; die "tarball has no backend/atlas"; }

  python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)' \
    || { rm -rf "$partial"; die "python3 is not 3.13 (the locks are resolved for CPython 3.13)"; }

  # One venv per service; dependencies only from the shipped, hash-checked wheels (offline).
  local name short lock
  for name in $(awk '!/^[[:space:]]*(#|$)/ { print $1 }' "$partial/deploy/provisioning/services.conf"); do
    short="${name#atlas-}"
    lock="$partial/backend/requirements-$short.lock"
    if [ -f "$lock" ]; then
      python3 -m venv "$partial/venv/$name"
      "$partial/venv/$name/bin/pip" install --quiet --no-index --find-links "$partial/wheels/$short" \
        --require-hashes --only-binary=:all: --no-deps -r "$lock"
    else
      python3 -m venv --without-pip "$partial/venv/$name"
    fi
  done
  rm -rf "$partial/wheels"
  # Byte-code is compiled now, as root, because the release is read-only to the services.
  python3 -m compileall -q -j 0 "$partial/backend/atlas"
  chown -R root:root "$partial"
  chmod -R u=rwX,go=rX "$partial"
  mv "$partial" "$dest"
  rm -f "$tarball"
  log "installed $version"
}

cmd_activate() {
  local version="${1:?version}" old
  valid_version "$version"
  [ -d "$BASE/releases/$version" ] || die "no such release"
  old="$(current_version)"
  if [ "$old" = "$version" ]; then
    log "already active"
  else
    [ -z "$old" ] || switch_symlink previous "$old"
    switch_symlink current "$version"
  fi
  restart_units
  if ! health_check; then
    log "health check failed after activating $version"
    if [ -n "$old" ] && [ "$old" != "$version" ]; then
      log "rolling back to $old"
      switch_symlink current "$old"
      restart_units
      health_check || log "WARNING: previous release is unhealthy too; investigate now"
    fi
    exit 1
  fi
  cmd_prune
  log "active: $version"
}

cmd_rollback() {
  [ -L "$BASE/previous" ] || die "no previous release recorded"
  local target cur
  target="$(basename "$(readlink "$BASE/previous")")"
  cur="$(current_version)"
  valid_version "$target"
  [ -d "$BASE/releases/$target" ] || die "previous release $target is gone"
  [ -z "$cur" ] || switch_symlink previous "$cur"
  switch_symlink current "$target"
  restart_units
  health_check || die "rolled back to $target but it is unhealthy"
  log "rolled back to $target"
}

cmd_prune() {
  local keep_cur keep_prev
  keep_cur="$(current_version)"
  keep_prev=""
  [ ! -L "$BASE/previous" ] || keep_prev="$(basename "$(readlink "$BASE/previous")")"
  local n=0 v
  # Version strings sort chronologically (UTC timestamp first), newest first.
  for v in $(cd "$BASE/releases" && ls -1 | grep -E "$VERSION_RE" | sort -r); do
    n=$((n + 1))
    if [ "$n" -gt "$KEEP" ] && [ "$v" != "$keep_cur" ] && [ "$v" != "$keep_prev" ]; then
      log "prune   $v"
      rm -rf "${BASE:?}/releases/$v"
    fi
  done
}

cmd_list() {
  local cur prev v
  cur="$(current_version)"
  prev=""
  [ ! -L "$BASE/previous" ] || prev="$(basename "$(readlink "$BASE/previous")")"
  for v in $(cd "$BASE/releases" && ls -1 | grep -E "$VERSION_RE" | sort -r); do
    if [ "$v" = "$cur" ]; then
      echo "$v  <- current"
    elif [ "$v" = "$prev" ]; then
      echo "$v  (previous)"
    else
      echo "$v"
    fi
  done
}

case "${1:-}" in
  install) shift; cmd_install "$@" ;;
  activate) shift; cmd_activate "$@" ;;
  rollback) cmd_rollback ;;
  prune) cmd_prune ;;
  list) cmd_list ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
