# ADR 0003: Voice through the client device's microphone (tap-to-talk)

- **Status:** Accepted 2026-11-25 (owner: "tap to talk is fine"). The direction is decided; the details below marked *Phase 3* are settled when Phase 3 starts.
- **Date:** 2026-11-25
- **Decision record:** amends D2 (audio hardware) and D3 (wake phrase) in PLAN §0.5, and R-HW1 / R-WW (§2.2, §2.6).
- **Contract change:** none yet. Phase 3 needs a proposal for audio in `events.v1` and for the `atlas-ui` ↔ `atlas-voice` path in `services.md`.

## Context

The plan put the microphone and speaker on the Pi: a USB conference speakerphone (D2) listening all the time for "Hey Atlas" (D3), with push-to-talk as the fallback. Phase 3's exit criteria followed from that: a far-field spike at 1/3/5 m, a 24 h wake-word soak (B4) and self-trigger tests (RT-13).

The owner's Pi has no display and no microphone, and the HUD already runs in browsers on the owner's phone and computer over HTTPS (Phase 2). The owner asked whether those devices could be the microphone instead, and chose tap-to-talk over hands-free listening across the room.

## Decision

**The primary voice path is the HUD in a browser.** The owner taps and holds the orb (or a key on a computer), speaks, and lets go.

- The browser records with `getUserMedia` (allowed because the HUD is served over HTTPS with the ATLAS CA) and streams the audio to `atlas-ui` over the existing authenticated WebSocket.
- `atlas-voice` **on the Pi** still does VAD, speech-to-text and text-to-speech. The reply's audio goes back over the same connection and plays on the device that asked.
- Audio never leaves the LAN and never reaches a cloud service. The privacy model is unchanged.
- The browser's built-in echo cancellation (`echoCancellation: true`) replaces the hardware AEC the speakerphone was chosen for.
- `atlas-voice` takes its audio from a source interface, so a room microphone on the Pi with "Hey Atlas" can be added later as a second source without redesign.

## Consequences

**Gained**

- No audio hardware to buy or tune.
- Better recognition: a phone near the mouth instead of a room microphone at 1–5 m.
- Voice from any room, on any paired device.
- **Safer input:** only a paired device holding its token can send audio. A TV or a read-aloud email saying "Hey Atlas" can't trigger anything on this path, so threat T11 (ambient audio) mostly drops away. **Voice can never approve** stays as it is.

**Lost**

- No hands-free use across the room. A browser only records while the HUD page is open and in front; a locked phone or a switch to another app stops it (iOS especially).
- Voice only on the home network, like the HUD itself. Use away from home needs a VPN to the Pi: a separate, later decision.

**Changes to Phase 3** (PLAN §7)

- The speakerphone spike (R-HW1) and the 24 h wake-word soak (B4) leave the Phase 3 exit criteria. They return only if the room-microphone path is built.
- RT-13 (self-trigger) applies only to the room path.
- B1 (STT benchmark) records its utterances through the browser path, on the owner's devices.
- B9 (A/V sync) measures playback on the client device, which includes the browser's output latency.
- The exit criterion "warm end-to-end p50 ≤ 3.0 s" stays, measured from releasing the button to the first audio on the device.

**Security work for Phase 3**

- A per-device `voice` permission, alongside the existing per-device permissions (the kiosk already has PTT).
- Size and rate limits on audio frames in `atlas-ui`, so a paired device can't flood `atlas-voice`.
- Audio is never written to disk or logged. Only the transcript is kept, under the existing 7-day transcript rule.
- One recording at a time: a second device that starts talking interrupts or is refused, never mixed in.

## Not decided yet (Phase 3)

- The codec and transport: Opus (WebCodecs or `MediaRecorder`) or 16 kHz PCM in binary WebSocket frames.
- Hold-to-talk or tap-to-start with VAD ending the turn; probably both, chosen per device.
- How iOS Safari behaves: permission prompts, audio playback without a fresh tap, and what happens when the screen locks.
- Whether the Pi-attached kiosk (when there is a display) gets voice through a USB microphone or stays text-only.
