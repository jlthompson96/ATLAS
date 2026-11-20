# Runbook: the HUD on live (fake-model) data, on your Mac

Source: PLAN Phase 2 (`atlas-ui`), contracts/services.md (atlas-ui, `core.sock`). Code: `backend/atlas/ui_server/`, `backend/atlas/core/control.py`.

This runs the **real** atlas-core orchestrator and approval flow (with the scripted fake model), the **real** control socket, the **real** `atlas-ui` server over HTTPS/WSS, and the built HUD. Weather, mail counts, markets and host status are demo fixtures from the mock scenarios' baseline; the orb, transcript, pending actions and approvals are live. For pure HUD work against scripted scenarios, keep using the mock server (CLAUDE.md).

## Run it

```sh
cd frontend/hud && npm ci --ignore-scripts && npm run build        # once, and after HUD changes
cd ../../backend && .venv/bin/pip install --require-hashes -r requirements-dev.lock   # once (includes the ui extra)
.venv/bin/python -m atlas.ui_server.dev                              # prints a URL and a pairing code
```

1. Open the printed `https://localhost:8443`. The certificate comes from a throwaway CA the script just made in `backend/.atlas-dev/tls/`; either click through the warning or trust `ca.pem` (Keychain Access, "Always Trust"). Nothing in `.atlas-dev/` is committed (gitignored).
2. You land on the pairing page. Enter the printed code (`XXXX-XXXX`, valid 10 minutes, single use). This registers the browser as a **remote** device and sets a `__Host-atlas-device` cookie (HttpOnly, Secure, SameSite=Strict).
3. The HUD opens on live state. Try these in the text box:
   - `what time is it`, `check my email`: the real orchestrator runs the scripted model and its tools; the orb goes thinking, then idle.
   - `send an email to Dana`: a pending action appears (exact payload, SHA-256, provenance). Press Approve, enter the PIN **`0000`**, confirm. Core checks the device, step-up token, hash and expiry, signs the approval, and the dev executor "sends" it after a second (it only logs the action ID).
   - `research solar panels`: queues a (stand-in) research task.
4. Options: `--port N`, `--kiosk-code` (also prints a code for a **kiosk** device: view-only, no approvals, no PIN; pair a second browser or a private window with it to see AP-4 in the UI), `--dir`, `--static-dir`.
5. `Ctrl-C` stops everything. Restarting keeps your paired browsers (`backend/.atlas-dev/state/devices.json`) but needs a new pairing code only for new browsers.

Pair more devices or revoke them while it runs:

```sh
cd backend
.venv/bin/python -m atlas.ui_server pair --role remote --state-dir .atlas-dev/state     # new code
.venv/bin/python -m atlas.ui_server devices --state-dir .atlas-dev/state
.venv/bin/python -m atlas.ui_server revoke dev-1a2b3c4d5e6f --state-dir .atlas-dev/state  # closes its open WebSocket within ~5 s
```

## Check it in a real browser (UI-SEC-3/4)

```sh
# pair with curl (or use a code from `pair`), then run the HUD's browser checker with the device cookie:
cd frontend/hud
node scripts/browser-check.mjs https://localhost:8443/ --insecure --cookie '__Host-atlas-device=<token from Set-Cookie>' --axe
```

It fails on any CSP violation, off-origin request, console error or serious axe issue. `--insecure` only ignores the dev CA's certificate error.

## What is where

| Piece | File | Notes |
|---|---|---|
| Core control socket | `backend/atlas/core/control.py` (server), `control_client.py` (client) | Newline-JSON; peer identity from `SO_PEERCRED` / `LOCAL_PEERCRED`; per-peer operation table; protocol in the module docstring. |
| atlas-ui server | `backend/atlas/ui_server/app.py` | TLS, security headers, static HUD, pairing endpoint, `/ws/events`, atlas-core link with reconnect. |
| One browser connection | `session.py` | events.v1 validation of every frame, privacy mode, step-up, forwarding to core. |
| Devices and pairing | `devices.py` | HMAC device tokens, hashed single-use pairing codes, JSON registry in the state dir. |
| Step-up PIN | `pin.py` | Argon2id; 5 wrong PINs lock a device for 5 minutes, 15 across devices lock all. |
| Dev wiring | `dev.py`, `devtls.py` | Core + UI in one process (real socket between them); throwaway CA. |

## Dev versus the Pi

- **Peer check.** On the Pi, `core.sock` accepts only the service users in contracts/services.md (`KernelPeers`, via the `atlas-core-clients` group). A Mac has no `atlas-ui` user, so the dev runner uses `SameUserPeers`: same uid as core, attributed to `atlas-ui`, socket mode 0660 inside a `0700` temp directory under `/tmp` (AF_UNIX paths are short).
- **Credentials.** On the Pi `tls-key`, `device-token-key` and `stepup-pin-hash` come from systemd credentials (`python -m atlas.ui_server serve --tls-cert ... --credentials-dir "$CREDENTIALS_DIRECTORY"`). The dev runner writes the same three as plain files under `backend/.atlas-dev/` with PIN `0000`. Make a real PIN hash with `python -m atlas.ui_server.pin` (reads the PIN from the terminal, never from argv).
- **Pi aarch64 lock.** The `ui` extra (`websockets`, `jsonschema`, `argon2-cffi`) is locked for this Mac only. The Linux aarch64 / Python 3.13 runtime lock for atlas-ui is generated in the ARM64 Debian VM later (CLAUDE.md, Dependencies). No `deploy/` unit for atlas-ui exists yet.
- **Executor and model.** The dev executor only logs; the model is `ScriptedBackend`. The LM Studio backend, hostd and wake flow are separate work.

## Known limits (by design, for now)

- **PIN verification is in atlas-ui** (current contract). Core trusts the step-up tokens atlas-ui registers, so a compromised atlas-ui could register a token for a device it controls. The pending proposal 2026-11-18-security-core-followups F3 moves the PIN check into core; not implemented here.
- Core learns a device's role from atlas-ui's `device.announce`. Core still refuses approvals from kiosks and unknown devices itself (AP-4), but it does not have an independent device list yet.
- **Pairing is a `GET /api/pair`** with the code in a header, because the websockets HTTP parser reads GET requests only. A custom header cannot be sent cross-site without a CORS preflight, which atlas-ui never answers, and the Origin must match as well.
- No voice (`ptt.start` answers `VOICE_UNAVAILABLE`) and no portfolio (`portfolio.unlock` answers `NOT_AVAILABLE`) until those services exist.
- UI-SEC-4 "zero CSP violations in the browser" is checked with `browser-check.mjs`, by hand, not in pytest.
