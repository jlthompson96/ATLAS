"""Regression tests for the open deploy/ issues in docs/known-issues.md (S1, N5, D2, D3). Each failed before its fix.

The release helper and the provisioning firewall step are exercised for real in bash, as in test_review_deploy.py.
Root-only commands (chown, systemd-run, nft, systemctl) are replaced by shell functions in the snippet, so the logic runs
unprivileged on throwaway paths; the real commands are checked in the ARM64 VM (docs/runbooks/pi-verification.md).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_deploy_structure import DEPLOY
from tests.test_review_deploy import GOOD, REPO, bash, make_tar

LIB = DEPLOY / "provisioning" / "lib.sh"
PROVISION = DEPLOY / "provisioning" / "provision-pi.sh"

VERSIONS = ["20261101T000000Z-aaaaaaa", "20261102T000000Z-bbbbbbb", "20261103T000000Z-ccccccc"]


# --- S1: shellcheck findings in the root-run release helper ---------------------------------------------------------


def odd_release_dir(tmp_path: Path) -> Path:
    releases = tmp_path / "releases"
    releases.mkdir()
    for version in [*VERSIONS, "20261104T000000Z-ddddddd", "20261105T000000Z-eeeeeee"]:
        (releases / version).mkdir()
    (releases / "evil\n20991231T000000Z-fffffff").mkdir()  # a newline in a name hides a "version" from `ls | grep`
    (releases / "-n").mkdir()
    (releases / "with space 20261106T000000Z-0000000").mkdir()
    return releases


def test_s1_release_list_is_read_with_a_glob_not_ls_grep(tmp_path: Path) -> None:
    odd_release_dir(tmp_path)
    result = bash(f'BASE="{tmp_path}"; cmd_list')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "20261105T000000Z-eeeeeee",
        "20261104T000000Z-ddddddd",
        *reversed(VERSIONS),
    ]


def test_s1_prune_keeps_the_newest_real_releases_whatever_else_is_in_the_directory(tmp_path: Path) -> None:
    releases = odd_release_dir(tmp_path)
    result = bash(f'BASE="{tmp_path}"; cmd_prune')
    assert result.returncode == 0, result.stderr
    left = sorted(p.name for p in releases.iterdir())
    assert "20261105T000000Z-eeeeeee" in left and "20261104T000000Z-ddddddd" in left
    assert VERSIONS[2] in left  # the third newest real release survives; the hidden "2099" name must not count
    assert VERSIONS[0] not in left and VERSIONS[1] not in left
    assert "evil\n20991231T000000Z-fffffff" in left and "-n" in left  # never touched: not release names


# --- N5: atlas-release install reads one root-owned copy, re-checks the tree, and cleans up ---------------------------


def _gnu_tar() -> bool:
    tar = shutil.which("tar")
    if tar is None:
        return False
    out = subprocess.run([tar, "--version"], capture_output=True, text=True, check=False).stdout  # noqa: S603
    return "GNU tar" in out


needs_gnu_tar = pytest.mark.skipif(
    not _gnu_tar(), reason="atlas-release extracts with GNU tar options (--no-overwrite-dir): runs in the ARM64 VM"
)
V = "20261120T120000Z-abcdef0"
LOCK = f"cffi==2.1.1 \\\n    --hash=sha256:{'a' * 64}\n".encode()
CLEAN: dict[str, Any] = {k: v for k, v in GOOD.items() if k != "./"}


def stage(tmp_path: Path, members: dict[str, Any]) -> tuple[Path, str]:
    """An admin upload in incoming/, and its sha256 as deploy.sh computes it."""
    base = tmp_path / "base"
    (base / "incoming").mkdir(parents=True)
    (base / "releases").mkdir()
    (tmp_path / "scratch").mkdir()
    (tmp_path / "services.conf").write_text("atlas-audit yes\n")
    tarball = make_tar(base / "incoming" / f"{V}.tar", members)
    return base, hashlib.sha256(tarball.read_bytes()).hexdigest()


def fake_python(tmp_path: Path) -> Path:
    """Stands in for /usr/bin/python3: reports 3.13.5, and does nothing for the 3.13 check, venv or compileall."""
    path = tmp_path / "python3"
    path.write_text('#!/bin/sh\ncase "$*" in *platform*) echo 3.13.5 ;; esac\n')
    path.chmod(0o755)
    return path


def install(tmp_path: Path, sha: str, extra: str = "") -> subprocess.CompletedProcess[str]:
    base = tmp_path / "base"
    return bash(
        f"""
