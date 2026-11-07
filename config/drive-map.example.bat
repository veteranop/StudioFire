@echo off
REM OPTIONAL. You usually do NOT need this. StudioFire resolves UNC paths
REM end-to-end, so prefer a UNC root in config\config.json, for example
REM   "nas_music_root": "//NAS/music"
REM
REM Only use this if some legacy playlist still points at a mapped drive
REM letter (e.g. Z:) inside a SERVICE session (services don't inherit the
REM drive letters you mapped while logged in interactively). If so, copy this
REM file to config\drive-map.bat, uncomment the line below and edit it for your
REM own share. Credentials come from the account the service logs on as
REM (services.msc -> service -> Log On -> This account).
REM
REM net use Z: \\NAS\music /persistent:no >nul 2>&1
