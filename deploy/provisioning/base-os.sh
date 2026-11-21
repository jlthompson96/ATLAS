#!/usr/bin/env bash
# Base OS setup for the ATLAS Pi: docs/runbooks/pi-bringup.md step 7 (inventory), 8 (apply) and 9 (check).
#
#   sudo ./base-os.sh apply [--dry-run]   # fewer card writes, wired only, key-only SSH for the admin. Idempotent.
#   sudo ./base-os.sh check               # PASS/FAIL per expectation; exit 1 if anything fails
#   sudo ./base-os.sh inventory           # Markdown for docs/inventory/pi-<date>.md (no MAC, serial or IP)
#
# The admin user is ATLAS_ADMIN_USER, or the user who ran sudo. Run apply before provisioning, with the root overlay
# off. The card layout (runbook step 6) stays manual: re-partitioning a live card is not something to automate.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"

# Paths. Plain assignments, not environment knobs: the tests override them after sourcing.
ETC=/etc
BOOT=/boot/firmware
DATA=/var/lib/atlas
SWAPFILE=/var/swap
PROC=/proc
SYS=/sys

PACKAGES="nftables python3-venv openssl sudo curl overlayroot ethtool"
OVERLAY_ARG="overlayroot=tmpfs:recurse=0"
REBOOT=0
FAILS=0

# --- helpers --------------------------------------------------------------------------------------------------------

# admin_user: the validated admin account name. AllowUsers takes patterns, so only a plain name is accepted.
admin_user() {
  local u="${ATLAS_ADMIN_USER:-${SUDO_USER:-}}"
  [ -n "$u" ] || die "set ATLAS_ADMIN_USER (or run through sudo as the admin)"
  [[ "$u" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "admin user '$u' is not a plain lowercase user name"
  case "$u" in
    root) die "the admin account must not be root" ;;
    atlas-*) die "the admin account must not be named atlas-* (reserved for services)" ;;
  esac
  printf '%s\n' "$u"
}

admin_home() {
  getent passwd "$1" | cut -d: -f6
}

# has_admin_key USER: true if USER has at least one public key that sshd would accept. Without one, key-only SSH
# with AllowUsers USER locks everyone out.
has_admin_key() {
  local home keys
  home="$(admin_home "$1")"
  keys="$home/.ssh/authorized_keys"
  [ -n "$home" ] && [ -f "$keys" ] && grep -qE '^[[:space:]]*(ssh-ed25519|ssh-rsa|ecdsa-sha2-[a-z0-9-]+|sk-[a-z0-9@.-]+) ' "$keys"
}

same_content() {
  [ -f "$1" ] && [ "$(cat "$1")" = "$2" ]
}

# write_reboot DST MODE CONTENT: write_file, and remember that the change takes effect at the next boot.
write_reboot() {
  if ! same_content "$1" "$3"; then
    REBOOT=1
  fi
  write_file "$1" "$2" "$3"
}

# append_text FILE TEXT: append TEXT (with a trailing newline) unless this is a dry run.
append_text() {
  log "append  $1"
  if [ "$DRY_RUN" = "1" ]; then
    log "[dry-run] $(printf '%s' "$2" | tr '\n' '|')"
  else
    printf '%s\n' "$2" >>"$1"
  fi
}

# config_txt_has FILE LINE: true if LINE applies to every model, i.e. it sits before the first [filter] section or
# under an [all] section. A line under [cm4] or [pi4] would not apply to the Pi 5.
config_txt_has() {
  [ -f "$1" ] || return 1
  awk -v want="$2" '
    BEGIN { all = 1; found = 0 }
    { line = $0; sub(/\r$/, "", line); gsub(/^[ \t]+|[ \t]+$/, "", line) }
    line ~ /^\[.*\]$/ { all = (line == "[all]"); next }
    all && line == want { found = 1 }
    END { exit found ? 0 : 1 }
  ' "$1"
}

