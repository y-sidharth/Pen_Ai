@echo off
setlocal
set "AGENT_DIR=%~dp0"
cd /d "%AGENT_DIR%"

if exist "python-portable\python.exe" (
  "python-portable\python.exe" "web_ui.py"
) else (
  python "web_ui.py"
)
