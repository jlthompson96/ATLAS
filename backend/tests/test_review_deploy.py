"""Regression tests for the security review of deploy/ (F2, F6, F7, F8, MemoryDenyWriteExecute). The release helper's
validation functions are exercised for real in bash (the script only defines functions when sourced); what needs Linux
(systemd-run, GNU tar, setuid handling) is listed in docs/runbooks/pi-verification.md."""

from __future__ import annotations

import io
import re
import subprocess
import tarfile
from pathlib import Path

import pytest

from tests.test_deploy_structure import DEPLOY, PI_SERVICES, chain, unit

RELEASE = DEPLOY / "provisioning" / "atlas-release.sh"
PROVISION = DEPLOY / "provisioning" / "provision-pi.sh"
REPO = DEPLOY.parent


def bash(snippet: str, *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    script = f'source "{RELEASE}"; {snippet}'
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, check=False, cwd=cwd)  # noqa: S603


def make_tar(path: Path, members: dict[str, bytes | tuple[str, str]]) -> Path:
    """bytes -> regular file; ('sym', target) -> symlink; ('hard', target) -> hard link; None-content dir if name ends /"""
    with tarfile.open(path, "w") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            if name.endswith("/"):
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif isinstance(content, tuple):
                info.type = tarfile.SYMTYPE if content[0] == "sym" else tarfile.LNKTYPE
                info.linkname = content[1]
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return path


GOOD = {
    "./": b"",
    "backend/": b"",
    "backend/atlas/": b"",
    "backend/atlas/x.py": b"print(1)\n",
    "contracts/services.md": b"x",
    "deploy/provisioning/services.conf": b"atlas-audit yes\n",
    "wheels/audit/a-1-py3-none-any.whl": b"zip",
}


def check(tmp_path: Path, members: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    tarball = make_tar(tmp_path / "r.tar", members)
    return bash(f'check_tar_entries "{tarball}"')


from typing import Any  # noqa: E402


def test_f2_a_clean_release_tarball_is_accepted(tmp_path: Path) -> None:
    result = check(tmp_path, {k: v for k, v in GOOD.items() if k != "./"})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "evil",
    [
        "venv/atlas-audit/lib/python3.13/site-packages/x.pth",  # the reviewer's reproduction: runs as root under `python -m venv`/pip
        "venv/atlas-audit/bin/python",
        "etc/cron.d/x",
        "usr/local/sbin/atlas-release",
        "scripts/run.sh",
        "../../etc/passwd",
        "/etc/passwd",
        "backend/../../escape",
        "backend/atlas/../../../escape",
    ],
)
def test_f2_unexpected_or_unsafe_entries_are_refused(tmp_path: Path, evil: str) -> None:
    members: dict[str, Any] = {k: v for k, v in GOOD.items() if k != "./"}
    members[evil] = b"x"
    result = check(tmp_path, members)
    assert result.returncode != 0, evil
    assert "tarball" in result.stderr or "unsafe" in result.stderr


@pytest.mark.parametrize("kind", ["sym", "hard"])
def test_f2_links_are_refused(tmp_path: Path, kind: str) -> None:
    members: dict[str, Any] = {k: v for k, v in GOOD.items() if k != "./"}
    members["backend/atlas/link"] = (kind, "backend/atlas/x.py" if kind == "hard" else "/etc/shadow")
    result = check(tmp_path, members)
    assert result.returncode != 0
    assert "link" in result.stderr or "special" in result.stderr


def lock(tmp_path: Path, text: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "requirements-x.lock"
    path.write_text(text)
    return bash(f'validate_lock "{path}"')


H = "a" * 64


def test_f2_the_real_audit_lock_passes_the_strict_validator() -> None:
    result = bash(f'validate_lock "{REPO / "backend" / "requirements-audit.lock"}"')
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "bad",
    [
        "--index-url https://evil.example.org/simple\n",
        "--find-links /tmp/x\n",
        "-r other.txt\n",
        "--no-binary :all:\n",
        "--trusted-host evil.example.org\n",
        f"evil @ https://evil.example.org/e.whl \\\n    --hash=sha256:{H}\n",
        f"pkg==1.0 ; python_version > '3' \\\n    --hash=sha256:{H}\n",
        f"pkg==1.0 \\\n    --hash=md5:{'a' * 32}\n",
        f"pkg==1.0 \\\n    --hash=sha256:{H} --global-option=x\n",
        f"pkg==1.0 \\\n    --hash=sha256:{H}\n--config-settings=x\n",
        "pkg==1.0\n",  # no hash at all
        "pkg>=1.0 \\\n    --hash=sha256:" + H + "\n",
    ],
)
def test_f2_lock_files_may_hold_only_pins_and_hashes(tmp_path: Path, bad: str) -> None:
    assert lock(tmp_path, bad).returncode != 0, bad


