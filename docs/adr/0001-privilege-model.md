# ADR 0001: Privilege model, taint enforcement and the approval record

- **Status:** Proposed (security core, Phase 1). Reviewed 2026-11-18 at `3b8e151` (verdict: merge after fixes); this revision includes the fixes.
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
- Provenance is a set of `Source(kind, resource_id, observed_at_ms)`.
- **Resource IDs are shown to the planner, so they must not carry attacker text** (review F4).
  - They use a closed list of namespaces (`gmail`, `gcal`, `discord`, `web`, `memory`, `utterance`, `system`, `portfolio`).
  - The path is 1–64 characters of `[A-Za-z0-9_-]` segments separated by `/`.
  - **Integrations must use provider-assigned opaque IDs** (the Gmail message ID, Discord snowflakes, the Calendar event ID), never sender-chosen ones such as an email `Message-ID` or an ICS `UID`.
- Sizes shown to the planner are bucketed (`under 1 KB`, `1-4 KB`, …), so exact lengths can't carry data.

### 2. Tainted text cannot be rendered into a planner prompt

There are three independent layers:

1. **Types.** `PlannerContext`, the only planner prompt builder, accepts only `TrustedText` and `VarRef` parts, and checks this at runtime. `TrustedText` accepts only an exact `str`. A `VarRef` renders as its name plus a description written by ATLAS code (label, source IDs, size bucket), never its content.
2. **Content scan**, aimed only at text ATLAS did not generate (review F2). Every tainted value in the store is fingerprinted (24-character windows, normalized for case and whitespace).
   - `AtlasText` is ATLAS's own text: built only by `t()` from a literal, or `gen()` from a literal template plus ints, bools and short plain strings. An AST test pins both. It is never scanned, and neither are `VarRef` descriptions. Before this fix, an email that quoted ATLAS's own output (" bytes, unread; from $FROM_") made every later inbox listing fail.
   - Other text in a tool result (a memory row the user stated) is scanned per part. A hit **withholds that one result**: the planner sees a fixed refusal, the event is audited as `TOOL_RESULT_WITHHELD`, and the request goes on.
   - Text the model authored, meaning assistant messages and every string in its tool-call arguments (raw, and decoded so `\n` or `\"` escapes can't split windows), **fails hard**. Arguments are checked before the tool runs (`TAINT_LEAK`), and assistant text when the prompt is rendered (`TaintLeak`, request FAILED). The model can only know tainted text if something leaked.
   - Windows that also occur in the trusted corpus (system prompt, tool specs) are ignored. System and user messages are not scanned.
   - Fingerprints are reference-counted per variable. Each request prunes variables older than 6 h and items no longer in the index, taking their fingerprints with them. At 4,096 variables the oldest is evicted rather than failing. Names are never reused.
3. **Static rules** (tests).
   - Only five pinned modules call `_reveal()`: display, payload building, contact matching, memory persistence and the taint index.
   - Only the reader calls `_raw_bytes()` and the sanitizer.
   - The name-mangled slots (`_TaintedStr__value`, `_UntrustedContent__raw`) and `getattr`-by-string access are covered too.
   - Only `prompt.py` and `reader.py` construct `Message`s.

### 3. The quarantined reader

- A fresh context per call, with a fixed system prompt first, one task from a closed enum, and one sanitized item.
- The sanitizer bounds raw input at 1 MiB, 256 KiB for HTML, and visible text at 12,000 characters (less if the backend's context is small).
- HTML parsing is linear in the input (review F1):
  - a hidden-depth counter replaces scanning the tag stack;
  - nesting deeper than 256 is treated as hidden;
  - an end tag closes only one of the last 32 open elements;
  - sanitizing runs in a worker thread inside the request deadline, so a hostile page can't stall atlas-core's event loop.
- It removes hidden HTML (`hidden`, `aria-hidden`, comments, `<font color=white>`) and inline hiding styles, parsed one declaration at a time (F10). Those styles are: `display:none`, `visibility:hidden`, `mso-hide:all`, opacity below 0.1, tiny fonts, 0 or 1 px boxes with hidden overflow, large negative offsets, clipping, `scale(0)`, transparent colours, and text the same colour as its inline background.
- It also removes zero-width, bidi, tag and variation-selector characters, and other format and control characters.
- Reader enum arrays must be unique and are passed on sorted, so order and repetition carry no data.
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
  - `provenance.tainted` on a proposal is set if any argument is a `$VAR`, **or** the planner has seen untrusted-derived data earlier in the request. Control flow that depended on untrusted data is flagged too (CaMeL's caveat).
    - That data includes reader enums, item metadata, tainted memory hits, and, since review F6, any `$VAR` the model passed to a tool, even a call that was refused. A refusal such as `INVALID_RECIPIENT` tells the model something about the value.
  - The HUD can tell "content flows into this payload" (`provenance.tainted` plus the `tainted_args` warning) from "the planner was only influenced" (`provenance.tainted` without `tainted_args`) using fields events.v1 already has. The owner keeps flagging both.
  - `queue_research` is refused (`RESEARCH_NEEDS_USER`) once the request has been influenced (review F7). §2.13 says research takes only the user's own question. A real proposal needs an action kind for a one-off research run, which events.v1 doesn't have, so it has been proposed instead.
- Every decision is audited: `tool_call` for reads and refusals, `action_proposed` (durable) for proposals. If the audit service is unavailable, the request fails closed.

### 5. Approvals and the approval record (version 1)

- `ApprovalService.decide` takes an `action.decide` that atlas-ui forwarded. Its checks, in the pure `decide_approval`:
  - channel is HUD (voice, text and planner can never approve);
  - the device is known to **core's** device policy and may approve (the kiosk can't by default);
  - the step-up token was registered by atlas-ui for **that device**, is less than 60 s old and, if atlas-ui bound it to an action, is for this action;
  - the action is still pending (a second decision is a replay; a concurrent one gets `BUSY`);
  - the action hasn't expired;
  - the HUD's hash matches.
- A decision that passes moves the action to `DECIDING` and **spends the step-up token** before the first `await` (review F5; single use is the owner's decision, stricter than events.v1's "valid 60 s").
  - A concurrent second decision is refused with `BUSY`, so an approve and a deny can't both be recorded.
  - If the audit write fails, the action returns to `PENDING` and the user enters the PIN again.
- On approval, core builds and signs the record, writes the durable `approval` audit record, and only then returns it. Nothing in the planner path holds a reference to `ApprovalService`.
- Approval windows: core issues 5-minute approvals; the executor accepts at most 10 minutes (owner decision).
- Payload strings are CR/LF-normalized, and control and format characters (ESC, bidi overrides, zero-width) are **removed** before canonicalization (review F9). `validate_payload` refuses any that remain, on both sides.
  - Removing rather than flagging makes what the human approves exactly what they can see, and needs no new events.v1 warning.
  - The cost: zero-width joiners in emoji sequences and bidi marks in right-to-left text are dropped too.

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

The socket handler accepts only `atlas-core`, by peer credentials, and serialises requests. Phase 1 ships `FakeSender` only. A sender exception is logged by type only, because provider error text can quote the payload.

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
- **Step-up tokens** are single use (owner decision). One PIN entry approves one action.
- **Voice laundering (RT-13, for the voice service).** TTS reading tainted content aloud, picked up again by the microphone and transcribed, turns tainted text into a "user" message. That message is trusted and never scanned. The security core can't see that path. atlas-voice must suppress wake and STT while TTS plays (unless echo cancellation is verified), and mark transcripts taken during or right after playback. Voice can never approve, whatever the transcript says.
- **Accepted, from the review: F3.** atlas-ui alone verifies the PIN, so a compromised atlas-ui could approve. Moving PIN verification into core is a contract proposal.
- **Accepted, from the review: F11.** The executor doesn't yet enforce the Discord channel allowlist or a send rate limit itself; core does. This is a contract proposal too.
