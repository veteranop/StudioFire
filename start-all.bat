@echo off
REM ============================================================
REM  StudioFire - start all services (dry-run / dev box).
REM  For the real on-air PC, run these as auto-restarting
REM  Windows services via NSSM instead (see DEPLOY.md).
REM
REM  Uses "python" from PATH. If Anaconda isn't on PATH, set it:
REM     set PYTHON=C:\Users\<you>\anaconda3\python.exe
REM  ...before running, or edit the line below.
REM ============================================================
setlocal
cd /d "%~dp0"
if "%PYTHON%"=="" set "PYTHON=python"
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

echo Starting P1 audio engine (the only process that must stay alive)...
start "StudioFire Engine (P1)" cmd /k ""%PYTHON%" -m services.engine.main "%CFG%""
timeout /t 2 >nul
echo Starting P2 web + GUI...
start "StudioFire Web (P2)" cmd /k ""%PYTHON%" -m services.core.main "%CFG%""
timeout /t 2 >nul
echo Starting P3 library indexer...
start "StudioFire Indexer (P3)" cmd /k ""%PYTHON%" -m services.worker.main "%CFG%""

echo.
echo All three launched in their own windows.
echo   Web GUI:  http://localhost:8080   (first visit creates the admin login)
echo   Stop all: run stop-all.bat, or just close the three windows.
endlocal
