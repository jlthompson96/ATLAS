# Proposal: document the atlas-ui HTTP surface, the core control socket and four result codes

| | |
|---|---|
| Contract | `contracts/services.md` (sections 2 and 4), `contracts/README.md` (code catalogue) |
| Bump | **PATCH** for the catalogue and wording; the protocol text is new documentation, nothing existing changes |
| Raised by | Agent E, while implementing `atlas-ui` and `core.sock` (branch `worktree-agent-a3440f65c9123dee1`) |
| Affects | `atlas-ui`, `atlas-core`; later `atlas-voice` and the other `core.sock` clients |
| Status | **Proposed.** The code already follows it; no contract file was edited. |

## 1. `core.sock` protocol and per-peer operations

`services.md` says which peers may connect, but not what they may do. Implemented in `backend/atlas/core/control.py`:

- Newline-delimited JSON, one request per line: `{"id": int, "op": str, ...}`, answered by `{"id", "ok", "code"?, ...}`. After `subscribe`, core also pushes `{"event": <events.v1 type>, "payload": {...}}`; atlas-ui adds the envelope, `seq`, privacy filtering and validation.
- Peer identity is `SO_PEERCRED` (Linux) / `LOCAL_PEERCRED` (macOS); the uid must map to one of the listed service users, otherwise the connection is dropped before any operation.
- Operations: `atlas-ui` may call `ping, subscribe, text.input, action.decide, stepup.register, device.announce, task.kill, killswitch.all`. Every other listed peer may call `ping` only, until its own operations are specified. A test pins this table against the peer list in `services.md`.
- **Suggested wording for `services.md` section 2, `core.sock` row:** "Newline-JSON; operations per peer in `atlas/core/control.py` (`PEER_OPS`); atlas-ui only for HUD operations."

## 2. atlas-ui HTTP surface (services.md section 2 row for `atlas-ui`)

- `GET /api/pair` with header `X-Atlas-Pairing-Code` redeems a one-time code (valid 10 minutes, stored hashed, 5 failures per client address in 5 minutes, 20 globally, then 429) and sets the cookie `__Host-atlas-device` (HttpOnly, Secure, SameSite=Strict, `Path=/`). It is a GET because the `websockets` HTTP parser only reads GET requests; the custom header cannot be sent cross-site without a CORS preflight (which is never answered) and `Origin` must match.
- Everything except `/pair`, `/pair.js`, `/pair.css` and `/api/pair` needs a valid device cookie; `/ws/events` additionally needs an allowed `Origin`. The `Host` header must name the server (DNS rebinding).
- Response headers on every response: the CSP from `frontend/hud/vite.config.ts` plus `frame-ancestors 'none'`, `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Cross-Origin-*-Policy`, `Permissions-Policy`, HSTS.

## 3. Request result codes (README code catalogue, `request.result` row)

Add (all optional-to-understand for old consumers, since unknown codes show `message`):

| Code | Meaning |
|---|---|
| `CORE_UNREACHABLE` | atlas-ui could not reach atlas-core (also used for `state.changed` / alerts already) |
| `VOICE_UNAVAILABLE` | `ptt.start` while atlas-voice isn't running |
| `NOT_AVAILABLE` | `portfolio.unlock` before atlas-market exists |
| `FORBIDDEN_PEER` | control-socket only (not on the HUD stream): a peer called an operation it may not |

## 4. Not proposed here

Moving the PIN check into core is already in `2026-11-18-security-core-followups.md` (F3). This change keeps the current contract: atlas-ui verifies the PIN and registers the step-up token with core.
