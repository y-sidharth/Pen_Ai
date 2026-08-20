@echo off
REM stop_and_clean.bat - cleans pendrive temp files, removes tokens, clears clipboard, and logs action.
SETLOCAL
set AGENT_DIR=%~dp0
set DATA_DIR=%AGENT_DIR%data\
set TMP_DIR=%DATA_DIR%tmp\
set AUTH_TOKEN=%AGENT_DIR%auth\allowlist.token
set LOG_DIR=%AGENT_DIR%logs\
if exist "%AUTH_TOKEN%" (
  del /f /q "%AUTH_TOKEN%" >nul 2>&1
)
REM remove tmp files created by agent
if exist "%TMP_DIR%" (
  del /f /q "%TMP_DIR%*" >nul 2>&1
)
REM clear Windows clipboard
echo. | clip
REM write audit log
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
echo Stop and clean performed at %DATE% %TIME% > "%LOG_DIR%clean_audit.txt"

echo Clean complete. You can now safely eject the pendrive.
ENDLOCAL
