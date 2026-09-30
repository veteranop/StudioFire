@echo off
REM ============================================================
REM  StudioFire - ONE-SHOT AUTO-START SETUP  (run as Administrator)
REM
REM  Makes the station come back by itself after a power bump or a
REM  Windows restart - no login, no hand-holding, no stop/start .bat.
REM
REM  It registers the three services with NSSM so they
REM      * start automatically at boot, and
REM      * restart themselves if they crash,
REM  sets each service's Log On account (a service session has no
REM  user profile and no NAS credentials, so LocalSystem starts but
REM  finds no music), starts them, and then VERIFIES they came up.
REM
REM  Usage (on the on-air PC, elevated):
REM      scripts\install-services.bat                        (prompts)
REM      scripts\install-services.bat .\kdpi MyPassword       (unattended)
REM      scripts\install-services.bat -check                 (no changes)
REM
REM  Undo:  scripts\remove-services.bat
REM  Stop/start by hand:  STOP-ON-AIR.bat / GO-LIVE.bat
REM ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0.."
set "APP=%CD%"
set "CHECK="
set "FAILED="
if /i "%~1"=="/check" set "CHECK=1"
if /i "%~1"=="-check" set "CHECK=1"

echo.
echo  StudioFire auto-start setup
echo  Install root: %APP%
echo  ------------------------------------------------------------
if defined CHECK echo  MODE: check only - nothing will be changed.
echo.

REM ---- must be elevated -------------------------------------------------
fltmc >nul 2>&1
if errorlevel 1 (
  echo [X]  This needs Administrator rights.
  echo     Close this window, right-click this .bat, then "Run as administrator".
  if not defined CHECK pause
  exit /b 1
)

REM ---- DO NOT USE THIS ON A BOX THAT PLAYS AUDIO ------------------------
REM A Windows service runs in session 0, which has NO audio endpoint: the engine
REM starts, mpv starts, and every track then fails with "audio output
REM initialization failed" - while this script reports success and services.msc
REM shows the service Running. Proven on .200, 2026-09-29: 3,529 failed tracks
REM and a silent station with green health. Use install-autostart.bat instead.
if /i not "%~1"=="-i-understand-no-audio" if not defined CHECK (
  echo.
  echo ============================================================
  echo  STOP - THIS SCRIPT BREAKS AUDIO ON A STATION
  echo ============================================================
  echo  A service runs in session 0, which has no audio device. The engine will
  echo  start, play nothing, and this script will still tell you it succeeded.
  echo  That is exactly what silenced .200 on 2026-09-29.
  echo.
  echo  For auto-start that keeps audio working, run this instead:
  echo      scripts\install-autostart.bat
  echo  ^(autologon + a logon task that starts start-all.bat in the session where
  echo   audio actually works. Decision record: docs\AUTOSTART-CONSENSUS.md^)
  echo.
  echo  If you really want the services anyway ^(e.g. a helper box that never
  echo  makes sound^), re-run as:
  echo      scripts\install-services.bat -i-understand-no-audio
  echo.
  if not defined CHECK pause
  exit /b 1
)

REM ---- what we need on disk ---------------------------------------------
set "NSSM=%APP%\bin\nssm.exe"
set "FATAL=0"
if exist "%NSSM%" (echo [ok] NSSM            : bin\nssm.exe) else (echo [X]  NSSM MISSING    : bin\nssm.exe & set "FATAL=1")
if exist "%APP%\config\config.json" (echo [ok] config          : config\config.json) else (echo [X]  config MISSING  : config\config.json & set "FATAL=1")
if exist "%APP%\bin\mpv.exe" (echo [ok] audio engine    : bin\mpv.exe) else (echo [X]  mpv MISSING     : bin\mpv.exe & set "FATAL=1")
if exist "%APP%\scripts\svc-run.bat" (echo [ok] service wrapper : scripts\svc-run.bat) else (echo [X]  wrapper MISSING : scripts\svc-run.bat & set "FATAL=1")

REM interpreter, exactly like start-all.bat / svc-run.bat
if "%PYTHON%"=="" (
  if exist "%APP%\runtime\python.exe" (
    set "PYTHON=%APP%\runtime\python.exe"
  ) else if exist "%USERPROFILE%\anaconda3\python.exe" (
    set "PYTHON=%USERPROFILE%\anaconda3\python.exe"
  ) else (
    set "PYTHON=python"
  )
)
echo [ok] interpreter     : %PYTHON%

