@echo off
REM ============================================================
REM  StudioFire - set the engine's audio output device (the one wired
REM  to the Barix). Wrapper around scripts\set-audio-device.ps1.
REM
REM  Usage:
REM    scripts\set-audio-device.bat                 (interactive picker)
REM    scripts\set-audio-device.bat -List           (just show the devices)
REM    scripts\set-audio-device.bat -Check          (what is set now)
REM    scripts\set-audio-device.bat -Value "wasapi/{GUID}"
REM    scripts\set-audio-device.bat -Value ""       (back to Windows default)
REM
REM  Why: config ships with audio_device_guid "" (Windows default), but a
REM  default device is per-session and moves around. Lock it to the Barix-feed
REM  output. This does NOT make a Windows service able to play audio - nothing
REM  can; use install-autostart.bat.
REM ============================================================
setlocal
cd /d "%~dp0"
set "PS1=%~dp0set-audio-device.ps1"
if not exist "%PS1%" (
  echo [X] %PS1% not found.
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %*
exit /b %errorlevel%
