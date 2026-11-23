# Runbook: bring up the Pi (Phase 0, before provisioning)

This takes a new Raspberry Pi 5 from a blank card to the point where [provisioning.md](provisioning.md) can run. It sets up the card layout from PLAN §2.7 (R-STO): a small OS partition that later becomes read-only, plus a separate writable data partition for `/var/lib/atlas`. It also cuts background writes to the card, locks SSH to your key, and records the hardware inventory the plan asks for (A9, §0.4).

Order of the runbooks: **this one** → [provisioning.md](provisioning.md) → [deploy.md](deploy.md) → the read-only root (step 11 here) → [pi-verification.md](pi-verification.md).

Allow about two hours the first time. Do steps 1–7 in one sitting: the Pi has open internet access until provisioning loads the firewall, and you need it for updates and packages.

Facts marked *(source-checked)* were read from the Raspberry Pi OS Trixie packages themselves (`raspberrypi-sys-mods 1:20260914`, `rpi-swap 1.2.4`, `raspi-config 20261026+2`, `cloud-init 25.2-1~bpo13+1+rpt20`), not from forum posts. Recheck them if a later image behaves differently.

## 1. What you need

| Item | Status | Notes |
|---|---|---|
| Raspberry Pi 5, 8 GB, with the Active Cooler fitted | Have | A9 assumes 8 GB; step 7 confirms it. |
| Official 27 W USB-C power supply | Have | R-HW2. A weaker supply causes undervoltage, which step 7 checks for. |
| **2 × high-endurance microSD, 64 GB or larger** | **To buy** | One for the Pi, one spare kept as a golden image (R-STO). Compare the vendors' endurance (TBW) ratings when you buy. |
| Ethernet cable to the same router or switch as the Windows desktop | ? | A5: Wake-on-LAN needs both on one LAN segment. No Wi-Fi: step 8 turns it off. |
| An SD card reader on the Mac | ? | |
| **Raspberry Pi Imager 2.0 or later** on the Mac | | Imager 1.x doesn't apply its settings to Trixie images and fails without telling you. |
| A micro-HDMI cable, a screen and a USB keyboard | Needed later | Console access for provisioning: a wrong LAN range in the firewall locks SSH out. |
| Admin access to your router | | To reserve IP addresses and check that UPnP is off. |

## 2. Decide these before you start

| Value | Suggestion | Notes |
|---|---|---|
| Hostname | `atlas-pi` | |
| Admin user | your own short name, e.g. `admin` | **Not** `atlas-*`: those names are reserved for services. This becomes `ATLAS_ADMIN_USER` in provisioning. |
| Admin password | long and random, kept in your password manager | Only for `sudo` at the console. SSH won't accept passwords. |
| SSH key | a dedicated key: `ssh-keygen -t ed25519 -f ~/.ssh/atlas-pi -C atlas-pi-admin` | Give it a passphrase. Only the `.pub` half ever leaves the Mac. |
| Pi's LAN IP | reserve one in the router (DHCP reservation) after first boot | Goes into the firewall's site file. |
| Mac's LAN IP | reserving one is optional | The firewall allows SSH from any address on the LAN (`LAN_CIDR`), so a changed address doesn't lock you out. Only your key can log in. |

## 3. Flash the card

1. Put the card in the Mac and open Raspberry Pi Imager.
2. **Device:** Raspberry Pi 5. **OS:** Raspberry Pi OS (other) → **Raspberry Pi OS Lite (64-bit)**. ATLAS needs no desktop. **Storage:** the card.
3. In the customisation screens:
   - Hostname `atlas-pi`; username and password from step 2.
   - Wi-Fi: leave empty.
   - Locale and time zone: yours.
   - **SSH: on, public-key authentication only.** Paste the contents of `~/.ssh/atlas-pi.pub`.
   - Raspberry Pi Connect: off. It's a remote-access service, and ATLAS only takes SSH from the Mac.
4. Write the card. When it finishes, **don't eject it yet**.

## 4. Before first boot: stop the card being filled

The card's boot partition shows up on the Mac as `bootfs`.

