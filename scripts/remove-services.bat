@echo off
REM Stop and unregister the StudioFire Windows services (NSSM).
REM Run as Administrator. Leaves files, config, and data in place.
setlocal
cd /d "%~dp0.."
set "NSSM=%CD%\bin\nssm.exe"
if not exist "%NSSM%" (
  echo [!] %NSSM% not found - nothing to remove.
  exit /b 0
)
for %%S in (StudioFireWorker StudioFireWeb StudioFireEngine) do (
  "%NSSM%" stop   %%S >nul 2>&1
  "%NSSM%" remove %%S confirm >nul 2>&1
  echo [ok] %%S removed
)
exit /b 0
