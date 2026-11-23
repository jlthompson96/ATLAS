#!/usr/bin/env bash
# Deploy the committed code to the Pi over SSH, from the admin device (the development Mac). PLAN 7.0 / services.md section 6.
#
#   PI_HOST=atlas-pi.example.org PI_USER=admin deploy/provisioning/deploy.sh            # build, upload, activate
#   PI_HOST=... PI_USER=... deploy/provisioning/deploy.sh --dry-run                      # build the tarball, print the plan
#   PI_HOST=... PI_USER=... deploy/provisioning/deploy.sh rollback                       # previous release
#   PI_HOST=... PI_USER=... deploy/provisioning/deploy.sh list
#
# What ships is `git archive HEAD` of backend/atlas, contracts, the per-service locks and services.conf (never
# uncommitted files, tests, virtualenvs or secrets), the HUD built from `git archive HEAD` of frontend/hud (hud/; no
# Node on the Pi), plus the hash-verified aarch64 wheels for each lock so the Pi installs offline. A new release directory is unpacked beside the others, then the `current` symlink is switched
# and the enabled units restarted by `atlas-release` on the Pi. The last 3 releases are kept. A failed health check
# switches back automatically. A deploy that changes no dependency writes about 7 MiB to the microSD; venvs whose lock
# changed are rebuilt (known issue R1, PLAN 3.5).
#
# Placeholders only: PI_HOST has no default and the placeholder host is refused for a real run.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"

action=deploy
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    rollback | list) action="$arg" ;;
    -h | --help) sed -n '2,16p' "$0"; exit 0 ;;
    *) die "unknown argument: $arg" ;;
  esac
done

PI_HOST="${PI_HOST:-}"
PI_USER="${PI_USER:-}"
if [ -z "$PI_HOST" ] || [ -z "$PI_USER" ]; then
  die "set PI_HOST and PI_USER (e.g. PI_HOST=atlas-pi.example.org PI_USER=admin)"
fi
if [ "$DRY_RUN" != "1" ]; then
  case "$PI_HOST" in *.example.org | *.example.com | *.example.net) die "PI_HOST is still a placeholder" ;; esac
fi
ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10)
remote() { run ssh "${ssh_opts[@]}" "$PI_USER@$PI_HOST" "$@"; }
release_cmd=(sudo /usr/local/sbin/atlas-release)

case "$action" in
  list) remote "${release_cmd[@]}" list; exit 0 ;;
  rollback) remote "${release_cmd[@]}" rollback; exit 0 ;;
esac

cd "$repo"
git diff --quiet HEAD -- backend contracts deploy frontend/hud 2>/dev/null || log "warning: uncommitted changes are NOT deployed (git archive HEAD)"
commit="$(git rev-parse --short=12 HEAD)"
version="$(date -u +%Y%m%dT%H%M%SZ)-$commit"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
stage="$work/stage"
mkdir -p "$stage"

log "== building release $version"
locks=() # the per-service runtime locks; the dev lock is resolved for the Mac and never ships
while IFS= read -r lock; do
  if [ "$lock" != backend/requirements-dev.lock ]; then
    locks+=("$lock")
  fi
done < <(git ls-files 'backend/requirements-*.lock')
# ${locks[@]+...}: an empty array is "unbound" under set -u in the Mac's bash 3.2
git archive --format=tar HEAD backend/atlas backend/pyproject.toml contracts deploy/provisioning/services.conf \
  ${locks[@]+"${locks[@]}"} | tar -x -C "$stage"
# Never ship tests or caches (git archive of backend/atlas contains none, but be explicit), nor the dev-only mock event
# server ("Never deployed", contracts/services.md section 2).
find "$stage" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$stage/backend/atlas/ui_server/mock"

