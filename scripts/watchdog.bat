@echo off
REM ============================================================
REM  StudioFire watchdog - the action of the "StudioFire-Watchdog"
REM  scheduled task (every 5 minutes, INTERACTIVE session).
REM
REM  Runs the health check. If the station is unhealthy it restarts
REM  the stack ONCE - but only after TWO consecutive failures, so a
REM  single hiccup cannot drop audio for no reason.
REM
REM  It restarting the stack means console mode in THIS session.
REM  It never touches NSSM or Windows services (a service-hosted
REM  engine cannot play audio).
REM
REM  Limits, stated honestly: this only proves the web layer and the
REM  engine's own idea of itself. It cannot hear the transmitter. The
REM  silence-aware watchdog has to live OFF this box.
REM ============================================================
setlocal
cd /d "%~dp0.."
set "APP=%CD%"
set "LOG=%APP%\logs\watchdog.log"
set "CNT=%APP%\logs\watchdog_fails.txt"
if not exist "%APP%\logs" mkdir "%APP%\logs"

if "%PYTHON%"=="" (
  if exist "%APP%\runtime\python.exe" (
    set "PYTHON=%APP%\runtime\python.exe"
  ) else if exist "%USERPROFILE%\anaconda3\python.exe" (
    set "PYTHON=%USERPROFILE%\anaconda3\python.exe"
  ) else (
    set "PYTHON=python"
  )
)

echo ---- %DATE% %TIME% ---- >> "%LOG%"
"%PYTHON%" "%APP%\scripts\healthcheck.py" >> "%LOG%" 2>&1
if not errorlevel 1 (
  if exist "%CNT%" del "%CNT%" 2>nul
  exit /b 0
)

REM ---- unhealthy: count how many checks in a row have failed ----------
set "N=0"
if exist "%CNT%" set /p N=<"%CNT%"
set /a N=N+1
> "%CNT%" echo %N%
echo NOT HEALTHY - failure %N% in a row >> "%LOG%"

if %N% LSS 2 (
  echo one more failure and it will restart the stack >> "%LOG%"
  exit /b 1
)

echo RESTARTING the stack in this session ^(console mode^) >> "%LOG%"
if exist "%APP%\stop-all.bat" call "%APP%\stop-all.bat" >> "%LOG%" 2>&1
ping -n 6 127.0.0.1 >nul
if exist "%APP%\start-all.bat" call "%APP%\start-all.bat" >> "%LOG%" 2>&1
if exist "%CNT%" del "%CNT%" 2>nul
echo restart attempted >> "%LOG%"
exit /b 1
