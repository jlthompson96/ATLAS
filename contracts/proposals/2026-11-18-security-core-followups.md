# Proposal: security-core follow-ups (payload shapes, actor pinning, step-up, approval record, PIN in core, executor guardrails, research.run)

**Targets:** `events.v1.schema.json` (1.1.0), `audit-record.v1.schema.json` (1.1.0), `services.md` (1.1.0).
**Author:** security-core agent.
**Status:** proposed.

The Phase 1 security core (docs/adr/0001-privilege-model.md) works within the current contracts. Nothing below blocks it. Each item closes a gap that is only enforced by convention today.

## 1. Kind-specific `payload_canonical` shapes (events.v1, MINOR)

`action.pending.payload_canonical` says that kind-specific shapes "are added in later minor revisions". Today atlas-core produces, and atlas-executor accepts, exactly the shapes used by the mock scenarios. They are validated in `backend/atlas/policy/payloads.py`.

| Kind | Shape |
|---|---|
| `email.send` | `to` (1–10 addresses), `cc` (0–10), `subject` (single line, ≤ 200), `body` (1–20,000), optional `in_reply_to` (resource ID) |
| `discord.post` | `channel_id` (17–20 digits), `channel_label`, `content` (1–2,000) |
| `research.schedule` | `question` (≤ 300, single line), `every_minutes` (60–10,080) |

**Proposal:**

- Add these shapes as `$defs.Payload*`, documented as the parsed form of `payload_canonical`.
- Mark the free-text members `x-atlas-untrusted`: they may contain tainted text, and the HUD must render them as plain text.

**Affected:** the HUD (rendering only), the mock (already conforms), atlas-core and atlas-executor (already conform).

## 2. Pin `action_proposed` to `atlas-core` (audit-record.v1, MINOR)

`docs/known-issues.md` lists actor pinning for `auth`, `config`, `power`, `action_proposed` and `system` as an open follow-up without a proposal.

**Proposal:** for `action_proposed`, add `"actor": {"const": "atlas-core"}` to the kind's `allOf` branch, as `approval` already has.

Only atlas-core proposes actions. Today a compromised reader service could write a fake `action_proposed` record. It can't create a pending action, but it could confuse an audit review.

**Affected:** `atlas.audit.record.KIND_ACTOR` and its tests (agent C's area).

## 3. Step-up registration on `core.sock` (services.md, PATCH)

atlas-ui verifies the PIN, because `stepup-pin-hash` is its credential. Core enforces step-up itself when deciding an approval. That needs atlas-ui to tell core about each token it grants.

**Proposal:** document two `core.sock` messages from `atlas-ui` (the peer is checked by `SO_PEERCRED`):

- `stepup.register {device_id, token_sha256, expires_at}`
- `action.decide {device_id, channel: "hud", …events.v1 ActionDecide}`

Core already stores only token hashes (`StepUpRegistry`). The `core.sock` wire protocol itself is not yet a contract; this would be its first entry.

**Affected:** the atlas-ui server (Phase 2) and atlas-core.

## 4. The approval record (no change proposed now)

The signed approval record (version 1) is internal between atlas-core and atlas-executor. Its format is in `backend/atlas/executor/approval_record.py` and the ADR.

It should move into `contracts/` only if a second implementation ever produces or consumes it. Until then it stays versioned in code, with a domain-separation prefix (`atlas-approval-v1\n`), so it can be hardened without a contract bump.

## 5. One audit-unavailable code for `request.result` (events.v1, PATCH)

If the durable `approval` audit record can't be written, core refuses the decision. It reports `BUSY` today, the closest catalogue code.

**Proposal:** add `AUDIT_UNAVAILABLE` to the `request.result` catalogue so the HUD can say why.

A concurrent decision on an action that another decision is already recording also gets `BUSY` (review F5). That use fits the catalogue as it is.

## 6. PIN verification moves into atlas-core (services.md and events.v1; review F3, from the 2026-11-18 security review)

**The gap.** atlas-ui verifies the PIN (`stepup-pin-hash` is its credential) and tells core which step-up tokens it granted. Core enforces everything else: device, channel, single use, hash and expiry. But core can't tell a token granted after a real PIN entry from one a compromised atlas-ui simply registered.

So **atlas-ui alone can approve**. atlas-ui is the most exposed Pi service: it parses LAN traffic and serves browsers. That makes it the weakest link in the approval chain.

**Proposal:**
- Move `stepup-pin-hash` to atlas-core's credentials (services.md §4).
- Core verifies the PIN, and keeps the rate limit and audit record for failures.
- atlas-ui forwards `stepup.verify {pin}` over `core.sock` (the peer is checked) and relays the `stepup.granted` token core returns.
- The PIN still never enters any LLM context or log.

**Bump:** services.md MINOR (credential ownership). events.v1 is unchanged on the wire.

**Affected:** atlas-ui (Phase 2), atlas-core, provisioning (the credential moves).

Until this lands, a compromised atlas-ui could approve any pending action. Everything else still holds: the exact payload hash, single use, the executor's checks, and the durable audit trail.

## 7. The executor enforces the channel allowlist and a send rate limit (services.md; review F11, same review)

**The gap.** The Discord channel allowlist and any sending rate are enforced in core, by the policy engine and the proposer. The executor checks only that a payload was approved, not where it goes or how often.

If core's signing key were stolen, or core itself compromised, the executor would post to any channel and send at any rate. That is a privilege the executor holds alone, so its own guardrails belong in the executor too.

**Proposal:**
- The executor's configuration (next to the approval public key) carries the Discord channel allowlist.
- Optionally, a recipient-domain policy.
- A per-kind send rate limit (for example, 20 emails an hour, 20 posts an hour).
- The executor refuses with `CHANNEL_NOT_ALLOWLISTED` or `RATE_LIMITED`, and records both.
- Document the config file and its owner in services.md.

**Bump:** services.md MINOR. Two new `action.resolved` codes in the catalogue (PATCH).

**Affected:** atlas-executor (Phase 6), provisioning.

## 8. A `research.run` action kind (events.v1 and audit-record.v1, MINOR)

Since review F7, `queue_research` is refused (`RESEARCH_NEEDS_USER`) once a request has seen untrusted-derived data, because research takes only the user's own question (§2.13). The safer middle ground is to let the planner *propose* the research for the user to approve, but `ActionKind` has no one-off research kind.

**Proposal:** add `research.run` to `ActionKind` in both schemas, with payload `{kind, question}` (the question being single-line, ≤ 300 characters, and `x-atlas-untrusted`). atlas-core would turn the refusal into a proposal. Approval would enqueue the task in core and never involve the executor.
