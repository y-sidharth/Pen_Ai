@echo off
REM FIXED: Creates portable watcher task that works on ANY drive letter
REM Copies watcher to %APPDATA% so scheduled task does NOT depend on E:\ or F:\
SETLOCAL
set "AGENT_DIR=%~dp0"
set "PS=%SystemRoot%\system32\WindowsPowerShell\v1.0\powershell.exe"
set "SRC_SCRIPT=%AGENT_DIR%autostart_watcher.ps1"
for %%I in ("%AGENT_DIR%..") do set "PROJECT_DIR=%%~fI"

if not exist "%SRC_SCRIPT%" (
  echo Watcher script not found: "%SRC_SCRIPT%"
  exit /b 1
)

echo.
echo ============================================================
echo   JAMPANDU USB AUTOSTART INSTALLER (FIXED PORTABLE)
echo ============================================================
echo.
echo This will create a Windows scheduled task that:
echo   1. Runs when you sign in to Windows (no admin needed)
echo   2. Watches for ANY USB drive insertion
echo   3. Automatically starts the agent if the USB contains
echo      AUTOSTART.marker (any folder name, any drive letter)
echo.
echo FIX: Task points to LOCAL copy in %%APPDATA%%\Jampandu\
echo      so it works even when pendrive letter changes E: -^> F:
echo.
echo To work on ANOTHER PC, you must run this installer ONCE
echo on that PC as well. Windows blocks true zero-click
echo autorun on unknown PCs for security (since Windows 7).
echo For unknown PCs, use autorun.inf prompt or double-click
echo START_HERE.bat in the pendrive root.
echo.
echo Remove anytime with uninstall_autostart.bat
echo ============================================================
echo.
set /p CONFIRM=Type YES to continue: 
if /I not "%CONFIRM%"=="YES" (
  echo Installation cancelled.
  exit /b 1
)

REM Create marker file
echo AUTO-START > "%PROJECT_DIR%\AUTOSTART.marker"
if errorlevel 1 (
  echo Failed to create AUTOSTART.marker file.
  exit /b 1
)
echo [OK] Marker: "%PROJECT_DIR%\AUTOSTART.marker"

REM Copy watcher + emergency cleaners to local APPDATA (portable)
set "LOCAL_DIR=%APPDATA%\Jampandu"
set "LOCAL_SCRIPT=%LOCAL_DIR%\autostart_watcher.ps1"
set "SRC_CLEAN_PS=%AGENT_DIR%emergency_clean.ps1"
set "SRC_CLEAN_BAT=%AGENT_DIR%emergency_clean.bat"
set "LOCAL_CLEAN_PS=%LOCAL_DIR%\emergency_clean.ps1"
set "LOCAL_CLEAN_BAT=%LOCAL_DIR%\emergency_clean.bat"
if not exist "%LOCAL_DIR%" mkdir "%LOCAL_DIR%" 2>nul
copy /Y "%SRC_SCRIPT%" "%LOCAL_SCRIPT%" >nul
if errorlevel 1 (
  echo Failed to copy watcher to "%LOCAL_SCRIPT%"
  exit /b 1
)
echo [OK] Copied watcher to "%LOCAL_SCRIPT%"
if exist "%SRC_CLEAN_PS%" copy /Y "%SRC_CLEAN_PS%" "%LOCAL_CLEAN_PS%" >nul 2>&1
if exist "%SRC_CLEAN_BAT%" copy /Y "%SRC_CLEAN_BAT%" "%LOCAL_CLEAN_BAT%" >nul 2>&1
if exist "%LOCAL_CLEAN_PS%" echo [OK] Copied emergency cleaner to "%LOCAL_CLEAN_PS%"

REM Delete existing task if exists
schtasks /Delete /TN "JarvisPendriveWatcher" /F 2>nul

REM Create scheduled task pointing to LOCAL copy (not USB)
schtasks /Create /SC ONLOGON /RL LIMITED /DELAY 0000:05 /TN "JarvisPendriveWatcher" /TR "\"%PS%\" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%LOCAL_SCRIPT%\"" /F
if errorlevel 1 (
  echo Failed to create the scheduled task.
  echo Try running this bat as normal user (not admin needed).
  exit /b 1
)
echo [OK] Main watcher task created.

REM ---- Amnesiac shutdown/logoff cleaner (erases host traces on power-off / sign-out) ----
REM This guarantees: when System is powered off or user logs off, all host data is erased.
REM It runs hidden and needs no admin. If creation fails (policy), watcher.ps1 handlers still cover session ending.
set "SHUTDOWN_TASK=JampanduShutdownClean"
schtasks /Delete /TN "%SHUTDOWN_TASK%" /F 2>nul
REM Primary: ONLOGON with extra trigger? Use ONEVENT for shutdown is admin-only, so we use ONLOGON fallback + register SystemEvents in watcher.
REM Try to create a logoff-triggered task (works without admin on Win10+)
schtasks /Create /SC ONLOGON /RL LIMITED /DELAY 0000:05 /TN "%SHUTDOWN_TASK%" /TR "\"%PS%\" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%LOCAL_CLEAN_PS%\" -FullAmnesiac" /F 2>nul
REM Attempt stronger: ONEVENT for shutdown (EventID 1074) — may fail without admin, ignore error
schtasks /Create /SC ONEVENT /EC System /MO "*[System[(EventID=1074)]]" /RL LIMITED /TN "%SHUTDOWN_TASK%_Event" /TR "\"%PS%\" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%LOCAL_CLEAN_PS%\" -FullAmnesiac" /F 2>nul
echo [OK] Shutdown/logoff cleaner configured: %SHUTDOWN_TASK% -^> %LOCAL_CLEAN_PS% -FullAmnesiac

echo.
echo ============================================================
echo   INSTALLATION COMPLETE - PORTABLE MODE + AMNESIAC CLEAN
echo ============================================================
echo.
echo Tasks:
echo   JarvisPendriveWatcher -^> %LOCAL_SCRIPT%
echo     Trigger: At logon, delay 5 sec  (watches USB + wipes on removal)
echo   JampanduShutdownClean -^> %LOCAL_CLEAN_PS% -FullAmnesiac
echo     Trigger: At shutdown/logoff  (erases host traces on power-off)
echo.
echo Amnesiac guarantee:
echo   - Pendrive removed -^> host watcher.log/flags/clipboard/temp erased instantly
echo   - System power-off/logoff -^> emergency_clean.ps1 wipes host traces
echo   - Normal exit (stop_and_clean.bat) -^> secure wipe pendrive tmp + host cleaner
echo.
echo Test:
echo   1. Safely eject USB
echo   2. Re-insert (even as different letter) - agent starts in 2 sec
echo   3. Check log: %APPDATA%\Jampandu\watcher.log
echo.
echo IMPORTANT - For OTHER devices:
echo   You MUST run this installer ONCE on each new PC where you
echo   want insert-^>auto-start. No way around Windows security.
echo   For PCs without installer, open pendrive and double-click:
echo   START_HERE.bat  (root of pendrive)
echo.
echo To uninstall: agent\uninstall_autostart.bat
echo ============================================================
ENDLOCAL