**Why:** on first boot, Raspberry Pi OS grows the system partition over the whole card. That leaves no room for the data partition, and a mounted ext4 root can't be shrunk again. The growth runs only when the word ` resize` is in `cmdline.txt` *(source-checked: `resize_early` in the initramfs)*. Cloud-init won't grow it either, because Raspberry Pi's cloud-init setup doesn't include the `growpart` module *(source-checked)*.

```sh
cat /Volumes/bootfs/cmdline.txt                            # one line; note it contains " resize"
sed -i '' 's/ resize//' /Volumes/bootfs/cmdline.txt
grep -c resize /Volumes/bootfs/cmdline.txt                 # must print 0
grep -c atlas-pi-admin /Volumes/bootfs/user-data           # your key's comment: must print 1 or more
grep -n ssh_pwauth /Volumes/bootfs/user-data               # if it says true, change it to false
diskutil eject /Volumes/bootfs
```

Side effect: the same flag also gives the card a new random disk ID on first boot *(source-checked: `set_partuuid`)*. Without it, the card keeps the image's disk ID. That's harmless here, because the live and spare cards are never in the Pi at the same time.

## 5. First boot and SSH

1. Card in, Ethernet in, then power. Wait two or three minutes: cloud-init applies the settings on first boot.
2. Find the Pi's address in your router's client list (or try `ping atlas-pi.local`). Reserve that address for the Pi, and reserve the Mac's address too.
3. On the Mac, add this to `~/.ssh/config` with your values:

   ```text
   Host atlas-pi
     HostName 192.0.2.10
     User admin
     IdentityFile ~/.ssh/atlas-pi
     IdentitiesOnly yes
   ```

4. `ssh atlas-pi`. Accept the host key the first time.
5. Check the partitions:

   ```sh
   lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINTS /dev/mmcblk0
   sudo parted /dev/mmcblk0 unit GiB print free
   ```

   Expect `mmcblk0p1` (boot, about 0.5 GiB) and `mmcblk0p2` (root, a few GiB), with most of the card as **free space** after them. If `p2` fills the card, step 4 didn't take: flash again.

## 6. Card layout: grow root, add the data partition

Target layout (R-STO): boot as it is, **root grown to 16 GiB**, and **data on the rest** (about 40 GiB on a 64 GB card, 103 GiB on a 128 GB one).

This comes before the upgrade on purpose. The root partition the image ships with (about 2.4 GiB) is too small for `apt full-upgrade`, which stops with "You don't have enough free space in /var/cache/apt/archives/". 16 GiB leaves room for OS upgrades and the later kiosk browser. The data partition holds everything ATLAS writes: releases, audit log, memory database, models.

Make sure no ATLAS data exists yet (`ls /var/lib/atlas` should fail or show nothing). Then:

```sh
sudo parted /dev/mmcblk0
  (parted) unit GiB
  (parted) print free
  (parted) resizepart 2 16GiB        # answer "Yes" when it warns that the partition is in use
  (parted) mkpart primary ext4 16GiB 100%
  (parted) print
  (parted) quit
sudo resize2fs /dev/mmcblk0p2        # grows the mounted root filesystem online
sudo mkfs.ext4 -L atlas-data /dev/mmcblk0p3
```

`mkfs.ext4` enables metadata checksums by default, which Raspberry Pi's resilient-file-system white paper recommends. Then mount it at `/var/lib/atlas` with the plan's options:

```sh
uuid=$(sudo blkid -s UUID -o value /dev/mmcblk0p3)
echo "UUID=$uuid  /var/lib/atlas  ext4  defaults,noatime,commit=60,errors=remount-ro  0  2" | sudo tee -a /etc/fstab
sudo mkdir -p /var/lib/atlas
sudo systemctl daemon-reload
sudo mount /var/lib/atlas
findmnt /var/lib/atlas                # SOURCE /dev/mmcblk0p3, FSTYPE ext4, rw,noatime,commit=60
sudo reboot                           # then check findmnt again: it must mount by itself
```

The mount options:
- `commit=60` batches writes that aren't explicitly synced. The audit log fsyncs every record, so it's not affected (§2.12).
- `errors=remount-ro` stops writes to a filesystem the kernel already knows is damaged.
- Journaling mode and write barriers stay at their safe defaults. Don't set `barrier=0` or `data=writeback`.

