# Create "Start StudioFire" and "Quit StudioFire" desktop shortcuts (TimeTrax #903).
#
# Works against an EXISTING install - no reinstall needed. The installer creates these too
# (Tasks: desktopicons); this script is for the boxes already running StudioFire.
#
# Usage (from anywhere):
#   powershell -NoProfile -ExecutionPolicy Bypass -File make-desktop-icons.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File make-desktop-icons.ps1 -AllUsers
#   powershell -NoProfile -ExecutionPolicy Bypass -File make-desktop-icons.ps1 `
#       -InstallDir "C:\StudioFire\StudioFire-OnAir-v1.0.0" -DesktopDir "C:\Temp\icons-test"
#
# -AllUsers      put them on the shared Public desktop (needs an elevated prompt)
# -Remove        delete the two shortcuts instead of creating them
# ASCII only, no BOM - PowerShell 5.1 parses .ps1 as ANSI.

param(
    [string]$InstallDir = (Split-Path -Parent $PSScriptRoot),
    [string]$DesktopDir = [Environment]::GetFolderPath('Desktop'),
    [switch]$AllUsers,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'

if ($AllUsers) { $DesktopDir = [Environment]::GetFolderPath('CommonDesktopDirectory') }

$icon   = Join-Path $InstallDir 'web\static\sf_icon_dark.ico'
$start  = Join-Path $InstallDir 'start-all.bat'
$stop   = Join-Path $InstallDir 'stop-all.bat'

$items = @(
    @{ Name = 'Start StudioFire'; Target = $start; Desc = 'Start the StudioFire playout stack (web GUI + audio engine)' },
    @{ Name = 'Quit StudioFire';  Target = $stop;  Desc = 'Stop the StudioFire playout stack - the station goes off air until it is started again' }
)

$shell = New-Object -ComObject WScript.Shell

function Test-Shortcut([string]$path) {
    try { return (Test-Path -LiteralPath $path) } catch { return $false }
}

if ($Remove) {
    foreach ($it in $items) {
        $lnk = Join-Path $DesktopDir ($it.Name + '.lnk')
        if (Test-Shortcut $lnk) {
            Remove-Item -LiteralPath $lnk -Force
            Write-Host ("removed  " + $lnk)
        } else {
            Write-Host ("absent   " + $lnk)
        }
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $InstallDir)) { throw "InstallDir not found: $InstallDir" }
if (-not (Test-Path -LiteralPath $start)) { throw "start-all.bat not found in $InstallDir - is that the StudioFire install root?" }
if (-not (Test-Path -LiteralPath $stop)) { throw "stop-all.bat not found in $InstallDir - is that the StudioFire install root?" }
if (-not (Test-Path -LiteralPath $icon)) { throw "sf_icon_dark.ico not found at $icon - copy web\static\sf_icon_dark.ico from the repo first" }

if (-not (Test-Path -LiteralPath $DesktopDir)) { New-Item -ItemType Directory -Path $DesktopDir | Out-Null }

foreach ($it in $items) {
    $lnkPath = Join-Path $DesktopDir ($it.Name + '.lnk')
    $s = $shell.CreateShortcut($lnkPath)
    $s.TargetPath       = $it.Target
    $s.WorkingDirectory = $InstallDir
    $s.IconLocation     = "$icon,0"
    $s.Description      = $it.Desc
    $s.WindowStyle      = 7          # minimised: the .bat opens consoles the operator does not need
    $s.Save()
    Write-Host ("created  " + $lnkPath)
}

# read back and report, so a silent failure cannot pass for success
foreach ($it in $items) {
    $lnkPath = Join-Path $DesktopDir ($it.Name + '.lnk')
    if (-not (Test-Path -LiteralPath $lnkPath)) { throw "shortcut was not created: $lnkPath" }
    $v = $shell.CreateShortcut($lnkPath)
    Write-Host ("  {0} -> {1}  (icon {2})" -f $it.Name, $v.TargetPath, $v.IconLocation)
}
Write-Host ('done - ' + $DesktopDir)
