# Proposal: signed purge records, and a count for aggregated egress denials

**Target:** `contracts/audit-record.v1.schema.json` (v1.1.0). **Bump:** MINOR (two optional additions). **Author:** agent C. **Status:** proposed; nothing here is needed for the current implementation, which works within 1.1.0 as described below.

## 1. Optional `signature` and `key_id` on `purge`

Review finding F1: a `purge` record re-anchors the chain (the first surviving record chains from `anchor_hash`), but the record is not signed. Within the 1.1.0 schema the verifier accepts a purge anchor only when a **signature-verified checkpoint at or after the purge covers it** (the next checkpoint's signed head hash commits to the purge record), and `purge_segments` signs a checkpoint immediately after writing the purge. That is sound, but it leaves a window between the purge record and the next checkpoint and makes the purge depend on a later record.

Proposal: add optional `key_id` and `signature` (same pattern as `Checkpoint.signature`) to `$defs.Purge`, signed over `"atlas-audit-purge-v1\n" + from_seq + "\n" + to_seq + "\n" + anchor_hash`, and require them for new purge records. Verifiers would then accept the anchor from the purge record's own signature. Affected: `atlas-audit` writer and verifier only.

## 2. Optional `count` on `egress`

Review finding F5: denial records are rate limited per caller and the suppressed ones are written as one aggregate per caller and `block_reason` (`result.code DENIALS_SUPPRESSED`). The schema has no field for "how many". The implementation carries the number in `egress.task_id` as `suppressed-<N>`, which is documented as a research task ID, so this is a stop-gap.

Proposal: add an optional integer `count` (minimum 1) to `$defs.Egress`, meaning "this record stands for N events" (always 1 when absent). Aggregate records would use it and drop the `task_id` convention. Affected: `atlas-egress` and anything that reads egress records for display.
