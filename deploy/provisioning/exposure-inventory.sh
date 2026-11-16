#!/usr/bin/env bash
# PT-11 helper (PLAN 8.2, exposure inventory): list every file under /var/lib/atlas and /etc/atlas that holds data or
# secrets, with owner and mode, so it can be compared with the PLAN 2.7 table (what someone holding the card can read).
# Run it on the Pi, or against a mounted card: ROOT=/mnt/card sudo deploy/provisioning/exposure-inventory.sh
#
# It prints names, owners, modes and sizes only, never contents. Review the list by hand: there must be no email, Discord or
# calendar bodies and no transcripts older than 7 days; the audit log, memory DB, queue and encrypted credentials are the
# expected stores.
set -euo pipefail

root="${ROOT:-}"
printf '%-9s %-18s %-6s %10s  %s\n' TYPE OWNER MODE BYTES PATH
for dir in "$root/var/lib/atlas" "$root/etc/atlas"; do
  [ -d "$dir" ] || continue
  # releases/ is code, not data: summarised instead of listed.
  find "$dir" -path "$root/var/lib/atlas/releases" -prune -o -type f -printf '%y %u:%g %m %s %p\n' 2>/dev/null |
    while read -r type owner mode size path; do
      printf '%-9s %-18s %-6s %10s  %s\n' "$type" "$owner" "$mode" "$size" "$path"
    done
done
if [ -d "$root/var/lib/atlas/releases" ]; then
  echo "(releases/: $(find "$root/var/lib/atlas/releases" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ') release directories, code only)"
fi
