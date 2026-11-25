#!/usr/bin/env bash
# atlas-release: install, activate, roll back and prune ATLAS releases on the Pi. Installed as /usr/local/sbin/atlas-release
# by provision-pi.sh and run as root through one narrow sudoers entry (deploy.sh calls it over SSH).
#
#   atlas-release install  <version> <sha256>   verify and unpack /var/lib/atlas/incoming/<version>.tar into releases/
#   atlas-release activate <version>            switch the `current` symlink, restart enabled units, health-check
#   atlas-release rollback                      switch back to the previous release
#   atlas-release prune                         keep the newest 3 releases (plus current and previous)
#   atlas-release list
#   atlas-release locks                         the active release's reusable venvs, as "<service> <lock sha256>"
#
# Layout (contracts/services.md section 6): /var/lib/atlas/releases/<version>/{backend,contracts,deploy,hud,venv/<service>},
# /var/lib/atlas/current -> releases/<version>, /var/lib/atlas/previous -> the release `current` replaced.
# Releases are root-owned and read-only to every service.
#
# THREAT MODEL (review F2). The admin account can run this as root, and the tarball it uploads is attacker-influenced data.
# Root therefore never executes anything that came out of the tarball:
#  - only the top-level entries backend/ contracts/ deploy/ hud/ wheels/ are accepted (no venv/, no scripts), links and special
#    files are refused, and the extraction directory is mode 0700;
#  - the list of services comes from the root-owned /etc/atlas/services.conf, never from the tarball, and each name is
#    validated;
#  - a dependency lock may contain only `name==version \` and `--hash=sha256:<64 hex>` lines (no option lines such as
#    --index-url or --find-links), and wheels install with --require-hashes --no-index --only-binary;
#  - python and pip run as an unprivileged throwaway user (systemd-run DynamicUser, no network) with `python -I` and
#    `pip --isolated`; the venv comes back as a tar stream on a pipe, is extracted by root without its owner or modes,
#    may hold only regular files, directories and the venv's own interpreter symlinks, and is then chowned to root,
#    stripped of setuid/setgid bits and made read-only;
#  - (R1) root trusts the ACTIVE release's venvs: one is reused (hard links) only if its lock is byte-identical, it was
#    built for this /usr/bin/python3, and every file still matches the SHA-256 manifest written when it was built;
#  - one atlas-release at a time (flock), so nothing is pruned while an install links from it;
#  - arguments are validated with strict patterns because they arrive through sudo;
#  - (review N5) incoming/ is admin-writable, so the uploaded file is first copied (size-bounded) into a root-only work
#    directory in RAM (/run, R1), and the checksum, the entry checks and the extraction all read that copy; the top-level
#    allowlist is checked again on the extracted tree; every failure path removes the work and build-input directories.
# No network access is needed or used: Python dependencies come from wheels shipped in the tarball.
set -euo pipefail

BASE=/var/lib/atlas
KEEP=3
HEALTH_TIMEOUT=30
HEALTH_STABLE=10 # seconds every unit must stay up, unrestarted, after becoming active (V5)
VERSION_RE='^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{7,40}$'
SERVICE_RE='^atlas-[a-z]+$'
CONF=/etc/atlas/services.conf
ALLOWED_TOP="backend contracts deploy hud wheels" # hud/: the built HUD, static files only (services.md 1.5.0)
# Card writes (known issue R1): everything between the upload and the finished release happens in RAM. The tarball
# copy, the extracted tree and the venv builds live under SCRATCH (tmpfs); the release is written to the card once.
SCRATCH=/run
SCRATCH_MARGIN_KB=$((200 * 1024))        # RAM for the extracted tree and the venv builds, besides 2x the tarball
MAX_TARBALL_BYTES=$((128 * 1024 * 1024)) # a release is 1-60 MB; bounds the copy out of incoming/ (and RAM use)
LOCK_FILE=/run/lock/atlas-release
PY3=/usr/bin/python3 # the one interpreter: the builder's venvs, the version checks, compileall
LEGACY_BUILD="/var/lib/atlas-build /var/lib/private/atlas-build" # the builder's state directories before R1

# What a failed install must not leave behind (review N5); removed by the EXIT trap that cmd_install sets.
WORK=""     # root-only (0700) directory under SCRATCH: the tarball copy and the extracted tree
INPUTS=""   # world-readable build inputs for the unprivileged builder (under SCRATCH)
STAGE=""    # root-only hidden directory under releases/: the release being written to the card, renamed when complete

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
  if [ -n "$WORK" ]; then rm -rf "$WORK"; fi
  if [ -n "$STAGE" ]; then rm -rf "$STAGE"; fi
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

