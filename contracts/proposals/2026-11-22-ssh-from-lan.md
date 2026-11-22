# Proposal: allow SSH to the Pi from the whole home LAN

**Target:** `contracts/services.md` (v1.2.0), section 2. **Bump:** MINOR (a listener admits more sources; nothing on the wire changes). **Author:** firewall work (`deploy/nftables/atlas.nft`), at the owner's request. **Status:** Approved by the owner 2026-11-22, applied in services.md 1.3.0.

## What

Section 2, the SSH row:

| | Allowed clients |
|---|---|
| Before | admin device only (the development Mac, §7.0) |
| After | the home LAN (`LAN_CIDR`). Only the admin's key can log in. |

The rest of the row stays: key-only, the key has a passphrase, deploys use this path.

In `atlas.nft` the rule becomes `ip saddr $LAN_CIDR tcp dport 22 accept`, the same source set as the HUD (:443) and ping. `ADMIN_IP` leaves `site.nft.example`. A site file that still defines it keeps loading, because nftables accepts an unused `define` (checked with `nft -c` on nftables 1.1.3, the Pi's version).

## Why

On 2026-11-22 the Mac's DHCP address changed from `.79` to `.92`. The firewall dropped SSH, and the Pi could only be reached at its console. The router reservations that would have prevented this were never made. The owner chose to allow the whole LAN rather than depend on a fixed address.

## Costs and limits

- **More of the network can reach sshd.** Every LAN device can now open a connection to port 22, including smart-home devices, guests, and anything compromised on the LAN. What still holds:
  - sshd accepts keys only: no passwords, no keyboard-interactive, no root, `AllowUsers <admin>` (`base-os.sh`, checked by `base-os.sh check`).
  - The only key that can log in is on the development Mac, protected by a passphrase.
  - The added risk is a pre-authentication flaw in OpenSSH reachable from the LAN, plus connection noise. Neither needs the key.
- **Not the internet:** the router has no port forwards (PLAN §5.1), and the rule matches IPv4 LAN sources only. There is still no IPv6 SSH rule.
- **Later hardening, not part of this proposal:** list a few device addresses again once the router reservations exist, or rate-limit new SSH connections.

## Who is affected

- `deploy/nftables/atlas.nft`, `site.nft.example` and `backend/tests/test_deploy_structure.py`.
- PLAN §5.1 and §7.0, and the provisioning, bring-up and verification runbooks.
- The Pi gets the new rule at the next `provision-pi.sh` run with the overlay off (`/etc/atlas` is on the read-only root).
- No service, unit, event, audit record or credential changes.
