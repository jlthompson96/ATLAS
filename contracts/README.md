# ATLAS contracts

**Status: v1 Approved 2026-11-16 (Step C0, PLAN.md §7.0).** Every change now follows the process below.

These files are the interfaces that parallel agents build against. They are code: tests in `backend/tests/test_contracts.py` validate them and check them against each other.

| File | What it fixes | Plan section | Consumers |
|---|---|---|---|
| [`events.v1.schema.json`](events.v1.schema.json) | Backend → HUD event stream (JSON Schema 2020-12) | §4.5 | HUD (agent A), `atlas-ui`, mock server |
| [`hostd.v1.yaml`](hostd.v1.yaml) | Pi ↔ `atlas-hostd` HTTP API (OpenAPI 3.1) | §5.4 | `atlas-hostd` (agent B), `atlas.power` |
| [`audit-record.v1.schema.json`](audit-record.v1.schema.json) | One audit log record, hash-chain rule | §2.12 | `atlas-audit` (agent C), every Pi service |
| [`services.md`](services.md) | Service names, Unix users, ports, sockets, egress allowlists, credentials | §5.2 | nftables, systemd units, egress proxy (agent C) |
| [`../backend/atlas/llm/interface.py`](../backend/atlas/llm/interface.py) | `LLMBackend` | §5.8 | orchestrator, LLM backends |
| [`../backend/atlas/power/interface.py`](../backend/atlas/power/interface.py) | `PowerController` | §5.8 | orchestrator, power controller |
| [`examples/audit-chain.v1.jsonl`](examples/audit-chain.v1.jsonl) | Golden 8-record hash chain | §2.12 | `atlas-audit` tests |

## Versioning

Each contract carries a revision `MAJOR.MINOR.PATCH` (`x-atlas-schema-revision`, `info.version`, `CONTRACT_REVISION`, or the header of `services.md`). The `v` field on the wire, and the `v1` in file names, is the MAJOR version.

| Change | Bump | Examples |
|---|---|---|
| Wording, descriptions, new entries in the code catalogue below | PATCH | Clarify a field; add alert code `FOO_BAR` |
| Additive and backwards-compatible | MINOR | New optional field, new event type, new enum value that old consumers can ignore, new `action.kind` payload shape |
| Anything that can break a consumer | MAJOR (new `*.v2.*` file, kept alongside v1 during migration) | Remove or rename a field, make an optional field required, change a type or unit, remove an enum value |

**Consumers must tolerate MINOR changes:**

- ignore unknown fields;
- log and drop unknown event types;
- treat an unknown enum value as "unknown" or "other";
- show `message` for an unknown `code`.

Producers validate strictly against the schema, which is closed (`additionalProperties: false`), so producer drift fails tests immediately.

## Change process (after approval)

1. **Agents never edit a contract directly.** An agent that needs a change writes a proposal in `contracts/proposals/YYYY-MM-DD-<topic>.md`: what, why, the bump level, and who is affected.
2. The owner reviews it. If approved, the contract owner applies it, bumps the revision, and adds a line to `contracts/CHANGELOG.md`.
3. Every agent building against that contract is told about the change before its next merge.

## Rules the schemas can't fully express

**Event stream (`events.v1`)**

- **State machine.** Legal orb-state transitions are in `$defs.State.x-atlas-state-transitions`; `StreamChecker` in `backend/atlas/ui_server/protocol.py` enforces them. `idle` and `llm_unreachable` are the two resting states: while the core is offline, the server returns to `llm_unreachable`, not `idle`. A `sync.snapshot` resets the machine.
- **Wake stages** never go backwards within one `waking_host` episode, and `elapsed_ms ≤ deadline_ms`.
- **TTS envelope lead.** `audio.level` batches with `stream: "tts"` are sent ≥ 100 ms before `t_play` (§4.4).
- **Privacy mode is enforced on the server, per device.** While it is on, the device receives redacted panels, actions and tasks, and no `transcript.*` or `assistant.delta` frames. Every privacy change is followed by a fresh `sync.snapshot`. The HUD's blur is a second layer, not the protection.
- **Inactivity.** The server turns privacy on (`reason: inactivity`) after a device's inactivity window. Every client frame except `clock.ping` and `sync.request` resets it. The HUD sends `activity.ping` (empty payload, no reply) only on real pointer, touch or key input, at most once every 30 s; the server never rejects a faster one, it just resets the timer.
- **Approvals are bound to what the user saw.** `action.pending.payload_canonical` is the exact JCS payload; `payload_sha256` is its SHA-256. The HUD recomputes the hash, refuses to offer Approve on a mismatch, and sends its own hash in `action.decide`. Approval also needs a step-up token (`stepup.verify` → `stepup.granted`, valid 60 s). Voice can never approve.
- **Untrusted text.** Fields typed `UntrustedText` (`x-atlas-untrusted`) come from third parties. Render them as plain text only: never as HTML or Markdown, and never as a link or image source.
- **Sensitive fields** (`x-atlas-sensitive`: `passphrase`, `pin`, `step_up_token`, `token`, `text`) are never logged. Use `redact_for_log()`, which redacts a sensitive field's name wherever it appears in a frame.
- **Every free-text string a client sends is sensitive.** Free text means a `string` with no `pattern`, `enum` or `const`, such as what the user typed (`text.input`). A contract test checks every `ClientMessage` payload (since events.v1 1.2.0).
- **Money** is a decimal string, never a JSON number.
- **Decoy tickers** (D22) never appear in `panel.update`.

