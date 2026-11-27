# Runbook: link atlas-core on the Pi to LM Studio on the desktop

Source: ADR 0004 (Gemma 4 E4B, reasoning off), PLAN §2.8 (R-LLM), §5.3 (R-LINK); contracts/services.md §2 (tunnel `10.77.0.1` ↔ `10.77.0.2`, LM Studio `:1234`) and §4 (`lmstudio-api-token`). Phase 1.

**Goal:** the HUD on the Pi answers with the real model. atlas-core talks to LM Studio over the WireGuard tunnel, with its own API token held in systemd-creds. This replaces the dev link from the Mac (llm-link-dev.md).

**First step: LM Studio only (`--no-hostd`).** Until `atlas-hostd` runs on the desktop, atlas-core doesn't wake the desktop or put it to sleep. The desktop must be awake and signed in, with LM Studio's server running. Wake-on-LAN and sleep come with hostd (step 7).

## How the flags reach atlas-core

The unit runs `python -m atlas.core $ATLAS_CORE_ARGS`. You don't edit the unit:

1. You edit `/etc/atlas/core-link.conf` (root-owned; `provision-pi.sh` creates it the first time, fully commented out, and never overwrites it).
2. `provision-pi.sh` checks the file and writes `/etc/systemd/system/atlas-core.service.d/link.conf` with `Environment="ATLAS_CORE_ARGS=..."`.

Only these flags are accepted: `--backend --model --reasoning-effort --no-hostd --mac --wol-broadcast --wol-port --model-vram-mb --idle-sleep`. The LM Studio and hostd URLs stay at their defaults on `10.77.0.2`, and the token file, sockets and state directory keep the unit's values. Quotes, `\`, `$`, a backtick or `%` inside the value stop provisioning. The file holds no secret: the token is a credential (step 3). `pi-verify.sh` checks the file and that the drop-in still matches it.

With the file commented out or the value empty, atlas-core runs exactly as before the link: `--backend none`, the orb shows `llm_unreachable`, and only time, date and help are answered on the Pi.

## Before you start

- main merged with this runbook, and the Mac checks pass (`ruff`, `mypy`, `pytest`).
- The desktop: LM Studio 0.4 or later, `google/gemma-4-e4b` (Q4_K_M) downloaded, context length 16384 (ADR 0004).
- WireGuard isn't set up on the desktop yet: steps 1 and 2 do that (runbook wireguard.md). On the Pi, `wireguard-tools` and the Pi's key are already in place (2026-11-25).
- A maintenance window on the Pi: console or SSH, `tmux`, `sudo -v`, overlay off (pi-bringup §11). Steps 3 to 5 write `/etc` and `/var/lib/atlas`.
- **Never paste the token into a Claude Code session, a chat or a shell command line.** It goes from LM Studio straight into `store-credential.sh`, which reads it without echo.

## 1. Desktop: LM Studio

1. Developer tab: **Require Authentication** on. Create a token named `atlas-pi` and keep the window open: step 3 needs it. Then **delete the `atlas-dev` token**. That ends the Mac's dev link (llm-link-dev.md §5); `rm ~/.atlas-dev/lmstudio-token` on the Mac too.
2. Leave **Serve on Local Network** on. LM Studio then listens on every interface, including the tunnel's, and the firewall rules from step 2 limit port 1234 to `10.77.0.1` inside the tunnel. They block it from everything else, the Mac included.
3. Load `google/gemma-4-e4b` with context length 16384. Leave reasoning to the request: atlas-core sends `reasoning_effort: none`.
4. Turn off conversation logging, or clear LM Studio's logs (PLAN §2.8, "Prompts on disk"). The B5 runs left prompts there.

## 2. Desktop: WireGuard and the firewall

Follow wireguard.md §2 (the `atlas` tunnel) and §4 (`desktop-firewall.ps1`, first without `-Apply` to read the plan, then with it). Before the Pi side is done, the tunnel shows no handshake. That's expected.

## 3. Pi: tunnel, token and link file (window)

```sh
tmux new -s link && sudo -v
# wireguard.md §3: the desktop's public key and LAN address
echo '<the desktop public key>' | sudo tee /etc/atlas/wireguard-desktop.pub
sudo nano /etc/atlas/nftables-site.nft        # DESKTOP_LAN_IP = the desktop's reserved LAN address

# the token, typed or pasted at the hidden prompt (never as an argument)
sudo ~/atlas-deploy/provisioning/store-credential.sh atlas-core lmstudio-api-token
```

`store-credential.sh` refuses to overwrite an existing credential. To rotate the token, remove the old `.cred` file deliberately first (theft.md).

`/etc/atlas/core-link.conf` doesn't exist until the next `provision-pi.sh` run creates it. Run provisioning (step 4) once, then edit the file and run provisioning again. Or create the file now with the one line below:

```sh
sudo nano /etc/atlas/core-link.conf
# uncomment (or write) exactly one line:
ATLAS_CORE_ARGS="--backend lmstudio --model google/gemma-4-e4b --reasoning-effort none --no-hostd"
```

## 4. Pi: provision and deploy

