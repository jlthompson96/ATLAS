# Runbook: atlas-audit (verify, off-box anchors, outcome unknown)

Source: PLAN 2.12, `contracts/audit-record.v1.schema.json`. Code: `backend/atlas/audit/`. Acceptance tests: PT-13 (tamper detection), FI-3 (power cuts).

## What is on disk

```
/var/lib/atlas/audit/
  segments/seg-0000000000.jsonl     one JCS-serialized record per line, named by its first seq (chattr +a while active, +i once sealed)
  checkpoints.jsonl                 append-only copy of every checkpoint record (evidence of truncation)
  checkpoint.pub                    Ed25519 public key, unpadded base64url
```

Records hold IDs, byte counts and SHA-256 hashes only (D7), stored in the clear, as the PLAN 2.7 exposure table says. Rotation: daily (UTC) or at 16 MiB. A signed checkpoint is written every 1,000 records or hourly, whichever comes first.

## Verify (PT-13)

```sh
sudo -u atlas-audit /var/lib/atlas/current/venv/atlas-audit/bin/python -m atlas.audit verify /var/lib/atlas/audit
# exit 0: verified. exit 1: tampering or corruption (prints AUDIT_CHAIN_BROKEN and the first problem). exit 2: usage.
```

It checks the schema of every record, the gap-free `seq` across segments, `prev_hash` links, each `hash` (SHA-256 of the RFC 8785 serialization), purge anchors, every checkpoint signature, and the sidecar. It detects a modified, deleted, reordered or truncated record, a deleted or renamed segment, a deleted sidecar, and a forged checkpoint. It cannot protect the records written **after the last checkpoint**: without an off-box anchor an attacker with write access can rewrite them (not only truncate them) and the chain still verifies, because only a checkpoint signature binds history to the signing key. `verify` reports how many records sit after the last verified checkpoint (`records_after_last_checkpoint`) and treats more than the checkpoint cadence (1,000 records or one hour) as an error, because the writer would have signed a checkpoint by then. The service signs a final checkpoint at every clean stop, so the exposed window is at most one cadence interval of live records.

**Trust rules (review F1).** Nothing unsigned vouches for anything. A chain that does not start at seq 0 (after a purge) is accepted only if a `purge` record anchors it **and a signature-verified checkpoint at or after that purge record covers it**: audit-record.v1 gives `purge` no signature field, so the next checkpoint, whose signed head hash commits to the purge record and through it to `anchor_hash`, is the signature. `purge_segments` therefore signs a checkpoint before it unlinks anything. An `--expect-head`, or a sidecar entry, that predates the surviving chain is an error unless such a covered purge accounts for it; a missing or empty sidecar on a chain longer than one cadence is an error. At start the service cross-checks the sidecar and, for a purged chain, runs the full verification before it appends. Still undetectable on the box: a freshly forged chain shorter than one cadence with the sidecar deleted, and a replaced key together with the whole chain; the off-box copy of `checkpoint.pub` and `checkpoints.jsonl` is the only defence.

### Off-box anchor (do this)

Copy `checkpoint.pub` once, and `checkpoints.jsonl` regularly (the nightly backup does, PLAN 2.12), to a place the Pi cannot write. To verify against it from a copy of the data:

```sh
python -m atlas.audit verify /path/to/copy --pubkey /safe/checkpoint.pub --expect-head <SEQ>:<HASH>   # SEQ:HASH from the last checkpoint line you saved
```

`--expect-head` fails if the log is shorter than that point or has a different record there. Without `--pubkey`, the verifier trusts the `checkpoint.pub` next to the data, which an attacker with write access could replace.

## Outcome unknown (FI-3)

A `write_ahead` with no `write_result` after a crash means the action may or may not have happened. It is listed by `verify` as `OUTCOME_UNKNOWN`, and by `python -m atlas.audit outcome-unknown <dir>` (JSON lines). The executor and core can ask the running service over the socket (`{"op":"outcome_unknown"}`). It must be surfaced to the user and **never retried automatically**.

## Torn writes and recovery

`write()` happens immediately for every record, so a process crash loses nothing acknowledged. `fsync` happens before acknowledging `write_ahead`, `write_result`, `approval`, `action_proposed`, `auth`, `config`, `purge`, `checkpoint`, `genesis` (and `chain_broken`/`killswitch`); all other kinds are fsynced at most every 30 s or 64 KB. A power cut therefore loses at most ~30 s of read-event records. A partial last line is ignored by the verifier (reported as a warning) and the service starts a new segment after it. On start the service re-validates the last segment; if it does not verify, the service refuses to start (fail closed) and the alert is `AUDIT_CHAIN_BROKEN`: investigate before touching anything.

## Limits, flooding and write volume (review F4, F5)

- A bad request never harms the log: `args_redacted` is capped at nesting depth 8 and 1,000 values and walked iteratively; any parse or validation failure is `BAD_RECORD` and nothing is written. Only an error after the write has started (disk full, I/O error, failed fsync or checkpoint) poisons the log, and then the service **exits non-zero** so systemd restarts it and start-up re-validates the last segment (`Restart=always`).
- Per actor: at most 8 connections (of 64) and 5 appends per second with a burst of 50 (`RATE_LIMITED`; durable kinds count too). Client records are refused below a free-space floor of 200 MB on the data partition (`NO_SPACE`); the service's own checkpoints and records are not blocked. egress treats any refusal as "deny".
- Egress denial records are rate limited per caller (burst 10, 1 per second); what is suppressed is counted and written as one aggregate record per caller and reason (`result.code DENIALS_SUPPRESSED`, `task_id suppressed-<N>`; audit-record.v1 has no count field, see the proposal).
- **Write volume against PLAN 3.5.** A typical record is 250 to 450 bytes (about 400 B for `egress` and `tool_call`). Honest use is on the order of a few hundred to a few thousand records a day (each allowed egress connection writes two, each tool call one, each approved write about four, a checkpoint every 1,000 records or hour): roughly 0.1 to 1 MB/day, well inside the 1 to 20 MB/day estimate and the 100 MB/day card budget, and `fsync` batching (30 s or 64 KB) keeps the physical writes close to the logical ones. A hostile caller is bounded to 5 records/s per actor, about 170 MB/day per actor at 400 B if it never stops, which would exceed the budget: the budget alert (`WRITE_BUDGET_EXCEEDED`, B10) and the free-space floor are the backstops, not these limits alone. Measure the real figure in B10.

## Retention and deletion (purge)

`AuditLog.purge_segments()` writes a `purge` record (anchor hash of the last purged record) and fsyncs it **before** unlinking the oldest sealed segments, so a crash in between still verifies. **Not implemented in Phase 0:** per-segment data keys (crypto-shredding with `audit-segment-master-key`), zstd compression of sealed segments (not in Python 3.13's standard library), and the scheduled 365-day retention job. The credential is provisioned and loaded but unused.

## Keys

`audit-checkpoint-key` is an unencrypted PKCS#8 Ed25519 PEM stored with `systemd-creds` (host-key mode: protects against casual exposure only, PLAN 2.12). Rotating it changes `key_id`; the verifier currently accepts one key. Plan a rotation record before rotating (open question in the handoff).
