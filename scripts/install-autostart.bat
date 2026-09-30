@echo off
REM ============================================================
REM  StudioFire - AUTO-START SETUP (autologon + logon task)
REM  Run as Administrator, ON THE BOX.
REM
REM  WHY THIS AND NOT A WINDOWS SERVICE
REM  A service runs in Windows "session 0", which has NO audio
REM  endpoint. The engine starts, mpv starts, and every track then
REM  fails with "audio output initialization failed" while the web
REM  UI and services.msc still report healthy. That is proven on
REM  our own .200 box (3,529 failed tracks, 2026-09-29) and it is a
REM  Windows platform limit, not a StudioFire bug. Audio only exists
REM  in an interactive user session.
REM
REM  So: the box logs itself in at boot (autologon), and a Task
REM  Scheduler task "at log on, run only when the user is logged on"
REM  starts start-all.bat in that session - audio works, no human
REM  needed after a power bump or a reboot.
REM
REM  Decision record: docs\AUTOSTART-CONSENSUS.md (Rosie + Elon, 8.7/8.7)
REM
REM  Usage:
REM    scripts\install-autostart.bat                 (prompts)
REM    scripts\install-autostart.bat -check          (no changes)
REM    scripts\install-autostart.bat .\kdpi MyPass    (unattended)
REM    scripts\install-autostart.bat -no-autologon    (tasks only)
REM
REM  Undo: scripts\remove-autostart.bat
REM ============================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0.."
set "APP=%CD%"
set "CHECK="
set "NOAUTO="
set "FAILED="
if /i "%~1"=="-check" set "CHECK=1"
if /i "%~1"=="/check" set "CHECK=1"
if /i "%~1"=="-no-autologon" set "NOAUTO=1"

echo.
echo  StudioFire auto-start setup  ^(autologon + logon task^)
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

REM ---- REFUSE if the session-0 services are installed ----------------
REM Services + console mode would fight over port 8080 and the engine, and a
REM service-hosted engine is silent anyway. Remove them first.
set "SVCPRESENT="
sc query StudioFireEngine >nul 2>&1 && set "SVCPRESENT=1"
if defined SVCPRESENT (
  echo [X]  The StudioFire Windows SERVICES are installed on this box.
  echo      A service-hosted engine CANNOT play audio ^(session 0 has no audio
  echo      device^) - that is why .200 went silent on 2026-09-29.
  echo.
  echo      Remove them first, then run this script again:
  echo          scripts\remove-services.bat
  echo      ...and after that, start the station in console mode with
  echo      GO-LIVE.bat before relying on it.
  echo.
  exit /b 1
)
echo [ok] no session-0 services installed

REM ---- what we need on disk ---------------------------------------------
set "FATAL=0"
if exist "%APP%\start-all.bat" (echo [ok] start-all.bat   : %APP%\start-all.bat) else (echo [X]  start-all.bat MISSING - run this from the StudioFire install root & set "FATAL=1")
if exist "%APP%\scripts\healthcheck.py" (echo [ok] healthcheck.py  : scripts\healthcheck.py) else (echo [X]  scripts\healthcheck.py MISSING & set "FATAL=1")
if exist "%APP%\scripts\watchdog.bat" (echo [ok] watchdog.bat    : scripts\watchdog.bat) else (echo [X]  scripts\watchdog.bat MISSING & set "FATAL=1")
if exist "%APP%\config\config.json" (echo [ok] config          : config\config.json) else (echo [X]  config MISSING    : config\config.json & set "FATAL=1")
if "%FATAL%"=="1" (
  echo.
  echo [X]  Fix the MISSING items above and run this again.
  if not defined CHECK pause
  exit /b 1
)
echo.

