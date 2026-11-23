#!/usr/bin/env bash
# Create atlas-ui's TLS certificate and device-token key on the Pi (contracts/services.md sections 2 and 4; runbook
# docs/runbooks/ui-on-pi.md).
#
#   sudo deploy/provisioning/gen-ui-keys.sh                # first time: CA + server certificate + device-token key
#   sudo deploy/provisioning/gen-ui-keys.sh --renew-tls    # new CA and certificate (expiry, or PI_LAN_IP changed)
#
# The ATLAS local CA keeps NO private key: it signs the one server certificate, then its key is deleted (it only ever
# exists in RAM, under /run). A stolen card therefore can't mint a certificate the owner's devices trust, and the CA's
# name constraints limit it to this Pi's LAN address, 127.0.0.1 and localhost even if it could. Renewing makes a new CA;
# each device then trusts the new ca.crt once.
#
# Outputs: /etc/atlas/tls/ca.crt and /etc/atlas/tls/atlas-ui.crt (public), the credentials atlas-ui/tls-key and
# atlas-ui/device-token-key (encrypted with the host key). Nothing secret is printed, so it may run in the shared tmux
# session. The step-up PIN is separate and needs the console: gen-ui-pin.sh.
# Run it in a maintenance window: /etc is on the read-only root.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
. "$here/lib.sh"

renew=0
for arg in "$@"; do
  case "$arg" in
    --renew-tls) renew=1 ;;
    -h | --help) sed -n '2,19p' "$0"; exit 0 ;;
    *) die "unknown argument: $arg" ;;
  esac
done
need_root
command -v openssl >/dev/null || die "openssl is required"
! root_is_overlay || die "the read-only overlay is on: /etc/atlas/tls would vanish at the next boot (maintenance window)"
site=/etc/atlas/nftables-site.nft
[ -f "$site" ] || die "$site missing: run provision-pi.sh first"
ip="$(pi_lan_ip "$site")"
[ -n "$ip" ] || die "set 'define PI_LAN_IP = <the Pi's reserved LAN address>' in $site first"
[ -d "$UI_CRED_DIR" ] || die "$UI_CRED_DIR missing: run provision-pi.sh first"

key_cred="$UI_CRED_DIR/tls-key.cred"
if [ "$renew" = 1 ]; then
  log "renewing: removing the old certificate and key"
  rm -f "$key_cred" "$UI_TLS_DIR/atlas-ui.crt" "$UI_TLS_DIR/ca.crt"
elif [ -e "$key_cred" ]; then
  die "$key_cred exists. To make a new CA and certificate: $0 --renew-tls"
fi

umask 077
tmp="$(mktemp -d -p /run atlas-ui-tls.XXXXXX)" # tmpfs: the keys never touch the card in the clear
trap 'rm -rf "$tmp"' EXIT
today="$(date -u +%Y-%m-%d)"

openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$tmp/ca.key" 2>/dev/null
openssl req -x509 -new -key "$tmp/ca.key" -sha256 -days "$UI_CERT_DAYS" -subj "/CN=ATLAS local CA $today" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -addext "nameConstraints=critical,permitted;IP:$ip/255.255.255.255,permitted;IP:127.0.0.1/255.255.255.255,permitted;DNS:localhost" \
  -out "$tmp/ca.crt"
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$tmp/server.key" 2>/dev/null
openssl req -new -key "$tmp/server.key" -subj "/CN=$ip" -out "$tmp/server.csr"
printf '%s\n' "basicConstraints=critical,CA:FALSE" "keyUsage=critical,digitalSignature" \
  "extendedKeyUsage=serverAuth" "subjectAltName=IP:$ip,IP:127.0.0.1,DNS:localhost" >"$tmp/ext"
openssl x509 -req -in "$tmp/server.csr" -CA "$tmp/ca.crt" -CAkey "$tmp/ca.key" -set_serial "0x$(openssl rand -hex 16)" \
  -days "$UI_CERT_DAYS" -sha256 -extfile "$tmp/ext" -out "$tmp/server.crt" 2>/dev/null
rm -f "$tmp/ca.key"
log "the CA's private key is deleted: this CA can sign nothing more"
openssl verify -CAfile "$tmp/ca.crt" "$tmp/server.crt" >/dev/null || die "the new certificate does not verify"

"$here/store-credential.sh" atlas-ui tls-key <"$tmp/server.key"
rm -f "$tmp/server.key"
install -d -m 0755 -o root -g root "$UI_TLS_DIR"
install -m 0644 -o root -g root "$tmp/ca.crt" "$UI_TLS_DIR/ca.crt"
install -m 0644 -o root -g root "$tmp/server.crt" "$UI_TLS_DIR/atlas-ui.crt"

if [ -e "$UI_CRED_DIR/device-token-key.cred" ]; then
  log "ok      device-token-key (kept: replacing it would unpair every device)"
else
  openssl rand 32 | "$here/store-credential.sh" atlas-ui device-token-key
fi

fingerprint="$(openssl x509 -in "$UI_TLS_DIR/ca.crt" -noout -fingerprint -sha256 | cut -d= -f2)"
until="$(openssl x509 -in "$UI_TLS_DIR/atlas-ui.crt" -noout -enddate | cut -d= -f2)"
cat <<EOF
atlas-ui certificate for https://$ip (and 127.0.0.1, localhost), valid until $until.
Trust $UI_TLS_DIR/ca.crt on each device that opens the HUD (runbook ui-on-pi.md). Before trusting, check that the
device shows this SHA-256 fingerprint:
  $fingerprint
EOF
if [ "$renew" = 1 ]; then
  run systemctl try-restart atlas-ui.service
fi
