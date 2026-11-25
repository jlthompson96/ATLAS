<#
.SYNOPSIS
  ATLAS desktop inventory (PLAN Phase 0: "hardware inventory of both hosts"; verifies A2, A3, A5, section 0.4 and the
  Wake-on-LAN prerequisites in section 2.13). Read-only.

.DESCRIPTION
  Prints a Markdown report for docs/inventory/desktop-<date>.md. It changes nothing: it only reads (Get-* cmdlets,
  registry reads, `powercfg` query switches, `nvidia-smi --query-gpu`) and pings the Pi once.

  It deliberately leaves out anything that identifies this machine or its owner, because the repository is public:
  no computer name, user name, MAC address, IP address, serial number, UUID, product key or user-profile path.
  The same-network check (A5) prints only yes/no.

  Run it in Windows PowerShell (5.1, built in). An administrator window gives the complete report: wake timers and
  Secure Boot need it; everything else works without.

      powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop-inventory.ps1 -PiAddress 192.0.2.48
      powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop-inventory.ps1 -PiAddress 192.0.2.48 -OutFile inventory.md

  -ExecutionPolicy Bypass applies to this one process only; it does not change the machine's policy.

.PARAMETER PiAddress
  The Pi's LAN IPv4 address, for the same-network check. Never printed.

.PARAMETER OutFile
  Also write the report to this file (UTF-8). Read it before sharing it.
#>
[CmdletBinding()]
param(
    [string]$PiAddress = "",
    [string]$OutFile = ""
)

$ErrorActionPreference = "Continue"
$rows = New-Object System.Collections.Generic.List[string]
$sections = New-Object System.Collections.Generic.List[string]

function Add-Row([string]$Item, [string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value)) { $Value = "(not found)" }
    $clean = ($Value -replace "\|", "/" -replace "[\r\n]+", " ").Trim()
    $rows.Add("| $Item | $clean |")
}

function Add-Section([string]$Title, [string[]]$Lines) {
    $sections.Add("")
    $sections.Add("## $Title")
    $sections.Add("")
    $sections.Add('```text')
    foreach ($l in $Lines) { if ($null -ne $l) { $sections.Add(($l -replace "\s+$", "")) } }
    $sections.Add('```')
}

function Get-Reg([string]$Path, [string]$Name) {
    try { return (Get-ItemProperty -Path $Path -Name $Name -ErrorAction Stop).$Name } catch { return $null }
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Format-GiB([double]$Bytes) { return ("{0:N1} GiB" -f ($Bytes / 1GB)) }

$isAdmin = Test-Admin

# --- Windows (A3) ------------------------------------------------------------------------------------------------
$nt = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
$build = Get-Reg $nt "CurrentBuild"
$ubr = Get-Reg $nt "UBR"
$product = Get-Reg $nt "ProductName"
# Windows 11 still says "Windows 10" in ProductName; build 22000 and later is Windows 11.
if ($build -and [int]$build -ge 22000 -and $product) { $product = $product -replace "Windows 10", "Windows 11" }
Add-Row "windows" ("{0} {1}, build {2}.{3}" -f $product, (Get-Reg $nt "DisplayVersion"), $build, $ubr)
$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
if ($os) { Add-Row "architecture" $os.OSArchitecture }
Add-Row "display language" ((Get-UICulture).Name + " (powercfg and event text are localised; hostd's parsers expect English)")
Add-Row "powershell" $PSVersionTable.PSVersion.ToString()
Add-Row "administrator" ($(if ($isAdmin) { "yes" } else { "no (wake timers and Secure Boot not read)" }))

# --- Hardware (A2, section 0.4) ----------------------------------------------------------------------------------
$cpu = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1
if ($cpu) { Add-Row "cpu" ("{0}, {1} cores / {2} threads" -f $cpu.Name.Trim(), $cpu.NumberOfCores, $cpu.NumberOfLogicalProcessors) }
$mem = (Get-CimInstance Win32_PhysicalMemory -ErrorAction SilentlyContinue | Measure-Object -Property Capacity -Sum).Sum
if ($mem) { Add-Row "ram" (Format-GiB $mem) }
$board = Get-CimInstance Win32_BaseBoard -ErrorAction SilentlyContinue
if ($board) { Add-Row "motherboard" ("{0} {1}" -f $board.Manufacturer, $board.Product) }
$bios = Get-CimInstance Win32_BIOS -ErrorAction SilentlyContinue
if ($bios) { Add-Row "firmware" ("{0} {1}" -f $bios.Manufacturer, $bios.SMBIOSBIOSVersion) }
if ($isAdmin) {
    try { Add-Row "secure boot" ([string](Confirm-SecureBootUEFI -ErrorAction Stop)) } catch { Add-Row "secure boot" "unknown (legacy BIOS or not readable)" }
}

$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $gpuLines = & $smi.Source --query-gpu=name,memory.total,driver_version,pcie.link.gen.max,power.limit --format=csv,noheader 2>$null
    $i = 0
    foreach ($g in $gpuLines) { Add-Row "gpu $i (nvidia-smi)" $g; $i++ }
} else {
    foreach ($v in (Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue)) {
        Add-Row "gpu (no nvidia-smi)" ("{0}, driver {1}" -f $v.Name, $v.DriverVersion)
    }
}

