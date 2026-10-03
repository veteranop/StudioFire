@echo off
REM ============================================================
REM  StudioFire - undo of scripts\install-autostart.bat
REM  Run as Administrator.
REM
REM  Removes the two scheduled tasks and turns Windows autologon back
REM  off, including the password stored as an LSA secret. Your music,
REM  config, logs and precache are untouched.
REM
REM  After this the station will NOT start by itself - run start-all.bat
REM  in a logged-in session to put it back on air.
REM ============================================================
setlocal
cd /d "%~dp0.."
set "APP=%CD%"

echo.
echo  Removing StudioFire auto-start...
echo.

fltmc >nul 2>&1
if errorlevel 1 (
  echo [X]  This needs Administrator rights. Right-click -^> Run as administrator.
  pause
  exit /b 1
)

schtasks /query /tn "StudioFire-Autostart" >nul 2>&1
if errorlevel 1 (
  echo [--] StudioFire-Autostart task was not present
) else (
  schtasks /delete /tn "StudioFire-Autostart" /f >nul 2>&1
  if errorlevel 1 (echo [X]  could not delete StudioFire-Autostart) else (echo [ok] removed StudioFire-Autostart)
)

schtasks /query /tn "StudioFire-Watchdog" >nul 2>&1
if errorlevel 1 (
  echo [--] StudioFire-Watchdog task was not present
) else (
  schtasks /delete /tn "StudioFire-Watchdog" /f >nul 2>&1
  if errorlevel 1 (echo [X]  could not delete StudioFire-Watchdog) else (echo [ok] removed StudioFire-Watchdog)
)

REM Prefer the LSA helper: it turns autologon off, clears the cleartext
REM registry value AND deletes the stored LSA secret in one step.
set "LSA_DONE="
if exist "%APP%\scripts\autologon-lsa.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%APP%\scripts\autologon-lsa.ps1" -Disable
  if not errorlevel 1 set "LSA_DONE=1"
)

if defined LSA_DONE (
  echo [ok] autologon turned off and stored LSA secret removed
) else (
  reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v AutoAdminLogon /t REG_SZ /d 0 /f >nul 2>&1
  if errorlevel 1 (echo [X]  could not turn autologon off) else (echo [ok] autologon turned off)
)

reg delete "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultPassword /f >nul 2>&1
echo [ok] any stored cleartext autologon password removed

echo.
echo  Done. The station will not start itself until you re-run
echo  scripts\install-autostart.bat, or start it by hand with start-all.bat.
echo.
pause
exit /b 0
