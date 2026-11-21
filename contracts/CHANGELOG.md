# Contracts changelog

| Date | Contract | Revision | Change |
|---|---|---|---|
| 2026-11-16 | all | 1.0.0 | Initial v1 contracts from Step C0, approved by the owner, including the 12 decisions beyond PLAN.md (calendar in `atlas-google`, two Discord bots, `atlas-fallback`, per-caller proxy ports, server-side per-device privacy, hash-bound approvals, new event types, `host.status` fields, extra state transitions, hostd leases, hostd source-IP access, code releases). |
| 2026-11-16 | hostd.v1 | 1.1.0 | Proposal 2026-11-16-hostd-1.1, approved by the owner: `PowerInfo.booted_at`; wake claims measured from the wake-cycle start (resume or boot) with a 90 s upper bound; nullable `preferred_sleep_state`; `default` error response (404/405/500); `BODY_TOO_LARGE`, `INVALID_LEASE_ID`, `LEASE_LIMIT` under 400; GPU and activity description clarifications. |
| 2026-11-16 | events.v1 | 1.1.0 | Proposal 2026-11-16-hud-activity-heartbeat, approved by the owner: client message `activity.ping` (empty payload, no reply, at most once every 30 s on real input); inactivity rule stated in `PrivacyMode` and README. |
| 2026-11-16 | services.md | 1.1.0 | Proposal 2026-11-16-socket-groups-and-reserved-names, approved by the owner: socket group table (`atlas-core-clients`, `atlas-audit-clients`), reserved names `previous`, `incoming`, `credentials`, address-family relaxations table, capability list. |
| 2026-11-16 | audit-record.v1 | 1.1.0 | Same proposal, section 4: `egress.block_reason` gains `malformed_request` and `limit`. |
| 2026-11-20 | services.md | 1.2.0 | Proposal 2026-11-20-logs-reserved-name, approved by the owner: reserved name `logs` under `/var/lib/atlas` (persistent journal, bind-mounted at `/var/log/journal`, 256 MB / 30 days, root-only). |
