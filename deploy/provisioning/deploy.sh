#!/usr/bin/env bash
# Deploy the committed code to the Pi over SSH, from the admin device (the development Mac). PLAN 7.0 / services.md section 6.
#
#   PI_HOST=atlas-pi.example.org PI_USER=admin deploy/provisioning/deploy.sh            # build, upload, activate
#   PI_HOST=... PI_USER=... deploy/provisioning/deploy.sh --dry-run                      # build the tarball, print the plan
#   PI_HOST=... PI_USER=... deploy/provisioning/deploy.sh rollback                       # previous release
#   PI_HOST=... PI_USER=... deploy/provisioning/deploy.sh list
#
# What ships is `git archive HEAD` of backend/atlas, contracts, the per-service locks and services.conf (never
# uncommitted files, tests, virtualenvs or secrets), plus the hash-verified aarch64 wheels for each lock so the Pi
# installs offline. A new release directory is unpacked beside the others, then the `current` symlink is switched
# and the enabled units restarted by `atlas-release` on the Pi. The last 3 releases are kept. A failed health check
# switches back automatically. Each deploy writes a few MB to the microSD (PLAN 3.5).
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
[ -n "$PI_HOST" ] && [ -n "$PI_USER" ] || die "set PI_HOST and PI_USER (e.g. PI_HOST=atlas-pi.example.org PI_USER=admin)"
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
git diff --quiet HEAD -- backend contracts deploy 2>/dev/null || log "warning: uncommitted changes are NOT deployed (git archive HEAD)"
commit="$(git rev-parse --short=12 HEAD)"
version="$(date -u +%Y%m%dT%H%M%SZ)-$commit"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
stage="$work/stage"
mkdir -p "$stage"

log "== building release $version"
git archive --format=tar HEAD backend/atlas backend/pyproject.toml contracts deploy/provisioning/services.conf \
  $(git ls-files 'backend/requirements-*.lock' | grep -v 'requirements-dev.lock') | tar -x -C "$stage"
# Never ship tests or caches (git archive of backend/atlas contains none, but be explicit), nor the dev-only mock event
# server ("Never deployed", contracts/services.md section 2).
find "$stage" -name __pycache__ -prune -exec rm -rf {} +
rm -rf "$stage/backend/atlas/ui_server/mock"

# Wheels for each per-service lock: downloaded on this Mac, hash-checked against the lock, installed offline on the Pi.
pip=python3
[ ! -x "$repo/backend/.venv/bin/python" ] || pip="$repo/backend/.venv/bin/python"
for lock in "$stage"/backend/requirements-*.lock; do
  [ -e "$lock" ] || continue
  short="$(basename "$lock" .lock)"
  short="${short#requirements-}"
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
tar -cf "$tarball" -C "$stage" .
sha="$(shasum -a 256 "$tarball" | cut -d' ' -f1)"
log "release $version: $(du -h "$tarball" | cut -f1), sha256 $sha"

log "== uploading and activating on $PI_HOST"
run scp "${ssh_opts[@]}" "$tarball" "$PI_USER@$PI_HOST:/var/lib/atlas/incoming/$version.tar"
remote "${release_cmd[@]}" install "$version" "$sha"
remote "${release_cmd[@]}" activate "$version"
log "deployed $version. Roll back with: PI_HOST=$PI_HOST PI_USER=$PI_USER $0 rollback"
