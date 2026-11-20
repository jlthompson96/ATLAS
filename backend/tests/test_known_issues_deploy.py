"""Regression tests for the open deploy/ issues in docs/known-issues.md (S1, N5, D2, D3). Each failed before its fix.

The release helper and the provisioning firewall step are exercised for real in bash, as in test_review_deploy.py.
Root-only commands (chown, systemd-run, nft, systemctl) are replaced by shell functions in the snippet, so the logic runs
unprivileged on throwaway paths; the real commands are checked in the ARM64 VM (docs/runbooks/pi-verification.md).
"""

from __future__ import annotations

from pathlib import Path

from tests.test_review_deploy import bash

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
