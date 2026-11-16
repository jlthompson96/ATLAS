# Proposal: `activity.ping` client message for the privacy inactivity timer

- **Date:** 2026-11-16
- **Author:** agent A (HUD frontend)
- **Contract:** `contracts/events.v1.schema.json`
- **Bump:** MINOR (new optional client message type; old servers ignore it, new HUDs tolerate a server that doesn't know it)
- **Status:** proposed, not applied. The HUD works without it (see "Workaround").

## What

Add one client -> server message type:

| type | payload | Notes |
|---|---|---|
| `activity.ping` | `{}` (`Empty`) | Sent by the HUD at most once every 10 s, only while the user is actively interacting with that device (pointer, touch, key press). |

The server resets that device's privacy-inactivity timer on `activity.ping` and on every other client frame.

## Why

PLAN.md §4.6: privacy mode turns on automatically after 5 minutes of no interaction on a remote device (kiosk default 10 minutes), and `PrivacyMode.reason` has the value `inactivity`. The server owns this timer (privacy is enforced server-side, per device), but today it can only see client frames that mean something: `text.input`, `ptt.*`, `action.decide`, and so on.

A person who is reading the HUD, scrolling a panel, opening an approval to review it, or editing the layout sends no frames for minutes. The server would then turn privacy mode on under them, which is a nuisance and trains people to switch the feature off. Counting `clock.ping` does not help, because it is sent by the HUD every 10 s whether or not anyone is there.

## Workaround in the HUD today

None is possible without the message. The HUD doesn't invent traffic. Until this is decided it relies on the server treating real client frames as activity. If the owner prefers to keep the contract as is, the alternative is to define "interaction" as any client frame other than `clock.ping` and `sync.request` in `contracts/README.md`, and have the HUD send nothing more.

## Who is affected

- HUD (agent A): sends the message (about 10 lines, debounced pointer/key listener).
- `atlas-ui` (real server) and the mock event server: accept it, reset the timer, reply with nothing (no `request.result` needed).
- Schema consumers that enumerate client types (`ClientMessage` enum, generated HUD types): regenerate.

## Privacy note

The message carries no content and no coordinates, so it leaks nothing beyond "someone is here", which the server already knows from the open socket.