REM ---- which account logs the box in and runs the station? --------------
REM It must be the account that (a) can read the music library and (b) owns the
REM desktop session the audio comes out of. Normally the box's own user.
set "PLUSER="
set "PLPASS="
if defined CHECK (
  if not "%~2"=="" set "PLUSER=%~2"
) else (
  if not "%~1"=="" if not "%~1"=="-no-autologon" set "PLUSER=%~1"
  if not "%~2"=="" set "PLPASS=%~2"
)
if "%PLUSER%"=="" (
  echo  Which Windows account should the station run as?
  set /p "PLUSER=  Account [.\%USERNAME%]: "
  if "!PLUSER!"=="" set "PLUSER=.\%USERNAME%"
)
REM split "DOMAIN\user" / ".\user" / "user" into domain + account name.
REM ".\user" and a bare "user" both mean the LOCAL machine account.
set "PLDOMAIN=%COMPUTERNAME%"
set "PLNAME=%PLUSER%"
for /f "tokens=1,2 delims=\" %%A in ("%PLUSER%") do (
  if not "%%B"=="" set "PLDOMAIN=%%A"
  if not "%%B"=="" set "PLNAME=%%B"
)
if "%PLDOMAIN%"=="." set "PLDOMAIN=%COMPUTERNAME%"

if defined CHECK (
  echo.
  echo  Would create : Task "StudioFire-Autostart"  - at log on of %PLUSER%,
  echo                 run only when that user is logged on, waits 45s, then
  echo                 runs %APP%\start-all.bat
  echo  Would create : Task "StudioFire-Watchdog"   - every 5 minutes, runs
  echo                 scripts\watchdog.bat ^(health check; restarts the stack
  echo                 once if it stays unhealthy twice in a row^)
  if defined NOAUTO (
    echo  Would skip   : autologon ^(asked not to^)
  ) else (
    echo  Would set    : autologon for %PLUSER% so the box logs itself in at boot
  )
  echo.
  echo  Check done - nothing was changed.
  exit /b 0
)

REM ---- autologon ---------------------------------------------------------
REM Prefer the Sysinternals tool when it is present: it stores the password as an
REM LSA secret. The plain registry method stores it in CLEARTEXT, which is why we
REM warn and ask before using it.
if defined NOAUTO goto :tasks

set "SYSAUTO="
if exist "%APP%\bin\autologon64.exe" set "SYSAUTO=%APP%\bin\autologon64.exe"
if not defined SYSAUTO if exist "%APP%\bin\autologon.exe" set "SYSAUTO=%APP%\bin\autologon.exe"

if defined SYSAUTO (
  echo Enabling autologon via Sysinternals Autologon ^(password kept as an LSA
  echo secret^)...
  "%SYSAUTO%" -accepteula "%PLNAME%" "%PLDOMAIN%" "%PLPASS%"
  if errorlevel 1 (
    echo [X]  Sysinternals Autologon refused. Nothing else was changed.
    echo      Check the account name/password, or run it by hand once.
    pause
    exit /b 1
  )
  echo [ok] autologon set for %PLUSER%
  goto :tasks
)

echo.
echo  ------------------------------------------------------------
echo  AUTOLOGON - please read
echo  ------------------------------------------------------------
echo  No Sysinternals Autologon found in bin\.
echo  The fallback writes the Windows registry method, which stores the
echo  password in CLEARTEXT at
echo      HKLM\...\Winlogon\DefaultPassword
echo  Anyone who can read the registry on this box can read that password.
echo  Microsoft only recommends this on a physically secured machine.
echo.
echo  If you want the password stored as an LSA secret instead, cancel now,
echo  put Sysinternals autologon64.exe in %APP%\bin\ and run this again.
echo.
set "GO="
set /p "GO=  Type YES to use the cleartext method anyway: "
if /i not "%GO%"=="YES" (
  echo [X]  Cancelled. Nothing was changed. Try: -no-autologon  to set up the
  echo     tasks only, and enable autologon yourself.
  pause
  exit /b 1
)
if "%PLPASS%"=="" (
  set /p "PLPASS=  Password for %PLNAME%: "
)
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v AutoAdminLogon /t REG_SZ /d 1 /f >nul
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultUserName /t REG_SZ /d "%PLNAME%" /f >nul
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultDomainName /t REG_SZ /d "%PLDOMAIN%" /f >nul
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultPassword /t REG_SZ /d "%PLPASS%" /f >nul
if errorlevel 1 (
  echo [X]  Could not write the autologon registry values.
  exit /b 1
)
echo [ok] autologon set for %PLUSER% ^(cleartext method - you were warned^)