foreach ($d in (Get-PhysicalDisk -ErrorAction SilentlyContinue | Sort-Object DeviceId)) {
    Add-Row ("disk " + $d.DeviceId) ("{0}, {1} {2}, {3}" -f $d.FriendlyName, $d.MediaType, $d.BusType, (Format-GiB $d.Size))
}
$sys = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($env:SystemDrive)'" -ErrorAction SilentlyContinue
if ($sys) { Add-Row "system drive free" ("{0} of {1}" -f (Format-GiB $sys.FreeSpace), (Format-GiB $sys.Size)) }

# --- Network and Wake-on-LAN (A5, section 2.13) ------------------------------------------------------------------
$nicLines = New-Object System.Collections.Generic.List[string]
foreach ($a in (Get-NetAdapter -Physical -ErrorAction SilentlyContinue | Sort-Object Name)) {
    $wired = $a.PhysicalMediaType -match "802\.3" -or $a.MediaType -eq "802.3"
    $label = "nic " + $(if ($wired) { "wired" } else { "wireless/other" })
    Add-Row $label ("{0}: {1}, {2}" -f $a.InterfaceDescription, $a.Status, $a.LinkSpeed)
    $nicLines.Add(("[{0}] {1} ({2}, {3})" -f $label, $a.InterfaceDescription, $a.Status, $a.LinkSpeed))
    $pm = Get-NetAdapterPowerManagement -Name $a.Name -ErrorAction SilentlyContinue
    if ($pm) {
        $nicLines.Add(("  power management: WakeOnMagicPacket={0} WakeOnPattern={1} AllowComputerToTurnOffDevice={2} DeviceSleepOnDisconnect={3}" -f `
            $pm.WakeOnMagicPacket, $pm.WakeOnPattern, $pm.AllowComputerToTurnOffDevice, $pm.DeviceSleepOnDisconnect))
        if ($wired) { Add-Row "wired nic wake on magic packet" ([string]$pm.WakeOnMagicPacket) }
    }
    foreach ($p in (Get-NetAdapterAdvancedProperty -Name $a.Name -ErrorAction SilentlyContinue |
            Where-Object { $_.DisplayName -match "Wake|WOL|Magic|Pattern|Energy|EEE|Green|Power Sav|Shutdown" })) {
        $nicLines.Add(("  advanced: {0} = {1}" -f $p.DisplayName, $p.DisplayValue))
    }
}
Add-Section "Network adapters and wake settings" $nicLines

$armed = & powercfg /devicequery wake_armed 2>$null
Add-Section "Devices allowed to wake the PC (powercfg /devicequery wake_armed)" $armed

if ($PiAddress) {
    $reachable = Test-Connection -ComputerName $PiAddress -Count 2 -Quiet -ErrorAction SilentlyContinue
    $neighbour = Get-NetNeighbor -IPAddress $PiAddress -ErrorAction SilentlyContinue |
        Where-Object { $_.State -in @("Reachable", "Stale", "Delay", "Probe", "Permanent") } | Select-Object -First 1
    # A directly connected host appears in the neighbour (ARP) table under its own address; behind a router it doesn't.
    Add-Row "pi answers ping" ([string][bool]$reachable)
    Add-Row "pi on the same layer-2 segment (A5)" ($(if ($neighbour) { "yes (in the neighbour table)" } else { "no or unknown" }))
} else {
    Add-Row "pi on the same layer-2 segment (A5)" "not checked (run with -PiAddress)"
}

# --- Power and sleep (section 2.13, B6) --------------------------------------------------------------------------
Add-Section "Sleep states (powercfg /a)" (& powercfg /a 2>&1)
$scheme = (& powercfg /getactivescheme 2>$null) -replace "^.*\((.*)\).*$", '$1'
Add-Row "power plan" $scheme
$hiberboot = Get-Reg "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power" "HiberbootEnabled"
Add-Row "fast startup" ($(switch ($hiberboot) { 1 { "ON (turn it off: it can block wake from shutdown)" } 0 { "off" } default { "unknown" } }))
$hib = Get-Reg "HKLM:\SYSTEM\CurrentControlSet\Control\Power" "HibernateEnabled"
Add-Row "hibernate" ($(switch ($hib) { 1 { "enabled" } 0 { "disabled" } default { "unknown" } }))
$ux = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
$ahs = Get-Reg $ux "ActiveHoursStart"; $ahe = Get-Reg $ux "ActiveHoursEnd"
if ($null -ne $ahs -and $null -ne $ahe) { Add-Row "windows update active hours" ("{0:D2}:00 to {1:D2}:00" -f $ahs, $ahe) }
if ($isAdmin) {
    Add-Section "Wake timers (powercfg /waketimers)" (& powercfg /waketimers 2>&1)
}

# --- Software ATLAS needs on this host (A1, hostd) ---------------------------------------------------------------
$py = Get-Command py -ErrorAction SilentlyContinue
$pyv = if ($py) { (& $py.Source -3.13 --version 2>&1) -join " " } else { $null }
Add-Row "python 3.13 (py launcher)" $pyv
$lmsExe = Join-Path $env:LOCALAPPDATA "Programs\LM Studio\LM Studio.exe"
$lmsVer = if (Test-Path $lmsExe) { (Get-Item $lmsExe).VersionInfo.ProductVersion } else { $null }
Add-Row "lm studio (A1: needs 0.4.0 or later)" $lmsVer
$wg = Get-Command wg -ErrorAction SilentlyContinue
Add-Row "wireguard tools" ($(if ($wg) { "installed" } else { $null }))

# --- Report ------------------------------------------------------------------------------------------------------
$report = New-Object System.Collections.Generic.List[string]
$report.Add("# Desktop inventory, $(Get-Date -Format 'yyyy-MM-dd')")
$report.Add("")
$report.Add("From ``hostd/scripts/desktop-inventory.ps1`` (A2, A3, A5, PLAN 0.4 and 2.13). No computer or user name, MAC, IP, serial or UUID: the repository is public.")
$report.Add("")
$report.Add("| Item | Value |")
$report.Add("|---|---|")
foreach ($r in $rows) { $report.Add($r) }
foreach ($s in $sections) { $report.Add($s) }

$text = $report -join [Environment]::NewLine
Write-Output $text
if ($OutFile) {
    $target = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutFile)
    [IO.File]::WriteAllText($target, $text + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
    Write-Host "`nWritten to $target. Read it before sharing it."
}
