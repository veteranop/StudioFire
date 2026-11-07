# StudioFire - production install helper
# ------------------------------------------------------------------------------
# Put this script in the SAME FOLDER as StudioFire-Setup-<version>.exe, copy the
# whole folder to the production PC, then right-click this file ->
# "Run with PowerShell", or in an elevated PowerShell:
#
#     .\INSTALL-Production.ps1              # walk the setup screens (recommended)
#     .\INSTALL-Production.ps1 -Silent      # unattended, wizard defaults
#     .\INSTALL-Production.ps1 -PreflightOnly   # change nothing, just check
#
# It checks the machine first, backs up any existing StudioFire data it finds,
# runs the installer, then proves the install works.
# ------------------------------------------------------------------------------

[CmdletBinding()]
param(
    [switch]$Silent,
    [switch]$PreflightOnly,
    [switch]$SkipHashCheck,
    [string]$Port
)

$ErrorActionPreference = 'Stop'
$script:Problems = @()
$script:Warnings = @()
$script:LogPath  = Join-Path $PSScriptRoot ("install-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))

function Log([string]$msg, [string]$kind = 'info') {
    $stamp = Get-Date -Format 'HH:mm:ss'
    $line  = "[$stamp] $msg"
    switch ($kind) {
        'ok'    { Write-Host $line -ForegroundColor Green }
        'warn'  { Write-Host $line -ForegroundColor Yellow; $script:Warnings += $msg }
        'bad'   { Write-Host $line -ForegroundColor Red;    $script:Problems += $msg }
        'head'  { Write-Host ""; Write-Host $msg -ForegroundColor Cyan }
        default { Write-Host $line }
    }
    try { Add-Content -Path $script:LogPath -Value $line -Encoding UTF8 } catch { }
}

function Section([string]$title) { Log "" ; Log "--- $title " 'head' }

# ---------------------------------------------------------------- elevation ---
$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Log "StudioFire production install helper" 'head'
Log "Computer : $env:COMPUTERNAME"
Log "User     : $env:USERNAME"
Log "Folder   : $PSScriptRoot"

if (-not $isAdmin -and -not $PreflightOnly) {
    Log "Not elevated - relaunching with a UAC prompt..." 'warn'
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    if ($Silent)        { $argList += '-Silent' }
    if ($SkipHashCheck) { $argList += '-SkipHashCheck' }
    if ($Port)          { $argList += @('-Port', $Port) }
    Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs
    exit 0
}

# ------------------------------------------------------------- the package ----
Section 'Package'

$exe = Get-ChildItem -Path $PSScriptRoot -Filter 'StudioFire-Setup-*.exe' -File |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $exe) {
    Log "No StudioFire-Setup-*.exe in this folder. Copy the exe next to this script." 'bad'
    exit 1
}
$ver = ($exe.BaseName -replace '^StudioFire-Setup-', '')
Log "Installer : $($exe.Name)"
Log "Version   : $ver"
Log ("Size      : {0:N0} bytes" -f $exe.Length)
if ($exe.Length -lt 20MB) {
    Log "That file is too small to be a real installer (20 MB minimum). Re-copy it." 'bad'
    exit 1
}

$actualHash = (Get-FileHash -Path $exe.FullName -Algorithm SHA256).Hash
Log "SHA-256   : $actualHash"

$sumsFile = Join-Path $PSScriptRoot 'SHA256SUMS.txt'
if ($SkipHashCheck) {
    Log "Hash check skipped on request." 'warn'
}
elseif (Test-Path $sumsFile) {
    $expected = (Get-Content $sumsFile | Where-Object { $_ -match [regex]::Escape($exe.Name) } |
                 Select-Object -First 1) -replace '\s.*$', ''
    if ($expected -and $expected.Trim().ToUpper() -ne $actualHash.ToUpper()) {
        Log "SHA-256 does not match SHA256SUMS.txt - the copy is damaged." 'bad'
        Log "  expected $expected" 
        Log "  actual   $actualHash"
        if (-not $PreflightOnly) { Log "Refusing to install a damaged file." 'bad'; exit 1 }
    } elseif ($expected) {
        Log "Checksum matches SHA256SUMS.txt" 'ok'
    } else {
        Log "SHA256SUMS.txt has no entry for this file - checksum unverified." 'warn'
    }
} else {
    Log "No SHA256SUMS.txt beside the installer - checksum unverified." 'warn'
}

# ------------------------------------------------------- what is already here --
Section 'Existing StudioFire'

$svc = Get-Service -Name 'StudioFire*' -ErrorAction SilentlyContinue
if ($svc) {
    foreach ($s in $svc) { Log "Service $($s.Name): $($s.Status)" 'warn' }
    Log "Uninstall StudioFire first (Windows Settings > Apps), then run this script." 'bad'
} else {
    Log "No StudioFire services installed." 'ok'
}

$installDirs = @(
    (Join-Path $env:ProgramFiles 'StudioFire'),
    (Join-Path ${env:ProgramFiles(x86)} 'StudioFire')
) | Where-Object { $_ -and (Test-Path $_) }
foreach ($d in $installDirs) { Log "Old install folder still present: $d" 'warn' }

$dataRoot = Join-Path $env:ProgramData 'StudioFire'
$dbPath   = Join-Path $dataRoot 'data\studiofire.db'
$hasData  = Test-Path $dbPath