REM ---- scheduled tasks ---------------------------------------------------
:tasks
echo.
echo Creating the scheduled tasks...
schtasks /create /tn "StudioFire-Autostart" /tr "\"%APP%\start-all.bat\"" /sc onlogon /ru "%PLUSER%" /it /delay 0000:45 /f >nul
if errorlevel 1 (
  echo   [X]  StudioFire-Autostart could NOT be created ^(see the error above^).
  set "FAILED=1"
) else (
  echo   [ok] StudioFire-Autostart  - at log on of %PLUSER%, interactive, 45s delay
)
schtasks /create /tn "StudioFire-Watchdog" /tr "\"%APP%\scripts\watchdog.bat\"" /sc minute /mo 5 /ru "%PLUSER%" /it /f >nul
if errorlevel 1 (
  echo   [X]  StudioFire-Watchdog could NOT be created ^(see the error above^).
  set "FAILED=1"
) else (
  echo   [ok] StudioFire-Watchdog   - every 5 minutes, interactive
)

if defined FAILED (
  echo.
  echo [X]  A task could not be created - nothing else was attempted.
  echo     Check the account name you gave ^(%PLUSER%^) and that it can log on
  echo     to this machine, then run this again.
  pause
  exit /b 1
)

echo.
echo ------------------------------------------------------------
echo Verifying...
echo ------------------------------------------------------------
schtasks /query /tn "StudioFire-Autostart" /v /fo LIST 2>nul | findstr /i "TaskName Logon Mode Status Next Run"
schtasks /query /tn "StudioFire-Watchdog" /v /fo LIST 2>nul | findstr /i "TaskName Logon Mode Status Next Run"
echo.
echo  Logon Mode should say "Interactive only" - that is what puts the station
echo  in the session where audio works. If it says "Interactive/Background" or
echo  something else, tell Mark: the task will run in session 0 and be silent.
echo.

echo ============================================================
echo  AUTO-START IS CONFIGURED
echo ============================================================
echo  What happens after a power bump or a restart, with nobody touching it:
echo    1. the box logs itself in as %PLUSER%
echo    2. 45 seconds later StudioFire-Autostart runs start-all.bat
echo    3. audio comes out of that logged-in session ^(where it works^)
echo    4. StudioFire-Watchdog checks every 5 minutes and restarts the stack
echo       if it is unhealthy twice in a row
echo.
echo  NOW TEST IT - this is the only proof that matters:
echo    1. confirm the station is playing right now
echo    2. reboot the box and do NOT touch the keyboard
echo    3. confirm it comes back on its own WITH AUDIO
echo.
echo  [X]  THIS SCRIPT DOES NOT PROVE AUDIO. A box can report healthy and be
echo      silent - that is exactly what tricked us on .200. Listen to it, and
echo      check the stream / the board.
echo.
echo  [X]  Still needed, and not done by this script:
echo      - BIOS: set "Restore on AC power loss" = On, or a power cut leaves the
echo        machine sitting off.
echo      - an OFF-BOX watchdog: if the only thing that knows the station is
echo        silent lives on the silent box, you have no watchdog.
echo      - audio_device_guid: set it to the output feeding the Barix instead of
echo        leaving it blank ^(the default device^).
echo.
echo  Undo:  scripts\remove-autostart.bat
echo.
pause
exit /b 0
