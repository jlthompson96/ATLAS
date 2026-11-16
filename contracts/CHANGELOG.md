# Contracts changelog

| Date | Contract | Revision | Change |
|---|---|---|---|
| 2026-11-16 | all | 1.0.0 | Initial v1 contracts from Step C0, approved by the owner, including the 12 decisions beyond PLAN.md (calendar in `atlas-google`, two Discord bots, `atlas-fallback`, per-caller proxy ports, server-side per-device privacy, hash-bound approvals, new event types, `host.status` fields, extra state transitions, hostd leases, hostd source-IP access, code releases). |
| 2026-11-16 | hostd.v1 | 1.1.0 | Proposal 2026-11-16-hostd-1.1, approved by the owner: `PowerInfo.booted_at`; wake claims measured from the wake-cycle start (resume or boot) with a 90 s upper bound; nullable `preferred_sleep_state`; `default` error response (404/405/500); `BODY_TOO_LARGE`, `INVALID_LEASE_ID`, `LEASE_LIMIT` under 400; GPU and activity description clarifications. |
