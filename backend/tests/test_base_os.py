"""Tests for deploy/provisioning/base-os.sh (docs/runbooks/pi-bringup.md steps 8-9, the check, and the inventory).

The script runs for real in bash on throwaway paths. Root-only and Pi-only commands (findmnt, swapon, sshd, systemctl,
vcgencmd, dpkg...) are shell functions in the snippet, as in test_known_issues_deploy.py; the real commands run on the
Pi itself (pi-bringup.md "Results").
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.test_deploy_structure import DEPLOY

BASE_OS = DEPLOY / "provisioning" / "base-os.sh"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleExample atlas-pi-admin\n"
STOCK_CONFIG = "dtparam=audio=on\ncamera_auto_detect=1\n\n[cm5]\ndtoverlay=dwc2,dr_mode=host\n"
CMDLINE = "console=serial0,115200 console=tty1 root=PARTUUID=00000000-02 rootfstype=ext4 fsck.repair=yes rootwait\n"
TMP = "/tmp"  # noqa: S108  # the mount point in the fake mount table, not a temporary file
GOOD_MOUNTS = {
    "/": "ext4 /dev/mmcblk0p2 rw,noatime",
    TMP: "tmpfs tmpfs rw,nosuid,nodev",
    "/var/lib/atlas": "ext4 /dev/mmcblk0p3 rw,noatime,errors=remount-ro,commit=60",
}
GOOD_SSHD_T = (
    "port 22\npasswordauthentication no\nkbdinteractiveauthentication no\npermitrootlogin no\n"
    "authenticationmethods publickey\nallowusers admin\n"
)
# `sudo -l -U admin` after provision-pi.sh, which adds the one password-free rule.
GOOD_SUDO_L = (
    "Matching Defaults entries for admin on atlas-pi:\n    env_reset, mail_badpass, use_pty, timestamp_type=tty\n\n"
    "User admin may run the following commands on atlas-pi:\n    (ALL : ALL) ALL\n"
    "    (root) NOPASSWD: /usr/local/sbin/atlas-release\n"
)


@dataclass
class Pi:
    """A fake Pi: file trees under tmp_path, plus the state the command stubs report."""

    root: Path
    mounts: dict[str, str]
    fstab_mounts: dict[str, str]
    swaps: str = "/dev/zram0\n"
    sshd_t: str = GOOD_SSHD_T
    sshd_rc: int = 0
    sudo_ts: str = "tty"
    sudo_l: str = GOOD_SUDO_L
    visudo_rc: int = 0
    throttled: str = "throttled=0x0"
    mem_kb: int = 8_245_000

    @property
    def etc(self) -> Path:
        return self.root / "etc"

    @property
    def boot(self) -> Path:
        return self.root / "boot"

    @property
    def calls(self) -> list[str]:
        path = self.root / "calls"
        return path.read_text().splitlines() if path.exists() else []


def make_pi(tmp_path: Path) -> Pi:
    """A Pi as it is after runbook steps 6 and 7: stock files, the data partition mounted, nothing from step 8 or 9 yet."""
    pi = Pi(tmp_path, dict(GOOD_MOUNTS), {"/var/lib/atlas": "UUID=00000000-0000-4000-8000-000000000000"})
    for d in ("etc/ssh/sshd_config.d", "boot", "home/admin/.ssh", "proc/device-tree", "sys/class/net/eth0", "data"):
        (tmp_path / d).mkdir(parents=True)
    (pi.boot / "config.txt").write_text(STOCK_CONFIG)
    (pi.boot / "cmdline.txt").write_text(CMDLINE)
    (pi.etc / "fstab").write_text("proc /proc proc defaults 0 0\n")
    (pi.etc / "os-release").write_text('PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nVERSION_CODENAME=trixie\n')
    (tmp_path / "home/admin/.ssh/authorized_keys").write_text(KEY)
    (tmp_path / "proc/device-tree/model").write_bytes(b"Raspberry Pi 5 Model B Rev 1.1\x00")
    (tmp_path / "proc/meminfo").write_text(f"MemTotal:        {pi.mem_kb} kB\n")
    return pi


def run(pi: Pi, snippet: str) -> subprocess.CompletedProcess[str]:
    (pi.root / "proc/meminfo").write_text(f"MemTotal:        {pi.mem_kb} kB\n")
    mounts = "\n".join(f"{t} {v}" for t, v in pi.mounts.items())
    fstab = "\n".join(f"{t} {s}" for t, s in pi.fstab_mounts.items())
    (pi.root / "mounts").write_text(mounts + "\n")
    (pi.root / "fstab-mounts").write_text(fstab + "\n")
    (pi.root / "sshd-T").write_text(pi.sshd_t)
    (pi.root / "sudo-l").write_text(pi.sudo_l)
    calls = pi.root / "calls"
    r = pi.root
    script = f"""
