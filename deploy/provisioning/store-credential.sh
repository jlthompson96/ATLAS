#!/usr/bin/env bash
# Store one secret for one service as a systemd-creds encrypted credential (contracts/services.md section 4).
#
#   sudo deploy/provisioning/store-credential.sh atlas-discord discord-reader-bot-token     # reads the secret from stdin
#   printf %s "$VALUE" | sudo deploy/provisioning/store-credential.sh atlas-market twelvedata-api-key
#
# The secret is read from stdin only (never an argument: arguments land in process lists and shell history). On a
# terminal it is read without echo. The credential name must be one the service's unit actually loads, so a typo or a
# secret for the wrong service is refused. Real secrets are created and stored only on the Pi, never on the Mac or in
# the repository. Output: /var/lib/atlas/credentials/<service>/<name>.cred (root-only, encrypted with the host key).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"

service="${1:-}"
name="${2:-}"
[[ "$service" =~ ^atlas-[a-z]+$ ]] || die "usage: $0 <atlas-service> <credential-name>  (secret on stdin)"
[[ "$name" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || die "bad credential name"
need_root

unit="/etc/systemd/system/$service.service"
[ -f "$unit" ] || die "$unit not found (run provision-pi.sh first)"
grep -q "^LoadCredentialEncrypted=$name:" "$unit" || die "$service does not load a credential named $name (see contracts/services.md section 4)"

dest="/var/lib/atlas/credentials/$service/$name.cred"
[ ! -e "$dest" ] || die "$dest exists; remove it deliberately to rotate (and update dependants)"

umask 077
if [ -t 0 ]; then
  read -rsp "secret for $service/$name: " secret
  echo >&2
  printf '%s' "$secret" | systemd-creds encrypt --name="$name" - "$dest"
  unset secret
else
  systemd-creds encrypt --name="$name" - "$dest"
fi
chmod 600 "$dest"
log "stored $dest"
