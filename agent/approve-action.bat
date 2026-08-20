@echo off
REM approve-action.bat <nonce> — wrapper that calls approve_action.py (Python required or portable python in python-portable) 
SETLOCAL
set AGENT_DIR=%~dp0
if "%1"=="" (
  echo Usage: approve-action.bat ^<nonce^>
  exit /b 1
)
if exist "%AGENT_DIR%python-portable\python.exe" (
  "%AGENT_DIR%python-portable\python.exe" "%AGENT_DIR%approve_action.py" %1
) else (
  python "%AGENT_DIR%approve_action.py" %1
)
ENDLOCAL
