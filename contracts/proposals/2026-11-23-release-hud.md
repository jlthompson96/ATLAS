# Proposal: the HUD ships in each release; the local CA keeps no private key

**Target:** `contracts/services.md` (v1.4.0), sections 4 and 6. **Bump:** MINOR (a new top-level release entry and a rule for the CA key; nothing existing changes meaning). **Author:** Phase 2, `atlas-ui` on the Pi. **Status:** Approved by the owner 2026-11-23, applied in services.md 1.5.0.

## What

**§6 Code location and releases:** add a bullet after "Releases are owned by `root`…".

> - A release holds `backend/`, `contracts/`, `deploy/`, `venv/<service>/` and `hud/`. `hud/` is the built HUD: static files built on the admin device from the committed `frontend/hud` (`npm ci --ignore-scripts`, `npm run build`), served read-only by `atlas-ui` from `/var/lib/atlas/current/hud`. There is no Node on the Pi. `atlas-release` accepts `hud/` like the other entries: regular files and directories only.

**§4 Secrets:** add to the paragraph under the table.

> `atlas-ui`'s certificate is signed by the ATLAS local CA, which is created on the Pi and keeps **no private key**: the CA key signs the one server certificate and is then deleted, so a stolen card can't mint certificates that the owner's devices trust. Renewal makes a new CA and a new certificate, and each device trusts the new CA once. The CA certificate and the server certificate are public and live in `/etc/atlas/tls/` (root-owned, world-readable); only the server key is a credential (`tls-key`).

## Why

PLAN §7.0 builds the HUD on the Mac and ships it as-is. `atlas-ui` already defaults to `/var/lib/atlas/current/hud`, but `atlas-release` refuses any top-level entry other than `backend contracts deploy wheels`, so a release can't carry the HUD today. Putting it in the release gives the HUD the same versioning, rollback and read-only treatment as the code that serves it.

The certificate binds the Pi's LAN address (the owner chose an IP address over an mDNS name: no `avahi` on the Pi). A long-lived CA key on the card would be the most valuable thing a thief could take from the UI side: it lets them impersonate the HUD to every trusting device. Deleting it costs one re-trust per device every ~800 days (Apple's limit for TLS server certificates is 825 days).

## Who is affected

- `deploy/provisioning/deploy.sh` (builds the HUD from `git archive HEAD` and adds `hud/`), `atlas-release.sh` (allowlist), `docs/runbooks/deploy.md`.
- A new `deploy/provisioning/gen-ui-keys.sh`; `atlas-ui.service` (reads the certificate from `/etc/atlas/tls/`).
- Tests that pin the release allowlist.