switch_symlink() { # switch_symlink NAME VERSION: atomic replace, on the card before and after (power cuts)
  local name="$1" version="$2"
  sync -f "$BASE/releases/$version"
  ln -sfn "releases/$version" "$BASE/.$name.new"
  mv -T "$BASE/.$name.new" "$BASE/$name"
  sync -f "$BASE"
}

# health_check: every enabled unit becomes active within HEALTH_TIMEOUT, then stays active with no restart for
# HEALTH_STABLE seconds. "Active once" isn't enough: a unit that crashes on start is briefly active between
# Restart= attempts (known issue V5, seen in the ARM64 VM).
health_check() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) name before="" after=""
  for name in $(enabled_units); do
    while ! systemctl is-active --quiet "$name.service"; do
      [ "$SECONDS" -lt "$deadline" ] || { log "unhealthy: $name"; return 1; }
      sleep 1
    done
    before+="$name=$(systemctl show -p NRestarts --value "$name.service") "
  done
  sleep "$HEALTH_STABLE"
  for name in $(enabled_units); do
    if ! systemctl is-active --quiet "$name.service"; then
      log "unhealthy: $name stopped within ${HEALTH_STABLE}s"
      return 1
    fi
    after+="$name=$(systemctl show -p NRestarts --value "$name.service") "
  done
  if [ "$after" != "$before" ]; then
    log "unhealthy: a unit restarted within ${HEALTH_STABLE}s (restart counts before: ${before% }; after: ${after% })"
    return 1
  fi
}

restart_units() {
  local name
  for name in $(enabled_units); do
    systemctl restart "$name.service"
  done
}

# build_venv NAME LOCK WHEELS DEST: create the venv and install the locked wheels as an unprivileged user, in RAM
# (RuntimeDirectory, tmpfs, removed by systemd when the builder exits). pip itself is removed afterwards unless the lock
# pins it: the services never need it, and it is ~13 MB a venv. The venv comes back as a tar stream on stdout (the
# builder's own output goes to stderr), so no file is left behind under a dynamic UID that systemd may hand out again.
# Root extracts it without owners or modes and accepts only regular files (no hard links), directories, and the
# interpreter symlinks a venv has (bin/python* -> python3 or /usr/bin/python3, lib64 -> lib).
build_venv() {
  local name="$1" lock="$2" wheels="$3" dest="$4" target
  command -v systemd-run >/dev/null || die "systemd-run is required to build as an unprivileged user"
  install -d -m 700 "$dest"
  # The single quotes are intended (SC2016): $RUNTIME_DIRECTORY, $1 and $2 expand in the unprivileged inner shell.
  # shellcheck disable=SC2016
  systemd-run --quiet --pipe --wait --collect \
    -p DynamicUser=yes -p RuntimeDirectory="atlas-build/$name" \
    -p PrivateTmp=yes -p PrivateNetwork=yes -p NoNewPrivileges=yes -p ProtectSystem=strict -p ProtectHome=yes \
    /bin/sh -c 'set -eu; exec 3>&1 1>&2; v="$RUNTIME_DIRECTORY/venv"; /usr/bin/python3 -I -m venv "$v"
      "$v/bin/python" -I -m pip --isolated install --quiet --no-cache-dir --no-index --find-links "$1" \
        --require-hashes --only-binary=:all: --no-deps -r "$2"
      grep -q "^pip==" "$2" || "$v/bin/python" -I -m pip --isolated uninstall --quiet --yes --no-cache-dir pip
      tar -C "$v" -cf - . >&3' sh "$wheels" "$lock" </dev/null |
    tar -x -C "$dest" --no-same-owner --no-same-permissions --no-overwrite-dir ||
    die "building the venv for $name failed"
  rmdir /run/atlas-build 2>/dev/null || true # systemd removed atlas-build/<name>, but not the parent it created
  if [ -n "$(find "$dest" \( -type b -o -type c -o -type p -o -type s \) -print -quit)" ]; then
    die "the venv for $name contains special files"
  fi
  [ -z "$(find "$dest" -type f -links +1 -print -quit)" ] || die "the venv for $name contains hard links"
  while IFS= read -r target; do
    [[ "$target" =~ ^(/usr/bin/python3(\.[0-9]+)?|python3?(\.[0-9]+)?|lib)$ ]] ||
      die "the venv for $name has an unexpected symlink (-> $target)"
  done < <(find "$dest" -type l -printf '%l\n')
  [ -f "$dest/pyvenv.cfg" ] || die "the venv for $name is incomplete"
}

