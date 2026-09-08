@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Project environment is missing. Run install.bat first.
  pause
  exit /b 1
)

if not exist "%ProgramFiles%\Volta\npm.cmd" (
  echo Volta is missing. Run install.bat after installing Volta.
  pause
  exit /b 1
)

call "%ProgramFiles%\Volta\npm.cmd" start
if errorlevel 1 pause
