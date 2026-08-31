@echo off
REM Pen AI - BROWSER MODE (local browser at 127.0.0.1:8765, NOT cmd)
SETLOCAL
set "ROOT=%~dp0"
echo Starting Pen AI in BROWSER mode...
if exist "%ROOT%pen AI\agent\start-ui.bat" (
  call "%ROOT%pen AI\agent\start-ui.bat"
  goto :done
)
if exist "%ROOT%agent\start-ui.bat" (
  call "%ROOT%agent\start-ui.bat"
  goto :done
)
REM Fallback to CLI if browser files missing
if exist "%ROOT%pen AI\agent\start-agent.bat" call "%ROOT%pen AI\agent\start-agent.bat" & goto :done
if exist "%ROOT%agent\start-agent.bat" call "%ROOT%agent\start-agent.bat" & goto :done
echo ERROR: start-ui.bat not found
pause
:done
ENDLOCAL