# venv_manifest DIR: "<sha256>  ./<path>" for every regular file under DIR, in a fixed order.
venv_manifest() {
  (cd "$1" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 -r sha256sum)
}

# venv_intact DIR MANIFEST: DIR holds exactly the files MANIFEST lists, with exactly those contents (review of R1, item 1:
# a venv shared by hard links must not carry corruption or a cut-short copy from release to release). Reads, no writes.
venv_intact() {
  [ -f "$2" ] && [ "$(venv_manifest "$1")" = "$(cat "$2")" ]
}

# active_release: the active release's directory, resolved once (so a check and a copy see the same release), or nothing.
# Only a real release directory counts: releases/<version>.
active_release() {
  local cur
  [ -L "$BASE/current" ] || return 0
  cur="$(readlink -f "$BASE/current")"
  if [ "$cur" = "$(readlink -f "$BASE/releases")/${cur##*/}" ] && [[ "${cur##*/}" =~ $VERSION_RE ]] && [ -d "$cur" ]; then
    printf '%s\n' "$cur"
  fi
}

# reusable_venv NAME RELEASE: RELEASE's venv for NAME can be reused: it was built by this tool from a lock (it has a
# manifest and no pip, so venvs from before R1 are rebuilt once), for the /usr/bin/python3 that runs now, and every file
# still matches its manifest. RELEASE is the root-owned active release, never anything from the tarball.
reusable_venv() {
  local short="${1#atlas-}" cur="$2" venv want
  venv="$cur/venv/$1"
  [ -n "$cur" ] && [ -f "$cur/backend/requirements-$short.lock" ] || return 1
  [ -d "$venv" ] && [ ! -L "$venv" ] && [ -f "$venv/pyvenv.cfg" ] || return 1
  [ ! -e "$venv/bin/pip" ] || return 1
  want="$("$PY3" -I -c 'import platform; print(platform.python_version())' 2>/dev/null)" || return 1
  [ -n "$want" ] && [ "$(sed -n 's/^version = //p' "$venv/pyvenv.cfg")" = "$want" ] || return 1
  venv_intact "$venv" "$cur/venv/$1.sha256"
}

