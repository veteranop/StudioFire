@echo off
REM Map the NAS drive inside a SERVICE session (services don't inherit your
REM logged-in drive letters). Copy this file to config\drive-map.bat and edit
REM the UNC path for this station. Credentials come from the account the
REM service logs on as (services.msc -> service -> Log On -> This account).
net use Z: \\KDPI-Media\music /persistent:no >nul 2>&1
