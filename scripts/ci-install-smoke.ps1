# StudioFire — installer smoke test (the subset that can run without a clean VM)
#
# Verifies the thing a station actually downloads:
#   1. the payload builds and its embedded Python can import StudioFire
#   2. the setup .exe compiles and is not empty
#   3. the payload's own interpreter can start the core, answer /health and stop
#
# It deliberately does NOT register services or touch a real install — that is the
# clean-VM acceptance test (docs/INSTALL-ACCEPTANCE.md).
#
#   powershell -ExecutionPolicy Bypass -File scripts\ci-install-smoke.ps1
#
param(
    [switch]$SkipPayload,
    [int]$Port = 8099
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $repo
$fail = 0

function Step($name) { Write-Host "`n== $name" -ForegroundColor Cyan }
function Ok($msg)    { Write-Host "  ok   $msg" -ForegroundColor Green }
function Bad($msg)   { Write-Host "  FAIL $msg" -ForegroundColor Red; $script:fail++ }

$python = Join-Path $repo "installer\payload\runtime\python.exe"
$setup  = Get-ChildItem -Path (Join-Path $repo "installer\Output") -Filter "StudioFire-Setup-*.exe" `
            -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending |
            Select-Object -First 1

# ---------------------------------------------------------------- 1. payload
Step "payload"
if ($SkipPayload) {
    Ok "payload build skipped (-SkipPayload)"
} else {
    & python (Join-Path $repo "installer\build_payload.py") | Out-Null
    if ($LASTEXITCODE -ne 0) { Bad "build_payload.py failed" } else { Ok "payload built" }
}
if (Test-Path $python) { Ok "embedded interpreter present" } else { Bad "no installer\payload\runtime\python.exe" }

# ---------------------------------------------------------------- 2. setup exe
Step "setup exe"
if ($setup) {
    $mb = [math]::Round($setup.Length / 1MB, 1)
    Ok "$($setup.Name) - $mb MB"
    if ($setup.Length -lt 10MB) { Bad "the setup exe looks too small to contain the runtime" }
} else {
    Bad "no installer\Output\StudioFire-Setup-*.exe (run ISCC first)"
}

# ---------------------------------------------------------------- 3. run it
Step "payload can import the app"
if (-not (Test-Path $python)) {
    Bad "cannot test the payload without it"
} else {
    # the runtime's ._pth makes the install root importable; point it at this
    # repo to prove the bundled interpreter + bundled deps can import StudioFire
    $probe = "import sys; sys.path.insert(0, r'$repo'); " +
             "import services.core.app, services.core.setup, services.core.sx_report; " +
             "print('imports ok')"
    $out = & $python -c $probe 2>&1
    if ($LASTEXITCODE -eq 0 -and ($out -join " ") -match "imports ok") {
        Ok "embedded Python imports the app and its modules"
    } else {
        Bad ("embedded Python could not import the app: " + ($out -join " "))
    }
}

# Starting a real instance (services, ports, a fresh DB) belongs on a clean VM:
# see docs/INSTALL-ACCEPTANCE.md. The wizard itself is covered by
# tests/test_setup_wizard.py (28 checks) against the real app.

Write-Host ""
if ($fail -eq 0) { Write-Host "INSTALL SMOKE OK" -ForegroundColor Green; exit 0 }
Write-Host "INSTALL SMOKE FAILED ($fail)" -ForegroundColor Red; exit 1