journal_content() {
  printf '%s\n' "# ATLAS: journal in RAM only (PLAN 2.7). Managed by deploy/provisioning/base-os.sh." \
    "[Journal]" "Storage=volatile" "RuntimeMaxUse=64M"
}

# rpi-swap's default mechanism ("auto", currently zram+file) writes idle pages to /var/swap on the card.
swap_content() {
  printf '%s\n' "# ATLAS: compressed RAM swap only, nothing on the card. Managed by deploy/provisioning/base-os.sh." \
    "[Main]" "Mechanism=zram" "" "[Zram]" "FixedSizeMiB=2048"
}

# Named 10-* on purpose: sshd keeps the FIRST value it reads, so this wins over cloud-init's 50-cloud-init.conf.
sshd_content() {
  printf '%s\n' "# ATLAS: key-only SSH for the admin user (PLAN Phase 0). Managed by deploy/provisioning/base-os.sh." \
    "PasswordAuthentication no" "KbdInteractiveAuthentication no" "PermitRootLogin no" \
    "AuthenticationMethods publickey" "AllowUsers $1"
}

# Pi OS ships /etc/sudoers.d/010_global-tty (timestamp_type=global): once the admin types the sudo password, every
# other session of that user, an SSH login with a stolen key included, gets root without it for 15 minutes. tty is
# sudo's own default. Named 020_* so it is read after 010_*: the last Defaults value wins.
sudo_content() {
  printf '%s\n' "# ATLAS: a cached sudo password counts only in the terminal it was typed in. Managed by base-os.sh." \
    "Defaults timestamp_type=tty"
}

# --- apply ----------------------------------------------------------------------------------------------------------

apply_journal() {
  log "== journal in RAM"
  write_reboot "$ETC/systemd/journald.conf.d/50-atlas-volatile.conf" 644 "$(journal_content)"
}

apply_swap() {
  log "== swap in zram only"
  write_reboot "$ETC/rpi/swap.conf.d/50-atlas.conf" 644 "$(swap_content)"
  if [ -e "$SWAPFILE" ]; then
    if swapon --show=NAME --noheadings 2>/dev/null | grep -qxF "$SWAPFILE"; then
      log "note    $SWAPFILE is still in use: reboot, then run apply again to remove it"
    else
      log "remove  $SWAPFILE"
      run rm -f "$SWAPFILE"
    fi
  else
    log "ok      no $SWAPFILE"
  fi
}

apply_radios() {
  log "== no Wi-Fi or Bluetooth"
  local cfg="$BOOT/config.txt" missing="" line
  [ -f "$cfg" ] || die "$cfg not found: is the boot partition mounted at $BOOT?"
  for line in dtoverlay=disable-wifi dtoverlay=disable-bt; do
    if config_txt_has "$cfg" "$line"; then
      log "ok      $line"
    else
      missing="$missing
$line"
    fi
  done
  [ -n "$missing" ] || return 0
  if [ "$DRY_RUN" != "1" ] && ! { : >>"$cfg"; } 2>/dev/null; then
    die "$cfg is read-only: sudo mount -o remount,rw $BOOT, then run apply again"
  fi
  # [all] ends whatever model filter the file ends with, so the lines apply to the Pi 5.
  append_text "$cfg" "
# ATLAS: wired only (docs/runbooks/pi-bringup.md step 8)
[all]${missing}"
  REBOOT=1
}

apply_tmp() {
  log "== /tmp in RAM"
  if [ "$(findmnt -no FSTYPE /tmp 2>/dev/null || true)" = "tmpfs" ]; then
    log "ok      /tmp is tmpfs"
  elif awk '!/^[[:space:]]*#/ && $2 == "/tmp" { found = 1 } END { exit found ? 0 : 1 }' "$ETC/fstab"; then
    log "WARNING: $ETC/fstab has a /tmp entry, but /tmp is not tmpfs now; check it by hand"
  else
    append_text "$ETC/fstab" "tmpfs  /tmp  tmpfs  defaults,noatime,mode=1777  0  0"
    REBOOT=1
  fi
}

