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
# Layout (contracts/services.md section 6): /var/lib/atlas/releases/<version>/{backend,contracts,deploy,venv/<service>},
# /var/lib/atlas/current -> releases/<version>, /var/lib/atlas/previous -> the release `current` replaced.
# Releases are root-owned and read-only to every service.
#
# THREAT MODEL (review F2). The admin account can run this as root, and the tarball it uploads is attacker-influenced data.
# Root therefore never executes anything that came out of the tarball:
#  - only the top-level entries backend/ contracts/ deploy/ wheels/ are accepted (no venv/, no scripts), links and special
#    files are refused, and the extraction directory is mode 0700;
#  - the list of services comes from the root-owned /etc/atlas/services.conf, never from the tarball, and each name is
#    validated;
#  - a dependency lock may contain only `name==version \` and `--hash=sha256:<64 hex>` lines (no option lines such as
#    --index-url or --find-links), and wheels install with --require-hashes --no-index --only-binary;
#  - python and pip run as an unprivileged throwaway user (systemd-run DynamicUser, no network) with `python -I` and
#    `pip --isolated`; the result is copied in, chowned to root, stripped of setuid/setgid bits and made read-only;
#  - arguments are validated with strict patterns because they arrive through sudo;
#  - (review N5) incoming/ is admin-writable, so the uploaded file is first copied (size-bounded) into a root-only work
#    directory under releases/, and the checksum, the entry checks and the extraction all read that copy; the top-level
#    allowlist is checked again on the extracted tree; every failure path removes the work and build-input directories.
# No network access is needed or used: Python dependencies come from wheels shipped in the tarball.
set -euo pipefail

BASE=/var/lib/atlas
KEEP=3
HEALTH_TIMEOUT=30
VERSION_RE='^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{7,40}$'
SERVICE_RE='^atlas-[a-z]+$'
CONF=/etc/atlas/services.conf
ALLOWED_TOP="backend contracts deploy wheels"
BUILD_STATE=/var/lib/atlas-build
BUILD_PRIVATE=/var/lib/private/atlas-build # where DynamicUser= really keeps BUILD_STATE
MAX_TARBALL_BYTES=$((512 * 1024 * 1024))   # a release is a few MB; bounds the copy out of incoming/

# What a failed install must not leave behind (review N5); removed by the EXIT trap that cmd_install sets.
WORK=""     # root-only (0700) directory under releases/: the tarball copy and the extracted tree
INPUTS=""   # world-readable build inputs for the unprivileged builder
BUILDING="" # service whose venv is being built

log() { printf '%s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

valid_version() { [[ "$1" =~ $VERSION_RE ]] || die "bad version string"; }

# check_tar_entries TARBALL: only regular files and directories, only allowlisted top-level names, no traversal.
check_tar_entries() {
  local tarball="$1" name top kinds
  kinds="$(tar -tvf "$tarball" | cut -c1 | sort -u | tr -d '\n')" || die "cannot list tarball"
  case "$kinds" in
    - | d | -d | d-) ;;
    *) die "tarball contains links, devices or other special entries" ;;
  esac
  if tar -tvf "$tarball" | grep -E ' link to |-> ' >/dev/null; then die "tarball contains links"; fi
  while IFS= read -r name; do
    name="${name#./}"
    if [ -z "$name" ] || [ "$name" = "." ]; then
      continue
    fi
    case "$name" in
      /* | ../* | */../* | */.. | ..) die "unsafe path in tarball: $name" ;;
    esac
    top="${name%%/*}"
    case " $ALLOWED_TOP " in
      *" $top "*) ;;
      *) die "unexpected top-level entry in tarball: $top (only: $ALLOWED_TOP)" ;;
    esac
  done < <(tar -tf "$tarball")
}

# check_tree_top TREE: re-check the top-level allowlist on what tar actually extracted (review N5), hidden names included.
check_tree_top() {
  local tree="$1" entry top
  for entry in "$tree"/* "$tree"/.[!.]* "$tree"/..?*; do
    if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then
      continue # an unmatched glob pattern
    fi
    top="${entry##*/}"
    case " $ALLOWED_TOP " in
      *" $top "*) ;;
      *) die "unexpected top-level entry after extraction: $top (only: $ALLOWED_TOP)" ;;
    esac
  done
}

