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

## With the real model (LM Studio on your desktop)

The same runner takes the CLI's LM Studio options (set up the desktop side and the token file first: [llm-link-dev.md](llm-link-dev.md)):

```sh
cd backend
.venv/bin/python -m atlas.ui_server.dev --backend lmstudio \
  --base-url http://<desktop-lan-ip>:1234 --token-file ~/.atlas-dev/lmstudio-token --no-hostd
```

- Typed requests go the same way as in the CLI. The local grammar answers time, date and desktop wake/sleep/status without the model. Everything else goes through the orchestrator and policy engine to LM Studio.
- With hostd running on the desktop, drop `--no-hostd` and add `--hostd-url http://<desktop-ip>:8770 --mac <aa:bb:..>`. Then:
  - A sleeping desktop is woken, and the orb shows `waking_host` with its stage.
  - If the desktop doesn't come up, the orb shows `llm_unreachable` and the request is saved.
  - The next request that reaches the model delivers the saved ones first.
- Add `--idle-sleep` to let ATLAS put the desktop back to sleep after 20 idle minutes.
- The demo inbox (Dana's fake email) and the recipient allowlist (`dana@example.org`) stay the same, so `send an email to Dana` still produces an approval card if the model proposes it. The model decides, so the wording varies.
- The same caveats as the CLI apply. The LAN link is plain HTTP until WireGuard exists, so type only fake content.

## Check it in a real browser (UI-SEC-3/4)

```sh
# pair with curl (or use a code from `pair`), then run the HUD's browser checker with the device cookie:
cd frontend/hud
node scripts/browser-check.mjs https://localhost:8443/ --insecure --cookie '__Host-atlas-device=<token from Set-Cookie>' --axe
```

It fails on any CSP violation, off-origin request, console error or serious axe issue. `--insecure` only ignores the dev CA's certificate error.

## Phase 2 acceptance

```sh
cd frontend/hud && npm run acceptance     # about 2 minutes; Chrome and backend/.venv needed; frees ports 8765, 4173, 8443 again
```

`scripts/phase2-acceptance.sh` builds the HUD and runs `scripts/browser-check.mjs` in headless Chrome six times:
- three widths against the mock server playing every scenario
- reduced motion
- the kiosk role
- the real `atlas-ui` over HTTPS, after pairing through `/api/pair` like a browser

Each run fails on a CSP violation, an off-origin request, a console error, an uncaught exception, or a serious or critical axe finding. These deliberate faults were each reported: a missing state, the wrong profile, an off-site `fetch`, script and image.

| Test (PLAN Phase 2, §8.9) | How | 2026-11-23 |
|---|---|---|
| UI-SEC-1: unauthenticated WebSocket rejected | `backend/tests/test_ui_security.py` | pass |
| UI-SEC-2: cross-origin rejected | same file (WebSocket and `/api/pair` with a foreign `Origin`) | pass |
| UI-SEC-3: no external requests | `build.test.ts` (no off-origin URL in the bundle) and `npm run acceptance` (every request the page makes, every run) | pass |
| UI-SEC-4: zero CSP violations | CSP header and meta tests, plus `npm run acceptance` (all 7 states rendered in Chrome) | pass |
| A11Y-1: axe, zero serious issues | `a11y.test.ts` (jsdom, every state and dialog) and axe in Chrome at 1920, 1000 and 390 px, kiosk, real `atlas-ui` | pass. In Chrome, axe can't decide colour contrast for text over blurred or gradient panels (it reports them as "incomplete"); the design tokens' contrast is checked in `a11y.test.ts`. |
| A11Y-2: reduced motion honoured | `a11y.test.ts`, `orb.test.ts`, and Chrome with `prefers-reduced-motion: reduce` (only the static tier ran) | pass |
| A11Y-3: keyboard-only operation | `a11y.test.ts` (real controls only, the whole approval flow from the keyboard) | pass (jsdom; a hands-on keyboard pass on the Pi kiosk is still worth doing) |
| Snapshot tests for every state | `orb.test.ts` snapshots: one per `events.v1` state (7) | pass |
| UP-4: auto-degrade and recover | `orb.test.ts` (`QualityGovernor`: 40 ms p95 for 10 s or 75 °C, one level at a time, 5 min to recover). In headless Chrome the governor really stepped down from full to lite. | logic pass; thresholds on the Pi with B8 |
| UP-1: kiosk 1 h, CPU ≤ 25%, frame p95 ≤ 33 ms | Needs the kiosk on the Pi (a display) | open |
| UP-2, UP-3: with voice, thermal | Need the kiosk and Phase 3 voice | open |
| B8: UI performance | Pi kiosk; remote browsers at 60 fps | open |
| HUD on the Pi from a LAN browser | [ui-on-pi.md](ui-on-pi.md): real `atlas-ui` and `atlas-core` from a release, local CA, pairing, WSS; tested end to end in the ARM64 VM (headless Chrome: zero CSP violations, off-origin requests, console errors, axe findings) | VM pass; Pi after the next window |
| Chromium kiosk on the Pi | `deploy/kiosk/` is still empty | open |

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
- UI-SEC-3/4 in a real browser are not in pytest: `npm run acceptance` (below) runs them in headless Chrome.
