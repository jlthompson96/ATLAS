"""Tests for lib.sh's install_timesync: timesyncd gets exactly the NTP_SERVERS the firewall allows (PLAN 5.7).

The home router serves no NTP, so the Pi syncs to fixed public addresses by IP. The firewall rule and the timesyncd
drop-in both come from the site file's NTP_SERVERS, so they cannot drift apart.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_deploy_structure import DEPLOY

LIB = DEPLOY / "provisioning" / "lib.sh"
EXAMPLE = DEPLOY / "nftables" / "site.nft.example"
GOOGLE = "216.239.35.0 216.239.35.4 216.239.35.8 216.239.35.12"


def timesync(tmp_path: Path, site: Path, dry_run: bool = False) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    calls = tmp_path / "calls.log"
    calls.write_text("")
    snippet = f"""
set -euo pipefail
source "{LIB}"
DRY_RUN={1 if dry_run else 0}
install() {{
  local a=()
  while [ $# -gt 0 ]; do case "$1" in -o | -g) shift 2 ;; -D) shift ;; *) a+=("$1"); shift ;; esac; done
  mkdir -p "$(dirname "${{a[${{#a[@]}}-1]}}")"
  command install "${{a[@]}}"
}}
systemctl() {{ echo "systemctl $*" >>"{calls}"; }}
install_timesync "{site}" "{tmp_path}/timesyncd.conf.d/50-atlas.conf"
"""
    result = subprocess.run(["/bin/bash", "-c", snippet], capture_output=True, text=True, check=False)  # noqa: S603
    return result, calls.read_text().splitlines()


def dropin(tmp_path: Path) -> list[str]:
    return (tmp_path / "timesyncd.conf.d/50-atlas.conf").read_text().splitlines()


def test_the_example_points_timesyncd_at_google_by_ip_with_no_fallback(tmp_path: Path) -> None:
    result, calls = timesync(tmp_path, EXAMPLE)
    assert result.returncode == 0, result.stderr
    lines = dropin(tmp_path)
    assert "[Time]" in lines
    assert f"NTP={GOOGLE}" in lines
    assert "FallbackNTP=" in lines  # never Debian's pool names: the firewall would drop them, and they need DNS
    assert calls == ["systemctl try-restart systemd-timesyncd.service"]


def test_unchanged_list_is_not_rewritten_or_restarted(tmp_path: Path) -> None:
    assert timesync(tmp_path, EXAMPLE)[0].returncode == 0
    result, calls = timesync(tmp_path, EXAMPLE)
    assert result.returncode == 0, result.stderr
    assert calls == []


def test_an_edited_list_is_written_and_timesyncd_restarted(tmp_path: Path) -> None:
    assert timesync(tmp_path, EXAMPLE)[0].returncode == 0
    site = tmp_path / "site.nft"
    site.write_text("define NTP_SERVERS = { 198.51.100.123 }   # a LAN time server\n")
    result, calls = timesync(tmp_path, site)
    assert result.returncode == 0, result.stderr
    assert "NTP=198.51.100.123" in dropin(tmp_path)
    assert calls == ["systemctl try-restart systemd-timesyncd.service"]


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    result, calls = timesync(tmp_path, EXAMPLE, dry_run=True)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "timesyncd.conf.d").exists()
    assert calls == []


@pytest.mark.parametrize(
    "define",
    [
        "",  # no NTP_SERVERS at all
        "define NTP_SERVERS = { }\n",
        "define NTP_SERVERS = { time.google.com }\n",  # a name would need DNS, which the daemon's user may not send
        "define NTP_SERVERS = { 216.239.35.0, 2001:4860:4806:: }\n",
        "define NTP_SERVERS = { 216.239.35.0/24 }\n",
        "define NTP_SERVERS = { 216.239.35.0; rm -rf / }\n",
    ],
)
def test_anything_but_plain_ipv4_addresses_is_refused(tmp_path: Path, define: str) -> None:
    site = tmp_path / "site.nft"
    site.write_text("define LAN_DNS = 198.51.100.1\n" + define)
    result, _ = timesync(tmp_path, site)
    assert result.returncode != 0
    assert "NTP_SERVERS must be" in result.stderr
    assert not (tmp_path / "timesyncd.conf.d").exists()


def test_provision_pi_configures_timesync_from_the_firewall_site_file() -> None:
    text = (DEPLOY / "provisioning" / "provision-pi.sh").read_text()
    assert text.index('install_firewall /etc/atlas "$deploy_dir/nftables"') < text.index("install_timesync")
    assert 'install_timesync "$site_file" /etc/systemd/timesyncd.conf.d/50-atlas.conf' in text
    assert "site_file=/etc/atlas/nftables-site.nft" in text
