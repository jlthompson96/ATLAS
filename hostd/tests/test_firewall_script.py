"""Static checks of scripts/desktop-firewall.ps1: it changes only its own "ATLAS" rule group, only with -Apply or
-Remove, and its rules match contracts/services.md section 2 (PowerShell isn't on the Mac; the owner runs it)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEXT = (ROOT / "hostd" / "scripts" / "desktop-firewall.ps1").read_text(encoding="utf-8")
CODE = re.sub(r"<#.*?#>", "", TEXT, flags=re.S)
SERVICES_MD = (ROOT / "contracts" / "services.md").read_text()


def test_changes_only_the_atlas_group() -> None:
    assert '$Group = "ATLAS"' in CODE
    changes = re.findall(r"\b(New|Remove|Set|Enable|Disable|Copy|Rename)-NetFirewall\w+[^\n]*", CODE)
    assert changes, "expected rule changes"
    for line in re.findall(r"\b(?:New|Remove|Set|Enable|Disable|Copy|Rename)-NetFirewall\w+[^\n]*", CODE):
        assert "-Group $Group" in line, line
    for forbidden in (
        "Set-NetFirewallProfile",
        "Set-NetFirewallSetting",
        "netsh",
        "Disable-NetFirewallRule",
        "Set-NetFirewallRule",
    ):
        assert forbidden not in CODE, forbidden


def test_nothing_changes_without_apply() -> None:
    plan_only = CODE.split("if (-not $Apply) {", 1)
    assert len(plan_only) == 2
    before_return = plan_only[1].split("return", 1)[0]
    assert "New-NetFirewallRule" not in before_return and "Remove-NetFirewallRule" not in before_return
    # Every change sits after an administrator check.
    for block in (
        "if ($Remove) {",
        'if (-not (Test-Admin)) { throw "Run this in an administrator PowerShell." }\nRemove',
    ):
        assert block in CODE


def test_rules_match_the_contract() -> None:
    assert "Pi `10.77.0.1`, desktop `10.77.0.2`" in SERVICES_MD
    assert '$PiTunnel = "10.77.0.1"' in CODE and '$DesktopTunnel = "10.77.0.2"' in CODE
    assert '$WgPort = "51820"' in CODE and '$TunnelPorts = @("1234", "8770")' in CODE
    assert '$EveryoneElse = @("0.0.0.0-10.77.0.0", "10.77.0.2-255.255.255.255", "::/0")' in CODE
    assert "LocalAddress = $DesktopTunnel; RemoteAddress = $PiTunnel" in CODE
    assert 'Action = "Block";' in CODE and "RemoteAddress = $EveryoneElse" in CODE
    assert "RemoteAddress = $PiLanAddress" in CODE


def test_block_range_leaves_exactly_the_pi_tunnel_address() -> None:
    def ip(s: str) -> int:
        a, b, c, d = (int(x) for x in s.split("."))
        return (a << 24) | (b << 16) | (c << 8) | d

    ranges = [(ip("0.0.0.0"), ip("10.77.0.0")), (ip("10.77.0.2"), ip("255.255.255.255"))]
    covered = sum(hi - lo + 1 for lo, hi in ranges)
    assert covered == 2**32 - 1
    assert not any(lo <= ip("10.77.0.1") <= hi for lo, hi in ranges)


def test_runs_on_windows_powershell_51() -> None:
    TEXT.encode("ascii")
    for newer in ("??", "?.", "-Parallel", "#Requires -Version 7"):
        assert newer not in CODE, newer
