@echo off
REM ============================================================
REM  StudioFire service wrapper (NSSM runs this, one per service):
REM    svc-run.bat services.engine.main
REM  A Windows service session has NO user drive mappings, so if
REM  config\drive-map.bat exists it runs first to map the NAS
REM  (e.g.  net use Z: \\KDPI-Media\music /persistent:no ).
REM  Copy config\drive-map.example.bat and edit. The service must
REM  log on as a real user (services.msc -> Log On) so the NAS
REM  accepts its credentials.
REM ============================================================
setlocal
cd /d "%~dp0.."
if exist config\drive-map.bat call config\drive-map.bat >nul 2>&1
if "%PYTHON%"=="" (
  if exist "%~dp0..\runtime\python.exe" (
    set "PYTHON=%~dp0..\runtime\python.exe"
  ) else if exist "%USERPROFILE%\anaconda3\python.exe" (
    set "PYTHON=%USERPROFILE%\anaconda3\python.exe"
  ) else (
    set "PYTHON=python"
  )
)
"%PYTHON%" -u -m %1 config\config.json
