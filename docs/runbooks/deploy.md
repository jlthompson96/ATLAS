# Runbook: deploy and roll back (Mac to Pi over SSH)

Scope: `deploy/provisioning/deploy.sh` (on the Mac) and `atlas-release` (on the Pi). Layout and rules: contracts/services.md section 6. The Pi's root can stay read-only: releases live on the data partition.

## Deploy

```sh
export PI_HOST=atlas-pi.example.org   # your Pi's name; the placeholder is refused for a real run
export PI_USER=admin                  # the admin account from provisioning
deploy/provisioning/deploy.sh --dry-run   # builds the tarball locally and prints the plan
deploy/provisioning/deploy.sh             # build, upload, install, activate
```

What happens:

1. `git archive HEAD` of `backend/atlas`, `backend/pyproject.toml`, `contracts/`, the per-service locks and `services.conf`. Only committed files ship (no tests, no virtualenvs, no secrets); uncommitted changes are reported and not deployed.
2. For each `backend/requirements-<service>.lock`, the Mac downloads the matching `aarch64` wheels with `pip download --require-hashes --only-binary=:all:`. A wheel whose SHA-256 differs from the lock fails the build. The Pi never uses the network to install anything.
3. The tarball goes to `/var/lib/atlas/incoming/<version>.tar` (`<version>` = UTC timestamp plus short commit). `sudo atlas-release install <version> <sha256>` verifies the hash and refuses anything but the top-level entries `backend/ contracts/ deploy/ wheels/` (so no pre-built `venv/`), links, special files and unsafe paths. It extracts into a mode-0700 directory and takes the list of services from the root-owned `/etc/atlas/services.conf`, never from the tarball. Each dependency lock may contain only `name==version` and `--hash=sha256:` lines. Each venv is built by an unprivileged throwaway user (`systemd-run -p DynamicUser=yes -p PrivateNetwork=yes`, `python3 -I`, `pip --isolated --require-hashes --no-index --only-binary`), copied in, chowned to root, stripped of setuid/setgid bits and made read-only for services; then the release moves to `/var/lib/atlas/releases/<version>/`. Root never runs Python or pip over anything that came from the tarball.
4. `sudo atlas-release activate <version>` records the old release as `previous`, switches `current` atomically, restarts the enabled units and waits up to 30 s for them to be active. If they are not, it switches back and exits non-zero. It then prunes to the newest 3 releases (never the current or previous one).

Each deploy writes a few MB to the microSD, counted against the 100 MB/day budget (PLAN 3.5); a few deploys a day fit, B10 measures it.

## Roll back

```sh
deploy/provisioning/deploy.sh rollback   # switch to `previous`, restart, health check
deploy/provisioning/deploy.sh list       # releases, marking current and previous
```

On the Pi directly: `sudo atlas-release rollback`.

## When something goes wrong

- *"python3 is not 3.13"*: the locks are CPython 3.13 only; install it or re-lock (`backend/scripts/relock-pi.sh`).
- *Health check failed, rolled back*: `journalctl -u atlas-audit -u atlas-egress -n 50` (journald is volatile; errors are also forwarded to audit).
- *wheel hash mismatch while building*: the lock and PyPI disagree; do not bypass, investigate (supply chain, PLAN T6).
- *A deploy changes a lock*: dependency changes are their own reviewed change (CLAUDE.md); regenerate with `backend/scripts/relock.sh` and `backend/scripts/relock-pi.sh audit <pins>`, and compare the hashes inside the ARM64 VM.

## Security notes

- `atlas-release` is the only thing the admin account may run as root. Its arguments are validated with strict patterns, because they arrive through sudo.
- The tarball hash is passed by the same admin that uploaded it, so it protects against a corrupt transfer, not a compromised admin device. The Mac is part of the trust boundary (PLAN 7.0).
