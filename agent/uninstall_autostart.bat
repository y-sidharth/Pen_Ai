@echo off
REM Removes portable watcher task and local copy
set /p CONFIRM=Type YES to remove JarvisPendriveWatcher: 
if /I not "%CONFIRM%"=="YES" (
  echo Removal cancelled.
  exit /b 1
)

schtasks /Delete /TN "JarvisPendriveWatcher" /F
if errorlevel 1 (
  echo Scheduled task was not removed. It may not exist or already deleted.
) else (
  echo Scheduled task 'JarvisPendriveWatcher' removed.
)
REM Also remove shutdown/logoff cleaner tasks (amnesiac)
schtasks /Delete /TN "JampanduShutdownClean" /F 2>nul
if %errorlevel%==0 echo Scheduled task 'JampanduShutdownClean' removed.
schtasks /Delete /TN "JampanduShutdownClean_Event" /F 2>nul
if %errorlevel%==0 echo Scheduled task 'JampanduShutdownClean_Event' removed.

REM Clean local copy and logs — with secure overwrite for amnesiac guarantee
set "LOCAL_DIR=%APPDATA%\Jampandu"
if exist "%LOCAL_DIR%\autostart_watcher.ps1" (
  powershell -NoProfile -Command "$p='%LOCAL_DIR%\autostart_watcher.ps1'; $s=(Get-Item $p).Length; if($s -gt 0){$r=New-Object byte[] 65536; (New-Object Random).NextBytes($r); $fs=[IO.File]::Open($p,[IO.FileMode]::Open,[IO.FileAccess]::Write); $rem=$s; while($rem -gt 0){$n=[Math]::Min(65536,$rem); $fs.Write($r,0,$n); $rem-=$n}; $fs.Close()}; Remove-Item -LiteralPath $p -Force" 2>nul
  if not exist "%LOCAL_DIR%\autostart_watcher.ps1" echo Secure-erased %LOCAL_DIR%\autostart_watcher.ps1
)
REM Secure-erase emergency cleaners too
for %%F in (emergency_clean.ps1 emergency_clean.bat) do (
  if exist "%LOCAL_DIR%\%%F" (
    powershell -NoProfile -Command "$p='%LOCAL_DIR%\%%F'; $s=(Get-Item $p).Length; if($s -gt 0){$r=New-Object byte[] 65536; (New-Object Random).NextBytes($r); $fs=[IO.File]::Open($p,[IO.FileMode]::Open,[IO.FileAccess]::Write); $rem=$s; while($rem -gt 0){$n=[Math]::Min(65536,$rem); $fs.Write($r,0,$n); $rem-=$n}; $fs.Close()}; Remove-Item -LiteralPath $p -Force" 2>nul
    echo Secure-erased %LOCAL_DIR%\%%F
  )
)
REM Secure-erase logs and flags (host traces)
for %%F in (watcher.log host_clean.log) do (
  if exist "%LOCAL_DIR%\%%F" (
    copy /Y nul "%LOCAL_DIR%\%%F" >nul 2>&1
    del /F /Q "%LOCAL_DIR%\%%F" 2>nul
    echo Erased %LOCAL_DIR%\%%F
  )
)
del /F /Q "%LOCAL_DIR%\insert_*.flag" 2>nul
del /F /Q "%LOCAL_DIR%\remove_*.flag" 2>nul
REM Try to remove dir if empty
rd "%LOCAL_DIR%" 2>nul
if not exist "%LOCAL_DIR%" echo Removed %LOCAL_DIR% directory.

REM Stop running watcher if any (hidden powershell)
powershell -NoProfile -Command "Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -like '*autostart_watcher.ps1*' } | ForEach-Object { $_.Terminate() }" 2>nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*autostart_watcher.ps1*' } | ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate }" 2>nul

echo Cleanup done.
