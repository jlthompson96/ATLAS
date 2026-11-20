# Proposal: user-requested sleep and a sleep-reason audit value (power.interface 1.0.0, audit-record.v1)

**Targets:** `backend/atlas/power/interface.py` (1.0.0), `audit-record.v1.schema.json` (1.1.0, `power.reason`).
**Author:** Phase 1 link agent (agent D).
**Status:** proposed. Nothing here blocks Phase 1; both items are worked around in `atlas.power.controller`.

## 1. `SleepReason.MANUAL` (power interface, MINOR)

`SleepReason` has `IDLE`, `QUEUE_DRAINED` and `SPECULATIVE_UNUSED`. "Put the desktop to sleep" said by the owner is none of these, and the Pi-side rule "20 min with no LLM request" should not apply to an explicit command (hostd still enforces every host-side condition).

**Workaround today:** `HostPowerController.request_sleep(reason, *, user_requested=False)` is a keyword-only extension on the concrete class. The grammar's sleep command passes `SleepReason.IDLE, user_requested=True`.

**Proposal:** add `SleepReason.MANUAL` ("the owner asked"), which skips the Pi-side idle timers but not the queue check, and map it to hostd `reason: idle` (hostd.v1's `SleepRequest.reason` has no `manual` value, so a matching hostd.v1 MINOR bump would add one).

## 2. `power.reason` in audit-record.v1 lacks `speculative_unused` (audit, PATCH)

The audit `power.reason` enum is `interactive, speculative, scheduled_research, manual, retry, idle, queue_drained`. The interface's `SleepReason.SPECULATIVE_UNUSED` has no value, so it is audited as `idle`.

**Proposal:** add `speculative_unused` to the enum, so the log can tell "slept after 20 idle minutes" from "slept because a speculative wake went unused" (useful when tuning D12).

**Affected:** `atlas.audit.record` (agent C's area) and `atlas.power.controller`'s reason mapping.
