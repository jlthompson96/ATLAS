#!/usr/bin/env bash
# Create the Pi's WireGuard key for the link to the LLM desktop (PLAN 5.3, runbook wireguard.md).
#
#   sudo deploy/provisioning/gen-wg-key.sh
#
# The private key goes straight into a systemd-creds credential (/etc/credstore.encrypted/network.wireguard.private.wg0,
# encrypted with the host key), which systemd-networkd imports for wg0. It is never printed or written in the clear.
# The PUBLIC key is printed: paste it into the desktop's WireGuard tunnel ([Peer] PublicKey). It isn't secret.
# Run it in a maintenance window (overlay off: /etc is on the root filesystem).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"
need_root

command -v wg >/dev/null || die "wg not found: install wireguard-tools first (runbook wireguard.md)"
dest="$WG_CRED_DIR/$WG_CRED_NAME"
[ ! -e "$dest" ] || die "$dest exists. To rotate, follow the runbook (the desktop needs the new public key too)."
if [ "$DRY_RUN" = "1" ]; then
  log "[dry-run] would create $dest and print the public key"
  exit 0
fi

umask 077
install -d -m 0700 -o root -g root "$WG_CRED_DIR"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
wg genkey >"$tmp/key"
pub="$(wg pubkey <"$tmp/key")"
systemd-creds encrypt --name="$WG_CRED_NAME" "$tmp/key" "$dest"
chmod 0600 "$dest"
rm -f "$tmp/key"
wg_key_ok "$pub" || die "unexpected public key format"
log "stored $dest (encrypted with the host key)"
printf 'Pi WireGuard public key (paste it into the desktop tunnel as [Peer] PublicKey):\n%s\n' "$pub"
