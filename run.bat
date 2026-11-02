@echo off
rem PCB Studio: creates a venv on KiCad's own Python (for pcbnew) on first run, then starts the local server.
setlocal
set "KIPY="
for /d %%D in ("%ProgramFiles%\KiCad\*") do if exist "%%D\bin\python.exe" set "KIPY=%%D\bin\python.exe"
if not defined KIPY (
  echo KiCad was not found under "%ProgramFiles%\KiCad". Install KiCad 10 first.
  exit /b 1
)
set "VENV=%LOCALAPPDATA%\pcbstudio\venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo Creating %VENV% from %KIPY% ...
  "%KIPY%" -m venv --system-site-packages "%VENV%" || exit /b 1
  "%VENV%\Scripts\python.exe" -m pip install -r "%~dp0requirements.txt" || exit /b 1
)
cd /d "%~dp0"
"%VENV%\Scripts\python.exe" -m server %*
