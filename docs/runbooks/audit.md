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

It checks the schema of every record, the gap-free `seq` across segments, `prev_hash` links, each `hash` (SHA-256 of the RFC 8785 serialization), purge anchors, every checkpoint signature, and the sidecar. It detects a modified, deleted, reordered or truncated record, a deleted or renamed segment, a deleted sidecar, and a forged checkpoint. It cannot see the loss of records written **after the last checkpoint** if an attacker also removes them cleanly; that needs an off-box anchor.

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

## Retention and deletion (purge)

`AuditLog.purge_segments()` writes a `purge` record (anchor hash of the last purged record) and fsyncs it **before** unlinking the oldest sealed segments, so a crash in between still verifies. **Not implemented in Phase 0:** per-segment data keys (crypto-shredding with `audit-segment-master-key`), zstd compression of sealed segments (not in Python 3.13's standard library), and the scheduled 365-day retention job. The credential is provisioned and loaded but unused.

## Keys

`audit-checkpoint-key` is an unencrypted PKCS#8 Ed25519 PEM stored with `systemd-creds` (host-key mode: protects against casual exposure only, PLAN 2.12). Rotating it changes `key_id`; the verifier currently accepts one key. Plan a rotation record before rotating (open question in the handoff).
