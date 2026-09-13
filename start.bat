@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Project environment is missing. Run install.bat first.
  pause
  exit /b 1
)

set "VOLTA_EXE="
for /f "delims=" %%I in ('where volta 2^>nul') do if not defined VOLTA_EXE set "VOLTA_EXE=%%I"
if not defined VOLTA_EXE (
  echo Volta is missing. Run install.bat after installing Volta.
  pause
  exit /b 1
)

"%VOLTA_EXE%" run npm start
if errorlevel 1 pause