**Audit (`audit-record.v1`)**

- `hash = SHA-256(JCS(record minus "hash"))`; `prev_hash` links to the previous record; genesis uses 64 zeros. No floats anywhere in a record. A reference implementation (`record_hash`, `verify_chain`) is in `backend/tests/test_contracts.py`.
- `actor` comes from the socket peer credentials, never from the submitted body.
- A `write_ahead` record is fsynced before the executor acts. A `write_ahead` with no `write_result` after a crash means "outcome unknown": surface it, never retry automatically.

**hostd (`hostd.v1`)**

- Access control is network-based: bind `10.77.0.2` only, firewall to `10.77.0.1`, and reject other source addresses in code. There is no HTTP credential (see the spec's description).

## Code catalogue

`code` fields match `^[A-Z][A-Z0-9_]{1,47}$`. Known codes (adding one is a PATCH):

| Where | Codes |
|---|---|
| `state.changed` (`error`) | `WAKE_FAILED`, `LLM_AUTH_FAILED`, `LLM_ERROR`, `STT_FAILED`, `TTS_FAILED`, `TIMEOUT` |
| `state.changed` (`llm_unreachable`) | `CORE_UNREACHABLE`, `LINK_DOWN`, `SIGNED_OUT`, `WAKE_FAILED` (closed enum in the schema) |
| `alert` | `WAKE_FAILED`, `CORE_UNREACHABLE`, `CORE_RECOVERED`, `LLM_AUTH_FAILED`, `HOST_DEGRADED`, `HOST_BUSY`, `VOICE_UNAVAILABLE`, `PI_THERMAL`, `WRITE_BUDGET_EXCEEDED`, `GOOGLE_REAUTH_DUE`, `MARKET_STALE`, `EGRESS_DOWN`, `AUDIT_CHAIN_BROKEN`, `PORTFOLIO_LOCKED`, `TASK_KILLED`, `KILLSWITCH`, `UNEXPECTED_WAKE`, `RESEARCH_NOT_RUN` |
| `request.result` | `INVALID_JSON`, `INVALID_MESSAGE`, `SEQ_NOT_INCREASING`, `BAD_PIN`, `BAD_PASSPHRASE`, `RATE_LIMITED`, `STEP_UP_REQUIRED`, `FORBIDDEN_DEVICE`, `UNKNOWN_ACTION`, `UNKNOWN_TASK`, `HASH_MISMATCH`, `EXPIRED`, `BUSY`, `NOT_LISTENING`; mock only: `MOCK_SCENARIO_PLAYING` |
| `action.resolved` | `EXECUTOR_REJECTED`, `SEND_FAILED`, `GRANT_EXPIRED` |
| hostd `Error` | `FORBIDDEN_SOURCE`, `INVALID_BODY`, `BODY_TOO_LARGE`, `INVALID_LEASE_ID`, `LEASE_LIMIT`, `NVML_UNAVAILABLE`, `NOT_FOUND`, `METHOD_NOT_ALLOWED`, `INTERNAL_ERROR` |

## Checking the contracts

```sh
cd backend
.venv/bin/pytest tests/test_contracts.py               # schemas, examples, cross-contract consistency
.venv/bin/python -m atlas.ui_server.mock --validate    # every mock scenario against events.v1
```
