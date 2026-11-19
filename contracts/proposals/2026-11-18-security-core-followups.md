# Proposal: security-core follow-ups (action payload shapes, actor pinning, step-up registration, approval record)

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