def test_f2_a_minimal_valid_lock_passes(tmp_path: Path) -> None:
    ok = lock(tmp_path, f"# comment\n\ncffi==2.1.1 \\\n    --hash=sha256:{H} \\\n    --hash=sha256:{'b' * 64}\n")
    assert ok.returncode == 0, ok.stderr


def test_f2_service_names_come_from_the_root_owned_conf_and_are_validated(tmp_path: Path) -> None:
    conf = tmp_path / "services.conf"
    conf.write_text("# c\natlas-audit yes\n\natlas-egress yes\n")
    ok = bash(f'CONF="{conf}"; service_names')
    assert ok.stdout.split() == ["atlas-audit", "atlas-egress"]
    for bad in ["atlas-../../x yes\n", "Atlas-x yes\n", "atlas-a_b yes\n", "evil yes\n", "atlas- yes\n", ""]:
        conf.write_text(bad)
        assert bash(f'CONF="{conf}"; service_names').returncode != 0, bad
    assert bash(f'CONF="{tmp_path / "missing"}"; service_names').returncode != 0


def test_f2_the_script_no_longer_reads_anything_privileged_from_the_tarball() -> None:
    text = RELEASE.read_text()
    assert "partial/deploy/provisioning/services.conf" not in text  # the service list is /etc/atlas/services.conf
    assert "CONF=/etc/atlas/services.conf" in text
    assert 'ALLOWED_TOP="backend contracts deploy hud wheels"' in text
    assert 'install -d -m 700 "$partial"' in text
    assert "install -d -m 755" not in text
    for needle in (
        "systemd-run",
        "DynamicUser=yes",
        "PrivateNetwork=yes",
        "python3 -I",
        "pip --isolated",
        "-I -m pip --isolated",
    ):
        assert needle in text, needle
    # root never runs a venv's python or pip over tarball content: only the unprivileged builder does
    assert "partial/venv/$name/bin" not in text
    assert "ug-s" in text and "/6000" in text  # setuid/setgid stripped and re-checked


def test_f2_deploy_ships_only_the_allowlisted_top_levels() -> None:
    text = (DEPLOY / "provisioning" / "deploy.sh").read_text()
    shipped = re.search(r"git archive --format=tar HEAD (.*?) \\\n", text)
    assert shipped
    tops = {p.split("/")[0] for p in shipped.group(1).split()}
    assert tops <= {"backend", "contracts", "deploy", "wheels"}


def test_f2_provisioning_installs_the_root_owned_service_list() -> None:
    text = PROVISION.read_text()
    assert 'install_file "$here/services.conf" /etc/atlas/services.conf 644' in text


# --- F8 -----------------------------------------------------------------------------------------------------------


def test_f8_a_changed_ruleset_is_validated_then_applied_with_a_rollback_copy() -> None:
    assert 'install_firewall /etc/atlas "$deploy_dir/nftables"' in PROVISION.read_text()
    lib = (DEPLOY / "provisioning" / "lib.sh").read_text()
    text = lib.split("install_firewall() {", 1)[
        1
    ]  # the step moved into lib.sh with D2 (behaviour: test_known_issues_deploy)
    check = text.index('nft -c -f "$etc/atlas.nft.new"')
    prev = text.index("atlas.nft.prev")
    move = text.index('mv -f "$etc/atlas.nft.new" "$etc/atlas.nft"')
    reload_ = text.index("systemctl reload nftables.service")
    assert check < move < reload_ and prev < move  # validate first; never replace with an unchecked file
    assert "nft_changed" in text


def test_f8_dry_run_shows_the_validation_and_reload_steps() -> None:
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(PROVISION), "--dry-run"], capture_output=True, text=True, check=False, cwd=REPO,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "nft -c -f /etc/atlas/atlas.nft.new" in result.stderr


