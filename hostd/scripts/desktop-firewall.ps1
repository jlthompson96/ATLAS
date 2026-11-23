<#
.SYNOPSIS
  Windows Defender Firewall rules for the ATLAS link (PLAN 5.1, 5.3, R-LLM; runbook docs/runbooks/wireguard.md).

.DESCRIPTION
  Without -Apply it only shows the rules it would create and the ATLAS rules that exist now. It never touches a rule
  outside its own group "ATLAS": -Apply removes that group's rules and creates them again, -Remove removes them.

  The rules:
    1. WireGuard from the Pi: inbound UDP 51820, only from the Pi's LAN address.
    2. LM Studio (TCP 1234) and atlas-hostd (TCP 8770): inbound only from the Pi's tunnel address 10.77.0.1 to the
       desktop's tunnel address 10.77.0.2 (contracts/services.md section 2).
    3. Block TCP 1234 and 8770 from every other address, IPv4 and IPv6. Block rules win over allow rules in Windows
       Defender Firewall, so the broad "allow LM Studio" rule Windows offers on LM Studio's first start can't open
       these ports to the LAN.

  Needs an administrator PowerShell for -Apply and -Remove. Windows PowerShell 5.1 (built in) is enough:

      powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop-firewall.ps1 -PiLanAddress 192.0.2.48
      powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop-firewall.ps1 -PiLanAddress 192.0.2.48 -Apply

.PARAMETER PiLanAddress
  The Pi's LAN IPv4 address (reserve it in the router).

.PARAMETER Apply
  Create the rules (replacing this script's earlier ones).

.PARAMETER Remove
  Remove this script's rules.
#>
[CmdletBinding()]
param(
    [string]$PiLanAddress = "",
    [switch]$Apply,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$Group = "ATLAS"
$PiTunnel = "10.77.0.1"
$DesktopTunnel = "10.77.0.2"
$WgPort = "51820"
$TunnelPorts = @("1234", "8770")
# Every IPv4 address except the Pi's tunnel address, plus all of IPv6.
$EveryoneElse = @("0.0.0.0-10.77.0.0", "10.77.0.2-255.255.255.255", "::/0")

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Show-Existing {
    $rules = Get-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue
    if (-not $rules) { Write-Output "No ATLAS rules exist yet."; return }
    Write-Output "ATLAS rules now:"
    foreach ($r in $rules) { Write-Output ("  {0} ({1}, {2}, enabled {3})" -f $r.DisplayName, $r.Direction, $r.Action, $r.Enabled) }
}

if ($Apply -and $Remove) { throw "Use -Apply or -Remove, not both." }

if ($Remove) {
    if (-not (Test-Admin)) { throw "Run this in an administrator PowerShell." }
    Remove-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue
    Write-Output "Removed the ATLAS rules."
    Show-Existing
    return
}

if ($PiLanAddress -notmatch '^(25[0-5]|2[0-4]\d|1?\d?\d)(\.(25[0-5]|2[0-4]\d|1?\d?\d)){3}$') {
    throw "Give the Pi's LAN address: -PiLanAddress <IPv4>"
}

$plan = @(
    @{ DisplayName = "ATLAS: WireGuard from the Pi"; Direction = "Inbound"; Action = "Allow"; Protocol = "UDP";
       LocalPort = $WgPort; RemoteAddress = $PiLanAddress },
    @{ DisplayName = "ATLAS: LM Studio and atlas-hostd from the Pi, tunnel only"; Direction = "Inbound"; Action = "Allow";
       Protocol = "TCP"; LocalPort = $TunnelPorts; LocalAddress = $DesktopTunnel; RemoteAddress = $PiTunnel },
    @{ DisplayName = "ATLAS: block LM Studio and atlas-hostd ports from everyone else"; Direction = "Inbound"; Action = "Block";
       Protocol = "TCP"; LocalPort = $TunnelPorts; RemoteAddress = $EveryoneElse }
)

Write-Output "Planned rules (group $Group, all network profiles):"
foreach ($p in $plan) {
    Write-Output ("  {0}: {1} {2} {3} port {4}, from {5}{6}" -f $p.DisplayName, $p.Direction, $p.Action, $p.Protocol,
        ($p.LocalPort -join ","), ($p.RemoteAddress -join ","), $(if ($p.LocalAddress) { " to " + $p.LocalAddress } else { "" }))
}

if (-not $Apply) {
    Write-Output ""
    Show-Existing
    Write-Output ""
    Write-Output "Nothing changed. Run again with -Apply (administrator) to create these rules."
    return
}

if (-not (Test-Admin)) { throw "Run this in an administrator PowerShell." }
Remove-NetFirewallRule -Group $Group -ErrorAction SilentlyContinue
foreach ($p in $plan) {
    New-NetFirewallRule -Group $Group -Profile Any -Enabled True @p | Out-Null
}
Write-Output ""
Write-Output "Applied."
Show-Existing
