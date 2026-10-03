<#
    StudioFire - set engine.audio_device_guid in config\config.json.

    WHY
    ---
    config\config.json ships with "audio_device_guid": "" which means "use the
    Windows default output device". A DEFAULT device is a per-session thing and
    it moves around: Windows renumbers outputs, a monitor wakes up and becomes
    the default, Bluetooth grabs the endpoint, etc. On a station box that feeds a
    fixed piece of hardware (the sound card wired to the Barix Instreamer) we want
    the engine locked to THAT device by GUID, not to "whatever Windows feels like
    today". This tool lists what mpv can see and writes the exact identifier into
    the config.

    (To be clear about what this does NOT do: it does not make audio work from a
    Windows service. A service runs in session 0, which has no audio endpoint at
    all - no GUID fixes that. This is hygiene for the interactive session.)

    USAGE
    -----
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\set-audio-device.ps1 -List
        powershell ... -File scripts\set-audio-device.ps1 -Value "wasapi/{2b8a20cf-...}"
        powershell ... -File scripts\set-audio-device.ps1 -Value ""      # back to default
        powershell ... -File scripts\set-audio-device.ps1 -Check         # report only
        powershell ... -File scripts\set-audio-device.ps1                # interactive picker

    Or the .bat wrapper: scripts\set-audio-device.bat  (same arguments).

    Restart the engine (or the stack) for a change to take effect.
#>
[CmdletBinding()]
param(
    [string]$Value,
    [switch]$List,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'

$Root   = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Config = Join-Path $Root 'config\config.json'
# mpv.exe is the GUI-subsystem build: PowerShell cannot capture its stdout, so
# use mpv.com (the console build) to list devices. Fall back to mpv.exe.
$MpvCom = Join-Path $Root 'bin\mpv.com'
$MpvExe = Join-Path $Root 'bin\mpv.exe'
$Mpv = if (Test-Path $MpvCom) { $MpvCom } elseif (Test-Path $MpvExe) { $MpvExe } else { $null }

if (-not (Test-Path $Config)) { Write-Host ("[X] config not found: " + $Config); exit 1 }

function Get-CurrentDevice {
    $raw = Get-Content -Path $Config -Raw
    $m = [regex]::Match($raw, '"audio_device_guid"\s*:\s*"([^"]*)"')
    if ($m.Success) { return $m.Groups[1].Value }
    return $null
}

function Get-DeviceList {
    if (-not $Mpv) {
        Write-Host ("[X] no mpv found in " + (Join-Path $Root 'bin'))
        return @()
    }
    $lines = & $Mpv --no-config --audio-device=help 2>&1
    $devices = @()
    foreach ($line in $lines) {
        $t = ([string]$line).Trim()
        # lines look like:  'wasapi/{GUID}' (Friendly name)
        $m = [regex]::Match($t, "^'([^']+)'\s*\((.*)\)\s*$")
        if ($m.Success) {
            $devices += [pscustomobject]@{ Id = $m.Groups[1].Value; Name = $m.Groups[2].Value }
        }
    }
    return $devices
}

function Show-Devices {
    Write-Host 'Audio devices mpv can see:'
    $devices = Get-DeviceList
    if ($devices.Count -eq 0) { Write-Host '  (none reported)'; return $devices }
    $i = 0
    foreach ($d in $devices) {
        Write-Host ("  [" + $i + "] " + $d.Id + "   (" + $d.Name + ")")
        $i++
    }
    $cur = Get-CurrentDevice
    if ($null -eq $cur) {
        Write-Host '  current: (no audio_device_guid key in config)'
    } elseif ($cur -eq '') {
        Write-Host '  current: ""  -> Windows default device'
    } else {
        Write-Host ('  current: ' + $cur)
    }
    return $devices
}

function Write-Device([string]$newValue) {
    $raw = Get-Content -Path $Config -Raw
    if (-not [regex]::IsMatch($raw, '"audio_device_guid"\s*:\s*"[^"]*"')) {
        Write-Host '[X] no "audio_device_guid" key in config.json - set one by hand.'
        exit 1
    }
    $backup = $Config + '.bak-' + (Get-Date -Format 'yyyyMMddHHmmss')
    Copy-Item -Path $Config -Destination $backup -Force
    $updated = [regex]::Replace($raw, '("audio_device_guid"\s*:\s*")[^"]*(")', ('${1}' + $newValue + '${2}'))
    # Write UTF-8 WITHOUT a BOM. Windows PowerShell 5.1's
    # `Set-Content -Encoding UTF8` prepends a BOM. StudioFire's own loaders read
    # the file in binary and tolerate it, but any strict UTF-8 text reader
    # (e.g. python json.load on a text-mode file) trips on it, so do not add one.
    [System.IO.File]::WriteAllText($Config, $updated, (New-Object System.Text.UTF8Encoding($false)))

    # Validate: it must still be JSON, and the value must have changed.
    try {
        $null = Get-Content -Path $Config -Raw | ConvertFrom-Json
    } catch {
        Copy-Item -Path $backup -Destination $Config -Force
        Write-Host ('[X] edit produced invalid JSON - restored from ' + $backup)
        exit 1
    }
    if ((Get-CurrentDevice) -ne $newValue) {
        Copy-Item -Path $backup -Destination $Config -Force
        Write-Host '[X] value did not change as expected - restored'
        exit 1
    }
    if ($newValue -eq '') {
        Write-Host '[ok] audio_device_guid is now empty -> Windows default device'
    } else {
        Write-Host ('[ok] audio_device_guid is now ' + $newValue)
    }
    Write-Host ('     (backup: ' + $backup + ')')
    Write-Host '     Restart the engine / stack for this to take effect.'
}

# ---------------------------------------------------------------------------
if ($Check) {
    $cur = Get-CurrentDevice
    if ($null -eq $cur) { Write-Host 'audio_device_guid: (key absent)' }
    elseif ($cur -eq '') { Write-Host 'audio_device_guid: "" (Windows default device)' }
    else { Write-Host ('audio_device_guid: ' + $cur) }
    exit 0
}

if ($List) { $null = Show-Devices; exit 0 }

if ($PSBoundParameters.ContainsKey('Value')) {
    Write-Device $Value
    exit 0
}

# interactive
$devices = Show-Devices
Write-Host ''
$choice = Read-Host 'Enter a [number] to use, "d" for the Windows default, or press Enter to cancel'
if ([string]::IsNullOrWhiteSpace($choice)) { Write-Host 'Cancelled - nothing changed.'; exit 0 }
if ($choice.Trim().ToLower() -eq 'd') { Write-Device ''; exit 0 }
$idx = 0
if (-not [int]::TryParse($choice.Trim(), [ref]$idx)) { Write-Host '[X] not a number'; exit 1 }
if ($idx -lt 0 -or $idx -ge $devices.Count) { Write-Host '[X] out of range'; exit 1 }
Write-Device $devices[$idx].Id
exit 0
