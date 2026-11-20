# Runbook: the ARM64 Debian VM (stand-in for the Pi)

Source: PLAN.md §7.0, Development environment. Config: `deploy/vm/atlas-vm.yaml`, `deploy/vm/start.sh`.

The VM runs Debian 13 (trixie) on `aarch64` under Lima with Apple's Virtualization.framework. It has the Pi's CPU architecture and OS base (current 64-bit Raspberry Pi OS is trixie-based) and Python 3.13. It has a real kernel and systemd, so nftables, `systemd-creds`, `chattr +a` and unit sandboxing can be tested. It is **not** the Pi: there's no microSD, no read-only overlay, and a different kernel build. Benchmarks never count here.

## Create or start it

```sh
brew install lima           # once
deploy/vm/start.sh          # first run creates the "atlas" VM (downloads the pinned Debian image), later runs start it
limactl shell atlas         # a shell inside
limactl stop atlas          # stop; `limactl delete atlas` throws it away
```

- **Mounts:** only this repository, **read-only**, at `/mnt/atlas`. Lima's default would mount your whole home directory, including `~/.ssh`, and `start.sh` replaces that.
- **Image:** pinned by URL and SHA-512 in `atlas-vm.yaml`; Lima verifies the digest. Update the pin in its own commit.
- **Size:** 4 CPUs, 4 GiB RAM, 20 GiB disk. The Pi has 8 GB RAM.
- **Packages installed at first boot:** python3 (3.13), python3-venv, git, nftables, e2fsprogs, shellcheck, dnsutils, netcat-openbsd.
- **No secrets in the VM**, as on the Mac. It's a development machine (CLAUDE.md).

## Run the backend checks on Python 3.13

Work on a clone inside the VM, never on the read-only mount:

```sh
limactl shell atlas -- bash -lc '
  git config --global --add safe.directory /mnt/atlas
  rm -rf ~/atlas && git clone -q /mnt/atlas ~/atlas && cd ~/atlas/backend
  python3 -m venv .venv && .venv/bin/pip install -q --require-hashes -r requirements-dev.lock
  .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest -q'
```

The clone takes the **committed** state of the branch checked out on the Mac. To test uncommitted edits, copy single files in with `limactl copy <file> atlas:atlas/<file>`. In the VM, `test_shellcheck_if_available` runs instead of skipping.

## Cross-check the Pi runtime locks

`scripts/relock-pi.sh` downloads the manylinux aarch64 wheels and records their hashes. Re-running it inside the VM must reproduce the committed lock exactly:

```sh
limactl shell atlas -- bash -lc 'cd ~/atlas/backend && cp requirements-audit.lock /tmp/a &&
  scripts/relock-pi.sh audit cffi==2.1.1 cryptography==47.0.0 pycparser==3.12 && diff /tmp/a requirements-audit.lock && echo identical'
```

## nftables syntax check

The skuid rules need the users to exist. Create them as system users in the VM only:

```sh
limactl copy deploy/nftables/atlas.nft atlas:/tmp/atlas.nft
limactl copy deploy/nftables/site.nft.example atlas:/tmp/site.nft
limactl shell atlas -- bash -lc '
  for u in $(grep -oE "\"atlas-[a-z]+\"" /tmp/atlas.nft | tr -d "\"" | sort -u); do
    id -u $u >/dev/null 2>&1 || sudo useradd --system --no-create-home --shell /usr/sbin/nologin $u; done
  sudo mkdir -p /etc/atlas && sudo cp /tmp/site.nft /etc/atlas/nftables-site.nft && sudo nft -c -f /tmp/atlas.nft && echo OK'
```

`nft -c` checks syntax and names only. **Loading** the ruleset (`nft -f`) applies default-deny output to the whole VM, which can cut Lima's own SSH and port forwarding. Snapshot or recreate the VM before doing that, and keep `limactl shell` open in another terminal.

## Results so far

The running list is in [pi-verification.md](pi-verification.md) and [known-issues.md](../known-issues.md).

- **2026-11-20:** Debian 13.7, Python 3.13.5, nftables 1.1.3, systemd 257.
  - Backend: 1276 passed; 1 fails (shellcheck, issue S1); 1 skipped (HUD not built in the VM).
  - mypy on Linux found one platform-narrowing error in `audit/peercred.py`, now fixed.
  - The Pi locks (audit, core, executor) reproduce byte-for-byte.
  - `nft -c` on `atlas.nft` with the example site file is OK.
- **2026-11-20, known-issues fix branch (N1–N5, G1, D2, D3, S1):** ruff, format and mypy clean; backend 1320 passed, 1 skipped (HUD not built); `shellcheck -x` clean, so `test_shellcheck_if_available` passes. The GNU-tar `atlas-release install` tests (N5) run here and skip on the Mac. `nft -c` of a `/tmp` copy of the ruleset with the updated example site file is OK. Note: a worktree's `.git` points at a Mac path, so to test a branch from a worktree, ship `git archive HEAD` into the VM instead of cloning the mount.
