"""atlas-backup-mount (drive allowlist) and gen-backup-key.sh (console only), ADR 0002. Mounting itself runs in the VM."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

PROV = Path(__file__).resolve().parents[2] / "deploy" / "provisioning"
MOUNT = PROV / "atlas-backup-mount.sh"
UUID_A = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
UUID_B = "1a2b3c4d-5e6f-4a1b-8c2d-3e4f5a6b7c8d"


def _select(tmp_path: Path, conf: str, plugged: list[str]) -> subprocess.CompletedProcess[str]:
    byuuid = tmp_path / "by-uuid"
    byuuid.mkdir(exist_ok=True)
    for i, uuid in enumerate(plugged):
        dev = tmp_path / f"sd{chr(ord('a') + i)}1"
        dev.touch()
        (byuuid / uuid).symlink_to(dev)
    (tmp_path / "backup.conf").write_text(conf)
    env = {
        "PATH": "/usr/bin:/bin",
        "ATLAS_BACKUP_CONF": str(tmp_path / "backup.conf"),
        "ATLAS_BACKUP_BYUUID": str(byuuid),
    }
    return subprocess.run(["/bin/bash", str(MOUNT), "select"], capture_output=True, text=True, env=env, check=False)  # noqa: S603


def test_selects_the_one_plugged_listed_drive(tmp_path: Path) -> None:
    r = _select(tmp_path, f"# drives\nA {UUID_A}\nB {UUID_B}   # offline\n", [UUID_B])
    assert r.returncode == 0, r.stderr
    name, dev = r.stdout.split()
    assert name == "B" and dev.endswith("/sda1")


def test_uuid_case_does_not_matter(tmp_path: Path) -> None:
    r = _select(tmp_path, f"A {UUID_A.upper()}\n", [UUID_A])
    assert r.returncode == 0 and r.stdout.startswith("A ")


@pytest.mark.parametrize(
    ("conf", "plugged", "msg"),
    [
        (f"A {UUID_A}\n", [], "none of the backup drives"),
        (f"A {UUID_A}\n", [UUID_B], "none of the backup drives"),  # an unlisted drive is never used
        (f"A {UUID_A}\nB {UUID_B}\n", [UUID_A, UUID_B], "2 backup drives are plugged in"),
        ("# nothing yet\n", [UUID_A], "lists no drives"),
        (f"A {UUID_A} extra\n", [UUID_A], "trailing text"),
        ("A 1234\n", [UUID_A], "bad filesystem UUID"),
        (f"A;rm {UUID_A}\n", [UUID_A], "bad drive name"),
        (f"../x {UUID_A}\n", [UUID_A], "bad drive name"),
    ],
)
def test_refuses_anything_but_exactly_one_listed_drive(tmp_path: Path, conf: str, plugged: list[str], msg: str) -> None:
    r = _select(tmp_path, conf, plugged)
    assert r.returncode == 1
    assert msg in r.stderr
    assert r.stdout == ""


def test_missing_conf_and_bad_usage(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "ATLAS_BACKUP_CONF": str(tmp_path / "absent")}
    r = subprocess.run(["/bin/bash", str(MOUNT), "select"], capture_output=True, text=True, env=env, check=False)  # noqa: S603
    assert r.returncode == 1 and "not found" in r.stderr
    r = subprocess.run(["/bin/bash", str(MOUNT), "format"], capture_output=True, text=True, env=env, check=False)  # noqa: S603
    assert r.returncode == 1 and "usage" in r.stderr


def test_mount_options_and_ownership_in_the_script() -> None:
    text = MOUNT.read_text()
    assert 'mount -t ext4 -o nosuid,nodev,noexec,noatime "$dev" "$MNT"' in text
    assert 'install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$MNT/restic"' in text
    assert "e2fsck -p" in text and '[ "$fstype" = ext4 ]' in text
    # the real conf file must be root-owned and not writable by others
    assert 'stat -c %u "$CONF"' in text and "group- or world-writable" in text


def test_gen_backup_key_refuses_outside_the_console(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "DRY_RUN": "1"}
    r = subprocess.run(  # noqa: S603
        ["/bin/bash", str(PROV / "gen-backup-key.sh")],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert r.returncode == 1
    assert "run this on the Pi's own console" in r.stderr
    assert r.stdout == ""


def test_gen_backup_key_never_echoes_or_logs_the_key() -> None:
    text = (PROV / "gen-backup-key.sh").read_text()
    assert "read -rsp" in text  # typed back without echo
    assert 'printf \'%s\' "$key" | "$here/store-credential.sh" atlas-backup backup-repo-password' in text
    assert "TMUX" in text and "/dev/tty[0-9]+" in text
    assert "logger" not in text and "| tee" not in text
    assert os.access(PROV / "gen-backup-key.sh", os.X_OK) and os.access(MOUNT, os.X_OK)
