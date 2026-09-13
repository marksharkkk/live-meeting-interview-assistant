@echo off
setlocal
echo ========================================
echo  Meeting Assistant - Reproducible Setup
echo ========================================

cd /d %~dp0

set "UV_EXE="
for /f "delims=" %%I in ('where uv 2^>nul') do if not defined UV_EXE set "UV_EXE=%%I"
if not defined UV_EXE (
  for /r "%LOCALAPPDATA%\Microsoft\WinGet\Packages" %%I in (uv.exe) do if not defined UV_EXE set "UV_EXE=%%I"
)

if not defined UV_EXE (
  echo ERROR: uv is not installed. Install it with: winget install astral-sh.uv
  pause
  exit /b 1
)

set "VOLTA_EXE="
for /f "delims=" %%I in ('where volta 2^>nul') do if not defined VOLTA_EXE set "VOLTA_EXE=%%I"
if not defined VOLTA_EXE (
  echo ERROR: Volta is not installed. Install it with: winget install Volta.Volta
  pause
  exit /b 1
)

echo.
echo Synchronizing the isolated Python environment...
"%UV_EXE%" sync --locked
if errorlevel 1 goto :failed

echo.
echo Restoring locked Node.js dependencies...
"%VOLTA_EXE%" run npm ci
if errorlevel 1 goto :failed

echo.
echo ========================================
echo  Installation Complete!
echo ========================================
echo.
echo To start the application, run: start.bat
echo.
pause
exit /b 0

:failed
echo.
echo Setup failed. Review the error above.
pause
exit /b 1
