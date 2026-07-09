@echo off
REM ============================================================
REM  StudioFire - is it actually on air, or stuck on emergency
REM  filler? Prints a one-shot status report. Same interpreter
REM  selection as start-all.bat. Exit code 0 = healthy.
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
"%PYTHON%" scripts\healthcheck.py
echo.
pause
endlocal
