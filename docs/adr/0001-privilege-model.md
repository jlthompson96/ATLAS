# ADR 0001: Privilege model, taint enforcement and the approval record

- **Status:** Proposed (security core, Phase 1). Needs owner approval and the reviewer agent's review.
- **Date:** 2026-11-18
- **Decision record:** R-AGT (thin custom orchestrator), PLAN.md §2.9 privilege model.
- **Code:** `backend/atlas/core/`, `backend/atlas/policy/`, `backend/atlas/memory/`, `backend/atlas/executor/`.

## Context

PLAN.md §2.9 fixes a simplified Dual LLM design: a privileged planner that never sees untrusted text, a quarantined reader with no tools, taint gating, context-bound tool registries, and a separate executor that acts only on a payload hash that a human approved. §6 (T1–T3, T5, T11, T13, T15) and the red-team cases in §8.3 and §8.8 are the threats.

The rules have to hold **in code**, with a model that is assumed hostile. No test may depend on the model following its system prompt.

## Decision

### 1. Taint is a type, not a flag

- Untrusted data exists in core only as `UntrustedContent` (a raw item) or `TaintedStr` (derived text). Neither is a `str`, and neither converts to one: `str()`, `format()`, f-strings, `%`, `bytes()`, iteration, indexing, `bool()`, pickling and copying all raise.
- Both are bound in a per-session `VariableStore` and handed around as opaque `VarRef` names (`$EMAIL_1`, `$SUMMARY_2`).
- Taint propagates: concatenation and `derive()` return a `TaintedStr` with the union of the inputs' provenance. Payloads built from `$VAR`s carry that union to the approval card.
- Provenance is a set of `Source(kind, resource_id, observed_at_ms)`. The resource ID uses the audit `<namespace>:<path>` form.

### 2. Tainted text cannot be rendered into a planner prompt

There are three independent layers:

1. **Types.** `PlannerContext`, the only planner prompt builder, accepts only `TrustedText` and `VarRef` parts, and checks this at runtime. `TrustedText` accepts only an exact `str`. A `VarRef` renders as its name plus a description written by ATLAS code (label, source IDs, size), never its content.
2. **Content scan.** `render()` fingerprints every tainted value in the store (24-character windows, normalized for case and whitespace) and refuses (`TaintLeak`) any system, tool or assistant message that contains one.
   - Windows that also occur in the trusted corpus (system prompt, tool specs) are ignored, so content can't jam the planner by quoting its instructions.
   - User messages are not scanned: they are the user's own words.
   - The orchestrator also scans every tool call's arguments, raw and after JSON escapes are decoded, before dispatch.
3. **Static rules** (tests).
   - Only five pinned modules call `_reveal()`: display, payload building, contact matching, memory persistence and the taint index.
   - Only the reader calls `_raw_bytes()` and the sanitizer.
   - Only `prompt.py` and `reader.py` construct `Message`s.

### 3. The quarantined reader