if "%FATAL%"=="1" (
  echo.
  echo [X]  Fix the MISSING items above and run this again.
  if not defined CHECK pause
  exit /b 1
)
echo.

REM ---- who should the services run as? ----------------------------------
REM A Windows service runs in its own session: no drive letters, no user
REM profile, no NAS credentials. LocalSystem therefore starts fine and then
REM finds no music (learned 2026-07-08). Use the box's own account.
set "SVCUSER="
set "SVCPASS="
REM the account is normally arg1, or arg2 when arg1 is the check flag
if defined CHECK (
  if not "%~2"=="" set "SVCUSER=%~2"
) else (
  if not "%~1"=="" set "SVCUSER=%~1"
  if not "%~2"=="" set "SVCPASS=%~2"
)
if defined CHECK goto :skipaccount
if "%SVCUSER%"=="" (
  echo  Which Windows account should the services run as?
  echo  It must be an account that can read the music library on the NAS.
  set /p "SVCUSER=  Account [.\%USERNAME%]: "
  if "!SVCUSER!"=="" set "SVCUSER=.\%USERNAME%"
)
if /i "%SVCUSER%"=="LocalSystem" (
  echo [X]  LocalSystem cannot read the NAS - the station would come up and find
  echo      no music. Use the box's real account instead.
  if not defined CHECK pause
  exit /b 1
)
if "%SVCPASS%"=="" (
  echo.
  echo  Password for %SVCUSER%
  echo  ^(typed at this console only - it is NOT stored in this .bat or anywhere
  echo   in the repo. Windows saves it against the service^).
  set /p "SVCPASS=  Password: "
)
if "%SVCPASS%"=="" (
  echo [X]  No password given. Set it afterwards in services.msc ^(Log On tab^),
  echo     or the services will sit "Paused" and the station stays off air.
)
:skipaccount

if defined CHECK (
  echo.
  echo  Would register : StudioFireEngine, StudioFireWeb, StudioFireWorker
  echo  Start mode     : automatic at boot  +  restart on crash ^(2s delay^)
  if "!SVCUSER!"=="" (
    echo  Log On         : ^(asked at run time - none passed to this check^)
  ) else (
    echo  Log On         : !SVCUSER!
  )
  echo  Service logs   : logs\StudioFire*_service.log
  echo.
  echo  Check done - nothing was changed.
  exit /b 0
)

REM ---- stop anything already running so the two don't fight ------------
REM Console mode (start-all.bat) and service mode both open port 8080 and both
REM start an mpv - running both would give you two engines on one station.
echo Stopping any console-mode stack first ^(a few seconds of audio^)...
if exist "%APP%\scripts\quit_all.py" (
  "%PYTHON%" "%APP%\scripts\quit_all.py" >nul 2>&1
)
call :sleep 3

echo.
echo Registering the three services...
if not exist "%APP%\logs" mkdir "%APP%\logs"
call :one StudioFireEngine services.engine.main
call :one StudioFireWeb    services.core.main
call :one StudioFireWorker services.worker.main

if defined FAILED (
  echo.
  echo [X]  A service could not be registered - see the message above.
  echo     Nothing was started. The station is still off air; to get it back
  echo     right now run GO-LIVE.bat, then tell Mark.
  if not defined CHECK pause
  exit /b 1
)

REM ---- bring them up: engine first, then web, then indexer ------------
echo.
echo Starting...
for %%S in (StudioFireEngine StudioFireWeb StudioFireWorker) do (
  sc start %%S >nul 2>&1
  call :await %%S
)

echo.
echo ------------------------------------------------------------
echo Verifying the station is actually up and ON AIR...
echo ------------------------------------------------------------
set "HC=-1"
if exist "%APP%\scripts\healthcheck.py" (
  "%PYTHON%" "%APP%\scripts\healthcheck.py"
  set "HC=!errorlevel!"
) else (
  echo [X]  scripts\healthcheck.py not found - skipping the live check.
)
echo.
if "!HC!"=="0" (
  echo [ok] Health check passed - the station is up and playing real audio.
) else (
  echo [X]  Health check did NOT pass. Do not leave it like this.
  echo      - Look at logs\StudioFireEngine_service.log and logs\core_error.log
  echo      - If a service shows PAUSED, its Log On account/password is wrong.
  echo      - To get back on air right now: run GO-LIVE.bat, then tell Mark.
)