# A sudoers file that does not parse breaks sudo for everyone, so it is checked with visudo before it is installed.
apply_sudo() {
  local dst="$ETC/sudoers.d/020_atlas-tty" content tmp
  log "== sudo: password cache per terminal"
  content="$(sudo_content)"
  if same_content "$dst" "$content"; then
    log "ok      $dst"
    return 0
  fi
  if [ "$DRY_RUN" != "1" ]; then
    tmp="$(mktemp)"
    printf '%s\n' "$content" >"$tmp"
    if ! visudo -cf "$tmp" >/dev/null; then
      rm -f "$tmp"
      die "visudo rejected the sudoers drop-in; $dst was not installed"
    fi
    rm -f "$tmp"
  fi
  write_file "$dst" 440 "$content"
}

apply_sshd() {
  local user="$1" dst="$ETC/ssh/sshd_config.d/10-atlas.conf" content backup=""
  log "== SSH: key only, $user only"
  content="$(sshd_content "$user")"
  if same_content "$dst" "$content"; then
    log "ok      $dst"
    return 0
  fi
  if [ "$DRY_RUN" = "1" ]; then
    write_file "$dst" 644 "$content"
    return 0
  fi
  if [ -f "$dst" ]; then
    backup="$(mktemp)"
    cp -p "$dst" "$backup"
  fi
  write_file "$dst" 644 "$content"
  if ! sshd -t; then
    if [ -n "$backup" ]; then
      install -m 644 -o root -g root "$backup" "$dst"
      rm -f "$backup"
    else
      rm -f "$dst"
    fi
    die "sshd -t rejected the configuration; $dst was put back as it was and ssh was not reloaded"
  fi
  [ -z "$backup" ] || rm -f "$backup"
  if systemctl is-active --quiet ssh.service; then
    run systemctl reload ssh.service
  else
    log "note    ssh.service is not running; the settings apply when it next starts"
  fi
  log "CHECK NOW from a second terminal on the Mac, before closing this session (runbook step 8):"
  log "  ssh -o ControlPath=none atlas-pi true && echo key-login-ok"
  log "  ssh -o ControlPath=none -o PubkeyAuthentication=no -o PreferredAuthentications=password atlas-pi true   # must be refused"
}

cmd_apply() {
  local user
  need_root
  user="$(admin_user)"
  if [ "$DRY_RUN" != "1" ]; then
    getent passwd "$user" >/dev/null || die "admin user '$user' does not exist (set ATLAS_ADMIN_USER)"
  fi
  if [ "$(findmnt -no FSTYPE / 2>/dev/null || true)" = "overlay" ]; then
    die "the root overlay is on, so changes would be lost at reboot: turn it off first (pi-bringup.md step 11)"
  fi
  if ! has_admin_key "$user"; then
    if [ "$DRY_RUN" = "1" ]; then
      log "WARNING: $user has no SSH public key in authorized_keys; a real run refuses (it would lock SSH out)"
    else
      die "$user has no SSH public key in ~/.ssh/authorized_keys: key-only SSH would lock you out"
    fi
  fi
  if ! findmnt -n "$DATA" >/dev/null 2>&1; then
    log "WARNING: $DATA is not a mount point: the data partition (runbook step 6) is not set up yet"
  fi
  apply_journal
  apply_swap
  apply_radios
  apply_tmp
  apply_sudo
  apply_sshd "$user"
  if [ "$REBOOT" = "1" ]; then
    log "done. Reboot (sudo reboot), then run: sudo $0 check"
  else
    log "done. Nothing needs a reboot. Run: sudo $0 check"
  fi
}

# --- check ----------------------------------------------------------------------------------------------------------

