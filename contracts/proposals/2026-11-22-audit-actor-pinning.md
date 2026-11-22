# Proposal: pin each audit record kind and event to the services that may write it

**Target:** `contracts/audit-record.v1.schema.json` (v1.1.0). **Bump:** MINOR (fewer records are valid; every record an honest service writes today stays valid). **Author:** audit work (`backend/atlas/audit/record.py`), at the owner's request. **Status:** Approved by the owner 2026-11-22, applied in audit-record.v1 1.2.0. **Supersedes** item 2 of `2026-11-18-security-core-followups.md` (pin `action_proposed`), which this includes.

## What

atlas-audit takes `actor` from the socket's peer credentials, so a service can't pretend to be another. But it can write **any kind** of record under its own name. Seven kinds are pinned today (`genesis`, `purge`, `checkpoint` to atlas-audit; `approval` to atlas-core; `write_ahead`, `write_result` to atlas-executor; `egress` to atlas-egress). Everything else is open to all 13 actors.

Pin the rest, per kind and, where one kind covers several owners, per event:

| Kind | Event | May be written by |
|---|---|---|
| `action_proposed` | — | `atlas-core` |
| `tool_call` | — | `atlas-core` |
| `power` | all | `atlas-core` (the power controller runs in core) |
| `config` | — | `atlas-core`, `atlas-ui` (keys such as `research.schedule` live in core; `layout.*` and `privacy.*` in the UI server) |
| `auth` | `device_paired`, `device_revoked`, `ws_rejected` | `atlas-ui` |
| `auth` | `stepup_verified`, `stepup_failed` | `atlas-ui` (it verifies the PIN today). Follow-up item 6, if approved, moves PIN checking into core and changes this row to `atlas-core` in the same bump. |
| `auth` | `portfolio_unlocked`, `portfolio_unlock_failed` | `atlas-market` (it derives the portfolio key, D21) |
| `auth` | `oauth_renewed`, `oauth_expired` | `atlas-google`, `atlas-executor` (each holds its own grant) |
| `auth` | `credential_rotated` | any service, but `subject` must name **one of its own credentials** from services.md §4 |
| `system` | `chain_verified`, `chain_broken`, `retention_run` | `atlas-audit` |
| `system` | `killswitch`, `task_killed` | `atlas-core` |
| `system` | `service_start`, `service_stop`, `error_forwarded` | any service: these only describe the writer itself |

The pins in the table go into the schema as `allOf` branches (`if` kind and event, `then` an `actor` enum), like the existing ones. atlas-audit enforces them in two places, both of which already exist for the current pins:

- **On write** (`validate_submission`): a record from the wrong service is refused with `BAD_RECORD` and nothing is written.
- **On verify** (`validate_record`): a record in the log that breaks a pin is an error, exit 1. Only a writer that bypassed atlas-audit could have put it there.

The `credential_rotated` rule turned out to be expressible after all: the schema lists each service's own credentials in an `anyOf`, and `record.py` holds the same table. Tests check that the schema, `record.py` and services.md §4 all list the same credentials.

## Why

A reader service talks to the internet and parses untrusted data. If it is compromised, today it can write, under its own name:

- `auth stepup_verified` or `portfolio_unlocked`, which suggest that a PIN or the passphrase was entered when it wasn't;
- `system killswitch`, `task_killed` or `chain_verified`, which suggest that something was stopped or checked when it wasn't;
- `power` and `tool_call` records that blur what core actually did.

None of these grants anything: approvals are bound to core's signed record and step-up tokens. But the audit log is what you read after an incident, and forged records there cost time or hide the real ones. Consumers must currently filter by actor themselves (known-issues "Contract follow-ups"). After this change the log itself guarantees it.

## Costs and limits

- **Honest writers today:** atlas-core (`power`, `tool_call`, `action_proposed`, `system error_forwarded`), atlas-executor, atlas-egress and atlas-audit itself. All already conform. No service writes `auth` or `config` yet, so those pins cost nothing now.
- **Existing logs:** only atlas-audit and atlas-egress run on the Pi so far, and their kinds are already pinned to them, so its log stays valid. `verify` is re-run there after the next deploy to confirm.
- **Future services:** a new event type needs a row here. That's intended: a writer has to be decided before an event is added.
- **Not covered:** a service can still write false records of a kind it owns (a compromised atlas-ui can still write `device_paired`). Pinning limits each service to the records it owns, but can't check whether a record is true.

## Who is affected

- `backend/atlas/audit/record.py` (`KIND_ACTOR` becomes a kind/event table, plus the credential check) and its tests; `backend/tests/test_contracts.py` (the reference validator).
- `contracts/audit-record.v1.schema.json` → 1.2.0; `contracts/CHANGELOG.md`.
- `docs/known-issues.md`: the actor-pinning follow-up closes. `docs/runbooks/audit.md` gets a short "who may write what" note.
- atlas-ui (Phase 2) and atlas-market and atlas-google (Phase 4) must write their `auth` events under these rules when they add them.
- No change to services.md, events.v1 or any unit.
