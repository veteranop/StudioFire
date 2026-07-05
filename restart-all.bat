@echo off
REM Restart all StudioFire services (stop, pause, start). Safe to run from the
REM command line, or triggered by the GUI's restart button.
setlocal
cd /d "%~dp0"
call "%~dp0stop-all.bat"
timeout /t 2 >nul
call "%~dp0start-all.bat"
endlocal