## 7. Update, install, inventory

Update while the Pi still has normal internet access. After provisioning, root can't reach the internet (PLAN §5.2).

```sh
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y nftables python3-venv openssl sudo curl overlayroot ethtool
sudo reboot
```

`overlayroot` isn't used until step 11, but it has to be installed now, while apt can still download.

Copy the repository's `deploy/` directory to the Pi (it holds no secrets), then record the inventory (verifies A9 and §0.4):

```sh
# on the Mac, from the repository
scp -rq deploy atlas-pi:atlas-deploy

# on the Pi
sudo ~/atlas-deploy/provisioning/base-os.sh inventory | tee ~/inventory.md

# on the Mac
scp atlas-pi:inventory.md "docs/inventory/pi-$(date +%F).md"
```

**The repository is public.** The inventory leaves out MAC addresses, serial numbers, IP addresses and UUIDs, and a test checks that it reads none of them. Don't add them by hand.

**Expect:** `Raspberry Pi 5 Model B`, about 8 GB of RAM (7.9 GiB shown), Debian 13 (trixie), `arm64`, Python 3.13, and `throttled=0x0` (no undervoltage or throttling so far). If any of these differ, stop: the dependency locks are built for exactly this (CLAUDE.md).

## 8. Fewer card writes, wired only, key-only SSH

```sh
sudo ~/atlas-deploy/provisioning/base-os.sh apply --dry-run    # shows what it would change
sudo ~/atlas-deploy/provisioning/base-os.sh apply
```

It takes the admin user from `sudo` (or from `ATLAS_ADMIN_USER`). It's safe to run again: it only rewrites what differs, because every write wears the card. It changes:

| What | File | Why |
|---|---|---|
| Journal on the data partition, capped | `/etc/systemd/journald.conf.d/50-atlas-journal.conf` (`Storage=persistent`, 256 MB, 30 days, synced every 5 min); creates `/var/lib/atlas/logs/journal` and adds an fstab bind mount of it at `/var/log/journal` | PLAN §2.7. Logs survive reboots (`journalctl -b -1` shows the previous boot) without writing to the read-only root. If the data partition is missing at boot, `nofail` lets the Pi boot with the journal in RAM. |
| Swap in compressed RAM only | `/etc/rpi/swap.conf.d/50-atlas.conf` (`Mechanism=zram`, 2 GiB), and removes an unused `/var/swap` | The default (`auto`, currently `zram+file`) writes idle pages to `/var/swap` on the card *(source-checked: swap.conf(5))*. |
| No Wi-Fi or Bluetooth | appends `dtoverlay=disable-wifi` and `disable-bt` to `/boot/firmware/config.txt`, under an `[all]` line | ATLAS is wired; fewer radios, less attack surface. If the file ends in a model section such as `[cm5]`, lines appended after it wouldn't apply to a Pi 5; `[all]` makes sure they do. |
| `/tmp` in RAM | adds a tmpfs line to `/etc/fstab`, only if `/tmp` isn't tmpfs already | Debian 13 usually has it already. |
| sudo password cached per terminal | `/etc/sudoers.d/020_atlas-tty` (`Defaults timestamp_type=tty`), checked with `visudo` before it's installed | Pi OS ships `010_global-tty` (`timestamp_type=global`): for 15 minutes after you type the sudo password, *every* session of the admin gets root without it, including an SSH login with a stolen key. `020_` is read after `010_`, and the last value wins. |
| `systemd-remount-fs` skipped while the overlay is on | `/etc/systemd/system/systemd-remount-fs.service.d/50-atlas-overlay.conf` (`ConditionPathIsMountPoint=!/media/root-ro`) | Known issue P1: under the overlay (step 11) it fails at every boot, because the kernel refuses to remount an overlay with changed options. It still runs in a maintenance window. |
| Boot partition root-only, cloud-init off | `/etc/fstab`: `fmask=0077,dmask=0077` on the `/boot/firmware` line (takes effect after the reboot); `/etc/cloud/cloud-init.disabled`; removes every line holding a password hash from `user-data` and the rest of the seed on `/boot/firmware`, from `/var/lib/cloud` and from cloud-init's logs; deletes macOS metadata (`.Spotlight-V100`, `.fseventsd`, `._*`) from `/boot/firmware` | Known issue P3: Imager puts your password hash (the one `sudo` checks) in `user-data`, and the FAT partition is readable by every user by default, sandboxed services included. Cloud-init only needs the seed at first boot. Flashing from a Mac leaves a Spotlight index on the card that had indexed `user-data`. |
| SSH: key only, admin only | `/etc/ssh/sshd_config.d/10-atlas.conf`: no passwords or keyboard-interactive, no root, `AllowUsers <admin>` | Named `10-…` because sshd keeps the *first* value it reads, so it wins over cloud-init's `50-cloud-init.conf`. |

