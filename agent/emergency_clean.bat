@echo off
REM emergency_clean.bat — invoked on shutdown/logoff/removal when PowerShell is unavailable
REM Best-effort fast wipe of host traces (mirrors emergency_clean.ps1 logic)
SETLOCAL
set "JDIR=%APPDATA%\Jampandu"
REM Clear clipboard
echo. | clip >nul 2>&1
REM Delete WMI flags
if exist "%JDIR%\insert_*.flag" del /F /Q "%JDIR%\insert_*.flag" >nul 2>&1
if exist "%JDIR%\remove_*.flag" del /F /Q "%JDIR%\remove_*.flag" >nul 2>&1
REM Overwrite + delete watcher.log if present (simple zero)
if exist "%JDIR%\watcher.log" (
  REM overwrite with zeros via type nul trick then delete
  copy /Y nul "%JDIR%\watcher.log" >nul 2>&1
  del /F /Q "%JDIR%\watcher.log" >nul 2>&1
)
REM Clean TEMP pen-ai artifacts
if defined TEMP (
  if exist "%TEMP%\popup_in_*.txt" del /F /Q "%TEMP%\popup_in_*.txt" >nul 2>&1
  if exist "%TEMP%\popup_out_*.txt" del /F /Q "%TEMP%\popup_out_*.txt" >nul 2>&1
  if exist "%TEMP%\prompt_*.txt" del /F /Q "%TEMP%\prompt_*.txt" >nul 2>&1
)
if defined TMP (
  if exist "%TMP%\popup_in_*.txt" del /F /Q "%TMP%\popup_in_*.txt" >nul 2>&1
)
REM If drive removal was passed as %1, also wipe host_clean.log
if not "%~1"=="" (
  if exist "%JDIR%\host_clean.log" (
    copy /Y nul "%JDIR%\host_clean.log" >nul 2>&1
    del /F /Q "%JDIR%\host_clean.log" >nul 2>&1
  )
)
ENDLOCAL
exit /b 0
