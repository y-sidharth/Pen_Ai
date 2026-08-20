@echo off
REM Removes the scheduled task created by install_autostart.bat.
set /p CONFIRM=Type YES to remove JarvisPendriveWatcher: 
if /I not "%CONFIRM%"=="YES" (
  echo Removal cancelled.
  exit /b 1
)

schtasks /Delete /TN "JarvisPendriveWatcher" /F
if errorlevel 1 (
  echo Scheduled task was not removed. It may not exist.
  exit /b 1
)
echo Scheduled task 'JarvisPendriveWatcher' removed.
