# Runbook: the WireGuard link between the Pi and the desktop

Source: PLAN §5.1, §5.3 (R-LINK), §2.8 (R-LLM); contracts/services.md §2 (Pi `10.77.0.1`, desktop `10.77.0.2`, UDP 51820, LM Studio `:1234`, `atlas-hostd` `:8770`); known issue D1. Phase 1, first hardware step.

**What it is.** A point-to-point WireGuard tunnel. Only `atlas-core` on the Pi may use it, and only to reach LM Studio and `atlas-hostd` on the desktop. Both ends authenticate with public keys, and each private key stays on its own machine:

- **Pi:** `systemd-networkd` runs `wg0`, while NetworkManager keeps the LAN and is told to leave `wg0` alone. The private key is a systemd-creds credential (`/etc/credstore.encrypted/network.wireguard.private.wg0`, encrypted with the host key, and in the nightly backup). The firewall lets the tunnel's own packets out only when they carry `FirewallMark=0x5182`, and lets only `atlas-core` send through the tunnel.
- **Desktop:** WireGuard for Windows, tunnel `atlas`. It starts at boot as a Windows service, before anyone signs in. Windows Defender Firewall rules in their own `ATLAS` group allow WireGuard from the Pi's LAN address, and LM Studio and `atlas-hostd` only from `10.77.0.1` inside the tunnel. Every other address is blocked on those two ports.

