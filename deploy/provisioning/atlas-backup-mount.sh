#!/usr/bin/env bash
# atlas-backup-mount: mount the plugged-in backup drive for atlas-backup.service, and unmount it afterwards (ADR 0002).
# Installed by provision-pi.sh as /usr/local/sbin/atlas-backup-mount, root-owned. Run only by systemd, as root, from
# atlas-backup-mount.service (ExecStart/ExecStop); the admin has no sudo rule for it.
#
#   atlas-backup-mount mount     find the one allowlisted drive that is plugged in, fsck, mount at /mnt/atlas-backup
#   atlas-backup-mount unmount   sync and unmount (no-op when nothing is mounted)
#   atlas-backup-mount select    print "<name> <device>" for the plugged-in drive (what mount would use)
#
# /etc/atlas/backup.conf (root-owned, not group/world-writable) lists the drives, one per line: "<name> <fs-uuid>",
# for example "A 0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0". A drive that isn't listed is never mounted, and neither is
# anything when two listed drives are plugged in at once. The drive must be ext4.
set -euo pipefail

CONF="${ATLAS_BACKUP_CONF:-/etc/atlas/backup.conf}"
MNT="${ATLAS_BACKUP_MNT:-/mnt/atlas-backup}"
BYUUID="${ATLAS_BACKUP_BYUUID:-/dev/disk/by-uuid}"
RUNDIR="${RUNTIME_DIRECTORY:-/run/atlas-backup-mount}"
USER_NAME=atlas-backup

log() { printf 'atlas-backup-mount: %s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }

# drives: validated "<name> <uuid>" lines from the conf file.
drives() {
  [ -f "$CONF" ] || die "$CONF not found: list the backup drives there (runbook backup.md)"
  if [ -z "${ATLAS_BACKUP_CONF:-}" ]; then # the real file; tests point ATLAS_BACKUP_CONF at a temporary one
    [ "$(stat -c %u "$CONF")" = 0 ] || die "$CONF must be owned by root"
    case "$(stat -c %A "$CONF")" in
      ?????w????|????????w?) die "$CONF must not be group- or world-writable" ;;
    esac
  fi
  local line name uuid extra n=0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    [ -n "${line//[[:space:]]/}" ] || continue
    read -r name uuid extra <<<"$line"
    [[ "$name" =~ ^[A-Za-z0-9-]{1,16}$ ]] || die "bad drive name in $CONF"
    [[ "$uuid" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]] || die "bad filesystem UUID for drive $name in $CONF"
    [ -z "$extra" ] || die "trailing text on the line for drive $name in $CONF"
    printf '%s %s\n' "$name" "$(printf '%s' "$uuid" | tr '[:upper:]' '[:lower:]')"
    n=$((n + 1))
  done <"$CONF"
  [ "$n" -gt 0 ] || die "$CONF lists no drives"
}

select_drive() {
  local list name uuid found="" count=0
  list="$(drives)" # a bad conf file stops here (set -e), not inside a pipe
  while read -r name uuid; do
    if [ -e "$BYUUID/$uuid" ]; then
      found="$name $(readlink -f "$BYUUID/$uuid")"
      count=$((count + 1))
    fi
  done <<<"$list"
  [ "$count" -gt 0 ] || die "none of the backup drives in $CONF is plugged in"
  [ "$count" -eq 1 ] || die "$count backup drives are plugged in; unplug all but one"
  printf '%s\n' "$found"
}

do_mount() {
  ! mountpoint -q "$MNT" || die "$MNT is already mounted (a previous run did not unmount; check, then umount $MNT)"
  local sel name dev fstype rc=0
  sel="$(select_drive)"
  read -r name dev <<<"$sel"
  fstype="$(blkid -o value -s TYPE "$dev" || true)"
  [ "$fstype" = ext4 ] || die "drive $name is '${fstype:-unknown}', not ext4"
  e2fsck -p "$dev" >&2 || rc=$?
  [ "$rc" -lt 4 ] || die "e2fsck found errors it could not fix on drive $name (exit $rc); repair it by hand"
  mount -t ext4 -o nosuid,nodev,noexec,noatime "$dev" "$MNT"
  [ ! -L "$MNT/restic" ] || { umount "$MNT"; die "$MNT/restic on drive $name is a symlink"; }
  install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$MNT/restic"
  if [ -d "$RUNDIR" ]; then
    printf '%s\n' "$name" >"$RUNDIR/drive"
    chmod 0644 "$RUNDIR/drive"
  fi
  log "mounted backup drive $name"
}

do_unmount() {
  rm -f "$RUNDIR/drive"
  if mountpoint -q "$MNT"; then
    sync
    umount "$MNT"
    log "unmounted $MNT"
  fi
}

case "${1:-}" in
  mount) do_mount ;;
  unmount) do_unmount ;;
  select) select_drive ;;
  *) die "usage: atlas-backup-mount mount|unmount|select" ;;
esac
