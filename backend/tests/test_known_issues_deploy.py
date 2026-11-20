"""Regression tests for the open deploy/ issues in docs/known-issues.md (S1, N5, D2, D3). Each failed before its fix.

The release helper and the provisioning firewall step are exercised for real in bash, as in test_review_deploy.py.
Root-only commands (chown, systemd-run, nft, systemctl) are replaced by shell functions in the snippet, so the logic runs
unprivileged on throwaway paths; the real commands are checked in the ARM64 VM (docs/runbooks/pi-verification.md).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_review_deploy import GOOD, bash, make_tar

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
    (tmp_path / "services.conf").write_text("atlas-audit yes\n")
    tarball = make_tar(base / "incoming" / f"{V}.tar", members)
    return base, hashlib.sha256(tarball.read_bytes()).hexdigest()


def install(tmp_path: Path, sha: str, extra: str = "") -> subprocess.CompletedProcess[str]:
    base = tmp_path / "base"
    return bash(
        f"""
BASE="{base}"; CONF="{tmp_path / "services.conf"}"
BUILD_STATE="{tmp_path / "build"}"; BUILD_PRIVATE="{tmp_path / "private"}"
python3() {{ :; }}  # no CPython 3.13 check, venv or compileall here: not what these tests are about
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
    assert list((base / "releases").iterdir()) == []  # no .work.*, no world-readable .in.*, no partial tree
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