pass() { printf 'PASS  %s\n' "$*"; }
fail() {
  printf 'FAIL  %s\n' "$*"
  FAILS=$((FAILS + 1))
}
note() { printf 'NOTE  %s\n' "$*"; }

# expect NAME ACTUAL WANTED
expect() {
  if [ "$2" = "$3" ]; then
    pass "$1: $2"
  else
    fail "$1: got '$2', want '$3'"
  fi
}

has_option() {
  case ",$1," in *",$2,"*) return 0 ;; *) return 1 ;; esac
}

check_platform() {
  local codename="" python mem_kb
  expect "arch" "$(dpkg --print-architecture 2>/dev/null || true)" arm64
  if [ -f "$ETC/os-release" ]; then
    codename="$(awk -F= '$1 == "VERSION_CODENAME" { gsub(/"/, "", $2); print $2 }' "$ETC/os-release")"
  fi
  expect "os" "$codename" trixie
  python="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  expect "python3" "$python" 3.13
  mem_kb="$(awk '$1 == "MemTotal:" { print $2 }' "$PROC/meminfo" 2>/dev/null || true)"
  if [ -n "$mem_kb" ] && [ "$mem_kb" -ge 7340032 ]; then
    pass "ram: $mem_kb kB"
  else
    fail "ram: got '${mem_kb:-?} kB', want an 8 GB Pi (A9)"
  fi
  local throttled
  throttled="$(vcgencmd get_throttled 2>/dev/null || true)"
  expect "power" "$throttled" "throttled=0x0"
}

check_packages() {
  local pkg status
  for pkg in $PACKAGES; do
    # shellcheck disable=SC2016 # ${Status} is dpkg-query's field syntax, not a shell expansion
    status="$(dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null || true)"
    expect "package $pkg" "$status" "install ok installed"
  done
}

check_data() {
  local fstype source options fstab_source opt
  fstype="$(findmnt -no FSTYPE "$DATA" 2>/dev/null || true)"
  if [ -z "$fstype" ]; then
    fail "data: $DATA is not a mount point (runbook step 6)"
    return 0
  fi
  source="$(findmnt -no SOURCE "$DATA" 2>/dev/null || true)"
  options="$(findmnt -no OPTIONS "$DATA" 2>/dev/null || true)"
  if [ "$fstype" = "ext4" ] && [ "${source#/dev/}" != "$source" ]; then
    pass "data: $DATA is $fstype on $source"
  else
    fail "data: $DATA is $fstype on $source, want ext4 on its own partition (an overlay here loses ATLAS's writes)"
  fi
  for opt in rw noatime commit=60 errors=remount-ro; do
    if has_option "$options" "$opt"; then
      pass "data mount option $opt"
    else
      fail "data mount option $opt missing (have: $options)"
    fi
  done
  fstab_source="$(findmnt --fstab -no SOURCE "$DATA" 2>/dev/null || true)"
  case "$fstab_source" in
    UUID=*) pass "data: in fstab by UUID" ;;
    "") fail "data: no fstab entry, so it won't mount at boot" ;;
    *) fail "data: fstab mounts it by '$fstab_source'; use UUID= (device names can change)" ;;
  esac
}

check_writes() {
  local swaps other storage
  swaps="$(swapon --show=NAME --noheadings 2>/dev/null || true)"
  other="$(printf '%s\n' "$swaps" | grep -v '^/dev/zram' | grep -v '^$' || true)"
  if [ -n "$swaps" ] && [ -z "$other" ]; then
    pass "swap: zram only"
  else
    fail "swap: got '$(printf '%s' "$swaps" | tr '\n' ' ')', want /dev/zram* only"
  fi
  if [ -e "$SWAPFILE" ]; then
    fail "swap: $SWAPFILE exists (run apply again after the reboot)"
  else
    pass "swap: no $SWAPFILE"
  fi
  storage="$(systemd-analyze cat-config systemd/journald.conf 2>/dev/null |
    awk -F= '/^[[:space:]]*Storage[[:space:]]*=/ { v = $2; gsub(/[[:space:]]/, "", v) } END { print v }' || true)"
  expect "journal Storage" "$storage" volatile
  expect "/tmp" "$(findmnt -no FSTYPE /tmp 2>/dev/null || true)" tmpfs
}