cleanup_install() {
  if [ -n "$INPUTS" ]; then rm -rf "$INPUTS"; fi
  if [ -n "$BUILDING" ]; then rm -rf "${BUILD_STATE:?}/$BUILDING" "${BUILD_PRIVATE:?}"; fi
  if [ -n "$WORK" ]; then rm -rf "$WORK"; fi
}

# validate_lock FILE: only pins and hashes. Anything else (pip option lines, URLs, markers) is refused.
validate_lock() {
  local bad
  bad="$(grep -vE '^(#.*|[[:space:]]*|[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9.+!_-]+ \\|[[:space:]]+--hash=sha256:[0-9a-f]{64}( \\)?)$' "$1" || true)"
  [ -z "$bad" ] || die "lock file $1 contains lines other than pins and hashes"
  grep -qE -- '--hash=sha256:' "$1" || die "lock file $1 has no hashes"
}

# service_names: validated service names from the root-owned services.conf (never from the tarball).
service_names() {
  local name found=0
  if [ ! -f "$CONF" ] || [ -L "$CONF" ]; then
    die "missing $CONF"
  fi
  while read -r name _; do
    case "$name" in '' | '#'*) continue ;; esac
    [[ "$name" =~ $SERVICE_RE ]] || die "bad service name in $CONF: $name"
    echo "$name"
    found=1
  done <"$CONF"
  [ "$found" = 1 ] || die "no services in $CONF"
}

enabled_units() {
  # services.conf column 2 == yes, then only those actually enabled in systemd.
  local name enable
  while read -r name enable _; do
    case "$name" in '' | '#'*) continue ;; esac
    if [ "$enable" = yes ] && systemctl is-enabled --quiet "$name.service" 2>/dev/null; then
      echo "$name"
    fi
  done <"$CONF"
}

# release_versions: names in releases/ that are valid version strings, newest first (they sort chronologically: UTC
# timestamp first). A glob, not `ls | grep`, so an odd file name (spaces, newlines, leading dashes) is never misread.
release_versions() {
  local path v
  for path in "$BASE"/releases/*; do
    v="${path##*/}"
    if [[ "$v" =~ $VERSION_RE ]]; then
      printf '%s\n' "$v"
    fi
  done | sort -r
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

# build_venv NAME LOCK WHEELS DEST: create the venv and install the locked wheels as an unprivileged user.
build_venv() {
  local name="$1" lock="$2" wheels="$3" dest="$4"
  command -v systemd-run >/dev/null || die "systemd-run is required to build as an unprivileged user"
  BUILDING="$name"
  rm -rf "${BUILD_STATE:?}/$name"
  # The single quotes are intended (SC2016): $STATE_DIRECTORY, $1 and $2 expand in the unprivileged inner shell.
  # shellcheck disable=SC2016
  systemd-run --quiet --pipe --wait --collect \
    -p DynamicUser=yes -p StateDirectory="atlas-build/$name" -p PrivateTmp=yes -p PrivateNetwork=yes \
    -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=yes \
    /bin/sh -c 'set -eu; v="$STATE_DIRECTORY/venv"; /usr/bin/python3 -I -m venv "$v"
      "$v/bin/python" -I -m pip --isolated install --quiet --no-index --find-links "$1" \
        --require-hashes --only-binary=:all: --no-deps -r "$2"' sh "$wheels" "$lock" \
    || die "building the venv for $name failed"
  cp -a --no-preserve=ownership "$BUILD_STATE/$name/venv" "$dest"
  rm -rf "${BUILD_STATE:?}/$name" "${BUILD_PRIVATE:?}"
  BUILDING=""
}

