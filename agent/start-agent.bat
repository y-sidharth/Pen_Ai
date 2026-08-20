@echo off
REM Start the local Jarvis-like agent controller. Prompts for password in the Python controller.
SETLOCAL
set AGENT_DIR=%~dp0
REM Change to the agent directory so local imports resolve correctly
pushd %AGENT_DIR%
REM Run Python with a tiny bootstrap to ensure the agent directory is on sys.path
REM Use portable python if available, otherwise rely on system python
if exist "python-portable\python.exe" (
  "python-portable\python.exe" -c "import sys,os; sys.path.insert(0, os.getcwd()); import run_agent"
) else (
  python -c "import sys,os; sys.path.insert(0, os.getcwd()); import run_agent"
)
popd
ENDLOCAL
