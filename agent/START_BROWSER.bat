@echo off
REM BROWSER MODE - Insert pendrive -> double-click this -> opens in local browser (127.0.0.1:8765)
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
echo ERROR: start-ui.bat not found
pause
:done
ENDLOCAL
