@echo off
REM ============================================================
REM  StudioFire - update this station from GitHub.
REM
REM  Checks the latest StudioFire release on GitHub and, if it's
REM  newer, installs it: backs up first, restarts only what changed,
REM  and puts the old version back by itself if the new one doesn't
REM  come up healthy. Config, database, logs and the music cache are
REM  never touched. (Same as Settings -> Software updates -> Install.)
REM
REM    update.bat              install the latest release
REM    update.bat check        just say whether one is available
REM    update.bat --tag v1.2.0 install a specific release
REM
REM  Details: DEPLOY.md "Updating a station". Log: logs\update.log
REM ============================================================
setlocal
cd /d "%~dp0"
if "%PYTHON%"=="" (
  if exist "%~dp0runtime\python.exe" (
    set "PYTHON=%~dp0runtime\python.exe"
  ) else if exist "%USERPROFILE%\anaconda3\python.exe" (
    set "PYTHON=%USERPROFILE%\anaconda3\python.exe"
  ) else (
    set "PYTHON=python"
  )
)
if /i "%~1"=="check" (
  "%PYTHON%" -m services.updater check
) else (
  "%PYTHON%" -m services.updater apply %*
)
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (echo [ok] Done.) else (echo [!] Update did not complete - see logs\update.log)
pause
exit /b %RC%
