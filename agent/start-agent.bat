@echo off
REM FIXED: Actually calls run_agent.main() and keeps window open on error (Defender-safe)
SETLOCAL
set "AGENT_DIR=%~dp0"
pushd "%AGENT_DIR%"
if exist "python-portable\python.exe" (
  "python-portable\python.exe" -c "import sys,os; sys.path.insert(0, os.getcwd()); import run_agent; run_agent.main()"
) else (
  python -c "import sys,os; sys.path.insert(0, os.getcwd()); import run_agent; run_agent.main()"
)
set "EXITCODE=%ERRORLEVEL%"
popd
if not "%EXITCODE%"=="0" (
  echo.
  echo [Exit code %EXITCODE%] Agent stopped. If window closed too fast, run from cmd.
  pause
)
ENDLOCAL
