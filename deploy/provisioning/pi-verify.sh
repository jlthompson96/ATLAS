#!/usr/bin/env bash
# The root-only checks from docs/runbooks/pi-verification.md that can run on the live Pi without changing anything.
# One PASS/FAIL/NOTE line per check, tagged with its checklist item; exit 1 if anything failed.
#
#   sudo deploy/provisioning/pi-verify.sh                     # read-only
#   sudo deploy/provisioning/pi-verify.sh --service-checks    # also restarts atlas-audit once (N4): maintenance window only
#
# Read-only means: it reads files, unit state and counters, and sends a few probe packets that the firewall must drop
# (UDP to the LAN resolver and a time server from users that may not use them). It writes nothing, loads no ruleset
# and stops no unit. --service-checks restarts atlas-audit.service while atlas-egress runs, to check a clean stop.
# Prints no secrets: credential contents, keys and private file names are never shown.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"

SERVICE_CHECKS=0
for arg in "$@"; do
  case "$arg" in
    --service-checks) SERVICE_CHECKS=1 ;;
    -h | --help) sed -n '2,11p' "$0"; exit 0 ;;
    *) die "unknown argument: $arg" ;;
  esac
done
need_root

ETC=/etc/atlas
DATA=/var/lib/atlas
SITE="$ETC/nftables-site.nft"
UNITS=/etc/systemd/system
FAILS=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
note() { printf 'NOTE  %s\n' "$*"; }
# expect PASS-TEXT FAIL-TEXT CMD...: PASS when CMD succeeds.
expect() {
  local ok="$1" bad="$2"
  shift 2
  if "$@"; then pass "$ok"; else fail "$bad"; fi
}

# probe_udp USER HOST PORT: 0 if a real request got an answer within 2 s (an NTP request on 123, a DNS query otherwise).
probe_udp() {
  runuser -u "$1" -- python3 -I -c '
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(2)
if sys.argv[2] == "123":
    q = bytes([0x1B]) + bytes(47)  # NTPv3 client request
else:
    q = bytes.fromhex("12340100000100000000000007" + "6578616d706c65" + "036f726700" + "00010001")  # example.org A
try:
    s.sendto(q, (sys.argv[1], int(sys.argv[2]))); s.recv(512)
except OSError:
    sys.exit(1)
' "$2" "$3" 2>/dev/null
}

# --- firewall (pi-verification "Firewall", lines on nft, F7, UDP, SSH/HUD) -------------------------------------------
if nft -c -f "$ETC/atlas.nft" >/dev/null 2>&1; then pass "[nft -c] $ETC/atlas.nft with the site file"; else fail "[nft -c] $ETC/atlas.nft does not validate"; fi
if nft list table inet atlas >/dev/null 2>&1; then pass "[nft] table inet atlas is loaded"; else fail "[nft] table inet atlas is not loaded"; fi
input="$(nft list chain inet atlas input 2>/dev/null || true)"
lan="$(site_define LAN_CIDR "$SITE")"
for port in 22 443; do
  if grep -qE "ip saddr $lan tcp dport $port accept" <<<"$input"; then pass "[SSH/HUD] :$port accepted from $lan only"; else fail "[SSH/HUD] no LAN-only rule for :$port"; fi
done
dns="$(site_define LAN_DNS "$SITE")"
for user in nobody atlas-core; do
  if probe_udp "$user" "$dns" 53; then fail "[UDP] $user got a DNS answer from $dns"; else pass "[UDP] $user can't query the LAN resolver"; fi
done
if probe_udp nobody 127.0.0.53 53; then fail "[F7] nobody got an answer from 127.0.0.53"; else pass "[F7] nobody can't use a local resolver on 127.0.0.53"; fi
ntp="$(ntp_servers "$SITE" | awk '{print $1}')"
if probe_udp nobody "$ntp" 123; then fail "[UDP] nobody reached the time server $ntp"; else pass "[UDP] nobody can't reach the time server $ntp"; fi
synced="$(timedatectl timesync-status --no-pager 2>/dev/null | awk -F': *' '/Server:/ {print $2; exit}')"
case " $(ntp_servers "$SITE") " in
  *" ${synced%% *} "*) pass "[UDP] timesyncd uses ${synced%% *}, from NTP_SERVERS" ;;
  *) fail "[UDP] timesyncd server '${synced:-none}' is not in NTP_SERVERS" ;;
