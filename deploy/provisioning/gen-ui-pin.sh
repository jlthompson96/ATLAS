#!/usr/bin/env bash
# Set the step-up PIN that approvals need (PLAN 4.5; credential atlas-ui/stepup-pin-hash, an Argon2id hash).
#
#   sudo deploy/provisioning/gen-ui-pin.sh            # on the Pi's own console (keyboard + screen), never over SSH
#   sudo deploy/provisioning/gen-ui-pin.sh --change   # replace the PIN
#
# The owner types the PIN; it is never shown, never an argument and never written in the clear. A 4-12 digit PIN's
# hash can be brute-forced, so the hash is as sensitive as the PIN: it goes straight from the hasher into
# store-credential.sh through a pipe. The script refuses unless it runs on the Pi's local console, outside tmux.
# The hash is computed by the active release's atlas-ui code (argon2-cffi lives only in that venv), run as the
# atlas-ui user in a sandbox and on pipes, never as root (root runs nothing from a release: atlas-release's threat
# model) and never on this terminal.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"

change=0
for arg in "$@"; do
  case "$arg" in
    --change) change=1 ;;
    -h | --help) sed -n '2,11p' "$0"; exit 0 ;;
    *) die "unknown argument: $arg" ;;
  esac
done
need_root
need_local_console

release=/var/lib/atlas/current
py="$release/venv/atlas-ui/bin/python"
[ -x "$py" ] || die "no atlas-ui venv in the active release: deploy a release first (deploy.sh)"
dest="$UI_CRED_DIR/stepup-pin-hash.cred"
if [ -e "$dest" ] && [ "$change" != 1 ]; then
  die "a PIN is already set. To replace it: $0 --change"
fi

read -rsp "New approval PIN (4 to 12 digits; 6 or more is better): " pin
echo
[[ "$pin" =~ ^[0-9]{4,12}$ ]] || { unset pin; die "the PIN must be 4 to 12 digits"; }
read -rsp "Type it again: " again
echo
if [ "$pin" != "$again" ]; then
  unset pin again
  die "the two PINs differ; nothing changed"
fi
unset again

# Hashed by the release's own code (argon2-cffi is only in that venv), as atlas-ui in a sandbox (lib.sh ui_python).
# Every stream is a pipe: the PIN goes in on stdin, the hash comes back on stdout, and nothing reaches this terminal.
# -I: isolated (no environment variables, no user site, not the current directory on sys.path).
# The single quotes are intended (SC2016): this is Python, not shell.
# shellcheck disable=SC2016
hasher='import sys; sys.path.insert(0, sys.argv[1] + "/backend")
from atlas.ui_server.pin import hash_pin
sys.stdout.write(hash_pin(sys.stdin.readline().rstrip("\n")))'
hash="$(printf '%s\n' "$pin" | ui_python "$py" -I -c "$hasher" "$release" 2>&1)" || hash=""
unset pin
# Exactly one Argon2id PHC string; anything else (an error) is not shown, since it came from release code.
# shellcheck disable=SC2016  # a literal prefix, not an expansion
if [[ ! "$hash" =~ ^'$argon2id$'[A-Za-z0-9+/=,\$]+$ ]]; then
  unset hash
  die "hashing the PIN failed; nothing changed (does the active release have the atlas-ui venv?)"
fi

if [ -e "$dest" ]; then
  rm -f "$dest"
fi
printf '%s' "$hash" | "$here/store-credential.sh" atlas-ui stepup-pin-hash
unset hash
run systemctl try-restart atlas-ui.service
log "PIN stored. atlas-ui reads it when it starts (restarted now if it was running)."
