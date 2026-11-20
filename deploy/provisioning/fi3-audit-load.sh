#!/usr/bin/env bash
# FI-3 load generator (PLAN 8.5): hammer the audit log with batched read events and fsynced write-ahead records while you
# cut the Pi's power (20 times). Afterwards, on the rebooted Pi, run:
#
#   sudo -u atlas-audit /var/lib/atlas/current/venv/atlas-audit/bin/python -m atlas.audit verify /var/lib/atlas/audit
#
# Expected after every cut: the chain verifies (exit 0, `UNANCHORED:` because no off-box --expect-head is given on the box;
# at most ~30 s of read-event records lost, nothing corrupt), any torn final line is reported as a warning, and every
# write_ahead without a write_result is listed as OUTCOME_UNKNOWN (never retried).
#
#   sudo deploy/provisioning/fi3-audit-load.sh [iterations]      (runs until power is cut or iterations are done)
#
# Records are tiny and use fake identifiers only. Each iteration writes: 20 tool_call records as atlas-core, then a
# write_ahead (and, 3 times out of 4, its write_result) as atlas-executor, so some actions are left "outcome unknown".
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"
need_root
iterations="${1:-1000000}"
py=/var/lib/atlas/current/venv/atlas-audit/bin/python
export PYTHONPATH=/var/lib/atlas/current/backend
export ATLAS_AUDIT_SOCKET=/run/atlas/audit.sock

append() { # append USER JSON
  # -G: units get this group through SupplementaryGroups=; a bare runuser would not be in it and the socket would refuse.
  printf '%s' "$2" | runuser -u "$1" -G atlas-audit-clients -- "$py" -m atlas.audit append >/dev/null
}

for ((i = 1; i <= iterations; i++)); do
  for ((j = 0; j < 20; j++)); do
    append atlas-core '{"kind":"tool_call","context":"system","tool":"fi3_probe","args_redacted":{"n":'"$j"'},"data_accessed":[],"result":{"status":"ok"}}'
  done
  approval="fi3-$i"
  append atlas-executor '{"kind":"write_ahead","context":"system","tool":"fi3_probe","action":{"action_id":"act-'"$i"'","action_kind":"email.send","payload_sha256":"'"$(printf '%064d' 0)"'","approval_id":"'"$approval"'"}}'
  if [ $((i % 4)) -ne 0 ]; then
    append atlas-executor '{"kind":"write_result","context":"system","tool":"fi3_probe","action":{"action_id":"act-'"$i"'","action_kind":"email.send","payload_sha256":"'"$(printf '%064d' 0)"'","approval_id":"'"$approval"'"},"result":{"status":"ok"}}'
  fi
done
