# Runbook: the Pi or its card is stolen, lost or copied

Source: PLAN §2.7 (no disk encryption, D5/D17), threat T10, known issues P3 and P4. Target: **steps 1–3 within 1 hour of noticing.**

## When to use it

Use it whenever the card, or the whole Pi, was out of your control for any length of time: stolen, lost, borrowed, sent off for repair, or found in a different place than you left it. Copying a microSD card takes a few minutes and leaves no trace, so a card that came back still counts as copied.

The data partition is not encrypted (D5). Whoever holds a copy can read every secret on it. The systemd-creds files don't help, because the key that decrypts them (`/var/lib/systemd/credential.secret`) is on the same card. Assume that every secret listed below is known to the attacker. The job is to make each one worthless, in order of how much damage it can still do.

## Step 0: write down when it happened

Note the last time you know the card was in your control, and the time you noticed. You need both for the audit check in step 4 and for reading the Google and Discord activity logs.

## Step 1: revoke what works on the internet (first 15 minutes)

These can be used from anywhere, so revoke them before anything else. **Each row applies only from the phase that adds it.** Skip rows for services that don't exist yet. Up to Phase 0, the Pi holds only the audit keys, and step 1 has nothing to revoke.

| Secret (service) | Phase | What the attacker can do with it | Revoke |
|---|---|---|---|
| `google-send-refresh-token` (`atlas-executor`) | 6 | Send mail as you | Google Account → Security → "Your connections to third-party apps & services" → remove the ATLAS app. That revokes **both** grants (read and send), because they belong to the same OAuth client. Then follow the next row. |
| `google-read-refresh-token` (`atlas-google`) | 4 | Read your mail and calendar | Revoked together with the send grant above. In "Testing" status a refresh token expires after 7 days anyway [S28], but don't rely on that. |
| `google-oauth-client` (`atlas-google`, `atlas-executor`) | 4 | Combined with a new grant from you, it could be used to phish you with a consent screen that looks genuine | Google Cloud console → APIs & Services → Credentials → the ATLAS OAuth client → add a new secret, then delete the old one. |
| `discord-poster-bot-token` (`atlas-executor`) | 6 | Post as the bot, in every server it is in. **The token never expires.** | Discord Developer Portal → the poster application → Bot → Reset Token. |
| `discord-reader-bot-token` (`atlas-discord`) | 4 | Read every channel the reader bot can see. **The token never expires.** | Same, for the reader application. |
| `twelvedata-api-key` (`atlas-market`) | 4 | Use your quota; low impact | Twelve Data dashboard → API keys → regenerate. |

Then check for misuse in the window from step 0: Gmail's Sent folder and Google's security activity page, and the Discord audit log of each server the bots are in.

## Step 2: cut the Pi off from the desktop