check_radios() {
  local line wlan
  for line in dtoverlay=disable-wifi dtoverlay=disable-bt; do
    if config_txt_has "$BOOT/config.txt" "$line"; then
      pass "config.txt: $line"
    else
      fail "config.txt: $line missing, or under a model filter instead of [all]"
    fi
  done
  wlan="$(find "$SYS/class/net" -maxdepth 1 -name 'wlan*' 2>/dev/null | head -1)"
  if [ -z "$wlan" ]; then
    pass "no wlan interface"
  else
    fail "wlan interface present: $(basename "$wlan") (reboot after apply)"
  fi
  if [ -z "$(find "$SYS/class/bluetooth" -maxdepth 1 -name 'hci*' 2>/dev/null | head -1)" ]; then
    pass "no bluetooth controller"
  else
    fail "bluetooth controller present (reboot after apply)"
  fi
}

check_sshd() {
  local user="$1" effective key want got
  effective="$(sshd -T 2>/dev/null || true)"
  if [ -z "$effective" ]; then
    fail "sshd: 'sshd -T' printed nothing (run check as root)"
    return 0
  fi
  for want in "passwordauthentication no" "kbdinteractiveauthentication no" "permitrootlogin no" \
    "authenticationmethods publickey" "allowusers $user"; do
    key="${want%% *}"
    got="$(printf '%s\n' "$effective" | awk -v k="$key" '$1 == k { $1 = ""; sub(/^ /, ""); print }' | tr '\n' ' ')"
    got="${got% }"
    expect "sshd $key" "$got" "${want#* }"
  done
}

# sudo's parsed settings, not the files: a rule can come from any file in sudoers.d (cloud-init writes
# 90-cloud-init-users when user-data asks for NOPASSWD). The one password-free rule allowed is provision-pi.sh's.
check_sudo() {
  local user="$1" ts rules bad
  ts="$(LC_ALL=C sudo -V 2>/dev/null | awk -F': ' '$1 == "Type of authentication timestamp record" { print $2 }' || true)"
  expect "sudo timestamp_type" "$ts" tty
  rules="$(LC_ALL=C sudo -l -U "$user" 2>/dev/null || true)"
  if [ -z "$rules" ]; then
    fail "sudo: 'sudo -l -U $user' printed nothing (run check as root)"
    return 0
  fi
  bad="$(printf '%s\n' "$rules" | grep -E 'NOPASSWD|!authenticate' |
    grep -vxE '[[:space:]]*\(root\) NOPASSWD: /usr/local/sbin/atlas-release' || true)"
  if [ -z "$bad" ]; then
    pass "sudo: $user needs the password (atlas-release excepted)"
  else
    fail "sudo: $user has root without a password: $(printf '%s' "$bad" | tr -s ' \n' ' ')"
  fi
}

check_overlay() {
  local cmdline token found=""
  cmdline="$(cat "$BOOT/cmdline.txt" 2>/dev/null || true)"
  for token in $cmdline; do
    case "$token" in overlayroot=*) found="$found $token" ;; esac
  done
  found="${found# }"
  case "$found" in
    "") note "overlay: off (fine until runbook step 11, and during a maintenance window)" ;;
    "$OVERLAY_ARG")
      if [ "$(findmnt -no FSTYPE / 2>/dev/null || true)" = "overlay" ]; then
        pass "overlay: on, with recurse=0"
      else
        note "overlay: set in cmdline.txt, active after the next reboot"
      fi
      ;;
    *) fail "overlay: cmdline.txt has '$found', want exactly '$OVERLAY_ARG' (plain overlayroot=tmpfs also covers $DATA)" ;;
  esac
}

