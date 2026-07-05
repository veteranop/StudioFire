@echo off
REM Stop all StudioFire services (and mpv) on this machine.
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'services\.(engine|core|worker)\.main' } | ForEach-Object { Start-Process taskkill -ArgumentList '/F','/T','/PID',$_.ProcessId -NoNewWindow -Wait }"
echo Stopped StudioFire engine / web / indexer (and mpv).
