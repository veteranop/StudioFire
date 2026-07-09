@echo off
REM Restart the NSSM-managed StudioFire services (run as Administrator).
REM Engine last down / first up keeps the off-air gap minimal.
setlocal
cd /d "%~dp0.."
set "NSSM=%CD%\bin\nssm.exe"
if not exist "%NSSM%" (
  echo [!] %NSSM% not found - are the services installed? See install-services.bat
  exit /b 1
)
for %%S in (StudioFireWorker StudioFireWeb StudioFireEngine) do (
  "%NSSM%" stop %%S >nul 2>&1
)
for %%S in (StudioFireEngine StudioFireWeb StudioFireWorker) do (
  "%NSSM%" start %%S >nul 2>&1
  echo [ok] %%S restarted
)
exit /b 0