BASE="{base}"; CONF="{tmp_path / "services.conf"}"
SCRATCH="{tmp_path / "scratch"}"; SCRATCH_MARGIN_KB=1; PY3="{fake_python(tmp_path)}"
chown() {{ :; }}    # an unprivileged run
{extra}
cmd_install "{V}" "{sha}"
"""
    )


@needs_gnu_tar
def test_n5_a_tarball_swapped_after_the_checksum_is_never_extracted(tmp_path: Path) -> None:
    base, sha = stage(tmp_path, CLEAN)
    evil = make_tar(tmp_path / "evil.tar", {**CLEAN, "backend/atlas/evil.py": b"import os  # runs as a service\n"})
    # The admin swaps incoming/<v>.tar right after the checksum passed (here: the moment the entries are listed).
    swap = f"""
eval "orig_$(declare -f check_tar_entries)"
check_tar_entries() {{ cp "{evil}" "$BASE/incoming/{V}.tar"; orig_check_tar_entries "$@"; }}
"""
    result = install(tmp_path, sha, swap)
    assert result.returncode == 0, result.stderr
    release = base / "releases" / V
    assert (release / "backend" / "atlas" / "x.py").is_file()
    assert not (release / "backend" / "atlas" / "evil.py").exists()  # only the checksummed copy was unpacked


@needs_gnu_tar
def test_n5_the_allowlist_is_checked_again_on_the_extracted_tree(tmp_path: Path) -> None:
    base, sha = stage(tmp_path, {**CLEAN, "venv/atlas-audit/lib/python3.13/site-packages/x.pth": b"import os\n"})
    # Even if the listing check were fooled (tar quirks between listing and extracting), the tree is re-checked.
    result = install(tmp_path, sha, "check_tar_entries() { :; }")
    assert result.returncode != 0
    assert "after extraction" in result.stderr
    assert list((base / "releases").iterdir()) == []


@needs_gnu_tar
@pytest.mark.parametrize("failure", ["checksum", "entries", "tree", "build"])
def test_n5_every_failure_path_cleans_up(tmp_path: Path, failure: str) -> None:
    members = {**CLEAN, "backend/requirements-audit.lock": LOCK}
    extra = ""
    if failure in ("entries", "tree"):
        members["scripts/run.sh"] = b"#!/bin/sh\n"
    if failure == "tree":
        extra = "check_tar_entries() { :; }"
    if failure == "build":
        extra = "systemd-run() { return 1; }"  # the unprivileged venv build fails
    base, sha = stage(tmp_path, members)
    result = install(tmp_path, "0" * 64 if failure == "checksum" else sha, extra)
    assert result.returncode != 0
    assert list((base / "releases").iterdir()) == []  # no .inst.*, no partial tree on the card
    assert list((tmp_path / "scratch").iterdir()) == []  # nothing left in RAM either: no work dir, no build inputs
    assert (base / "incoming" / f"{V}.tar").is_file()  # the upload stays for a retry
    assert not (tmp_path / "build" / "atlas-audit").exists()


@needs_gnu_tar
def test_n5_a_good_release_installs_and_leaves_only_the_release(tmp_path: Path) -> None:
    base, sha = stage(tmp_path, CLEAN)
    result = install(tmp_path, sha)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in (base / "releases").iterdir()) == [V]
    assert not (base / "incoming" / f"{V}.tar").exists()


def test_n5_the_install_reads_only_the_copy() -> None:
    text = (Path(__file__).resolve().parents[2] / "deploy" / "provisioning" / "atlas-release.sh").read_text()
    body = text.split("cmd_install() {", 1)[1].split("\n}\n", 1)[0]
    assert 'tarball="$WORK/release.tar"' in body
    after_copy = body.split('>"$tarball"', 1)[1]
    assert "$incoming" not in after_copy.split('rm -f "$incoming"')[0]  # nothing reads incoming/ after the copy
    assert "trap cleanup_install EXIT" in body


# --- D2 / D3: the provisioning firewall step ---------------------------------------------------------------------------

EDITED_SITE_VALUES = {  # obviously fake, but no longer the example file and no RFC 3849 prefix
    "192.0.2.1 ": "198.51.100.1 ",
    "2001:db8:1234:5600::/64": "fd12:3456:789a:1::/64",
}


def firewall_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """A copy of deploy/nftables (SRC) and an /etc/atlas stand-in (ETC) holding an owner-edited site file."""
    src, etc = tmp_path / "src", tmp_path / "etc"
    src.mkdir()
    etc.mkdir()
    for name in ("atlas.nft", "site.nft.example"):
        shutil.copy(DEPLOY / "nftables" / name, src / name)
    site = (src / "site.nft.example").read_text()
    for old, new in EDITED_SITE_VALUES.items():
        site = site.replace(old, new)
    (etc / "nftables-site.nft").write_text(site)
    return src, etc


def firewall(
    tmp_path: Path,
    src: Path,
    etc: Path,
    *,
    resolved_active: bool = False,
    resolv_conf: str = "nameserver 198.51.100.1\n",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run lib.sh's install_firewall for real, as root would, except that nft and systemctl are recorded, not run, and
    install's owner flags are dropped. Returns the result and the recorded nft/systemctl calls.

    ``systemctl is-active`` answers for systemd-resolved from ``resolved_active``; /etc/resolv.conf is a stand-in (V1).
    """
    calls = tmp_path / "calls.log"
    calls.write_text("")
    resolv = tmp_path / "resolv.conf"
    resolv.write_text(resolv_conf)
    snippet = f"""
set -euo pipefail
source "{LIB}"
DRY_RUN=0
install() {{
  local a=()
  while [ $# -gt 0 ]; do case "$1" in -o | -g) shift 2 ;; -D) shift ;; *) a+=("$1"); shift ;; esac; done
  command install "${{a[@]}}"
}}
nft() {{ echo "nft $*" >>"{calls}"; if [ -n "${{NFT_FAIL_ON:-}}" ] && grep -q "$NFT_FAIL_ON" "{etc}/nftables-site.nft"; then return 1; fi; }}
systemctl() {{
  echo "systemctl $*" >>"{calls}"
  if [ "$1" = is-active ]; then [ "$RESOLVED_ACTIVE" = 1 ]; fi
}}
install_firewall "{etc}" "{src}"
"""
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "NFT_FAIL_ON": "BROKEN",
        "RESOLVED_ACTIVE": "1" if resolved_active else "0",
        "RESOLV_CONF": str(resolv),
    }
    result = subprocess.run(["/bin/bash", "-c", snippet], capture_output=True, text=True, check=False, env=env)  # noqa: S603
    return result, calls.read_text().splitlines()


