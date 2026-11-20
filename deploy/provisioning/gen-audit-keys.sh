#!/usr/bin/env bash
# Generate atlas-audit's two credentials on the Pi (contracts/services.md section 4): the Ed25519 checkpoint signing key
# and the segment master key (reserved for crypto-shredding; unused until per-segment data keys exist).
#
#   sudo deploy/provisioning/gen-audit-keys.sh
#
# Keys are created in memory/pipes and stored encrypted by store-credential.sh; nothing is written in clear, and the
# private key never leaves the Pi. After the audit service first starts it writes the PUBLIC key to
# /var/lib/atlas/audit/checkpoint.pub: copy that file and the genesis hash (and later each checkpoints.jsonl) off the Pi
# so verification does not depend on files an attacker on the Pi could also replace (docs/runbooks/audit.md).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"
need_root
command -v openssl >/dev/null || die "openssl is required"

openssl genpkey -algorithm ed25519 | "$here/store-credential.sh" atlas-audit audit-checkpoint-key
openssl rand 32 | "$here/store-credential.sh" atlas-audit audit-segment-master-key
log "audit credentials stored. Start the service, then record checkpoint.pub and the genesis hash off the Pi."