**Not used, on purpose:** a preshared key (it would be a secret to carry between machines; WireGuard's public keys already authenticate both ends) and `PersistentKeepalive` (the desktop sleeps; the Pi starts a handshake whenever it has something to send).

## Before you start

- **Fixed addresses for both machines.** Reserve the Pi's and the desktop's LAN addresses in the router: each end names the other by IP address.
- **WireGuard for Windows** on the desktop, from the official site `https://www.wireguard.com/install/`. Before running the installer, check its signature (right-click > Properties > Digital Signatures: WireGuard LLC).
- **A maintenance window on the Pi** (overlay off, pi-bringup §11) and the console. Steps 1 and 3 write `/etc`.
- The desktop inventory (`hostd/scripts/desktop-inventory.ps1`) should say the Pi is on the same layer-2 segment.

## 1. Pi: tools and key

Install `wireguard-tools` the same way as restic (backup.md step 1): download it in the ARM64 VM and check its hash against the Pi's own index, so the firewall needn't open.

```sh
# Mac
limactl shell atlas -- bash -c 'cd /tmp && apt-get download wireguard-tools=1.0.20210914-3 && sha256sum wireguard-tools_*_arm64.deb'
limactl copy atlas:/tmp/wireguard-tools_1.0.20210914-3_arm64.deb . && scp wireguard-tools_*_arm64.deb atlas-pi:/tmp/
# Pi: the two hashes must match (name the version: with backports enabled apt-cache lists two), then install offline
# (--no-install-recommends: otherwise apt wants wireguard-dkms and downloads) and create the key
apt-cache show wireguard-tools=1.0.20210914-3 | grep '^SHA256:'; sha256sum /tmp/wireguard-tools_*_arm64.deb
sudo apt-get install --no-install-recommends /tmp/wireguard-tools_1.0.20210914-3_arm64.deb
sudo ~/atlas-deploy/provisioning/gen-wg-key.sh
```

`gen-wg-key.sh` prints the **Pi's public key**. That's safe to copy anywhere: it isn't a secret. The private key goes straight from `/tmp` (RAM on the Pi) into the encrypted credential and is never shown.

## 2. Desktop: the tunnel

1. WireGuard app > Add Tunnel > **Add empty tunnel...** Name: `atlas`. The app writes `[Interface] PrivateKey` itself and shows the **desktop's public key** at the top. Copy that key.
2. Below the `PrivateKey` line, paste the rest of [`deploy/wireguard/desktop-atlas.conf.example`](../../deploy/wireguard/desktop-atlas.conf.example), then put in the Pi's public key and the Pi's LAN address. Save. Don't add a `DNS =` line.
3. Activate it. Windows will show "atlas" as a network adapter. The WireGuard app runs it as a service that starts at boot.

## 3. Pi: the desktop's key and address

```sh
echo '<the desktop public key>' | sudo tee /etc/atlas/wireguard-desktop.pub
sudo nano /etc/atlas/nftables-site.nft        # DESKTOP_LAN_IP = the desktop's reserved LAN address
sudo ATLAS_ADMIN_USER=... ./provision-pi.sh --dry-run    # then without --dry-run (same variables as always)
```

`provision-pi.sh` reloads the firewall (the site file changed), writes `/etc/systemd/network/50-atlas-wg0.netdev`, enables `systemd-networkd` and disables its wait-online unit, which NetworkManager's own replaces. As with every reload, **SSH sessions that are open freeze**: reconnect (provisioning.md).

## 4. Desktop: firewall rules

In an **administrator** PowerShell, from the folder with the script:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop-firewall.ps1 -PiLanAddress <Pi LAN IP>          # shows the plan, changes nothing
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop-firewall.ps1 -PiLanAddress <Pi LAN IP> -Apply   # creates the ATLAS rules
```

It only creates or removes rules in its own `ATLAS` group. `-Remove` takes them away again.

## 5. Check it

On the Pi:

```sh
sudo runuser -u atlas-core -- ping -c 3 10.77.0.2     # atlas-core is the only user allowed through
sudo wg show wg0                                       # "latest handshake: ... seconds ago", fwmark 0x5182
sudo runuser -u nobody -- ping -c 1 -W 2 10.77.0.2     # must fail: the firewall drops it
```

From another machine on the LAN (the Mac), the desktop's ports must **not** answer: `nc -vz -w 3 <desktop LAN IP> 1234` should time out. Once `atlas-hostd` runs, `sudo runuser -u atlas-core -- curl -s http://10.77.0.2:8770/v1/health` answers.

The Pi drops pings from the desktop over the tunnel. That's by design: nothing on the desktop starts a connection to the Pi.

Then turn the overlay back on (pi-bringup §11), and run `base-os.sh check`.

## Troubleshooting

| Symptom | Check |
|---|---|
| No handshake | The desktop is awake and the tunnel is active; `DESKTOP_LAN_IP` is right; the desktop has the "ATLAS: WireGuard from the Pi" rule; `sudo wg show wg0 fwmark` is `0x5182`. |
| Handshake, but `ping` as `atlas-core` fails | The desktop's `AllowedIPs` is `10.77.0.1/32` and its `Address` is `10.77.0.2/32`. |
| After changing `FirewallMark` the result doesn't change | Connection tracking still remembers the old flow for up to 3 minutes. Wait, or reboot. (Seen in the VM.) |
| `networkctl` shows `wg0` failed | `journalctl -u systemd-networkd`: a missing or unreadable credential, or a malformed public key in `/etc/atlas/wireguard-desktop.pub`. |

## Changing keys (theft runbook, or a lost machine)

- **New Pi key:** in a maintenance window, `sudo rm /etc/credstore.encrypted/network.wireguard.private.wg0`, then `gen-wg-key.sh`, then paste the new public key into the desktop tunnel's `[Peer]`, then `sudo networkctl reload`.
- **New desktop key:** in the WireGuard app, remove the `atlas` tunnel and create it again (step 2). Then put the new public key in `/etc/atlas/wireguard-desktop.pub` and rerun `provision-pi.sh`.

## Tested in the ARM64 VM (2026-11-23)

A disposable VM played the Pi, and a network namespace with its own WireGuard interface played the desktop. The tests used the real `lib.sh`, `gen-wg-key.sh`, `wg0.network`, the NetworkManager drop-in and `atlas.nft`. All of the following passed:

- `systemd-networkd` (systemd 257) loads the private key from the encrypted credential (`PrivateKey=@network.wireguard.private.wg0`). The key isn't readable in the credential file, or anywhere in networkd's config.
- `FirewallMark=0x5182` is applied. With the firewall loaded before the first handshake, the tunnel comes up.
- `atlas-core` reaches the desktop's 1234 and 8770. `atlas-egress` and `nobody` are dropped, and so is `atlas-core` to any other desktop port.
- Unmarked UDP to the desktop's WireGuard port, from root or `nobody`: none of it arrives.
- **D1 reproduced:** with `FirewallMark=0x1`, the tunnel is silently blocked. Rerunning provisioning restores it.
- NetworkManager installed alongside: without the drop-in it adopts `wg0` ("connected (externally)"); with it, `wg0` is `unmanaged`. The tunnel survives NetworkManager and networkd restarts.

**Not tested:** the Windows side (WireGuard for Windows and `desktop-firewall.ps1`). PowerShell isn't on the Mac; its first run is yours.
