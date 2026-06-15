@echo off
rem Double-click to launch MoonShell Spirit v14.
rem First run builds the venv and installs deps; later runs are instant.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if errorlevel 1 pause