cmd_install() {
  local version="${1:?version}" sha="${2:?sha256}"
  valid_version "$version"
  [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || die "bad sha256"
  local incoming="$BASE/incoming/$version.tar" dest="$BASE/releases/$version" tarball partial size
  if [ ! -f "$incoming" ] || [ -L "$incoming" ]; then
    die "missing $incoming"
  fi
  [ ! -e "$dest" ] || die "release $version already exists"

  # From here on, only a private copy is read (review N5): the admin could swap the file in incoming/ between steps.
  trap cleanup_install EXIT
  trap 'exit 1' HUP INT TERM
  WORK="$(mktemp -d "$BASE/releases/.work.XXXXXX")" # 0700, root-owned, hidden from release_versions
  tarball="$WORK/release.tar"
  partial="$WORK/tree"
  head -c "$((MAX_TARBALL_BYTES + 1))" <"$incoming" >"$tarball"
  size="$(($(wc -c <"$tarball")))"
  [ "$size" -le "$MAX_TARBALL_BYTES" ] || die "tarball is larger than $MAX_TARBALL_BYTES bytes"
  echo "$sha  $tarball" | sha256sum --check --status || die "sha256 mismatch: refusing to unpack"

  check_tar_entries "$tarball"
  python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)' || die "python3 is not 3.13 (the locks are resolved for CPython 3.13)"
  install -d -m 700 "$partial"
  tar -xf "$tarball" -C "$partial" --no-same-owner --no-same-permissions --no-overwrite-dir
  rm -f "$tarball"
  check_tree_top "$partial"
  if [ -n "$(find "$partial" \( -type l -o -type b -o -type c -o -type p -o -type s \) -print -quit)" ]; then
    die "tarball contains links or special files"
  fi
  [ -d "$partial/backend/atlas" ] || die "tarball has no backend/atlas"

  # One venv per service, from the root-owned service list. Dependencies only from shipped, hash-checked wheels.
  install -d -m 700 "$partial/venv"
  local name short lock
  for name in $(service_names); do
    short="${name#atlas-}"
    lock="$partial/backend/requirements-$short.lock"
    if [ -f "$lock" ]; then
      validate_lock "$lock"
      [ -d "$partial/wheels/$short" ] || die "no wheels for $name"
      # The unprivileged builder must be able to read its inputs; the release tree itself stays 0700 root.
      INPUTS="$(mktemp -d "$BASE/releases/.in.XXXXXX")"
      cp -R "$partial/wheels/$short" "$INPUTS/wheels"
      cp "$lock" "$INPUTS/lock"
      chmod -R a+rX "$INPUTS"
      build_venv "$name" "$INPUTS/lock" "$INPUTS/wheels" "$partial/venv/$name"
      rm -rf "$INPUTS"
      INPUTS=""
    else
      python3 -I -m venv --without-pip "$partial/venv/$name"
    fi
  done
  rm -rf "$partial/wheels"
  # Byte-code is compiled now, as root, because the release is read-only to the services.
  python3 -I -m compileall -q -j 0 "$partial/backend/atlas"
  chown -R root:root "$partial"
  find "$partial" -perm /6000 -exec chmod ug-s {} +
  chmod -R u=rwX,go=rX "$partial"
  [ -z "$(find "$partial" -perm /6000 -print -quit)" ] || die "setuid or setgid bit survived"
  mv "$partial" "$dest"
  rm -rf "$WORK"
  WORK=""
  trap - EXIT HUP INT TERM
  rm -f "$incoming"
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
  for v in $(release_versions); do
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
  for v in $(release_versions); do
    if [ "$v" = "$cur" ]; then
      echo "$v  <- current"
    elif [ "$v" = "$prev" ]; then
      echo "$v  (previous)"
    else
      echo "$v"
    fi
  done
}

main() {
  [ "$(id -u)" -eq 0 ] || die "must run as root"
  case "${1:-}" in
    install) shift; cmd_install "$@" ;;
    activate) shift; cmd_activate "$@" ;;
    rollback) cmd_rollback ;;
    prune) cmd_prune ;;
    list) cmd_list ;;
    *) sed -n '2,12p' "$0"; exit 2 ;;
  esac
}

# Sourced by the tests (functions only); executed normally otherwise.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
