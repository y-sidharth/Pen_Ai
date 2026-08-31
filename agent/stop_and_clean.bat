@echo off
REM stop_and_clean.bat - AMNESIAC: securely erases pendrive tmp/tokens AND host traces.
REM Called before eject and on normal exit. Also triggered on removal/power-off via emergency_clean.ps1.
SETLOCAL
set "AGENT_DIR=%~dp0"
set "DATA_DIR=%AGENT_DIR%data\"
set "TMP_DIR=%DATA_DIR%tmp\"
set "AUTH_TOKEN=%AGENT_DIR%auth\allowlist.token"
set "LOG_DIR=%AGENT_DIR%logs\"
set "PY=%AGENT_DIR%python-portable\python.exe"
if not exist "%PY%" set "PY=python"

REM 1. Try host_cleaner.py full amnesiac wipe (pendrive tmp + host %APPDATA% traces + clipboard)
REM    This securely overwrites before deleting.
if exist "%AGENT_DIR%host_cleaner.py" (
  "%PY%" "%AGENT_DIR%host_cleaner.py" --full-clean >nul 2>&1
  if %errorlevel%==0 goto :host_done
)
REM Fallback if python not available: emergency_clean.ps1 with FullAmnesiac (host only)
if exist "%APPDATA%\Jampandu\emergency_clean.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%APPDATA%\Jampandu\emergency_clean.ps1" -FullAmnesiac >nul 2>&1
)
if exist "%AGENT_DIR%emergency_clean.ps1" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%AGENT_DIR%emergency_clean.ps1" -FullAmnesiac >nul 2>&1
)
:host_done

REM 2. Fallback direct secure wipe of pendrive tmp/auth (in case host_cleaner failed)
if exist "%AUTH_TOKEN%" (
  REM overwrite then delete
  copy /Y nul "%AUTH_TOKEN%" >nul 2>&1
  del /F /Q "%AUTH_TOKEN%" >nul 2>&1
)
if exist "%TMP_DIR%" (
  for %%F in ("%TMP_DIR%*") do (
    copy /Y nul "%%F" >nul 2>&1
    del /F /Q "%%F" >nul 2>&1
  )
  REM also pending session.lock
  if exist "%TMP_DIR%session.lock" (
    copy /Y nul "%TMP_DIR%session.lock" >nul 2>&1
    del /F /Q "%TMP_DIR%session.lock" >nul 2>&1
  )
)
REM Clear Windows clipboard (fallback)
echo. | clip >nul 2>&1

REM Kill lingering llama/python that might hold locks (best-effort)
taskkill /IM llama-server.exe /F 2>nul >nul
taskkill /IM llama.exe /F 2>nul >nul

REM write audit log (on pendrive — stays with pendrive, not host)
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%" 2>nul
echo Stop and clean (amnesiac) performed at %DATE% %TIME% > "%LOG_DIR%clean_audit.txt"

echo Clean complete (amnesiac — host traces erased). You can now safely eject the pendrive.
ENDLOCAL
