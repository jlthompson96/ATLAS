#!/usr/bin/env bash
# Pair, list and revoke HUD devices on the Pi (runbook docs/runbooks/ui-on-pi.md).
#
#   sudo deploy/provisioning/ui-devices.sh pair remote     # a one-time pairing code for a browser (10 minutes, single use)
#   sudo deploy/provisioning/ui-devices.sh pair kiosk      # the same for the Pi's own screen (view-only device)
#   sudo deploy/provisioning/ui-devices.sh list
#   sudo deploy/provisioning/ui-devices.sh revoke <device-id>
#
# Runs the active release's atlas-ui code as the atlas-ui user (never as root), on atlas-ui's own state files; a running
# server notices within seconds. A pairing code is short-lived and single use, but it is what lets a browser in: run
# `pair` yourself and type the code into the browser, rather than pasting it anywhere.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"

case "${1:-}" in
  pair)
    [[ "${2:-}" =~ ^(remote|kiosk)$ ]] || die "usage: $0 pair remote|kiosk"
    args=(pair --role "$2")
    ;;
  list) args=(devices) ;;
  revoke)
    [[ "${2:-}" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || die "usage: $0 revoke <device-id> (from: $0 list)"
    args=(revoke "$2")
    ;;
  -h | --help | '') sed -n '2,12p' "$0"; exit 0 ;;
  *) die "unknown command: $1 (pair, list, revoke)" ;;
esac
need_root
release=/var/lib/atlas/current
py="$release/venv/atlas-ui/bin/python"
[ -x "$py" ] || die "no atlas-ui venv in the active release: deploy a release first (deploy.sh)"

# -I: isolated (no environment variables, no user site, not the current directory on sys.path).
# The single quotes are intended (SC2016): this is Python, not shell.
# shellcheck disable=SC2016
cli='import sys; sys.path.insert(0, sys.argv[1] + "/backend")
from atlas.ui_server.__main__ import main
sys.exit(main(sys.argv[2:]))'
cd /
exec runuser -u atlas-ui -- "$py" -I -c "$cli" "$release" "${args[@]}" --state-dir /var/lib/atlas/ui
