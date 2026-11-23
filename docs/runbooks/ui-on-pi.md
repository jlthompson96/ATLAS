# Runbook: the HUD on the Pi (`atlas-ui` and `atlas-core`)

Scope: put `atlas-core` and `atlas-ui` on the Pi so that a browser on the home LAN shows the real HUD at `https://<Pi LAN address>/`. No display is needed; the kiosk on the Pi's own screen comes later. Sources: PLAN Phase 2, contracts/services.md 1.5.0 (sections 2, 4 and 6), [proposal 2026-11-23-release-hud](../../contracts/proposals/2026-11-23-release-hud.md).

**What you get until the desktop is linked:** `atlas-core` runs with `--backend none`. The orb shows `LINK DOWN` ("No desktop linked"). Time, date and help are answered on the Pi; every other request gets "No desktop is linked to ATLAS yet…". Nothing reaches a model, and nothing can be sent: an approval would go to `atlas-executor`, which isn't installed, and fail.

**Who does what:** **Owner** steps need you at the Pi's console or a browser. **Claude** steps run in the shared tmux session after you've typed `sudo -v`. Steps 1–4 need the read-only overlay off (they write `/etc`); deploys and pairing don't.

## Before you start

- **Router:** a DHCP reservation for the Pi. The certificate names the Pi's LAN address; if the address changes, the certificate must be renewed (step 9).
- A release that contains the HUD and the `atlas-ui` venv: any `deploy.sh` from main after this change.
- Overlay off (pi-bringup §11), tmux and `sudo -v`.

## Steps

| # | Who | What |
|---|---|---|
| 1 | Claude | In `/etc/atlas/nftables-site.nft`, add `define PI_LAN_IP = <the reserved address>`. It isn't used by the firewall rules, but changing the site file reloads the firewall: open SSH sessions freeze for a moment and reconnect. Then copy `deploy/` from main and run `provision-pi.sh`. Expect `install` of `atlas-ui.service` and `atlas-core.service`, the `atlas-ui` drop-in with `ATLAS_UI_LAN_IP`, `/etc/atlas/tls`, and notes that `atlas-core` and `atlas-ui` are not enabled yet (credentials missing). |
| 2 | Claude | `sudo ~/atlas-deploy/provisioning/gen-core-keys.sh`: the Ed25519 approval-signing key, encrypted; prints only the public key (also saved to `/etc/atlas/approval-signing.pub` for `atlas-executor` later). |
| 3 | Claude | `sudo ~/atlas-deploy/provisioning/gen-ui-keys.sh`: a new local CA, one server certificate for `PI_LAN_IP`, `127.0.0.1` and `localhost` (800 days), then the CA's private key is **deleted**; plus the device-token key. Prints the CA's SHA-256 fingerprint: write it down for step 7. |
| 4 | Claude | `deploy.sh` (if the active release predates this change), then `provision-pi.sh` again: `atlas-core` is enabled now; `atlas-ui` still waits for the PIN. |
| 5 | **Owner** | **At the Pi's keyboard and screen**, not tmux or SSH: `sudo ~/atlas-deploy/provisioning/gen-ui-pin.sh`. Choose the approval PIN (4–12 digits; 6 or more is better) and type it twice. It isn't shown, and Claude never sees it or its hash. |
| 6 | Claude | `provision-pi.sh` once more: `atlas-ui` is enabled. `sudo systemctl start atlas-core.service atlas-ui.service`, then `sudo ~/pv/pi-verify.sh`: expect `[security] atlas-core`, `[security] atlas-ui`, `[ui] :443 only on 127.0.0.1 and <address>`, `[ui] certificate valid…`, `[ui] no private key in /etc/atlas`. Overlay back on (byte-for-byte), reboot, and check that both services come back by themselves. |
| 7 | **Owner** | Trust the CA on each device (below), then pair it (step 8). |
| 8 | **Owner** | Pair a browser: over your own SSH session or at the console, `sudo ~/atlas-deploy/provisioning/ui-devices.sh pair remote` prints a one-time code (10 minutes, single use). Open `https://<Pi address>/` on the device and type the code. It refuses to run inside tmux or screen, which Claude can read: the code is what lets a browser in. |

### Trusting the CA on a device

