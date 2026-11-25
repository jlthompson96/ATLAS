"""Static checks of scripts/desktop-inventory.ps1 (PowerShell isn't on the Mac; the owner runs it on the desktop).

It must be read-only, must not print anything that identifies the machine or owner (the repository is public), and
must run on the built-in Windows PowerShell 5.1.
"""

from __future__ import annotations

import re
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "desktop-inventory.ps1"
TEXT = SCRIPT.read_text(encoding="utf-8")
# Code only: drop the comment-based help block and line comments.
CODE = re.sub(r"<#.*?#>", "", TEXT, flags=re.S)
CODE = "\n".join(line.split(" #", 1)[0] for line in CODE.splitlines() if not line.lstrip().startswith("#"))

DEFINED = set(re.findall(r"^function ([A-Za-z]+-[A-Za-z]+)", CODE, flags=re.M))
ALLOWED_COMMANDS = {
    "Get-ItemProperty", "Get-CimInstance", "Get-UICulture", "Get-PhysicalDisk", "Get-NetAdapter",
    "Get-NetAdapterPowerManagement", "Get-NetAdapterAdvancedProperty", "Get-NetNeighbor", "Get-Command", "Get-Item",
    "Get-Date", "Test-Connection", "Test-Path", "Select-Object", "Where-Object", "Sort-Object", "Measure-Object",
    "Join-Path", "Split-Path", "New-Object", "Write-Output", "Write-Host", "Confirm-SecureBootUEFI",
}  # fmt: skip


def test_only_read_only_commands() -> None:
    used = set(re.findall(r"\b([A-Z][a-z]+-[A-Z][A-Za-z0-9]+)\b", CODE)) - DEFINED
    assert used <= ALLOWED_COMMANDS, f"not on the read-only list: {sorted(used - ALLOWED_COMMANDS)}"
    assert {"Add-Row", "Add-Section", "Get-Reg", "Test-Admin", "Format-GiB"} == DEFINED
    for verb in ("Set-", "Remove-", "Enable-", "Disable-", "Invoke-", "Start-", "Stop-", "Restart-", "Rename-"):
        assert verb not in CODE, verb
    assert "New-Object" in CODE and not re.search(r"New-(Item|ItemProperty|NetFirewallRule|ScheduledTask)", CODE)


def test_external_tools_only_with_query_switches() -> None:
    assert set(re.findall(r"powercfg (/[a-z]+)", CODE)) == {"/devicequery", "/a", "/getactivescheme", "/waketimers"}
    assert "powercfg /devicequery wake_armed" in CODE
    smi = re.findall(r"\$smi\.Source (\S+)", CODE)
    assert smi and all(arg.startswith("--query-gpu=") for arg in smi)
    assert "reg add" not in CODE.lower() and "bcdedit" not in CODE.lower() and "netsh" not in CODE.lower()


NOTE = "No computer or user name, MAC, IP, serial or UUID: the repository is public."


def test_nothing_identifying_is_read_or_printed() -> None:
    code = CODE.replace(NOTE, "")  # the report's own statement of what it leaves out
    for needle in (
        "MacAddress", "LinkLayerAddress", "SerialNumber", "UUID", "IdentifyingNumber", "ProcessorId", "CSName",
        "COMPUTERNAME", "USERNAME", "USERPROFILE", "Get-NetIPAddress", "Get-NetIPConfiguration", ".IPAddress",
        "ProductId", "DigitalProductId", "RegisteredOwner", "SID",
    ):  # fmt: skip
        assert needle not in code, needle
    # The Pi's address is a parameter for the check only: never part of a printed row or section.
    for line in CODE.splitlines():
        if "Add-Row" in line or "Add-Section" in line or "$report.Add" in line or "Write-" in line:
            assert "$PiAddress" not in line, line


def test_runs_on_windows_powershell_51() -> None:
    TEXT.encode("ascii")  # 5.1 reads a BOM-less UTF-8 file as the ANSI code page: keep it pure ASCII
    for newer in ("??", "?.", "-Parallel", "ForEach-Object -Parallel", "Clean {", "using namespace"):
        assert newer not in CODE, newer
    assert "#Requires -Version 7" not in TEXT


def test_report_states_what_it_leaves_out() -> None:
    assert NOTE in CODE
    assert "Read it before sharing it" in CODE


def test_console_output_never_shows_the_output_path() -> None:
    """The full -OutFile path names the user profile (C:\\Users\\<name>\\...), and people paste the console output.

    Found on the first real run (2026-11-25): only the file name is printed.
    """
    host_lines = [line for line in TEXT.splitlines() if "Write-Host" in line]
    assert host_lines
    for line in host_lines:
        assert "$target" not in line.replace("Split-Path -Leaf $target", ""), line
        assert "$OutFile" not in line, line