```sh
cd ~/atlas-deploy/provisioning
sudo ATLAS_ADMIN_USER=joe ATLAS_LAN_CIDR=192.168.0.0/24 ATLAS_LAN_BROADCAST=192.168.0.255 \
  ATLAS_LAN_PREFIX6=fd00::/8 ./provision-pi.sh --dry-run
# read the plan: wg0 configured, "write /etc/systemd/system/atlas-core.service.d/link.conf", no "error:"
sudo ATLAS_ADMIN_USER=joe ATLAS_LAN_CIDR=192.168.0.0/24 ATLAS_LAN_BROADCAST=192.168.0.255 \
  ATLAS_LAN_PREFIX6=fd00::/8 ./provision-pi.sh
```

The firewall reloads, so **open SSH sessions freeze**. Reconnect and `tmux attach -t link`. The same run also applies anything else pending for provisioning, such as atlas-core's ProcSubset change from 2026-11-25.

Then deploy main from the Mac (deploy.md): `deploy/provisioning/deploy.sh --dry-run`, then `deploy/provisioning/deploy.sh`. The Pi still runs 8281a4f, which doesn't have the LM Studio stall guard or the B5 fixes. If the deploy's health check passes, atlas-core is already running with the new drop-in. If you only changed `core-link.conf`, `sudo systemctl restart atlas-core` instead.

## 5. Check it

On the Pi:

```sh
sudo wg show wg0                                    # latest handshake: seconds ago
sudo runuser -u atlas-core -- curl -s -o /dev/null -w '%{http_code}\n' http://10.77.0.2:1234/v1/models
#   401: the tunnel, both firewalls and LM Studio work, and LM Studio requires a token (no token is sent here)
sudo runuser -u nobody -- curl -s -m 3 http://10.77.0.2:1234/v1/models; echo "rc=$?"   # must fail (rc 28 or 7)
systemctl show -p Environment --value atlas-core    # ...ATLAS_CORE_ARGS=--backend lmstudio ...
systemctl is-active atlas-core && journalctl -u atlas-core -n 30 --no-pager
sudo ~/atlas-deploy/provisioning/pi-verify.sh       # [core link] lines PASS; flags note says --backend lmstudio
```

From the Mac, the desktop's port must not answer: `nc -vz -w 3 <desktop LAN IP> 1234` times out.

In the HUD (`https://<Pi LAN IP>/`, a paired device):

| You do | You should see |
|---|---|
| Open the HUD | The orb no longer shows `LINK DOWN` ("No desktop linked") |
| "what time is it" | Answered on the Pi, as before |
| "tell me a joke" | A streamed answer from Gemma |
| Stop LM Studio's server, ask again | `The model isn't ready`, orb `llm_unreachable`; start the server, and the next question works |

Then turn the overlay back on (pi-bringup §11) and run `sudo ~/atlas-deploy/provisioning/base-os.sh check`.

## 6. Roll back

Comment out the `ATLAS_CORE_ARGS` line (or empty it), rerun `provision-pi.sh`, then `sudo systemctl restart atlas-core`. Core is unlinked again. The token credential can stay. To remove it, delete it in LM Studio and remove `/var/lib/atlas/credentials/atlas-core/lmstudio-api-token.cred`.

## 7. Later: with atlas-hostd

Once `atlas-hostd` runs on the desktop (B6), replace `--no-hostd` with `--mac <the desktop's wired NIC MAC>` and, when B6 has measured it, `--model-vram-mb <n>`. `--idle-sleep` lets ATLAS put the desktop to sleep after 20 idle minutes (hostd refuses while you use the machine). The Wake-on-LAN broadcast comes from the unit's `lan.conf` drop-in. Rerun `provision-pi.sh` and restart atlas-core.

## Troubleshooting

| Symptom | Check |
|---|---|
| `provision-pi.sh` stops with `error: /etc/atlas/core-link.conf: ...` | The message names the flag or character. Fix the file; nothing was written |
| `atlas-core` exits with status 2 | `journalctl -u atlas-core`: `--backend lmstudio` without `--model`, or no signing key |
| HUD alert `LLM_AUTH_FAILED` | The stored token doesn't match LM Studio's `atlas-pi` token. Rotate it (step 3) |
| The `curl` check as `atlas-core` hangs | No handshake: wireguard.md Troubleshooting |
| `curl` gets a handshake but no 401 | LM Studio's server isn't running, or the desktop firewall's allow rule is missing (`desktop-firewall.ps1` without `-Apply` lists what it expects) |
| pi-verify: `link.conf is missing or stale` | `core-link.conf` was edited after the last provisioning run. Rerun `provision-pi.sh` |

## Tested

- On the Mac: `backend/tests/test_ui_on_pi.py` checks the unit line, the template (inert, and both examples parse to the ADR 0004 flags with the URLs on `10.77.0.2`), the flag allowlist, and refusal of other flags, `$()`, backticks, `\`, `%`, single quotes and a second line.
- In the ARM64 VM (systemd 257, 2026-11-27): `lib.sh` generated the drop-in for a throwaway unit with the same `ExecStart` form. The ADR 0004 line arrived as seven separate arguments; `--model *` stayed a literal `*`; the commented-out template and a missing drop-in both gave no arguments (unlinked); `%h` was refused before anything was written. `shellcheck` clean on `lib.sh`, `pi-verify.sh` and `provision-pi.sh`.
- Not tested: the desktop side (WireGuard for Windows, `desktop-firewall.ps1`) and the real model over the tunnel. Their first run is yours.
