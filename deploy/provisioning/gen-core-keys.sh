#!/usr/bin/env bash
# Create atlas-core's approval-signing key on the Pi (contracts/services.md section 4: approval-signing-key, Ed25519).
#
#   sudo deploy/provisioning/gen-core-keys.sh
#
# The private key exists in the clear only in RAM (/run) and is stored encrypted by store-credential.sh. The PUBLIC key
# goes to /etc/atlas/approval-signing.pub (it is atlas-executor's configuration when that service arrives) and is
# printed. Nothing secret is printed, so it may run in the shared tmux session. Run it in a maintenance window: /etc
# is on the read-only root.
#
# atlas-core's other credential, lmstudio-api-token, comes from the desktop when the link is set up (runbook
# core-link.md); until then the unit's empty default stands in and atlas-core runs without a desktop.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"
need_root
command -v openssl >/dev/null || die "openssl is required"
! root_is_overlay || die "the read-only overlay is on: /etc/atlas would lose the public key at the next boot (maintenance window)"

dest=/var/lib/atlas/credentials/atlas-core/approval-signing-key.cred
pub=/etc/atlas/approval-signing.pub
[ ! -e "$dest" ] || die "$dest exists. Rotating it also needs atlas-executor's public key replaced (runbook ui-on-pi.md)."

umask 077
tmp="$(mktemp -d -p /run atlas-core-key.XXXXXX)"
trap 'rm -rf "$tmp"' EXIT
openssl genpkey -algorithm ed25519 -out "$tmp/key"
openssl pkey -in "$tmp/key" -pubout -out "$tmp/pub"
"$here/store-credential.sh" atlas-core approval-signing-key <"$tmp/key"
rm -f "$tmp/key"
install -m 0644 -o root -g root "$tmp/pub" "$pub"
log "public key (not secret) in $pub:"
cat "$pub"
