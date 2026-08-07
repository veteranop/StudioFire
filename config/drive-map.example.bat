@echo off
REM Map the NAS drive inside a SERVICE session (services don't inherit your
REM logged-in drive letters). Copy this file to config\drive-map.bat and edit
REM only if you need a legacy mapped-drive path such as Z: in the service.
REM Prefer a UNC root in config\config.json like //KDPI-Media/music/G.
REM Credentials come from the account the service logs on as
REM (services.msc -> service -> Log On -> This account).
net use Z: \\KDPI-Media\music /persistent:no >nul 2>&1
