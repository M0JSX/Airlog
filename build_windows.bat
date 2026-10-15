@echo off
rem Double-click to build Airlog.exe (see build_windows.ps1 for options).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build_windows.ps1" %*
echo.
pause