if ($hasData) {
    $size = (Get-Item $dbPath).Length
    Log ("Existing database: {0} ({1:N1} MB)" -f $dbPath, ($size / 1MB)) 'warn'
    Log "If this station has history you want to keep, it must be backed up BEFORE installing." 'warn'
    if (-not $PreflightOnly) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $backup = Join-Path $dataRoot "backups\pre-install-$stamp.db"
        New-Item -ItemType Directory -Force -Path (Split-Path $backup) | Out-Null
        Copy-Item -Path $dbPath -Destination $backup -Force
        $backupHash = (Get-FileHash $backup -Algorithm SHA256).Hash
        Log "Backed up to: $backup" 'ok'
        Log "Backup SHA-256: $backupHash"
    }
} else {
    Log "No existing database in $dataRoot - this will be a clean first run." 'ok'
}

# Legacy in-app data from 1.x checkouts
$stale = Join-Path $PSScriptRoot 'data\studiofire.db'
if (Test-Path $stale) {
    Log "This folder itself contains data\studiofire.db - move it aside, it is a dev copy." 'warn'
}

# --------------------------------------------------------------- the machine ---
Section 'This machine'

$os = Get-CimInstance Win32_OperatingSystem
Log ("OS        : {0} build {1}" -f $os.Caption, $os.BuildNumber)
if ([int]$os.BuildNumber -lt 10240) { Log "Windows 10 or newer is required." 'bad' }

$freeGB = [math]::Round((Get-PSDrive -Name ($env:ProgramFiles.Substring(0,1))).Free / 1GB, 1)
Log ("Free space: {0} GB on {1}:" -f $freeGB, $env:ProgramFiles.Substring(0,1))
if ($freeGB -lt 3) { Log "Under 3 GB free - the installer needs ~1 GB and the library needs room." 'warn' }

$audio = Get-CimInstance Win32_SoundDevice -ErrorAction SilentlyContinue
if ($audio) {
    foreach ($a in $audio) { Log "Audio out : $($a.Name)" }
} else {
    Log "No sound device reported by Windows - the wizard's audio step will have nothing to pick." 'warn'
}

$portToCheck = if ($Port) { [int]$Port } else { 8080 }
$inUse = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
         Where-Object { $_.LocalPort -eq $portToCheck }
if ($inUse) {
    Log "Port $portToCheck is already in use (PID $($inUse[0].OwningProcess)) - pick another in the wizard." 'warn'
} else {
    Log "Port $portToCheck is free." 'ok'
}

if ($PreflightOnly) {
    Section 'Result'
    if ($script:Problems) {
        Log "PREFLIGHT FAILED - fix the red items above." 'bad'
        foreach ($p in $script:Problems) { Log "  - $p" }
        exit 1
    }
    Log "Preflight OK. Run again without -PreflightOnly to install." 'ok'
    if ($script:Warnings) { Log ("{0} warning(s) - see above." -f $script:Warnings.Count) 'warn' }
    exit 0
}

# ----------------------------------------------------------------- install ----
Section 'Install'

if ($script:Problems) {
    Log "Not installing - fix the red items above first." 'bad'
    foreach ($p in $script:Problems) { Log "  - $p" }
    exit 1
}

$installerLog = Join-Path $PSScriptRoot ("inno-setup-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
$installerArgs = @("/LOG=$installerLog")
if ($Silent) { $installerArgs += '/SILENT' }

if ($Silent) {
    Log "Running unattended (/SILENT) - the installer takes the port from config.json if one exists, else 8080."
} else {
    Log "Running the setup screens - the wizard inside StudioFire continues after this."
}

$proc = Start-Process -FilePath $exe.FullName -ArgumentList $installerArgs -Wait -PassThru
Log "Installer exit code: $($proc.ExitCode)"
if ($proc.ExitCode -ne 0) {
    Log "Installer failed (exit $($proc.ExitCode)). Its log: $installerLog" 'bad'
    exit 1
}
Log "Installer finished." 'ok'

# ------------------------------------------------------------- verification ---
Section 'Verification'

Start-Sleep -Seconds 8

$svc = Get-Service -Name 'StudioFire*' -ErrorAction SilentlyContinue
if ($svc) {
    foreach ($s in $svc) {
        Log "Service $($s.Name): $($s.Status) / $($s.StartType)"
        if ($s.Status -ne 'Running') { Log "Service $($s.Name) is not running." 'warn' }
    }
} else {
    Log "No services were installed (the 'services' task was not ticked) - start StudioFire from the Start menu." 'warn'
}

$listening = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
             Where-Object { $_.LocalPort -eq $portToCheck }
if ($listening) { Log "Port $portToCheck is listening." 'ok' }
else { Log "Nothing is listening on $portToCheck yet - it may still be starting." 'warn' }

$url = "http://localhost:$portToCheck"
try {
    $r = Invoke-WebRequest -Uri "$url/health" -UseBasicParsing -TimeoutSec 15
    Log "GET /health -> $($r.StatusCode) $($r.Content)" 'ok'
} catch {
    Log "GET /health failed: $($_.Exception.Message)" 'warn'
    Log "Check the log at $dataRoot\logs\ before trying again." 'warn'
}

Log ""
Log "Open $url to finish setup (station name, music library, audio output)." 'ok'
Log "Logs:  $dataRoot\logs\"
Log "Data:  $dataRoot\data\"
Log "This run's log: $script:LogPath"

if ($script:Warnings) {
    Log ""
    Log ("{0} warning(s) - review them above." -f $script:Warnings.Count) 'warn'
}