# cmd_locks: "<service-short-name> <sha256>" for each reusable venv of the active release. deploy.sh leaves those
# services' wheels out of the tarball; install reuses the venv only if the new lock is byte-identical, and otherwise
# refuses for lack of wheels (the active release changed in between: deploy again). Read-only.
cmd_locks() {
  local name short active
  active="$(active_release)"
  for name in $(service_names); do
    short="${name#atlas-}"
    if reusable_venv "$name" "$active"; then
      printf '%s %s\n' "$short" "$(sha256sum <"$active/backend/requirements-$short.lock" | cut -d' ' -f1)"
    fi
  done
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
  local avail need upload
  upload="$(stat -c %s "$incoming")"
  [ "$upload" -le "$MAX_TARBALL_BYTES" ] || die "tarball is larger than $MAX_TARBALL_BYTES bytes"
  need=$((2 * upload / 1024 + SCRATCH_MARGIN_KB)) # the copy, its extraction, and the venv builds
  avail="$(df -Pk "$SCRATCH" | awk 'NR == 2 { print $4 }')"
  [ "${avail:-0}" -ge "$need" ] || die "less than $((need / 1024)) MB free in $SCRATCH (RAM) to build the release"
  WORK="$(mktemp -d "$SCRATCH/atlas-release.XXXXXX")" # 0700, root-owned, in RAM
  tarball="$WORK/release.tar"
  partial="$WORK/tree"
  head -c "$((MAX_TARBALL_BYTES + 1))" <"$incoming" >"$tarball"
  size="$(($(wc -c <"$tarball")))"
  [ "$size" -le "$MAX_TARBALL_BYTES" ] || die "tarball is larger than $MAX_TARBALL_BYTES bytes"
  echo "$sha  $tarball" | sha256sum --check --status || die "sha256 mismatch: refusing to unpack"

  check_tar_entries "$tarball"
  "$PY3" -I -c 'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)' || die "$PY3 is not 3.13 (the locks are resolved for CPython 3.13)"
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
  local name short lock reuse=() active
  active="$(active_release)"
  for name in $(service_names); do
    short="${name#atlas-}"
    lock="$partial/backend/requirements-$short.lock"
    if [ -f "$lock" ]; then
      validate_lock "$lock"
      if reusable_venv "$name" "$active" && cmp -s "$lock" "$active/backend/requirements-$short.lock"; then
        log "reuse   venv/$name (lock unchanged: hard links to the active release)"
        reuse+=("$name")
        continue
      fi
      [ -d "$partial/wheels/$short" ] || die "no wheels for $name (if the active release changed since deploy.sh asked: deploy again)"
      # The unprivileged builder must be able to read its inputs; the release tree itself stays 0700 root.
      INPUTS="$(mktemp -d "$SCRATCH/atlas-release-in.XXXXXX")"
      cp -R "$partial/wheels/$short" "$INPUTS/wheels"
      cp "$lock" "$INPUTS/lock"
      chmod -R a+rX "$INPUTS"
      build_venv "$name" "$INPUTS/lock" "$INPUTS/wheels" "$partial/venv/$name"
      venv_manifest "$partial/venv/$name" >"$partial/venv/$name.sha256" # what a later release checks before reusing it
      rm -rf "$INPUTS"
      INPUTS=""
    else
      "$PY3" -I -m venv --without-pip "$partial/venv/$name"
    fi
  done
  rm -rf "$partial/wheels"
  # Byte-code is compiled now, as root, because the release is read-only to the services.
  "$PY3" -I -m compileall -q -j 0 "$partial/backend/atlas"
  # chown/chmod -R stay BEFORE the reused venvs are linked in below: after, they would touch the active release's files.
  chown -R root:root "$partial"
  find "$partial" -perm /6000 -exec chmod ug-s {} +
  chmod -R u=rwX,go=rX "$partial"
  [ -z "$(find "$partial" -perm /6000 -print -quit)" ] || die "setuid or setgid bit survived"
  # The one write to the card: a hidden root-only directory beside the releases, then an atomic rename. Reused venvs
  # are hard links into the active release (no data written); both are root-owned and read-only to every service.
  STAGE="$(mktemp -d "$BASE/releases/.inst.XXXXXX")" # root-owned, hidden from release_versions; 0755 after the copy
  cp -a "$partial/." "$STAGE/"
  rm -rf "$WORK"
  WORK=""
  for name in ${reuse[@]+"${reuse[@]}"}; do
    cp -al "$active/venv/$name" "$STAGE/venv/$name"
    cp "$active/venv/$name.sha256" "$STAGE/venv/$name.sha256"
    venv_intact "$STAGE/venv/$name" "$STAGE/venv/$name.sha256" || die "the reused venv for $name is incomplete after linking"
  done
  [ -z "$(find "$STAGE" -perm /6000 -print -quit)" ] || die "setuid or setgid bit survived"
  sync -f "$STAGE" # on the card before it gets its name (power cuts: no zero-length files in an active release)
  mv -T "$STAGE" "$dest"
  STAGE=""
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
  local keep_cur keep_prev leftover
  # What an interrupted install left on the card (power cut, SIGKILL), and the builder's state from before R1. Safe: the
  # flock in main means no install is running.
  for leftover in "$BASE"/releases/.inst.* "$BASE"/releases/.work.* "$BASE"/releases/.in.* $LEGACY_BUILD; do
    if [ -e "$leftover" ] || [ -L "$leftover" ]; then
      log "prune   leftover $leftover"
      rm -rf "$leftover" 2>/dev/null || log "warning: could not remove $leftover"
    fi
  done
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

# one_at_a_time: no two atlas-release runs that change releases overlap (review of R1, item 2: an install links from
# the active release, which a concurrent activate could prune; two builds would share a RuntimeDirectory).
one_at_a_time() {
  exec 9>"$LOCK_FILE"
  flock -n 9 || die "another atlas-release is running; try again when it has finished"
}

main() {
  [ "$(id -u)" -eq 0 ] || die "must run as root"
  case "${1:-}" in
    install | activate | rollback | prune) one_at_a_time ;;
  esac
  case "${1:-}" in
    install) shift; cmd_install "$@" ;;
    activate) shift; cmd_activate "$@" ;;
    rollback) cmd_rollback ;;
    prune) cmd_prune ;;
    list) cmd_list ;;
    locks) cmd_locks ;;
    *) sed -n '2,13p' "$0"; exit 2 ;;
  esac
}

# Sourced by the tests (functions only); executed normally otherwise.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