- A fresh context per call, with a fixed system prompt first, one task from a closed enum, and one sanitized item.
- The sanitizer bounds raw input at 1 MiB and visible text at 12,000 characters (less if the backend's context is small).
- It removes hidden HTML (inline hiding styles, `hidden`, `aria-hidden`, comments, `<font color=white>`), zero-width, bidi, tag and variation-selector characters, and other format and control characters.
- Every removal raises a closed-enum flag, which the planner sees.
- Output must be exactly one JSON object matching the task's closed schema. Validation uses a small dependency-free validator, `atlas.core.schema`.
- Enum, integer and boolean fields reach the planner; free-text fields become tainted `$VAR`s.
- Anything else refuses the whole result: tool calls, extra keys, prose, duplicate keys, oversize output.

### 4. Context-bound registries and the policy engine

- Registries per audit context:
  - `interactive`: get_time, read_index, search_memory, ask_reader, queue_research, propose_action;
  - `unattended`: the same, minus `queue_research`;
  - `quarantined` and `system`: none.
- `ToolRegistry` refuses any tool whose tag is outside its context's tags, and `write` is in no context's tags.
- Write tools (`Sender`) exist only in `atlas.executor.senders`, which no planner-side module imports. A fresh interpreter importing all of `atlas.core`, `atlas.policy` and `atlas.memory` loads no executor module except `approval_record`. This is UW-1.
- `atlas.policy.engine.decide` is a pure function. The rules are in its docstring. Two are worth noting:
  - Read and queue tools **refuse** `$VAR` arguments outright. This is stricter than "tainted arguments force a proposal", because a read has nothing to propose and a tainted research question is an exfiltration channel.
  - `provenance.tainted` on a proposal is set if any argument is a `$VAR`, **or** the planner has seen untrusted-derived data (reader enums, item metadata) earlier in the request. Control flow that depended on untrusted data is flagged too (CaMeL's caveat).
- Every decision is audited: `tool_call` for reads and refusals, `action_proposed` (durable) for proposals. If the audit service is unavailable, the request fails closed.

### 5. Approvals and the approval record (version 1)

- `ApprovalService.decide` takes an `action.decide` that atlas-ui forwarded. Its checks, in the pure `decide_approval`:
  - channel is HUD (voice, text and planner can never approve);
  - the device is known to **core's** device policy and may approve (the kiosk can't by default);
  - the step-up token was registered by atlas-ui for **that device** and is less than 60 s old;
  - the action is still pending (a second decision is a replay);
  - the action hasn't expired;
  - the HUD's hash matches.
- On approval, core builds and signs the record, writes the durable `approval` audit record, and only then returns it. Nothing in the planner path holds a reference to `ApprovalService`.

Record (JSON, exact member set):

| Member | Meaning |
|---|---|
| `v` = 1, `type` = `atlas.approval` | Version and type. The executor accepts exactly one version. |
| `approval_id` | `apr-` + 32 hex |
| `action_id`, `action_kind`, `payload_sha256` | Bind the exact payload the human saw |
| `origin_context` | `interactive` or `unattended`: who *proposed* it |
| `decision_context` = `interactive`, `approved_via` = `hud`, `step_up` = true, `decision` = `approve` | A present human approved it with a PIN |
| `device_id` | The approving device |
| `action_created_at` < `decided_at` < `expires_at` ≤ `decided_at` + 10 min | The approval follows the proposal and is short-lived (core uses 5 min) |
| `nonce` | 32 hex, single use |
| `key_id` | `ak-` + first 16 hex of SHA-256(raw public key) |

- **Signature:** Ed25519 with `approval-signing-key` over `b"atlas-approval-v1\n" + JCS(record)`, as unpadded base64url.
- **Envelope on `executor.sock`:** `{"op":"execute","approval":…,"signature":…,"payload_canonical":…}`.

### 6. The executor

`ApprovalVerifier.verify` runs these checks, in order:

1. exact envelope and record shape;
2. the key ID is the configured key, then the signature verifies;
3. SHA-256 of `payload_canonical` equals the signed hash;
4. the payload is exactly canonical JCS, its kind matches the signed kind, it passes the payload rules, and the executor supports the kind;
5. the record is an approval: decision, step-up, channel and decision context;
6. `decided_at` is strictly after `action_created_at`. This covers UW-3's "unattended without a later human approval".
7. expiry, lifetime and clock skew.

Then, failing closed at each step:

- claim the nonce, approval ID **and** action ID in a durable SQLite store (`synchronous=FULL`);
- write the `write_ahead` audit record, which must be acknowledged as durable;
- send;
- write the `write_result` audit record.

Claiming before the write-ahead record means a crash can't turn one approval into two sends.

The socket handler accepts only `atlas-core`, by peer credentials, and serialises requests. Phase 1 ships `FakeSender` only.

### 7. Memory

SQLite with FTS5 (falling back to `LIKE` if FTS5 is missing).

- Every row stores its provenance and a tainted bit.
- Tainted rows come back as `TaintedStr`, so `search_memory` binds them as `$MEM_n`. A poisoned "new address" can't become a recipient silently: the proposal carries the memory row and the original email in its sources, plus `tainted_args`, `not_on_allowlist` and `new_recipient`.
- No planner tool writes memory.

## Why the approval record is not (yet) a contract

The record crosses a process boundary but not an agent boundary: atlas-core and atlas-executor are both owned by the security core and ship in the same release.

Keeping it in code, as `atlas.executor.approval_record` with a version and a domain-separation prefix, avoids a contract bump for every hardening change during Phases 1–6. If another implementation ever needs to produce or consume it, it should move to `contracts/` (see the proposal `contracts/proposals/2026-11-18-security-core-followups.md`).

## Consequences

- **Planner visibility.** The planner can't read subjects, senders or bodies. It works with references and reader classifications, and the user sees content by reference (HUD, TTS, CLI `/show`). This is by design; answer quality depends on the reader tasks, which are a closed list that can grow.
- **Proposal flags.** Proposals made after reading any untrusted item are flagged as tainted. Expect most email-related proposals to show the provenance banner.
- **Accepted limitation.** The leak scanner catches copies of 24 or more characters, or short values copied whole. Shorter tainted fragments typed by a colluding model, such as an email address on its own, are not detected. Recipient allowlist warnings cover that case, and the approval is the backstop.
- **Stylesheet hiding.** Hiding through stylesheet classes can't be resolved without a CSS engine. It is flagged (`hidden_html`), not stripped.
- **Step-up tokens.** A step-up token is reusable within its 60 s life on its own device. This matches events.v1 ("valid 60 s").