def test_d2_a_site_file_only_change_is_validated_and_reloaded(tmp_path: Path) -> None:
    src, etc = firewall_dirs(tmp_path)
    first, calls = firewall(tmp_path, src, etc)
    assert first.returncode == 0, first.stderr
    assert f"nft -c -f {etc}/atlas.nft.new" in calls and "systemctl enable --now nftables.service" in calls

    again, calls = firewall(tmp_path, src, etc)  # nothing changed: nothing validated anew, nothing reloaded
    assert again.returncode == 0, again.stderr
    assert f"nft -c -f {etc}/atlas.nft.new" not in calls and "systemctl reload nftables.service" not in calls

    before = (etc / "nftables-site.nft").read_text()
    (etc / "nftables-site.nft").write_text(before.replace("198.51.100.1 ", "198.51.100.53 "))  # a new LAN resolver
    edited, calls = firewall(tmp_path, src, etc)  # atlas.nft itself is unchanged
    assert edited.returncode == 0, edited.stderr
    validate, reload_ = calls.index(f"nft -c -f {etc}/atlas.nft.new"), calls.index("systemctl reload nftables.service")
    assert validate < reload_  # validated, then applied
    assert (etc / "nftables-site.nft.prev").read_text() == before  # the last known-good site file, for rollback
    assert "198.51.100.53" in (etc / "nftables-site.nft.applied").read_text()