if defined SVCACCT_FAIL (
  echo.
  echo [X]  At least one service could NOT be given its Log On account, so it
  echo      may sit PAUSED and the station would stay off air.
  echo      Fix it per service: services.msc -^> the StudioFire* service -^>
  echo      Log On tab -^> "This account" -^> the box's user + password -^> Apply,
  echo      then run GO-LIVE.bat.
)

echo.
echo ============================================================
echo  AUTO-START IS ON
echo ============================================================
echo  After a power cut or a Windows restart this box now brings the
echo  station back by itself: the three services start at boot ^(no login
echo  needed^), and each one restarts itself if it crashes.
echo.
echo  Web GUI   : http://localhost:8080      from the LAN: http://%COMPUTERNAME%:8080
echo  Health    : healthcheck.bat   ^(ON AIR vs. stuck on emergency filler^)
echo  Stop      : STOP-ON-AIR.bat
echo  Start     : GO-LIVE.bat
echo  Undo all  : scripts\remove-services.bat
echo  Service logs: logs\StudioFireEngine_service.log, logs\StudioFireWeb_service.log,
echo                logs\StudioFireWorker_service.log
echo.
echo  [X]  Console mode ^(start-all.bat^) and service mode are not meant to run
echo      together. Pick service mode on the on-air PC.
echo.
pause
exit /b 0

REM ============================================================
:sleep
REM seconds-ish pause that also works when stdin is redirected (unlike timeout)
ping -n %1 127.0.0.1 >nul
goto :eof

REM ============================================================
:one
REM register/replace one service via the wrapper that maps the NAS first
"%NSSM%" stop   %1 >nul 2>&1
"%NSSM%" remove %1 confirm >nul 2>&1
"%NSSM%" install %1 "%APP%\scripts\svc-run.bat" %2
if errorlevel 1 (
  echo   [X]  %1 could NOT be registered ^(reason above^).
  set "FAILED=1"
  goto :eof
)
"%NSSM%" set %1 AppDirectory "%APP%"
"%NSSM%" set %1 DisplayName "%1"
"%NSSM%" set %1 Description "StudioFire radio automation (%2)"
"%NSSM%" set %1 AppStdout "%APP%\logs\%1_service.log"
"%NSSM%" set %1 AppStderr "%APP%\logs\%1_service.log"
"%NSSM%" set %1 AppRotateFiles 1
"%NSSM%" set %1 AppRotateOnline 1
"%NSSM%" set %1 AppRotateBytes 5242880
REM the whole point of this script: come back by itself, and stay back
"%NSSM%" set %1 Start SERVICE_AUTO_START
"%NSSM%" set %1 AppExit Default Restart
"%NSSM%" set %1 AppRestartDelay 2000
"%NSSM%" set %1 AppThrottle 5000
REM Log On as the real account (else it starts and finds no music).
REM This is the step that decides whether the station actually reads the NAS,
REM so say so out loud if Windows refuses the account or the password.
if not "%SVCUSER%"=="" (
  "%NSSM%" set %1 ObjectName "%SVCUSER%" "%SVCPASS%" >nul 2>&1
  if errorlevel 1 (
    echo   [X]  %1: could not set the Log On account to %SVCUSER%.
    echo        Usually a wrong password, or the account lacks the
    echo        "Log on as a service" right. The service may sit PAUSED.
    set "SVCACCT_FAIL=1"
  )
)
echo   [ok] %1 registered ^(auto-start + restart-on-crash^)
goto :eof

REM ============================================================
:await
REM wait up to ~24s for the service to report RUNNING, then say what happened.
REM State comes from sc (native, always present) rather than nssm, so nothing
REM here can pop a dialog and stall an unattended run.
set "ST="
for /l %%I in (1,1,12) do (
  for /f "tokens=4" %%S in ('sc query %1 ^| findstr /i "STATE"') do set "ST=%%S"
  if /i "!ST!"=="RUNNING" goto :await_ok
  if /i "!ST!"=="PAUSED" goto :await_report
  call :sleep 3
)
goto :await_report

:await_ok
echo   %1 : RUNNING
goto :eof

:await_report
if "!ST!"=="" set "ST=not registered"
echo   %1 : !ST!
if /i "!ST!"=="PAUSED" (
  echo        ^<- PAUSED. Almost always the Log On account/password. Fix it in
  echo          services.msc ^(Log On tab^) or re-run this with the right password.
)
if /i "!ST!"=="STOPPED" (
  echo        ^<- did not stay up. Read logs\%1_service.log and logs\*_error.log.
)
if "!ST!"=="not registered" (
  echo        ^<- Windows does not know this service. The registration above failed.
)
goto :eof