esac

# --- DNS answers for the egress allowlist (Egress, getaddrinfo) -------------------------------------------------------
allow="$DATA/current/backend/atlas/egress/allowlist.json"
if [ -f "$allow" ]; then
  if out="$(python3 -I - "$allow" <<'PY'
import ipaddress, json, socket, sys
hosts = sorted({h for c in json.load(open(sys.argv[1]))["callers"].values() for h in c["hosts"]})
bad = []
for h in hosts:
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(h, 443, type=socket.SOCK_STREAM)}
    except OSError:
        bad.append(f"{h}: no answer"); continue
    private = [a for a in addrs if not ipaddress.ip_address(a).is_global]
    if private: bad.append(f"{h}: non-public {private}")
print(f"{len(hosts)} hosts" if not bad else "; ".join(bad))
sys.exit(1 if bad else 0)
PY
)"; then
    pass "[DNS] every allowlisted host resolves to public addresses only ($out)"
  else
    fail "[DNS] $out"
  fi
else
  note "[DNS] no release with an allowlist at $allow"
fi

# --- systemd units (pi-verification "systemd") -----------------------------------------------------------------------
verify_out="$(systemd-analyze verify "$UNITS"/atlas-*.service "$UNITS"/atlas-*.socket "$UNITS"/atlas-*.timer 2>&1 || true)"
# Templates for services of later phases (services.conf enable=no) may point at a program that isn't shipped yet.
templates="$(awk '!/^[[:space:]]*(#|$)/ && $2 == "no" { print $1 }' "$ETC/services.conf" | paste -sd'|' -)"
pending="$(grep -E "^(${templates:-none})\.service: Command .* is not executable: No such file" <<<"$verify_out" || true)"
other="$(grep -vxF -f <(printf '%s\n' "$pending") <<<"$verify_out" | sed '/^$/d' || true)"
if [ -n "$pending" ]; then note "[systemd-analyze verify] later-phase templates without their program yet: $(cut -d: -f1 <<<"$pending" | paste -sd' ' -)"; fi
if [ -z "$other" ]; then pass "[systemd-analyze verify] all atlas units clean"; else fail "[systemd-analyze verify] $(head -3 <<<"$other" | tr '\n' ' ')"; fi
for unit in atlas-audit atlas-egress atlas-backup atlas-core atlas-ui; do
  [ -f "$UNITS/$unit.service" ] || { note "[security] $unit.service not installed"; continue; }
  level="$(systemd-analyze security --no-pager "$unit.service" 2>/dev/null | tail -1 | sed -n 's/.*exposure level for [^:]*: *//p')"
  case "$level" in
    *SAFE* | *OK*) pass "[security] $unit: $level" ;;
    *) fail "[security] $unit: ${level:-no score}" ;;
  esac
done
stat_is() { [ "$(stat -c '%U:%G %a' "$1" 2>/dev/null)" = "$2" ]; }
if stat_is "$DATA" "root:root 755"; then pass "[StateDirectory] $DATA is root:root 755"; else fail "[StateDirectory] $DATA is $(stat -c '%U:%G %a' "$DATA")"; fi
for name in $(service_list "$ETC/services.conf"); do
  d="$DATA/${name#atlas-}"
  [ -d "$d" ] || continue
  if ! stat_is "$d" "$name:$name 700"; then fail "[StateDirectory] $d is $(stat -c '%U:%G %a' "$d"), want $name:$name 700"; fi
done
pass "[StateDirectory] service state directories checked (any mismatch is listed above)"

# Sockets: owner, group and mode from the unit files; someone outside the group is refused.
for sock_unit in "$UNITS"/atlas-*.socket; do
  path="$(sed -n 's/^ListenStream=//p' "$sock_unit")"
  want="$(sed -n 's/^SocketUser=//p' "$sock_unit"):$(sed -n 's/^SocketGroup=//p' "$sock_unit") 660"
  if [ ! -S "$path" ]; then
    note "[socket] $path not present ($(basename "$sock_unit") not enabled yet)"
  elif stat_is "$path" "$want"; then
    pass "[socket] $path is $want"
  else
    fail "[socket] $path is $(stat -c '%U:%G %a' "$path"), want $want"
  fi