def test_d2_an_invalid_site_change_is_not_reloaded_and_stays_pending(tmp_path: Path) -> None:
    src, etc = firewall_dirs(tmp_path)
    assert firewall(tmp_path, src, etc)[0].returncode == 0
    good = (etc / "nftables-site.nft").read_text()
    (etc / "nftables-site.nft").write_text(good + "BROKEN\n")
    result, calls = firewall(tmp_path, src, etc)
    assert result.returncode != 0
    assert "systemctl reload nftables.service" not in calls
    assert (etc / "nftables-site.nft.applied").read_text() == good  # retried on the next run
    assert (etc / "atlas.nft").read_text() == (src / "atlas.nft").read_text()  # the installed ruleset is untouched


def test_d3_an_edited_site_file_with_the_ipv6_placeholder_is_refused(tmp_path: Path) -> None:
    src, etc = firewall_dirs(tmp_path)
    site = (etc / "nftables-site.nft").read_text().replace("fd12:3456:789a:1::/64", "2001:db8:1234:5600::/64")
    (etc / "nftables-site.nft").write_text(site)
    result, calls = firewall(tmp_path, src, etc)
    assert result.returncode != 0
    assert "2001:db8::" in result.stderr and "LAN_PREFIX6" in result.stderr
    assert calls == []  # nothing validated, enabled or reloaded
    assert not (etc / "atlas.nft").exists()


def test_d3_the_untouched_example_still_only_postpones_the_firewall(tmp_path: Path) -> None:
    src, etc = firewall_dirs(tmp_path)
    shutil.copy(src / "site.nft.example", etc / "nftables-site.nft")  # first run: the owner has not edited it yet
    result, calls = firewall(tmp_path, src, etc)
    assert result.returncode == 0, result.stderr
    assert "NOT loaded" in result.stderr and calls == []


@pytest.mark.parametrize(
    ("text", "placeholder"),
    [
        ("define LAN_PREFIX6 = 2001:db8:1234:5600::/64\n", True),
        ("define LAN_PREFIX6 = 2001:DB8::/64\n", True),
        ("define LAN_PREFIX6 = 2001:0db8:1::/48\n", True),
        ("# e.g. 2001:db8::/64 in the documentation\ndefine LAN_PREFIX6 = fd12:3456:789a:1::/64\n", False),
        ("define LAN_PREFIX6 = fd12:3456:789a:1::/64   # was 2001:db8:1234:5600::/64\n", False),
        ("define LAN_PREFIX6 = 2001:db9::/64\n", False),
    ],
)
def test_d3_placeholder_detection(tmp_path: Path, text: str, placeholder: bool) -> None:
    path = tmp_path / "site.nft"
    path.write_text(text)
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", f'source "{LIB}"; has_ipv6_placeholder "{path}"'], capture_output=True, check=False
    )
    assert (result.returncode == 0) is placeholder


def test_d3_a_real_run_refuses_the_placeholder_lan_prefix_before_changing_anything(tmp_path: Path) -> None:
    if os.getuid() == 0:
        pytest.skip("running as root: this test must never reach the real provisioning steps")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "id").write_text('#!/bin/sh\nif [ "$1" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi\n')
    (fake / "id").chmod(0o755)  # looks like root to need_root and user_exists, nothing else
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(PROVISION)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        env={"PATH": f"{fake}:/usr/bin:/bin:/usr/sbin:/sbin", "ATLAS_ADMIN_USER": "admin"},
    )
    assert result.returncode != 0
    assert "RFC 3849" in result.stderr and "ATLAS_LAN_PREFIX6" in result.stderr
    assert "== groups and users" not in result.stderr  # refused before the first step


