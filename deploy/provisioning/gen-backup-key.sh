#!/usr/bin/env bash
# Create the restic repository password for atlas-backup (ADR 0002 section 7) and store it as the systemd-creds
# credential atlas-backup/backup-repo-password. It is shown ONCE so the owner can write it down, then cleared.
#
#   sudo deploy/provisioning/gen-backup-key.sh      # on the Pi's own console (keyboard + screen), never over SSH
#
# The key must never pass through a remote or shared terminal: a Claude Code session can read the tmux pane, and SSH
# sessions can be logged. The script refuses unless stdin and stdout are the same local virtual console (/dev/ttyN).
# 128 random bits as 26 base32 characters (A-Z, 2-7: no 0/O or 1/I to confuse on paper), in groups of four.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"
need_root

dest=/var/lib/atlas/credentials/atlas-backup/backup-repo-password.cred
[ ! -e "$dest" ] || die "$dest exists. Changing the password needs 'restic key add' on every drive first (runbook backup.md)."

console="$(tty 2>/dev/null || true)"
[[ "$console" =~ ^/dev/tty[0-9]+$ ]] || die "run this on the Pi's own console (got '${console:-no terminal}'), not over SSH or in tmux"
[ "$(readlink -f /proc/self/fd/1)" = "$console" ] || die "stdout must be the console too (no redirection or pipes)"
if [ -n "${TMUX:-}" ] || [ -n "${STY:-}" ]; then
  die "inside tmux or screen: the pane can be read from elsewhere"
fi

key="$(head -c 16 /dev/urandom | base32 | tr -d '=\n')"
[ "${#key}" -eq 26 ] || die "key generation failed"
grouped="$(printf '%s' "$key" | fold -w4 | paste -sd' ' -)"

clear
cat <<EOF
ATLAS backup password (restic repository). Write it on paper now, then store the paper offline.
Without it, nothing on the backup drives can be restored.

        $grouped

The spaces are only for reading; the password is the 26 characters without them.
Press Enter when you have written it down.
EOF
read -r _
clear

tries=0
while :; do
  read -rsp "Type it back to check (spaces optional): " typed
  echo
  typed="$(printf '%s' "$typed" | tr -d ' -' | tr '[:lower:]' '[:upper:]')"
  [ "$typed" = "$key" ] && break
  tries=$((tries + 1))
  [ "$tries" -lt 3 ] || { unset key typed grouped; die "didn't match three times; nothing stored. Run the script again."; }
  echo "That doesn't match. Check the paper and try again."
done
unset typed grouped

printf '%s' "$key" | "$here/store-credential.sh" atlas-backup backup-repo-password
unset key
log "Stored. Keep the paper offline; next: list the drives in /etc/atlas/backup.conf and rerun provision-pi.sh (runbook backup.md)."