source "{BASE_OS}"
ATLAS_ADMIN_USER=admin; unset SUDO_USER
ETC="{pi.etc}"; BOOT="{pi.boot}"; DATA=/var/lib/atlas; SWAPFILE="{r}/var-swap"; PROC="{r}/proc"; SYS="{r}/sys"
id() {{ if [ "$1" = -u ]; then echo 0; else command id "$@"; fi; }}
getent() {{ [ "$1" = passwd ] && [ "$2" = admin ] && echo "admin:x:1000:1000::{r}/home/admin:/bin/bash"; }}
install() {{
  local a=()
  while [ $# -gt 0 ]; do case "$1" in -o | -g) shift 2 ;; -D) shift ;; *) a+=("$1"); shift ;; esac; done
  mkdir -p "$(dirname "${{a[${{#a[@]}}-1]}}")"
  command install "${{a[@]}}"
}}
findmnt() {{
  local col="" target="" table="{r}/mounts"
  while [ $# -gt 0 ]; do
    case "$1" in --fstab) table="{r}/fstab-mounts" ;; -no) col="$2"; shift ;; -n) ;; *) target="$1" ;; esac
    shift
  done
  awk -v t="$target" -v c="$col" -v f="$table" '
    $1 == t {{ found = 1; if (f ~ /fstab/) print $2; else if (c == "FSTYPE") print $2; else if (c == "SOURCE") print $3;
               else if (c == "OPTIONS") print $4; else print }}
    END {{ exit found ? 0 : 1 }}' "$table"
}}
swapon() {{ printf '%s' "{pi.swaps}"; }}
sshd() {{
  echo "sshd $*" >>"{calls}"
  if [ "$1" = -T ]; then cat "{r}/sshd-T"; return 0; fi
  return {pi.sshd_rc}
}}
systemctl() {{ echo "systemctl $*" >>"{calls}"; return 0; }}
visudo() {{ echo "visudo $1" >>"{calls}"; return {pi.visudo_rc}; }}
sudo() {{
  case "$1" in
    -V) echo "Authentication timestamp timeout: 15.0 minutes"; echo "Type of authentication timestamp record: {pi.sudo_ts}" ;;
    -l) cat "{r}/sudo-l" ;;
  esac
}}
vcgencmd() {{
  case "$1" in get_throttled) echo "{pi.throttled}" ;; measure_temp) echo "temp=45.1'C" ;; *) echo "2026/09/01 12:00:00" ;; esac
}}
dpkg() {{ echo arm64; }}
dpkg-query() {{ echo "install ok installed"; }}
python3() {{ if [ "$1" = --version ]; then echo "Python 3.13.5"; else echo 3.13; fi; }}
systemd-analyze() {{ cat "$ETC"/systemd/journald.conf.d/*.conf 2>/dev/null; }}
{snippet}
"""
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, check=False)  # noqa: S603


def tree(pi: Pi) -> dict[str, str]:
    return {
        str(p.relative_to(pi.root)): p.read_text()
        for p in sorted(pi.root.rglob("*"))
        if p.is_file() and p.name not in {"calls", "mounts", "fstab-mounts", "sshd-T", "sudo-l"}
    }


def applied(tmp_path: Path) -> Pi:
    pi = make_pi(tmp_path)
    result = run(pi, "cmd_apply")
    assert result.returncode == 0, result.stderr
    return pi


# --- apply ----------------------------------------------------------------------------------------------------------


def test_apply_writes_steps_8_and_9(tmp_path: Path) -> None:
    pi = make_pi(tmp_path)
    pi.mounts[TMP] = "ext4 /dev/mmcblk0p2 rw"  # /tmp on the card: apply adds the tmpfs line
    (tmp_path / "var-swap").write_text("")  # a stale swap file, not in use
    result = run(pi, "cmd_apply")
    assert result.returncode == 0, result.stderr

    journal = (pi.etc / "systemd/journald.conf.d/50-atlas-volatile.conf").read_text()
    assert "Storage=volatile" in journal and "RuntimeMaxUse=64M" in journal
    swap = (pi.etc / "rpi/swap.conf.d/50-atlas.conf").read_text()
    assert "Mechanism=zram" in swap and "FixedSizeMiB=2048" in swap
    sshd = (pi.etc / "ssh/sshd_config.d/10-atlas.conf").read_text().splitlines()
    for line in ("PasswordAuthentication no", "KbdInteractiveAuthentication no", "PermitRootLogin no",
                 "AuthenticationMethods publickey", "AllowUsers admin"):  # fmt: skip
        assert line in sshd
    assert "tmpfs  /tmp  tmpfs  defaults,noatime,mode=1777  0  0" in (pi.etc / "fstab").read_text()
    sudoers = pi.etc / "sudoers.d/020_atlas-tty"  # read after Pi OS's 010_global-tty, so it wins
    assert "Defaults timestamp_type=tty" in sudoers.read_text().splitlines()
    assert sudoers.stat().st_mode & 0o777 == 0o440
    assert not (tmp_path / "var-swap").exists()

    config = (pi.boot / "config.txt").read_text()
    assert config.startswith(STOCK_CONFIG)  # appended, nothing rewritten
    tail = config[len(STOCK_CONFIG) :].splitlines()
    assert tail.index("[all]") < tail.index("dtoverlay=disable-wifi")  # not under the stock file's last [cm5]
    assert "dtoverlay=disable-bt" in tail

    assert pi.calls == [
        "visudo -cf",
        "sshd -t",
        "systemctl is-active --quiet ssh.service",
        "systemctl reload ssh.service",
    ]
    assert "Reboot" in result.stderr and "second terminal" in result.stderr


def test_apply_is_idempotent(tmp_path: Path) -> None:
    pi = applied(tmp_path)
    before = tree(pi)
    (pi.root / "calls").unlink()
    result = run(pi, "cmd_apply")
    assert result.returncode == 0, result.stderr
    assert tree(pi) == before  # no rewrites: every write costs the card
    assert pi.calls == []  # sshd not re-validated or reloaded
    assert "Nothing needs a reboot" in result.stderr


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    pi = make_pi(tmp_path)
    before = tree(pi)
    result = run(pi, "DRY_RUN=1; cmd_apply")
    assert result.returncode == 0, result.stderr
    assert tree(pi) == before
    assert pi.calls == []


def test_apply_refuses_without_an_admin_key(tmp_path: Path) -> None:
    pi = make_pi(tmp_path)
    (tmp_path / "home/admin/.ssh/authorized_keys").write_text("# no keys yet\n")
    before = tree(pi)
    result = run(pi, "cmd_apply")
    assert result.returncode != 0
    assert "lock you out" in result.stderr
    assert tree(pi) == before


def test_apply_refuses_while_the_overlay_is_on(tmp_path: Path) -> None:
    pi = make_pi(tmp_path)
    pi.mounts["/"] = "overlay overlayroot rw"
    before = tree(pi)
    result = run(pi, "cmd_apply")
    assert result.returncode != 0
    assert "overlay is on" in result.stderr
    assert tree(pi) == before


@pytest.mark.parametrize("name", ["atlas-core", "root", "ad*", "Admin", "a b", ""])
def test_admin_user_must_be_a_plain_non_service_name(tmp_path: Path, name: str) -> None:
    pi = make_pi(tmp_path)
    result = run(pi, f"unset SUDO_USER; ATLAS_ADMIN_USER='{name}'; cmd_apply")
    assert result.returncode != 0
    assert not (pi.etc / "ssh/sshd_config.d/10-atlas.conf").exists()


def test_admin_user_defaults_to_the_sudo_user(tmp_path: Path) -> None:
    pi = make_pi(tmp_path)
    result = run(pi, "unset ATLAS_ADMIN_USER; SUDO_USER=admin; cmd_apply")
    assert result.returncode == 0, result.stderr
    assert "AllowUsers admin" in (pi.etc / "ssh/sshd_config.d/10-atlas.conf").read_text()


def test_rejected_sshd_config_is_put_back_and_not_reloaded(tmp_path: Path) -> None:
    pi = make_pi(tmp_path)
    old = "# an earlier version\nPasswordAuthentication no\n"
    (pi.etc / "ssh/sshd_config.d/10-atlas.conf").write_text(old)
    pi.sshd_rc = 1
    result = run(pi, "cmd_apply")
    assert result.returncode != 0
    assert "sshd -t rejected" in result.stderr
    assert (pi.etc / "ssh/sshd_config.d/10-atlas.conf").read_text() == old
    assert not [c for c in pi.calls if "reload" in c]


def test_rejected_new_sshd_config_is_removed(tmp_path: Path) -> None:
    pi = make_pi(tmp_path)
    pi.sshd_rc = 1
    result = run(pi, "cmd_apply")
    assert result.returncode != 0
    assert not (pi.etc / "ssh/sshd_config.d/10-atlas.conf").exists()


def test_sudoers_drop_in_that_visudo_rejects_is_not_installed(tmp_path: Path) -> None:
    pi = make_pi(tmp_path)
    pi.visudo_rc = 1
    result = run(pi, "cmd_apply")
    assert result.returncode != 0
    assert "visudo rejected" in result.stderr
    assert not (pi.etc / "sudoers.d/020_atlas-tty").exists()
    assert "sshd -t" not in pi.calls  # stopped before SSH was touched


def test_swap_file_in_use_is_left_until_after_the_reboot(tmp_path: Path) -> None:
    pi = make_pi(tmp_path)
    (tmp_path / "var-swap").write_text("")
    pi.swaps = f"/dev/zram0\n{tmp_path / 'var-swap'}\n"
    result = run(pi, "cmd_apply")
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "var-swap").exists()
    assert "still in use" in result.stderr


@pytest.mark.parametrize(
    ("config", "found"),
    [
        ("dtoverlay=disable-wifi\n[cm4]\nx=1\n", True),  # before any section: applies to every model
        ("[cm4]\ndtoverlay=disable-wifi\n", False),  # CM4 only
        ("[cm4]\nx=1\n[all]\n  dtoverlay=disable-wifi  \n", True),
        ("[all]\n[pi4]\ndtoverlay=disable-wifi\n[all]\n", False),
        ("#dtoverlay=disable-wifi\n", False),
        ("dtoverlay=disable-wifi-pi5\n", False),
    ],
)
def test_config_txt_line_only_counts_when_it_applies_to_the_pi_5(tmp_path: Path, config: str, found: bool) -> None:
    pi = make_pi(tmp_path)
    (pi.boot / "config.txt").write_text(config)
    result = run(pi, f'config_txt_has "{pi.boot}/config.txt" dtoverlay=disable-wifi')
    assert (result.returncode == 0) is found


def test_line_under_a_model_filter_gets_an_all_copy(tmp_path: Path) -> None:
    pi = make_pi(tmp_path)
    (pi.boot / "config.txt").write_text("[cm4]\ndtoverlay=disable-wifi\n")
    assert run(pi, "cmd_apply").returncode == 0
    lines = (pi.boot / "config.txt").read_text().splitlines()
    assert lines[lines.index("[all]") + 1 :] == ["dtoverlay=disable-wifi", "dtoverlay=disable-bt"]


# --- check ----------------------------------------------------------------------------------------------------------


def fails(result: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in result.stdout.splitlines() if line.startswith("FAIL")]


def test_check_passes_on_a_set_up_pi(tmp_path: Path) -> None:
    pi = applied(tmp_path)  # after apply and the reboot
    result = run(pi, "cmd_check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert fails(result) == []
    assert "all checks passed" in result.stdout
    assert "NOTE  overlay: off" in result.stdout


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda pi: pi.mounts.pop("/var/lib/atlas"), "not a mount point"),
        (lambda pi: pi.mounts.update({"/var/lib/atlas": "overlay overlayroot rw"}), "own partition"),
        (lambda pi: pi.mounts.update({"/var/lib/atlas": "ext4 /dev/mmcblk0p3 rw,relatime"}), "noatime"),
        (lambda pi: pi.mounts.update({"/var/lib/atlas": "ext4 /dev/mmcblk0p3 rw,noatime,commit=60"}), "errors="),
        (lambda pi: pi.fstab_mounts.update({"/var/lib/atlas": "/dev/mmcblk0p3"}), "use UUID="),
        (lambda pi: pi.fstab_mounts.clear(), "no fstab entry"),
        (lambda pi: setattr(pi, "swaps", "/dev/zram0\n/var/swap\n"), "zram* only"),
        (lambda pi: setattr(pi, "swaps", ""), "zram* only"),
        (lambda pi: (pi.root / "var-swap").write_text(""), "var-swap exists"),
        (lambda pi: setattr(pi, "throttled", "throttled=0x50000"), "power"),
        (lambda pi: setattr(pi, "mem_kb", 4_000_000), "8 GB"),
        (lambda pi: setattr(pi, "sshd_t", GOOD_SSHD_T.replace("allowusers admin", "allowusers admin other")), "allowusers"),
        (lambda pi: setattr(pi, "sshd_t", GOOD_SSHD_T.replace("passwordauthentication no", "passwordauthentication yes")), "passwordauthentication"),
        (lambda pi: setattr(pi, "sshd_t", ""), "sshd -T"),
        (lambda pi: setattr(pi, "sudo_ts", "global"), "timestamp_type"),
        (lambda pi: setattr(pi, "sudo_l", GOOD_SUDO_L + "    (ALL) NOPASSWD: ALL\n"), "without a password"),
        (lambda pi: setattr(pi, "sudo_l", GOOD_SUDO_L.replace("/usr/local/sbin/atlas-release", "/usr/bin/apt")), "/usr/bin/apt"),
        (lambda pi: setattr(pi, "sudo_l", GOOD_SUDO_L.replace("use_pty,", "use_pty, !authenticate,")), "!authenticate"),
        (lambda pi: setattr(pi, "sudo_l", ""), "sudo -l"),
        (lambda pi: pi.mounts.update({TMP: "ext4 /dev/mmcblk0p2 rw"}), f"{TMP}:"),
        (lambda pi: (pi.root / "sys/class/net/wlan0").mkdir(), "wlan0"),
        (lambda pi: (pi.root / "sys/class/bluetooth/hci0").mkdir(parents=True), "bluetooth"),
        (lambda pi: (pi.boot / "config.txt").write_text(STOCK_CONFIG + "dtoverlay=disable-wifi\n"), "disable-wifi"),
        (lambda pi: (pi.etc / "systemd/journald.conf.d/50-atlas-volatile.conf").unlink(), "journal"),
        (lambda pi: (pi.etc / "systemd/journald.conf.d/60-later.conf").write_text("[Journal]\nStorage = persistent\n"), "persistent"),
        (lambda pi: (pi.boot / "cmdline.txt").write_text("overlayroot=tmpfs " + CMDLINE), "overlayroot=tmpfs'"),
        (lambda pi: (pi.etc / "os-release").write_text("VERSION_CODENAME=bookworm\n"), "trixie"),
    ],
)  # fmt: skip
def test_check_fails_each_wrong_setting(tmp_path: Path, change: Callable[[Pi], object], expected: str) -> None:
    pi = applied(tmp_path)  # after apply and the reboot
    change(pi)
    result = run(pi, "cmd_check")
    assert result.returncode != 0
    assert len(fails(result)) >= 1
    assert any(expected in line for line in fails(result)), result.stdout


def test_check_accepts_the_overlay_with_recurse_0(tmp_path: Path) -> None:
    pi = applied(tmp_path)  # after apply and the reboot
    (pi.boot / "cmdline.txt").write_text("overlayroot=tmpfs:recurse=0 " + CMDLINE)
    pi.mounts["/"] = "overlay overlayroot ro"
    result = run(pi, "cmd_check")
    assert result.returncode == 0, result.stdout
    assert "PASS  overlay: on, with recurse=0" in result.stdout


# --- inventory ------------------------------------------------------------------------------------------------------

MAC = re.compile(r"\b[0-9a-f]{2}(:[0-9a-f]{2}){5}\b", re.IGNORECASE)
IPV4 = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")


def test_inventory_records_the_a9_facts_and_nothing_identifying(tmp_path: Path) -> None:
    pi = make_pi(tmp_path)
    (tmp_path / "sys/block/mmcblk0/device").mkdir(parents=True)
    (tmp_path / "sys/block/mmcblk0/device/name").write_text("SE64G\n")
    (tmp_path / "sys/block/mmcblk0/device/serial").write_text("0x1234abcd\n")
    (tmp_path / "sys/block/mmcblk0/size").write_text("124735488\n")
    stubs = """
ethtool() { printf '%s\\n' 'Settings for eth0:' '	Speed: 1000Mb/s' '	Duplex: Full' \
  '	Permanent address: 2c:cf:67:00:00:01' '	Link detected: yes'; }
lsblk() { printf '%s\\n' 'mmcblk0 59.5G  ' 'mmcblk0p1 512M vfat /boot/firmware' 'mmcblk0p2 16G ext4 /' \
  'mmcblk0p3 43G ext4 /var/lib/atlas'; }
uname() { echo 6.12.47+rpt-rpi-2712; }
"""
    result = run(pi, stubs + "cmd_inventory")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    for fact in ("Raspberry Pi 5 Model B", "7.9 GiB", "Debian GNU/Linux 13 (trixie)", "arm64", "Python 3.13",
                 "throttled=0x0", "SE64G 59 GiB", "1000Mb/s Full", "mmcblk0p3 43G ext4 /var/lib/atlas"):  # fmt: skip
        assert fact in out, fact
    assert not MAC.search(out)
    assert "0x1234abcd" not in out
    assert not IPV4.search(out.replace("Rev 1.1", ""))


def test_inventory_reads_no_identifying_source() -> None:
    body = BASE_OS.read_text().split("cmd_inventory() {", 1)[1].split("\n}\n", 1)[0]
    for forbidden in (
        "cpuinfo",
        "/serial",
        "/address",
        "ip a",
        "ip -",
        "hostname",
        "ssh_host",
        "UUID",
        "blkid",
        "/cid",
    ):
        assert forbidden not in body, forbidden


# --- CLI ------------------------------------------------------------------------------------------------------------


def test_cli_usage_and_unknown_arguments() -> None:
    usage = subprocess.run(["/bin/bash", str(BASE_OS)], capture_output=True, text=True, check=False)  # noqa: S603
    assert usage.returncode == 2
    assert "apply" in usage.stdout and "inventory" in usage.stdout
    bad = subprocess.run(["/bin/bash", str(BASE_OS), "apply", "--force"], capture_output=True, text=True, check=False)  # noqa: S603
    assert bad.returncode != 0 and "unknown argument" in bad.stderr