def test_d3_dry_run_only_warns_about_the_placeholder_lan_prefix() -> None:
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(PROVISION), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    assert result.returncode == 0, result.stderr
    assert "ATLAS_LAN_PREFIX6 is the 2001:db8:: placeholder" in result.stderr


# --- D1: WireGuard runs under systemd-networkd, where the key is FirewallMark= ----------------------------------------


def test_d1_the_wireguard_mark_is_documented_with_the_networkd_key() -> None:
    files = [*(DEPLOY / "nftables").iterdir(), *(REPO / "docs" / "runbooks").glob("*.md")]
    wrong = [f.name for f in files if f.is_file() and "FwMark = 0x5182" in f.read_text()]
    assert wrong == []  # wg-quick syntax: networkd ignores it, the mark rule never matches, the tunnel is blocked
    for path in (DEPLOY / "nftables" / "site.nft.example", REPO / "docs" / "runbooks" / "provisioning.md"):
        assert "FirewallMark=0x5182" in path.read_text(), path.name


# --- R1: a deploy writes the release to the card once, and reuses unchanged venvs ---------------------------------------

OLD = "20261119T120000Z-0123456"


def active_release(
    tmp_path: Path, base: Path, *, lock: bytes = LOCK, pip: bool = False, version: str = "3.13.5"
) -> Path:
    """An installed, active release with a venv for atlas-audit built from ``lock``."""
    old = base / "releases" / OLD
    (old / "backend").mkdir(parents=True)
    (old / "backend" / "requirements-audit.lock").write_bytes(lock)
    venv = old / "venv" / "atlas-audit"
    (venv / "lib").mkdir(parents=True)
    (venv / "bin").mkdir()
    (venv / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion = {version}\n")
    (venv / "lib" / "cryptography.so").write_bytes(b"x" * 4096)
    if pip:
        (venv / "bin" / "pip").write_text("#!/bin/sh\n")
    write_manifest(venv, old / "venv" / "atlas-audit.sha256")
    (base / "current").symlink_to(f"releases/{OLD}")
    return venv


def write_manifest(venv: Path, manifest: Path) -> None:
    """What atlas-release's venv_manifest writes: sha256sum lines for ./<path>, in byte order."""
    files = sorted((p for p in venv.rglob("*") if p.is_file() and not p.is_symlink()), key=lambda p: str(p).encode())
    rels = sorted(("./" + str(p.relative_to(venv)) for p in files), key=str.encode)
    manifest.write_text("".join(f"{hashlib.sha256((venv / r[2:]).read_bytes()).hexdigest()}  {r}\n" for r in rels))


@needs_gnu_tar
def test_r1_an_unchanged_lock_reuses_the_venv_as_hard_links_without_wheels(tmp_path: Path) -> None:
    no_wheels = {k: v for k, v in CLEAN.items() if not k.startswith("wheels/")}
    base, sha = stage(tmp_path, {**no_wheels, "backend/requirements-audit.lock": LOCK})
    old = active_release(tmp_path, base)
    result = install(tmp_path, sha, "systemd-run() { echo BUILT >&2; return 1; }")
    assert result.returncode == 0, result.stderr
    assert "reuse   venv/atlas-audit" in result.stderr and "BUILT" not in result.stderr
    new = base / "releases" / V / "venv" / "atlas-audit" / "lib" / "cryptography.so"
    assert new.stat().st_ino == (old / "lib" / "cryptography.so").stat().st_ino  # a hard link: nothing written
    assert sorted(p.name for p in (base / "releases").iterdir()) == [OLD, V]  # no .inst.* left
    assert list((tmp_path / "scratch").iterdir()) == []


@pytest.mark.parametrize("why", ["lock", "pip", "python", "no-current", "corrupt", "extra-file", "no-manifest"])
@needs_gnu_tar
def test_r1_anything_but_an_exact_match_needs_wheels(tmp_path: Path, why: str) -> None:
    no_wheels = {k: v for k, v in CLEAN.items() if not k.startswith("wheels/")}  # what deploy.sh sends after `locks`
    base, sha = stage(tmp_path, {**no_wheels, "backend/requirements-audit.lock": LOCK})
    if why != "no-current":
        other = LOCK.replace(b"2.1.1", b"2.1.0") if why == "lock" else LOCK
        venv = active_release(
            tmp_path, base, lock=other, pip=why == "pip", version="3.13.4" if why == "python" else "3.13.5"
        )
        if why == "corrupt":  # a flipped byte on the card
            (venv / "lib" / "cryptography.so").write_bytes(b"y" + b"x" * 4095)
        if why == "extra-file":  # something added that the manifest doesn't list
            (venv / "lib" / "evil.pth").write_text("import os\n")
        if why == "no-manifest":  # a venv from before the manifests
            (venv.parent / "atlas-audit.sha256").unlink()
    result = install(tmp_path, sha)
    assert result.returncode != 0
    assert "no wheels for atlas-audit" in result.stderr
    assert [p.name for p in (base / "releases").iterdir() if p.name != OLD] == []


def fake_builder(tmp_path: Path, extra: str = "") -> str:
    """systemd-run that records its arguments and streams back a small venv as a tar on stdout."""
    src = tmp_path / "built"
    return f"""
systemd-run() {{
  printf '%s\\n' "$@" >"{tmp_path / "args"}"
  rm -rf "{src}"; mkdir -p "{src}/lib" "{src}/bin"
  echo built >"{src}/lib/x"; echo "version = 3.13.5" >"{src}/pyvenv.cfg"
  ln -s /usr/bin/python3 "{src}/bin/python3"; ln -s python3 "{src}/bin/python"
  {extra}
  tar -C "{src}" -cf - .
}}
"""


WHEELED = {**CLEAN, "backend/requirements-audit.lock": LOCK, "wheels/audit/a-1-py3-none-any.whl": b"zip"}


def test_every_pip_call_in_the_builder_skips_the_cache() -> None:
    """The builder has no HOME, so pip without --no-cache-dir warns about '/~/.cache/pip' (window 2026-11-25, step 8)."""
    text = (DEPLOY / "provisioning" / "atlas-release.sh").read_text()
    calls = [line for line in text.splitlines() if "-m pip " in line and not line.lstrip().startswith("#")]
    assert len(calls) >= 2  # install and uninstall
    assert all("--no-cache-dir" in line for line in calls), calls


@needs_gnu_tar
def test_r1_venvs_are_built_in_ram_streamed_back_and_lose_pip(tmp_path: Path) -> None:
    base, sha = stage(tmp_path, WHEELED)
    result = install(tmp_path, sha, fake_builder(tmp_path))
    assert result.returncode == 0, result.stderr
    args = (tmp_path / "args").read_text()
    assert "RuntimeDirectory=atlas-build/atlas-audit" in args
    assert "StateDirectory" not in args and "RuntimeDirectoryPreserve" not in args  # nothing left under a dynamic UID
    assert "pip --isolated uninstall --quiet --yes --no-cache-dir pip" in args and "tar -C" in args
    venv = base / "releases" / V / "venv" / "atlas-audit"
    assert (venv / "lib" / "x").read_text() == "built\n"
    assert (venv / "bin" / "python3").readlink() == Path("/usr/bin/python3")
    manifest = (base / "releases" / V / "venv" / "atlas-audit.sha256").read_text()
    assert (
        manifest
        == f"{hashlib.sha256(b'built\n').hexdigest()}  ./lib/x\n{hashlib.sha256(b'version = 3.13.5\n').hexdigest()}  ./pyvenv.cfg\n"
    )
    assert list((tmp_path / "scratch").iterdir()) == []


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('ln -s /etc "{src}/lib/etc"', "unexpected symlink"),
        ('ln "{src}/lib/x" "{src}/lib/y"', "hard links"),
        ('mkfifo "{src}/lib/fifo"', "special files"),
        ('rm "{src}/pyvenv.cfg"', "incomplete"),
    ],
)
@needs_gnu_tar
def test_r1_root_accepts_only_what_a_venv_holds(tmp_path: Path, extra: str, message: str) -> None:
    base, sha = stage(tmp_path, WHEELED)
    result = install(tmp_path, sha, fake_builder(tmp_path, extra.format(src=tmp_path / "built")))
    assert result.returncode != 0 and message in result.stderr, result.stderr
    assert list((base / "releases").iterdir()) == [] and list((tmp_path / "scratch").iterdir()) == []


