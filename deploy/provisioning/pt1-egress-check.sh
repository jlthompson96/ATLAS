#!/usr/bin/env bash
# PT-1 (PLAN 8.2): for each ATLAS service user, try outbound connections to a canary internet host on 53, 80, 443 and some
# random ports. Every attempt must FAIL. Then check the one legitimate path: through atlas-egress, with the right caller.
#
#   sudo CANARY_HOST=<a host you control or any public IP> deploy/provisioning/pt1-egress-check.sh
#
# Run on the Pi (or the ARM64 VM with atlas.nft loaded). Exit code 0 only if nothing leaked and the proxy path behaves.
# CANARY_HOST has no default on purpose: use an address you are happy to send a SYN to.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"
need_root
CANARY_HOST="${CANARY_HOST:?set CANARY_HOST to a public IP or hostname}"
conf="${SERVICES_CONF:-/etc/atlas/services.conf}"
[ -f "$conf" ] || conf="$here/services.conf"

leaks=0
try_connect() { # try_connect USER HOST PORT -> 0 if the TCP connection succeeded
  runuser -u "$1" -- timeout 3 bash -c "exec 3<>/dev/tcp/$2/$3" 2>/dev/null
}

for user in $(service_list "$conf"); do
  if [ "$user" = "atlas-egress" ]; then
    continue # the only user allowed out; checked separately below
  fi
  for port in 53 80 443 22 8080 "$((20000 + RANDOM % 40000))"; do
    if try_connect "$user" "$CANARY_HOST" "$port"; then
      log "LEAK: $user connected to $CANARY_HOST:$port"
      leaks=$((leaks + 1))
    fi
  done
done

# atlas-egress itself: 443 may work, everything else must fail (80 stays closed until Phase 5).
for port in 53 80 22 8080; do
  if try_connect atlas-egress "$CANARY_HOST" "$port"; then
    log "LEAK: atlas-egress connected to $CANARY_HOST:$port"
    leaks=$((leaks + 1))
  fi
done
# atlas-egress must not reach private addresses even on 443 (LAN router, loopback services other than its own).
for target in 192.0.2.1 169.254.169.254; do
  if try_connect atlas-egress "$target" 443; then
    log "LEAK: atlas-egress reached non-public $target:443"
    leaks=$((leaks + 1))
  fi
done

# The proxy path: each caller only on its own port, only for its allowlist (needs the egress service and the network).
check_proxy() { # check_proxy USER PORT HOST EXPECT(allow|deny)
  local code
  code="$(runuser -u "$1" -- curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 \
    --proxy "http://127.0.0.1:$2" "https://$3/" 2>/dev/null || true)"
  if [ "$4" = "deny" ] && [ "$code" != "000" ]; then
    log "FAIL: $1 got HTTP $code from $3 via proxy port $2 (expected a refusal)"
    leaks=$((leaks + 1))
  fi
}
check_proxy atlas-google 3131 gmail.googleapis.com allow # informational: needs the real internet, not asserted
check_proxy atlas-google 3131 discord.com deny           # not on atlas-google's allowlist
check_proxy atlas-google 3131 example.org deny
# Wrong port for the caller: nftables drops it before the proxy ever sees it.
if runuser -u atlas-google -- timeout 3 bash -c 'exec 3<>/dev/tcp/127.0.0.1/3132' 2>/dev/null; then
  log "FAIL: atlas-google reached atlas-discord's proxy port 3132"
  leaks=$((leaks + 1))
fi

if [ "$leaks" -eq 0 ]; then
  log "PT-1 egress check passed"
else
  log "PT-1 egress check FAILED ($leaks problem(s))"
  exit 1
fi
