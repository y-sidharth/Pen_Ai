@echo off
REM Creates the optional user-logon watcher after explicit confirmation.
REM This watcher will automatically start the agent when a USB drive containing
REM this project is inserted into the computer.
SETLOCAL
set "AGENT_DIR=%~dp0"
set "PS=%SystemRoot%\system32\WindowsPowerShell\v1.0\powershell.exe"
set "SCRIPT=%AGENT_DIR%autostart_watcher.ps1"
for %%I in ("%AGENT_DIR%..") do set "PROJECT_DIR=%%~fI"

if not exist "%SCRIPT%" (
  echo Watcher script not found: "%SCRIPT%"
  exit /b 1
)

echo.
echo ============================================================
echo   JARVIS USB AUTOSTART INSTALLER
echo ============================================================
echo.
echo This will create a Windows scheduled task that:
echo   1. Runs when you sign in to Windows
echo   2. Watches for USB drive insertions
echo   3. Automatically starts the agent if the USB drive contains
echo      this project with the AUTOSTART.marker file
echo.
echo The watcher runs in the background with minimal resource usage.
echo It can be removed later with uninstall_autostart.bat.
echo.
echo Note: Only USB drives with the AUTOSTART.marker file will trigger
echo the agent to start. Other USB drives will be ignored.
echo.
echo ============================================================
echo.
set /p CONFIRM=Type YES to continue: 
if /I not "%CONFIRM%"=="YES" (
  echo Installation cancelled.
  exit /b 1
)

REM Create the marker file that identifies this project
echo AUTO-START > "%PROJECT_DIR%\AUTOSTART.marker"
if errorlevel 1 (
  echo Failed to create AUTOSTART.marker file.
  exit /b 1
)

REM Delete existing task if it exists (to allow re-installation)
schtasks /Delete /TN "JarvisPendriveWatcher" /F 2>nul

REM Create the scheduled task
REM /SC ONLOGON - Run when user logs on
REM /RL LIMITED - Run with limited privileges (no admin required)
REM /DELAY 00:05 - Wait 5 seconds after logon before starting
schtasks /Create /SC ONLOGON /RL LIMITED /DELAY 00:05 /TN "JarvisPendriveWatcher" /TR "\"%PS%\" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%SCRIPT%\"" /F
if errorlevel 1 (
  echo Failed to create the scheduled task.
  exit /b 1
)

echo.
echo ============================================================
echo   INSTALLATION COMPLETE
echo ============================================================
echo.
echo The autostart watcher has been installed successfully.
echo.
echo To test:
echo   1. Copy this entire project to a USB drive
echo   2. Safely eject the USB drive
echo   3. Insert the USB drive into any computer where you're logged in
echo   4. The agent should start automatically within a few seconds
echo.
echo To uninstall, run: uninstall_autostart.bat
echo.
echo ============================================================
ENDLOCAL