def test_f8_runbook_rollback_does_not_leave_the_pi_open() -> None:
    text = (REPO / "docs" / "runbooks" / "provisioning.md").read_text()
    assert "nft delete table inet atlas" not in text.split("## Rollback")[1].split("\n\n")[0] or "stop" in text.lower()
    rollback = text.split("## Rollback", 1)[1]
    assert "atlas.nft.prev" in rollback
    assert "systemctl stop" in rollback


# --- F6 / F7 ------------------------------------------------------------------------------------------------------


def test_f6_ipv6_internet_rule_excludes_the_lan_prefix_and_the_site_defines_it() -> None:
    v6 = [ln for ln in chain("output") if "ip6 daddr 2000::/3" in ln]
    assert len(v6) == 1 and "ip6 daddr != $LAN_PREFIX6" in v6[0]
    site = (DEPLOY / "nftables" / "site.nft.example").read_text()
    assert re.search(r"^define LAN_PREFIX6 = 2001:db8:", site, flags=re.M)  # RFC 3849 documentation prefix


def test_f6_the_provisioned_egress_unit_passes_the_prefixes_to_the_proxy() -> None:
    assert "ATLAS_EGRESS_LAN_PREFIXES=$ATLAS_LAN_CIDR,$ATLAS_LAN_PREFIX6" in PROVISION.read_text()


def test_f7_loopback_dns_is_dropped_for_everyone_but_egress_and_root() -> None:
    out = chain("output")
    lo_dns = [ln for ln in out if ln.startswith('oifname "lo"') and "dport 53" in ln]
    assert lo_dns[-1].endswith("counter drop")
    assert all('skuid "atlas-egress"' in ln or 'skuid "root"' in ln for ln in lo_dns[:-1])
    assert max(out.index(ln) for ln in lo_dns) < out.index('oifname "lo" accept')


def test_f7_proxy_port_squatters_cannot_answer() -> None:
    out = chain("output")
    rule = 'oifname "lo" tcp sport 3131-3136 meta skuid != "atlas-egress" counter drop'
    assert rule in out
    assert out.index(rule) < out.index('oifname "lo" accept')


def test_f7_wireguard_is_pinned_by_fwmark_and_ntp_by_user() -> None:
    out = chain("output")
    wg = [ln for ln in out if "udp dport 51820" in ln]
    assert wg == ["meta mark $WG_FWMARK ip daddr $DESKTOP_LAN_IP udp dport 51820 accept"]
    ntp = [ln for ln in out if "udp dport 123" in ln]
    assert ntp == ["meta skuid $NTP_USER ip daddr $NTP_SERVERS udp dport 123 accept"]
    site = (DEPLOY / "nftables" / "site.nft.example").read_text()
    assert 'define NTP_USER = "systemd-timesync"' in site and "define WG_FWMARK = 0x5182" in site


def test_f7_router_advertisements_only_from_the_router() -> None:
    inp = chain("input")
    assert not [ln for ln in inp if "nd-router-advert" in ln and "$LAN_ROUTER6" not in ln]
    assert "icmpv6 type nd-router-advert ip6 saddr $LAN_ROUTER6 accept" in inp
    assert "nd-router-advert" not in " ".join(ln for ln in inp if "{" in ln)  # not in the generic accept set


def test_f7_syntax_points_are_listed_for_the_vm() -> None:
    checklist = (REPO / "docs" / "runbooks" / "pi-verification.md").read_text()
    for needle in ("quoted `skuid` names", "variables inside anonymous sets", "`limit", "meta mark", "th dport"):
        assert needle in checklist, needle


# --- MemoryDenyWriteExecute ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", PI_SERVICES)
def test_memory_deny_write_execute_is_yes_everywhere(name: str) -> None:
    assert unit(name).one("MemoryDenyWriteExecute") == "yes"


@pytest.mark.parametrize("name", ["atlas-voice", "atlas-fallback", "atlas-search", "atlas-kiosk"])
def test_services_expected_to_need_wx_carry_a_vm_todo_and_are_in_the_checklist(name: str) -> None:
    assert any(
        c.startswith("#") and "TODO(VM): measure" in c and "MemoryDenyWriteExecute" in c for c in unit(name).comments
    )
    checklist = (REPO / "docs" / "runbooks" / "pi-verification.md").read_text()
    assert name in checklist.split("MemoryDenyWriteExecute")[1] or name.removeprefix("atlas-") in checklist
