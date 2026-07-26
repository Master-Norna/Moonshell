@echo off
rem Double-click to launch MoonShell Spirit.
rem First run builds the venv and installs deps; later runs are instant.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "exitCode=%errorlevel%"
if not "%exitCode%"=="0" pause
exit /b %exitCode%