# The HUD, built from the committed frontend/hud only (services.md section 6): a clean copy, `npm ci --ignore-scripts`
# against package-lock.json's integrity hashes (threat T6), `npm run build`. The working tree's node_modules and dist/
# are never shipped.
if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] would build the HUD from HEAD into hud/"
else
  command -v npm >/dev/null || die "npm is needed to build the HUD"
  git archive --format=tar HEAD frontend/hud | tar -x -C "$work"
  # A clean environment: Vite builds any VITE_* variable from the shell into the bundle (review nit).
  (cd "$work/frontend/hud" && env -i PATH="$PATH" HOME="$HOME" npm ci --ignore-scripts --prefer-offline --no-audit --no-fund >/dev/null &&
    env -i PATH="$PATH" HOME="$HOME" npm run -s build >/dev/null) ||
    die "building the HUD failed"
  [ -f "$work/frontend/hud/dist/index.html" ] || die "the HUD build produced no index.html"
  cp -R "$work/frontend/hud/dist" "$stage/hud"
  rm -rf "$work/frontend"
  log "hud: $(find "$stage/hud" -type f | wc -l | tr -d ' ') files"
fi

# Wheels for each per-service lock: downloaded on this Mac, hash-checked against the lock, installed offline on the Pi.
# Left out for a service whose venv the Pi's active release can reuse (same lock; known issue R1): the Pi answers
# `atlas-release locks` with "<service> <lock sha256>" lines. An older atlas-release prints its usage instead, which
# matches nothing, so every wheel is sent.
pip=python3
[ ! -x "$repo/backend/.venv/bin/python" ] || pip="$repo/backend/.venv/bin/python"
reusable=""
if [ "$DRY_RUN" != "1" ]; then
  reusable="$(ssh "${ssh_opts[@]}" "$PI_USER@$PI_HOST" "${release_cmd[@]}" locks 2>/dev/null | grep -E '^[a-z]+ [0-9a-f]{64}$' || true)"
fi
for lock in "$stage"/backend/requirements-*.lock; do
  [ -e "$lock" ] || continue
  short="$(basename "$lock" .lock)"
  short="${short#requirements-}"
  if printf '%s\n' "$reusable" | grep -Fqx "$short $(shasum -a 256 "$lock" | cut -d' ' -f1)"; then
    log "wheels: $short left out (the Pi's active release has a venv for this exact lock)"
    continue
  fi
  mkdir -p "$stage/wheels/$short"
  if [ "$DRY_RUN" = "1" ]; then
    log "[dry-run] would download aarch64 wheels for $short into the tarball"
  else
    "$pip" -m pip download --quiet --no-deps --only-binary=:all: --require-hashes \
      --platform manylinux2014_aarch64 --platform manylinux_2_17_aarch64 --platform manylinux_2_28_aarch64 \
      --python-version 3.13 --implementation cp --abi cp313 --abi abi3 --abi none \
      -r "$lock" -d "$stage/wheels/$short"
  fi
done

tarball="$work/$version.tar"
# macOS tar (bsdtar) would add AppleDouble `._*` entries and xattr headers (com.apple.provenance), which
# atlas-release refuses as unexpected top-level entries. Found in the ARM64 VM, 2026-11-20.
tar_opts=()
if tar --version 2>/dev/null | grep -q bsdtar; then
  tar_opts=(--no-xattrs --no-mac-metadata)
fi
COPYFILE_DISABLE=1 tar ${tar_opts[@]+"${tar_opts[@]}"} -cf "$tarball" -C "$stage" .
sha="$(shasum -a 256 "$tarball" | cut -d' ' -f1)"
log "release $version: $(du -h "$tarball" | cut -f1), sha256 $sha"

log "== uploading and activating on $PI_HOST"
run scp "${ssh_opts[@]}" "$tarball" "$PI_USER@$PI_HOST:/var/lib/atlas/incoming/$version.tar"
remote "${release_cmd[@]}" install "$version" "$sha"
remote "${release_cmd[@]}" activate "$version"
log "deployed $version. Roll back with: PI_HOST=$PI_HOST PI_USER=$PI_USER $0 rollback"