Copy `/etc/atlas/tls/ca.crt` from the Pi (it's public): `scp admin@<pi>:/etc/atlas/tls/ca.crt ~/Downloads/atlas-ca.crt`. Before trusting it, compare its fingerprint with the one `gen-ui-keys.sh` printed: `openssl x509 -in ~/Downloads/atlas-ca.crt -noout -fingerprint -sha256`.

- **Mac:** double-click the file (Keychain Access opens), add it to the *login* keychain, open it, expand *Trust*, set *When using this certificate* to *Always Trust*.
- **iPhone/iPad:** AirDrop or mail the file to yourself, install the profile (Settings → *Profile Downloaded*), then Settings → General → About → Certificate Trust Settings → turn on full trust for "ATLAS local CA".
- **Windows desktop:** `certmgr.msc` → *Trusted Root Certification Authorities* → *Certificates* → *All Tasks* → *Import*.
- **Android:** Settings → Security → *Encryption & credentials* → *Install a certificate* → *CA certificate*.

The CA can't vouch for anything else: its name constraints allow only the Pi's address, `127.0.0.1` and `localhost`, and its private key no longer exists.

## Devices

```sh
sudo ~/atlas-deploy/provisioning/ui-devices.sh list
sudo ~/atlas-deploy/provisioning/ui-devices.sh revoke dev-0123abcd4567    # a lost phone, a browser you no longer use
sudo ~/atlas-deploy/provisioning/ui-devices.sh pair kiosk                 # later: the Pi's own screen (view-only)
```

Revoking takes effect within seconds; the device must pair again with a new code.

## Changing the PIN, renewing the certificate, rotating keys

| What | How | Effect |
|---|---|---|
| PIN | `sudo gen-ui-pin.sh --change` at the console | `atlas-ui` restarts; paired devices stay paired |
| Certificate (expiry within 30 days, `pi-verify` fails; or the Pi's address changed) | Overlay off, update `PI_LAN_IP` if needed, `sudo gen-ui-keys.sh --renew-tls`, then `provision-pi.sh` if the address changed | A new CA: every device trusts the new `ca.crt` once (and removes the old one). Paired devices stay paired. |
| Device-token key | Not by script: remove `atlas-ui/device-token-key.cred` deliberately, rerun `gen-ui-keys.sh --renew-tls` | Every device must pair again |
| Approval-signing key | Remove `atlas-core/approval-signing-key.cred` deliberately, rerun `gen-core-keys.sh` | `atlas-executor`'s public key (when it exists) must be replaced too |

After a theft ([theft.md](theft.md)): the attacker has the card's credentials, but not the CA key (there is none). Remove the old CA from every device, then renew the certificate and rotate the device-token key on the replacement card.

## When something goes wrong

- **`atlas-ui` won't start:** `journalctl -u atlas-ui -b`. *"credential … not readable"* or status `243/CREDENTIALS`: a `.cred` is missing (`provision-pi.sh` names it). *"Cannot assign requested address"*: `PI_LAN_IP` isn't the Pi's current address.
- **`atlas-core` exits with status 2:** the message names the problem (no signing key, or `--backend lmstudio` without `--model`).
- **The browser warns about the certificate:** the device doesn't trust this CA (or still trusts an old one), or you used a name the certificate doesn't cover. Use the IP address, not the hostname.
- **"core unreachable" on the orb:** `atlas-core` is down or restarting; `atlas-ui` reconnects by itself.
- **A deploy fails the health check on `atlas-ui` or `atlas-core`:** it rolls back. Check the journal as above.

## Notes

- `atlas-core`'s `lmstudio-api-token` doesn't exist until the desktop is linked. The unit's blank default (`SetCredential=lmstudio-api-token:\n`) lets it start; the LM Studio backend refuses a blank token, so a linked core fails closed if the real credential is missing. (An empty `SetCredential=` value is invalid syntax in a unit file on systemd 257: found in the VM.)
- `provision-pi.sh` enables a service only once every credential its unit loads exists (and, for `atlas-ui`, `PI_LAN_IP` and the certificate). Rerun it after storing credentials.
- The HUD ships inside each release (`/var/lib/atlas/current/hud`), built on the Mac from the committed `frontend/hud`; rollback rolls the HUD back with the code.
- Release code that these scripts run (the PIN hasher, `ui-devices.sh`) runs as `atlas-ui` under `systemd-run` with the unit's sandbox (no network, read-only filesystem except the state directory for `ui-devices.sh`), on pipes only: never as root and never on your terminal.
- The console check (`need_local_console` in `lib.sh`, also used by `gen-backup-key.sh`) asks logind whether the login session is local on a virtual console (`Remote=no`, `TTY=ttyN`), refuses if tmux, screen or sshd is among the process's ancestors, and needs stdin and stdout on the same terminal. In the VM it passed in a login session on `tty5`, plain and through sudo, and refused SSH, tmux started over SSH, tmux started from the console login, and redirected output.
- Tested end to end in a clone of the ARM64 VM on 2026-11-23: provisioning, all three key scripts (the PIN step with its exact hasher pipeline, since the VM has no console), deploy with the HUD, pairing (a used code refused, a foreign Origin refused), a paired WSS client (`LINK_DOWN`, local time, the fixed line), a reboot, certificate renewal with the device still paired, `pi-verify.sh`, and the HUD in headless Chrome from the Mac with zero CSP violations, off-origin requests, console errors or axe findings.
