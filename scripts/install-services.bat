@echo off
REM ============================================================
REM  StudioFire - register the three services with NSSM so they
REM  auto-start at boot and auto-restart on crash (production).
REM  Run as Administrator. Safe to re-run (reinstalls cleanly).
REM  Uses the bundled runtime\python.exe if present (installer
REM  boxes), else falls back like start-all.bat.
REM ============================================================
setlocal
cd /d "%~dp0.."
set "APP=%CD%"
set "NSSM=%APP%\bin\nssm.exe"
if not exist "%NSSM%" (
  echo [!] %NSSM% not found. See installer\README.md.
  exit /b 1
)
if "%PYTHON%"=="" (
  if exist "%APP%\runtime\python.exe" (
    set "PYTHON=%APP%\runtime\python.exe"
  ) else if exist "%USERPROFILE%\anaconda3\python.exe" (
    set "PYTHON=%USERPROFILE%\anaconda3\python.exe"
  ) else (
    set "PYTHON=python"
  )
)
if not exist "%APP%\logs" mkdir "%APP%\logs"

REM P1 first: the engine is the process that must always come back.
call :one StudioFireEngine services.engine.main
call :one StudioFireWeb    services.core.main
call :one StudioFireWorker services.worker.main

echo.
echo Services installed and started. Manage with:  services.msc
echo   or:  bin\nssm.exe status StudioFireEngine
exit /b 0

:one
"%NSSM%" stop   %1 >nul 2>&1
"%NSSM%" remove %1 confirm >nul 2>&1
"%NSSM%" install %1 "%PYTHON%" -u -m %2 "%APP%\config\config.json"
"%NSSM%" set %1 AppDirectory "%APP%"
"%NSSM%" set %1 DisplayName "%1"
"%NSSM%" set %1 Description "StudioFire radio automation (%2)"
"%NSSM%" set %1 AppStdout "%APP%\logs\%1_service.log"
"%NSSM%" set %1 AppStderr "%APP%\logs\%1_service.log"
"%NSSM%" set %1 AppRotateFiles 1
"%NSSM%" set %1 AppRotateOnline 1
"%NSSM%" set %1 AppRotateBytes 5242880
"%NSSM%" set %1 Start SERVICE_AUTO_START
"%NSSM%" set %1 AppExit Default Restart
"%NSSM%" set %1 AppRestartDelay 2000
"%NSSM%" start %1
echo [ok] %1
goto :eof