@needs_gnu_tar
def test_r1_a_reused_venv_carries_its_manifest_and_is_checked(tmp_path: Path) -> None:
    no_wheels = {k: v for k, v in CLEAN.items() if not k.startswith("wheels/")}
    base, sha = stage(tmp_path, {**no_wheels, "backend/requirements-audit.lock": LOCK})
    active_release(tmp_path, base)
    assert install(tmp_path, sha).returncode == 0
    new = base / "releases" / V / "venv"
    assert (new / "atlas-audit.sha256").read_text() == (
        base / "releases" / OLD / "venv" / "atlas-audit.sha256"
    ).read_text()


def test_r1_prune_removes_what_interrupted_installs_left(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    for name in (".inst.abc123", ".work.def456", ".in.ghi789"):
        (releases / name / "venv").mkdir(parents=True)
    (tmp_path / "legacy").mkdir()
    result = bash(f'BASE="{tmp_path}"; LEGACY_BUILD="{tmp_path / "legacy"}"; cmd_prune')
    assert result.returncode == 0, result.stderr
    assert list(releases.iterdir()) == [] and not (tmp_path / "legacy").exists()


def test_r1_only_one_atlas_release_at_a_time(tmp_path: Path) -> None:
    if shutil.which("flock") is None:
        pytest.skip("flock is util-linux: runs in the ARM64 VM")
    lock = tmp_path / "lock"
    holder = subprocess.Popen(["flock", str(lock), "sleep", "5"])  # noqa: S603, S607
    try:
        for _ in range(50):
            if lock.exists():
                break
            time.sleep(0.05)
        result = bash(f'id() {{ echo 0; }}; LOCK_FILE="{lock}"; main prune')
        assert result.returncode != 0 and "another atlas-release is running" in result.stderr
        listed = bash(f'id() {{ echo 0; }}; LOCK_FILE="{lock}"; BASE="{tmp_path}"; main list')
        assert listed.returncode == 0  # read-only commands don't wait for the lock
    finally:
        holder.kill()
        holder.wait()


def test_r1_locks_lists_only_reusable_venvs(tmp_path: Path) -> None:
    base = tmp_path / "base"
    (base / "releases").mkdir(parents=True)
    (tmp_path / "services.conf").write_text("atlas-audit yes\natlas-core yes\n")
    active_release(tmp_path, base)
    snippet = f'BASE="{base}"; CONF="{tmp_path / "services.conf"}"; PY3="{fake_python(tmp_path)}"; cmd_locks'
    result = bash(snippet)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"audit {hashlib.sha256(LOCK).hexdigest()}\n"  # atlas-core has no venv there: not listed
    empty = bash(f'BASE="{tmp_path / "none"}"; CONF="{tmp_path / "services.conf"}"; cmd_locks')
    assert empty.returncode == 0 and empty.stdout == ""


def test_r1_deploy_leaves_out_only_wheels_the_pi_reported() -> None:
    text = (DEPLOY / "provisioning" / "deploy.sh").read_text()
    assert "\"${release_cmd[@]}\" locks 2>/dev/null | grep -E '^[a-z]+ [0-9a-f]{64}$'" in text
    assert 'grep -Fqx "$short $(shasum -a 256 "$lock" | cut -d\' \' -f1)"' in text