| Secret | Phase | Revoke |
|---|---|---|
| Pi's WireGuard private key (root, `systemd-networkd`) | 1 | On the desktop, remove the Pi's `[Peer]` from the WireGuard tunnel (or deactivate the tunnel until the new Pi exists). The desktop then accepts nothing from `10.77.0.1`, which is also what `atlas-hostd` on port 8770 trusts. Rotate the desktop's own WireGuard key too: the copy holds its public key and endpoint, not its private key, so this is optional. |
| `lmstudio-api-token` (`atlas-core`) | 1 | LM Studio → Developer → API tokens: delete the ATLAS token. Without the tunnel it can only be used from your LAN, but delete it anyway. |
| ATLAS local CA on your devices (`atlas-ui`'s certificate) | 2 | On every device that trusts "ATLAS local CA": remove it now (Mac: Keychain Access; iPhone: Settings → General → VPN & Device Management; Windows: `certmgr.msc`). The thief has the server key, so a device that still trusts the CA would accept an impostor HUD at the Pi's address and could hand it your PIN. The CA itself keeps no private key, so it can't sign anything new. |

## Step 3: things that stay dangerous after a re-image

| Item | What the attacker has | Do |
|---|---|---|
| Admin password hash (`/etc/shadow`; before P3 was fixed on the Pi, also `/boot/firmware/user-data`) | Can be cracked offline at leisure | If you use that password anywhere else, change it there **now**. The new image gets a new password. |
| SSH host keys (`/etc/ssh/ssh_host_*`) | Can impersonate the Pi to your Mac, which would then hand over your sudo password at the next `sudo` | On the Mac: `ssh-keygen -R atlas-pi` and `ssh-keygen -R <pi-ip>`. Don't log in to anything that answers as `atlas-pi` until step 5 gives you the new key. |
| Your admin SSH key | Nothing: only the `.pub` half is on the Pi | Nothing to do. If the Mac itself was also stolen, that is a different incident. |
| `.bash_history` of the admin | Every command you typed, including any secret typed on a command line | Read your own history from the last backup or from memory, and rotate anything that ever appeared on a command line. `store-credential.sh` reads secrets from stdin to avoid this. |

## Step 4: check the audit log before you trust anything from it

The stolen copy contains `audit-checkpoint-key`, so the attacker can now write a chain, and sign checkpoints for it, that the old key accepts. Only what you saved **off the Pi before the theft** can tell a genuine log from a forged one (audit runbook, "Off-box anchor").

1. Take the latest backup made before the time in step 0, or the Pi itself if you got it back, and verify it on the Mac with the off-box values. Don't use the `checkpoint.pub` stored next to the data:
   ```sh
   cd backend
   .venv/bin/python -m atlas.audit verify /path/to/copy/audit --pubkey /safe/checkpoint.pub \
       --expect-genesis <GENESIS_HASH> --expect-head <SEQ>:<HASH>   # the last checkpoint you saved off the Pi
   ```
2. Only records up to that `--expect-head` are proven genuine. Treat records after it as untrusted, even if they verify: they may have been written with the stolen key.
3. Keep the old segments, `checkpoints.jsonl`, the old `checkpoint.pub` and the old anchors together, read-only, as evidence. Don't import them into the new install.

## Step 5: re-image and re-key

Start from a fresh card ([pi-bringup.md](pi-bringup.md), then [provisioning.md](provisioning.md)). Don't restore credentials from a backup: the backup holds the same secrets the attacker now has. Restore data only (memory DB, task queue, config), and only from a backup made before the time in step 0.

- **New admin password** at flashing time (pi-bringup step 2), not reused anywhere. Check that `base-os.sh check` passes the P3 lines.
- **New SSH host key:** at the console, run `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` and compare it with what the Mac shows at the first `ssh atlas-pi`.
- **New audit chain:** `gen-audit-keys.sh`, start the service, record the new `checkpoint.pub` and genesis hash off the Pi. These are new anchors; keep the old ones with the evidence from step 4.
- **New credentials,** each created fresh and stored with `store-credential.sh`, never copied from the old card:

| Credential (service) | Phase | Re-create |
|---|---|---|
| `audit-checkpoint-key`, `audit-segment-master-key` (`atlas-audit`) | 0 | `gen-audit-keys.sh` (above) |
| WireGuard key pair | 1 | New key pair on the Pi; put the new public key in the desktop's `[Peer]` |
| `lmstudio-api-token` (`atlas-core`) | 1 | New token in LM Studio |
| `approval-signing-key` (`atlas-core`) | 2 | `gen-core-keys.sh`. Put the new public key in `atlas-executor`'s config (Phase 6). Any approval signed with the old key must be refused. |
| `tls-key` (`atlas-ui`), ATLAS local CA | 2 | `gen-ui-keys.sh` on the new card: a new CA and server key. Install the new `ca.crt` on each device (the old one was removed in step 2). |
| `device-token-key` (`atlas-ui`) | 2 | New key (`gen-ui-keys.sh` creates it on a fresh card). Every paired device's token becomes invalid; pair each device again (`ui-devices.sh pair remote`). |
| `stepup-pin-hash` (`atlas-ui`) | 2 | **Choose a new PIN** with `gen-ui-pin.sh` at the console. The old hash is Argon2id, but a short PIN falls to an offline search quickly. |
| Google grants, `google-oauth-client` | 4, 6 | New consent for the read grant (and later the send grant), using the new client secret from step 1 |
| Discord bot tokens | 4, 6 | The reset tokens from step 1 |
| `twelvedata-api-key` | 4 | The regenerated key from step 1 |
| Backup repository password (`atlas-backup`) | 0 | It is always on the Pi (ADR 0002: the nightly run is unattended), so the thief can decrypt it with the host key. Run `gen-backup-key.sh` on the new image, then **re-key every drive you still have**, including the offline one: `restic key add` (old password, then the new one), `key remove` the old key (backup.md, "Changing the password"). Treat a drive that was plugged into the stolen Pi as lost with it. |

## What can't be rotated

The stolen copy also holds data, and that stays exposed. Note it in the incident record:

- **Memory DB and transcripts:** at most 30 days of memory and 7 days of transcripts (§2.12 retention).
- **Portfolio (Phase 4):** ciphertext only. The key is derived from your passphrase and never stored (D21), so its protection is the passphrase's strength. If the passphrase is weak, assume the holdings are known.
- **Journal:** up to 30 days of logs. They should hold IDs, sizes and timings only, never content or secrets (PT-6 checks that).
- **Audit log:** resource IDs and hashes, not content (D7).
- **No email, Discord or calendar bodies** are stored on the Pi (§2.7), so there are none to lose.

## Do now, before anything happens

The steps above only work if these exist **off the Pi**, somewhere the Pi can't write:

- The audit anchors: `checkpoint.pub`, the genesis hash, and regular copies of `checkpoints.jsonl` (the last line gives the `--expect-head`).
- The backup encryption key, printed.
- This runbook, and the console links in steps 1–2 (Google security page, Google Cloud credentials, Discord Developer Portal, Twelve Data, LM Studio), somewhere you can reach from a phone.
- An admin password that is used nowhere else.
