# Proposal: socket groups for multi-client sockets, reserved names under `/var/lib/atlas`, proxy-client address family

**Target:** `contracts/services.md` (v1.0.0). **Bump:** MINOR (additive clarifications and new names). **Author:** agent C (Pi foundations). **Status:** Approved 2026-11-16, applied in services.md 1.1.0 and audit-record.v1 1.1.0 (commit 1944f05).

## 1. Socket groups cannot always be `atlas-<consumer>`

services.md section 2 says sockets are created "with mode `0660` and group `atlas-<consumer>` so only the named clients can connect". That works for single-consumer sockets and cannot work for two of them:

| Socket | Clients | What the units do now |
|---|---|---|
| `/run/atlas/executor.sock` | `atlas-core` only | group `atlas-core` (as specified) |
| `/run/atlas/fallback.sock` | `atlas-core` only | group `atlas-core` (as specified) |
| `/run/atlas/egress-research.sock` | `atlas-research` only | group `atlas-research` (as specified) |
| `/run/atlas/audit.sock` | every Pi service that writes audit records (13) | **new** group `atlas-audit-clients` |
| `/run/atlas/core.sock` | `atlas-ui`, `atlas-voice`, `atlas-google`, `atlas-discord`, `atlas-panels`, `atlas-market`, `atlas-research`, `atlas-fallback` | **new** group `atlas-core-clients` |

Why not reuse `atlas-core` for `core.sock` clients: that group also guards `executor.sock` and `fallback.sock`, so every core client would be able to reach the executor socket. Membership is granted per unit with `SupplementaryGroups=` (no `usermod`), and a test asserts that no unit other than the `atlas-core` user is ever in group `atlas-core`.

**Proposed text for services.md section 2:** add the two group names and the rule "a group named `atlas-<consumer>` is used only for a socket with exactly one client; the group `atlas-<consumer>` then contains only that user".

## 2. Reserved names under `/var/lib/atlas`

Section 6 reserves `releases` and `current`. The deploy mechanism also needs `previous` (symlink to the release `current` replaced, for rollback), `incoming` (SSH upload staging, owned by the admin account) and `credentials` (`LoadCredentialEncrypted=` files, root-only, `credentials/<service>/<name>.cred`). **Proposed:** reserve `releases`, `current`, `previous`, `incoming`, `credentials`. None collides with a service state directory (`atlas/<name>` for the 14 service names).

## 3. The sandbox baseline cannot hold for proxy clients

The baseline sets `RestrictAddressFamilies=AF_UNIX` for everything except `atlas-egress` and `atlas-ui`. But `atlas-google`, `atlas-discord`, `atlas-executor`, `atlas-panels`, `atlas-market` and `atlas-search` use `HTTPS_PROXY=http://127.0.0.1:<port>`, which is a TCP connection to loopback, so they need `AF_INET`. `atlas-core` needs it for the WireGuard tunnel (LM Studio, `atlas-hostd`) and the Wake-on-LAN broadcast, and `atlas-kiosk` for `https://127.0.0.1`. The units add `AF_INET` with `IPAddressAllow=localhost` (core additionally `10.77.0.2` and the broadcast) and document it as a relaxation, which section 5 already permits ("relaxes only what it needs"). **Proposed:** list these in the baseline comment so the table matches reality. The tests pin exactly which services may have IP address families.

## 4. Nice to have (optional)

- Name the capability exceptions in section 5: `atlas-audit` needs `CAP_LINUX_IMMUTABLE` (`chattr +a` on the active segment, `+i` when sealed); `atlas-ui` needs `CAP_NET_BIND_SERVICE` (already stated).
- `audit-record.v1` `egress.block_reason` has no value for request-shape or resource-limit refusals (oversized head, slowloris, connection limit, DNS failure, audit unavailable). Today the proxy omits `block_reason` for those and puts a stable `result.code` (`HEADER_TOO_LARGE`, `HEADER_TIMEOUT`, `TOO_MANY_CONNECTIONS`, `DNS_FAILED`, `TLS_TIMEOUT`, ...). If the owner wants them filterable by `block_reason`, add values such as `malformed_request` and `limit` (MINOR; consumers must already tolerate unknown enum values).

## Who is affected

`atlas-audit`, `atlas-egress` and every service unit (agents building the other Pi services use the group names above); nothing in `events.v1`, `hostd.v1` or `audit-record.v1` changes unless the optional enum addition in section 4 is accepted.