cmd_check() {
  local user
  need_root
  user="$(admin_user)"
  check_platform
  check_packages
  check_data
  check_writes
  check_radios
  check_sshd "$user"
  check_sudo "$user"
  check_overlay
  if [ "$FAILS" -eq 0 ]; then
    printf 'all checks passed\n'
  else
    printf '%d check(s) failed\n' "$FAILS"
    return 1
  fi
}

# --- inventory ------------------------------------------------------------------------------------------------------

# Prints Markdown for docs/inventory/. THE REPOSITORY IS PUBLIC: no MAC addresses, serial numbers, IP addresses, host
# keys or UUIDs. Each value comes from a source that holds none of them, and ethtool's output is filtered to two fields.
cmd_inventory() {
  local model="" os="" card="" sectors="" eth="" parts
  if [ -r "$PROC/device-tree/model" ]; then
    model="$(tr -d '\0' <"$PROC/device-tree/model")"
  fi
  if [ -f "$ETC/os-release" ]; then
    os="$(awk -F= '$1 == "PRETTY_NAME" { gsub(/"/, "", $2); print $2 }' "$ETC/os-release")"
  fi
  card="$(cat "$SYS/block/mmcblk0/device/name" 2>/dev/null || true)"
  sectors="$(cat "$SYS/block/mmcblk0/size" 2>/dev/null || echo 0)"
  eth="$(ethtool eth0 2>/dev/null | awk -F': ' '/^[[:space:]]*(Speed|Duplex):/ { printf "%s ", $2 }' || true)"
  parts="$(lsblk -nro NAME,SIZE,FSTYPE,MOUNTPOINT /dev/mmcblk0 2>/dev/null |
    awk '$1 ~ /p[0-9]+$/ { printf "%s %s %s %s; ", $1, $2, $3, $4 }' || true)"
  printf '# Pi inventory, %s\n\n' "$(date -u +%Y-%m-%d)"
  printf 'From %s (A9, PLAN 0.4). No MAC, serial or IP: the repository is public.\n\n' \
    "\`deploy/provisioning/base-os.sh inventory\`"
  printf '| Item | Value |\n|---|---|\n'
  printf '| model | %s |\n' "$model"
  printf '| ram | %s |\n' "$(awk '$1 == "MemTotal:" { printf "%.1f GiB", $2 / 1048576 }' "$PROC/meminfo" 2>/dev/null || true)"
  printf '| os | %s |\n' "$os"
  printf '| kernel | %s |\n' "$(uname -r)"
  printf '| arch | %s |\n' "$(dpkg --print-architecture 2>/dev/null || true)"
  printf '| python | %s |\n' "$(python3 --version 2>&1 || true)"
  printf '| bootloader | %s |\n' "$(vcgencmd bootloader_version 2>/dev/null | head -1 || true)"
  printf '| temp idle | %s |\n' "$(vcgencmd measure_temp 2>/dev/null || true)"
  printf '| power | %s |\n' "$(vcgencmd get_throttled 2>/dev/null || true)"
  printf '| card | %s %s GiB |\n' "$card" "$((sectors / 2097152))"
  printf '| partitions | %s |\n' "${parts%; }"
  printf '| ethernet | %s |\n' "${eth% }"
}

main() {
  local cmd="${1:-}"
  [ $# -eq 0 ] || shift
  for arg in "$@"; do
    case "$arg" in
      --dry-run) DRY_RUN=1 ;;
      *) die "unknown argument: $arg" ;;
    esac
  done
  case "$cmd" in
    apply) cmd_apply ;;
    check) cmd_check ;;
    inventory) cmd_inventory ;;
    *)
      sed -n '2,9p' "$0"
      exit 2
      ;;
  esac
}

# Sourced by the tests (functions only); executed normally otherwise.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
