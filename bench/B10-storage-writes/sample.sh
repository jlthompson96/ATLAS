#!/bin/sh
# B10 (PLAN 3.5, 8.3): sample the microSD card's write counters from /proc/diskstats for 24 h.
#
#   sh sample.sh [--interval SECONDS] [--hours HOURS] [--out FILE]
#
# Runs on the Pi as the admin user: no root needed. Defaults: every 600 s for 24 h, to /tmp/b10-<UTC start>.log.
# /tmp is tmpfs, so the measurement itself writes nothing to the card. A reboot clears /tmp and resets the
# counters: start again. Don't deploy during the run (a release is ~15 MB unpacked plus venvs).
# Turn the samples into a result with report.py on the Mac.
set -eu

interval=600
hours=24
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    --interval) interval="$2"; shift 2 ;;
    --hours) hours="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    *) echo "usage: sample.sh [--interval SECONDS] [--hours HOURS] [--out FILE]" >&2; exit 2 ;;
  esac
done
case "$interval$hours" in
  *[!0-9]*) echo "interval and hours must be whole numbers" >&2; exit 2 ;;
esac
[ "$interval" -gt 0 ] && [ "$hours" -gt 0 ] || { echo "interval and hours must be > 0" >&2; exit 2; }
[ -n "$out" ] || out="/tmp/b10-$(date -u +%Y%m%dT%H%M%SZ).log"

disk="${B10_DISK:-mmcblk0}"
grep -q " $disk " /proc/diskstats || { echo "no $disk in /proc/diskstats" >&2; exit 1; }

sample() {
  printf 'T %s %s\n' "$(date -u +%s)" "$(cut -d' ' -f1 /proc/uptime)"
  awk -v d="$disk" '$3 ~ "^" d "(p[0-9]+)?$" { print "D", $0 }' /proc/diskstats
  # The persistent journal (PLAN 2.7) is the one write source the admin can size without root (group adm).
  printf 'J %s\n' "$(du -sk /var/log/journal 2>/dev/null | cut -f1)"
}

{
  echo "# B10 storage writes"
  echo "# start $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "# host $(uname -n) kernel $(uname -r)"
  echo "# booted $(uptime -s)"
  echo "# root $(findmnt -n -o FSTYPE /)"
  echo "# release $(readlink /var/lib/atlas/current 2>/dev/null || echo none)"
  echo "# units $(systemctl list-units --state=active --no-legend --plain 'atlas-*' 2>/dev/null | awk '{print $1}' | tr '\n' ' ')"
  echo "# interval $interval hours $hours disk $disk"
  sample
} > "$out"

end=$(( $(date -u +%s) + hours * 3600 ))
while [ "$(date -u +%s)" -lt "$end" ]; do
  sleep "$interval"
  sample >> "$out"
done
echo "# end $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$out"
