# Proposal: mark what you type (`text.input`) as sensitive, so atlas-ui never logs it

- **Date:** 2026-11-25
- **Author:** Claude Code session with the owner (found during the first live HUD run with a real model)
- **Contract:** `contracts/events.v1.schema.json` (now 1.1.0), and the rules section of `contracts/README.md`
- **Bump:** MINOR, to 1.2.0. Nothing changes on the wire, so every producer and consumer stays compatible. But conforming servers must now redact one more field, which is more than a wording change.
- **Status:** Approved by the owner 2026-11-25, applied in events.v1 1.2.0 (with the logging fix recommended below)

## What

1. In `$defs.TextInput.properties.text`, add `"x-atlas-sensitive": true` and a description: "What the user typed. Content: never logged (`redact_for_log`)."
2. In `contracts/README.md`, under "Rules the schemas can't fully express", add one rule: **every free-text string a client sends is `x-atlas-sensitive`.** Free text means a `string` with no `pattern`, `enum` or `const`.
3. When the change is applied, add a contract test that enforces rule 2 over `ClientMessage`, so a future client field can't repeat the gap.

## Why

On 2026-11-25 the owner paired a browser with the dev server and typed `test`. atlas-ui's log then showed:

```
INFO atlas.ui: client dev-867372b38b47 {'v': 1, 'type': 'text.input', 'seq': 11, 'ts': 1795622383669, 'payload': {'text': 'test'}}
```

atlas-ui logs every client frame at INFO (`ui_server/session.py`), after `redact_for_log`. That function replaces every field the schema marks `x-atlas-sensitive`. `TextInput.text` isn't marked, so what you type is logged verbatim.

On the Pi this would break three rules at once:

- **CLAUDE.md:** never log private content; log IDs, sizes, hashes and timings.
- **Retention (PLAN §2.7, §2.12):** transcripts are kept 7 days. The journal keeps 30 days (`base-os.sh`: `MaxRetentionSec=30day`) on the unencrypted data partition. A typed request would outlive its own transcript by more than three weeks.
- **PT-11 (exposure inventory):** "no transcripts older than 7 days" anywhere on the card. A journal full of typed requests is a transcript in all but name.

**Nothing real has been logged yet.** atlas-ui doesn't run on the Pi yet: its approval PIN is set at the Pi's console, which has no display so far. The only affected log is the dev server's terminal output on the Mac, which isn't kept.

The other free-text fields a client sends are already marked: the PIN, both step-up tokens and the portfolio passphrase. Every remaining client string is limited by a `pattern` (IDs, hashes). `TextInput.text` is the only gap.

## Consequence of name-based redaction

`redact_for_log` collects the *names* of sensitive properties from the schema, and redacts that key wherever it appears in a logged frame. Marking `TextInput.text` therefore also redacts `text` in `Transcript` and `AssistantDelta` frames if they are ever logged. Both are content too, so that's the desired effect. Today only client frames are logged, so in practice only `text.input` changes.

## Alternatives considered

- **Fix it in code only:** special-case `text.input` in `session.py`. Rejected: the schema is where sensitivity is declared (CLAUDE.md, `x-atlas-sensitive` → `redact_for_log`). A rule kept only in code drifts from the contract, and the mock server would need the same exception.
- **Stop logging client frames altogether:** log only type, device, sequence number and size. This is complementary, and it's a code change, not a contract change, so it isn't part of this proposal. It's still worth doing: at INFO, every `activity.ping` (at most one every 30 s per device) is also a journal line on the SD card, which counts against the 100 MB/day write budget. Recommended as a separate small fix: log frame types at DEBUG, and at INFO only what an operator needs (connect, disconnect, refusals).

## Who is affected

| Component | Change |
|---|---|
| `atlas-ui` (`ui_server/session.py`) | None in code: `redact_for_log` reads the schema. After the bump the log line reads `'payload': {'text': '[REDACTED]'}`. |
| Mock event server | None in code, same function. |
| HUD (`@atlas/hud`) | Regenerate `src/lib/generated/events.ts` (`npm run gen:types`): `SCHEMA_REVISION` becomes 1.2.0 and `text` joins its list of never-logged fields. No TypeScript type changes. `npm run lint` fails until it's regenerated. |
| `atlas-core` | None: it doesn't log frames. |
| Tests | A contract test that `TextInput.text` is sensitive, and the rule-2 test over every client string. Plus a `session.py` test that a logged `text.input` frame shows `[REDACTED]`. |

## Migration

None. The wire format is unchanged, and no real logs exist to clean up.