It refuses if the admin has no key in `~/.ssh/authorized_keys` (key-only SSH would lock you out), if the read-only overlay is on (the changes would vanish), if `/var/lib/atlas` isn't mounted (the journal would land on the root filesystem), if fstab already has a different `/var/log/journal` line, or if `sshd -t` rejects the result (it puts the file back and doesn't reload SSH), or if `visudo` rejects the sudoers drop-in.

**Before closing your current session**, check from a second terminal on the Mac:

```sh
ssh -o ControlPath=none atlas-pi true && echo key-login-ok
ssh -o ControlPath=none -o PubkeyAuthentication=no -o PreferredAuthentications=password atlas-pi true   # must be refused
```

`ControlPath=none` makes each check open a new connection. Without it, an ssh setup that shares connections (`ControlMaster`) reuses your open session, and the second line "succeeds" without testing anything. That happened in the VM test.

Then `sudo reboot`, and run `apply` once more after it: a `/var/swap` that was in use before the reboot is removed then.

## 9. Check the base setup

```sh
sudo ~/atlas-deploy/provisioning/base-os.sh check
```

One `PASS`/`FAIL` line per expectation, exit status 1 if anything fails. It covers steps 5–8: platform (arm64, trixie, Python 3.13, 8 GB, `throttled=0x0`), the packages from step 7, the data partition (ext4 on its own partition, `rw,noatime,commit=60,errors=remount-ro`, in fstab by UUID), zram-only swap, the journal (persistent, 256 MB, 30 days, bind-mounted from the data partition), tmpfs `/tmp`, no Wi-Fi or Bluetooth, sshd's effective settings (`sshd -T`), sudo's effective settings (`timestamp_type=tty`; no `NOPASSWD` or `!authenticate` for the admin except provisioning's `atlas-release` rule), the boot units (`systemd-remount-fs` not failed, its drop-in present, the clock synchronized), the boot partition (root-only in fstab and as mounted, `nobody` can't read it, cloud-init disabled, no copy of a password hash in the seed or cloud-init's files, no macOS metadata), and the overlay setting in `cmdline.txt`. Until step 11 the overlay shows as a `NOTE`, not a failure. Run it again after step 11, and after every maintenance window.

## 10. Provision and deploy

Continue with [provisioning.md](provisioning.md). The root filesystem is still writable at this point, which the provisioning script needs.
- Use the console for the run that loads the firewall.
- Afterwards, `deploy.sh` installs a release ([deploy.md](deploy.md)).
- `gen-audit-keys.sh` creates the audit keys. Record the off-box anchors (G1) as the audit runbook says.

## 11. Make the root filesystem read-only

Do this last, once provisioning and the first deploy work.

**Don't use raspi-config's overlay option for this** *(source-checked)*:
- It writes `overlayroot=tmpfs`. With that setting, overlayroot also overlays every other mount, *including `/var/lib/atlas`*, so ATLAS's writes would go to RAM and disappear on reboot. The audit log would silently lose its history.
- Its "disable" command only removes the exact text `overlayroot=tmpfs ` (with a space after it). It won't remove the `recurse=0` form used below.

Enable it by hand:

```sh
lsinitramfs /boot/firmware/initramfs_2712 | grep -c overlayroot   # must be > 0 (the package added its hook)
sudo sed -i '1s/^/overlayroot=tmpfs:recurse=0 /' /boot/firmware/cmdline.txt
cat /boot/firmware/cmdline.txt                                     # starts with overlayroot=tmpfs:recurse=0
sudo reboot
```

Check that it does what we want:

```sh
findmnt /                                   # FSTYPE overlay
findmnt /var/lib/atlas                      # still /dev/mmcblk0p3, ext4, rw: NOT overlay
sudo touch /var/lib/atlas/.persist-test /etc/.overlay-test
sudo reboot
ls /var/lib/atlas/.persist-test             # must exist  (data partition persists)
ls /etc/.overlay-test                       # must NOT exist (root writes went to RAM)
sudo rm /var/lib/atlas/.persist-test
```

If `/var/lib/atlas` shows up as `overlay`, the `recurse=0` didn't apply. Turn the overlay off (below) before running ATLAS, and record what you saw in pi-verification.md.

Optionally make `/boot/firmware` read-only too (the Raspberry Pi white paper recommends this, because FAT has no journal): add `,ro` after `defaults` on its line in `/etc/fstab`.

**Maintenance window (OS updates, re-provisioning):** turn the overlay off, do the work, turn it back on.

```sh
sudo mount -o remount,rw /boot/firmware
sudo sed -i '1s/^overlayroot=tmpfs:recurse=0 //' /boot/firmware/cmdline.txt
sudo reboot
# ... apt, provision-pi.sh, etc. (root has no internet path after provisioning; see provisioning.md "Maintenance")
sudo sed -i '1s/^/overlayroot=tmpfs:recurse=0 /' /boot/firmware/cmdline.txt && sudo reboot
```

Deploys don't need this: releases live on the data partition (rev 2.6).

## What comes after

- **Golden image and spare card** (Phase 0 deliverable): image the card once it's set up but before real secrets are stored, write it to the spare, and test that the spare boots (PT-9). That procedure comes with the backup job.
- **B10:** measure 24 h of card writes (`/proc/diskstats`) against the 100 MB/day budget (§3.5).
- **FI-3:** 20 power cuts under `fi3-audit-load.sh`.

## Results

Record what actually happened on the hardware here: anything that didn't match the expected output above.

| Date | Step | Result |
|---|---|---|
| 2026-11-20 | 3 | Raspberry Pi OS Lite 64-bit, image dated 2026-10-06, on a 128 GB high-endurance card (119.4 GiB usable). |
| 2026-11-20 | 4 | The stock `config.txt` ends in an `[all]` section, so `base-os.sh`'s own `[all]` line is redundant but harmless. |
| 2026-11-20 | 5 | As expected: trixie, aarch64, Python 3.13.5, `p1` 512 MiB vfat, `p2` 2.4 GiB ext4, about 117 GiB free after it (removing ` resize` worked). cloud-init finished. |
| 2026-11-20 | 5 | `sudo -n` seemed to work without a password from a second SSH session. Cause: Pi OS's `timestamp_type=global` shared the password typed in the first session. No `NOPASSWD` rule exists (user-data has `sudo: null`). `base-os.sh` now sets `timestamp_type=tty`, and `check` verifies it. |
| 2026-11-20 | 6 | The image's 2.4 GiB root filled up during `apt upgrade` ("not enough free space in /var/cache/apt/archives"), before anything was half-installed. Steps 6 and 7 were swapped so the card layout comes first. Root grown online to 16 GiB (`resizepart` + `resize2fs`, no reboot needed); data partition 103.4 GiB, mounted by itself after the reboot. |
| 2026-11-20 | 7 | `full-upgrade`: 45 upgraded, kernel 6.18.50 to 6.18.60. With root no longer full, rpi-swap created a 2 GiB `/var/swap` at the next boot; step 8 removes it. `ethtool` is in `/usr/sbin`, so `inventory` without sudo leaves `ethernet` blank. Inventory: [pi-2026-11-21.md](../inventory/pi-2026-11-21.md). |
| 2026-11-20 | 8 | `apply` as expected. From the Mac, key login works; password and root logins get `Permission denied (publickey)`. rpi-swap's default mode had attached `/var/swap` to zram through a loop device, not as swap, so `apply` didn't see it in use and deleted it. Harmless (the loop device kept it until the reboot), but `apply` now also checks loop devices. |
| 2026-11-20 | 9 | `check`: all passed after the reboot (overlay `NOTE` until step 11). The journal is on `/dev/mmcblk0p3[/logs/journal]`, 16 MB at first. Earlier boots were in RAM, so `journalctl -b -1` works from the next reboot on. |
| 2026-11-20 | 10 | The router serves no NTP, so the Pi syncs to `time.google.com`'s four addresses by IP (`NTP_SERVERS`, PLAN §5.7). Root commands ran in a `tmux` session on the Pi where the owner had typed the sudo password; the owner watched from the console. Before the firewall loaded, `nft -c` checked the site file. The firewall load froze the SSH sessions already open (see provisioning.md); new ones worked. Checks after it: other users get no DNS or internet, root gets DNS but no HTTPS, the clock syncs. `gen-audit-keys.sh` created the machine key `/var/lib/systemd/credential.secret` on the root filesystem (it must exist before the overlay, step 11). First deploy OK; PT-1 passed; `verify` OK (7 records, 1 checkpoint); the anchors are stored off the Pi. |
| 2026-11-20 | 11 | The initramfs had the `overlayroot` script and `overlay.ko`. After `overlayroot=tmpfs:recurse=0`: `/` is `overlay`, `/var/lib/atlas` stays ext4 on `mmcblk0p3`, the journal mount survives, and `journalctl --list-boots` shows earlier boots. Test files: the one in `/var/lib/atlas` survived a reboot, the one in `/etc` didn't. `systemd-remount-fs.service` fails at every boot with the overlay on (known issue P1). The first time server didn't answer after boot; the next one did, 0.2 s off. Clock after a real power cut: known issue P2. |
| 2026-11-21 | 11 (maintenance window) | Installed the P1 and P2 fixes: overlay argument removed and rebooted (root `rw` on `mmcblk0p2`), new `deploy/` copied, `base-os.sh apply` and `provision-pi.sh` dry runs then real runs (changes: the remount-fs drop-in, the 14 `atlas-*.service` files, the time-wait-sync drop-in and enable; the firewall didn't reload, so no SSH freeze), overlay argument put back byte-for-byte, rebooted. With the overlay on: `systemctl --failed` empty, `systemd-remount-fs` skipped (`ConditionResult=no`). `systemd-time-wait-sync` waited 31 s for the first answer from 216.239.35.0 (34.9 s after boot); `time-sync.target` and `atlas-egress` started in the same second, after it. |
| 2026-11-23 | 11 (maintenance window) | Installed the P3 fix and SSH from the whole LAN (services.md 1.3.0): overlay argument removed and rebooted, `deploy/` from main `199bd94` copied (40 files, hashes match), `base-os.sh apply` and `provision-pi.sh` dry runs then real runs. `apply` changed: the `/boot/firmware` fstab line (`fmask=0077,dmask=0077`), `cloud-init.disabled`, password-hash lines removed from `user-data`, `cloud-init.log` and three files under `/var/lib/cloud/instances/`, the binary `obj.pkl` deleted, `.Spotlight-V100` and `._cmdline.txt` deleted. `provision-pi.sh` changed only the ruleset (`ip saddr 192.168.0.0/24 tcp dport 22 accept`; the site file keeps its now unused `ADMIN_IP` define, which nftables accepts). A new SSH connection worked after the reload. Overlay argument put back byte-for-byte, rebooted: `/boot/firmware` mounted `fmask=0077,dmask=0077` (`joe` gets `Permission denied`), `systemctl --failed` empty, `base-os.sh check` all 47 passed. Deployed `20261123T145505Z-199bd946fe93` (audit actor pinning); `verify` `UNANCHORED`, exit 0: 36 records, 12 checkpoints, same genesis, and `checkpoint.pub` matches the off-box SHA-256. SSH from an address other than the Mac's wasn't tried; the rule was checked in the loaded ruleset only. |
