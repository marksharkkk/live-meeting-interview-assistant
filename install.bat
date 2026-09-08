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

if not exist "%ProgramFiles%\Volta\npm.cmd" (
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
call "%ProgramFiles%\Volta\npm.cmd" ci
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