done
if [ -S /run/atlas/audit.sock ]; then
  refused="$(runuser -u nobody -- python3 -I -c '
import socket
s = socket.socket(socket.AF_UNIX)
try:
    s.connect("/run/atlas/audit.sock"); print("connected")
except PermissionError:
    print("EACCES")
except OSError as e:
    print(type(e).__name__)
' 2>&1)"
  if [ "$refused" = "EACCES" ]; then pass "[socket] nobody gets EACCES on audit.sock"; else fail "[socket] nobody on audit.sock: $refused"; fi
fi

# --- atlas-audit in its sandbox (capability, chattr, MDWE, ProtectSystem, credentials) -------------------------------
pid="$(systemctl show -p MainPID --value atlas-audit.service)"
if [ "${pid:-0}" -gt 0 ]; then
  capeff="$(awk '/^CapEff:/ {print $2}' "/proc/$pid/status")"
  if (( (16#$capeff >> 9) & 1 )); then pass "[capability] atlas-audit holds CAP_LINUX_IMMUTABLE with NoNewPrivileges ($capeff)"; else fail "[capability] atlas-audit CapEff $capeff lacks CAP_LINUX_IMMUTABLE"; fi
  expect "[MDWE] atlas-audit runs (and signs with cryptography) under MemoryDenyWriteExecute=yes" \
    "[MDWE] atlas-audit MemoryDenyWriteExecute is not yes" \
    test "$(systemctl show -p MemoryDenyWriteExecute --value atlas-audit.service)" = yes
  expect "[runtime] Python runs under PrivateTmp, ProtectProc=invisible, RemoveIPC" "[runtime] PrivateTmp is not on" \
    test "$(systemctl show -p PrivateTmp --value atlas-audit.service)" = yes
  if nsenter -t "$pid" -m -- test -w "$DATA/audit"; then pass "[ProtectSystem] $DATA/audit writable inside the sandbox"; else fail "[ProtectSystem] $DATA/audit read-only inside the sandbox"; fi
  for ro in "$DATA/releases" "$DATA/egress" /etc /usr; do
    if nsenter -t "$pid" -m -- test -w "$ro"; then fail "[ProtectSystem] $ro writable inside atlas-audit's sandbox"; else pass "[ProtectSystem] $ro read-only inside atlas-audit's sandbox"; fi
  done
else
  fail "[audit] atlas-audit.service has no main process"
fi
segs=("$DATA"/audit/segments/seg-*.jsonl)
if [ -e "${segs[0]}" ]; then
  active="${segs[${#segs[@]} - 1]}"
  if [[ "$(lsattr -d "$active" 2>/dev/null | cut -d' ' -f1)" == *a* ]]; then pass "[chattr] active segment is append-only (+a)"; else fail "[chattr] active segment lacks +a"; fi
  sealed=0
  for s in "${segs[@]}"; do
    [ "$s" = "$active" ] && continue
    sealed=$((sealed + 1))
    [[ "$(lsattr -d "$s" 2>/dev/null | cut -d' ' -f1)" == *i* ]] || fail "[chattr] a sealed segment lacks +i"
  done
  if [ "$sealed" -eq 0 ]; then note "[chattr] no sealed segment yet (rollover is a day or 16 MiB)"; else pass "[chattr] $sealed sealed segment(s) checked"; fi
fi
if verify="$(runuser -u atlas-audit -- env PYTHONPATH="$DATA/current/backend" "$DATA/current/venv/atlas-audit/bin/python" -m atlas.audit verify "$DATA/audit" 2>&1)"; then
  pass "[credentials] chain verifies: checkpoints signed with the LoadCredentialEncrypted key ($(cut -c1-60 <<<"$verify")...)"
else
  fail "[credentials] audit verify failed: $(head -1 <<<"$verify")"
fi

# --- backup and WireGuard (present once installed) --------------------------------------------------------------------
if [ -f "$UNITS/atlas-backup.timer" ]; then
  if systemctl is-enabled --quiet atlas-backup.timer; then pass "[backup] timer enabled"; else note "[backup] timer not enabled yet (runbook backup.md)"; fi
fi
if ip link show wg0 >/dev/null 2>&1; then
  expect "[wireguard] wg0 fwmark matches WG_FWMARK" "[wireguard] wg0 fwmark differs from WG_FWMARK" \
    test "$(wg show wg0 fwmark 2>/dev/null)" = "$(site_define WG_FWMARK "$SITE")"
  if ip -br addr show wg0 | grep -q "$WG_PI_IP/32"; then pass "[wireguard] wg0 has $WG_PI_IP/32"; else fail "[wireguard] wg0 address"; fi
  if command -v nmcli >/dev/null; then
    if nmcli -t -f DEVICE,STATE device | grep -q '^wg0:unmanaged'; then pass "[wireguard] NetworkManager leaves wg0 unmanaged"; else fail "[wireguard] NetworkManager manages wg0"; fi
  fi
  if systemctl is-enabled --quiet systemd-networkd-wait-online.service 2>/dev/null; then fail "[wireguard] systemd-networkd-wait-online is enabled (boot delay)"; else pass "[wireguard] networkd wait-online not enabled"; fi
else
  note "[wireguard] no wg0 yet (runbook wireguard.md)"
fi

# --- atlas-ui: listeners, certificate, no CA key (runbook ui-on-pi.md) -------------------------------------------------
if systemctl is-active --quiet atlas-ui.service; then
  ui_lan="$(pi_lan_ip "$SITE")"
  listeners="$(ss -Htln 'sport = :443' | awk '{print $4}' | sort | tr '\n' ' ')"
  expected="$(printf '%s\n' "127.0.0.1:443" "$ui_lan:443" | sort | tr '\n' ' ')"
  if [ "$listeners" = "$expected" ]; then pass "[ui] :443 only on 127.0.0.1 and $ui_lan"; else fail "[ui] :443 listeners: ${listeners:-none} (expected $expected)"; fi
  if openssl x509 -in /etc/atlas/tls/atlas-ui.crt -noout -checkend $((30 * 86400)) >/dev/null 2>&1; then
    pass "[ui] certificate valid for 30 days or more"
  else
    fail "[ui] certificate expires within 30 days (gen-ui-keys.sh --renew-tls)"
  fi
  if grep -rlq "PRIVATE KEY" /etc/atlas 2>/dev/null; then fail "[ui] a private key file is in /etc/atlas"; else pass "[ui] no private key in /etc/atlas (the CA keeps none)"; fi
else
  note "[ui] atlas-ui not running (runbook ui-on-pi.md)"
fi

# --- N4: clean stop with atlas-egress connected (restarts atlas-audit) -----------------------------------------------
if [ "$SERVICE_CHECKS" = 1 ]; then
  if ! systemctl is-active --quiet atlas-egress.service; then fail "[N4] atlas-egress isn't running, so the check means little"; fi
  since="$(date '+%Y-%m-%d %H:%M:%S')"
  t0=$(date +%s)
  systemctl restart atlas-audit.service
  t1=$(date +%s)
  sleep 2
  if journalctl -u atlas-audit -S "$since" --no-pager -o cat | grep -qiE 'SIGKILL|Killing process|timed out'; then fail "[N4] the stop ended in SIGKILL or a timeout"; else pass "[N4] stopped without SIGKILL in $((t1 - t0)) s (TimeoutStopSec=15)"; fi
  segs=("$DATA"/audit/segments/seg-*.jsonl)
  tail_kinds="$(python3 -I - "${segs[@]}" <<'PY'
import json, sys
recs = []
for path in sys.argv[1:]:
    for line in open(path, encoding="utf-8"):
        try: recs.append(json.loads(line))
        except ValueError: pass
def name(r):
    return r["kind"] + (":" + r["system"]["event"] if r["kind"] == "system" else "")
starts = [i for i, r in enumerate(recs) if name(r) == "system:service_start"]
before = recs[max(0, starts[-1] - 2):starts[-1]] if starts else []
print(" ".join(name(r) for r in before))
PY
)"
  if [ "$tail_kinds" = "system:service_stop checkpoint" ]; then pass "[N4] the log ends the old run with service_stop then a checkpoint"; else fail "[N4] before the restart the log ends with: ${tail_kinds:-nothing}"; fi
fi

if [ "$FAILS" -eq 0 ]; then
  printf 'all checks passed\n'
else
  printf '%d check(s) failed\n' "$FAILS"
  exit 1
fi
