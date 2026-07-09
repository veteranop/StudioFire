@echo off
REM ============================================================
REM  StudioFire - start all services (dry-run / dev box).
REM  For the real on-air PC, run these as auto-restarting
REM  Windows services via NSSM instead (see DEPLOY.md).
REM
REM  Picks the interpreter in this order: an explicit %PYTHON% (the GUI restart
REM  passes P2's own Anaconda interpreter), then a per-user Anaconda install,
REM  then bare "python". The Windows Store `python` stub on PATH lacks our deps,
REM  so we don't rely on PATH. Override any time:  set PYTHON=C:\path\python.exe
REM ============================================================
setlocal
cd /d "%~dp0"
if "%PYTHON%"=="" (
  if exist "%~dp0runtime\python.exe" (
    REM installer-bundled embedded Python (customer boxes)
    set "PYTHON=%~dp0runtime\python.exe"
  ) else if exist "%USERPROFILE%\anaconda3\python.exe" (
    set "PYTHON=%USERPROFILE%\anaconda3\python.exe"
  ) else (
    set "PYTHON=python"
  )
)
set "CFG=config\config.json"

if not exist "%CFG%" (
  echo [!] %CFG% not found. Copy config\config.example.json to config\config.json
  echo     and edit it first. See DEPLOY.md.
  pause
  exit /b 1
)
if not exist "bin\mpv.exe" (
  echo [!] bin\mpv.exe not found. The audio engine needs it. See DEPLOY.md.
  pause
  exit /b 1
)

REM Capture each service's stdout+stderr to logs\<svc>_console.log (append, so a
REM crash log survives the next restart - matches scripts\launch_detached.py). The
REM windows stay open but stay quiet; tail the logs or run healthcheck.bat instead.
REM Python -u keeps output unbuffered so tracebacks land in the log immediately.
if not exist logs mkdir logs

echo Starting P1 audio engine (the only process that must stay alive)...
start "StudioFire Engine (P1)" cmd /k ""%PYTHON%" -u -m services.engine.main "%CFG%" >> "logs\engine_console.log" 2>&1"
timeout /t 2 >nul
echo Starting P2 web + GUI...
start "StudioFire Web (P2)" cmd /k ""%PYTHON%" -u -m services.core.main "%CFG%" >> "logs\core_console.log" 2>&1"
timeout /t 2 >nul
echo Starting P3 library indexer...
start "StudioFire Indexer (P3)" cmd /k ""%PYTHON%" -u -m services.worker.main "%CFG%" >> "logs\worker_console.log" 2>&1"

echo.
echo All three launched in their own windows.
echo   Web GUI:  http://localhost:8080   (first visit creates the admin login)
echo   Health:   run healthcheck.bat  (is it on air, or stuck on emergency filler?)
echo   Logs:     logs\engine_console.log  logs\core_console.log  logs\worker_console.log
echo   Stop all: run stop-all.bat, or just close the three windows.
endlocal
