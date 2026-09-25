@echo off
rem ============================================================================
rem  run_ui.bat -- start the console from source (no packaging; edit and retry)
rem
rem  First run: install dependencies once
rem      python -m pip install -r requirements.txt
rem
rem  NOTE: keep this file ASCII-only. An earlier version had UTF-8 Chinese
rem  comments and cmd mis-decoded them on Chinese Windows (OEM codepage): the
rem  byte right before CR/LF swallowed the CR, lines merged, and -- because the
rem  `rem` prefix was eaten too -- `%PY% -m pip install -r requirements.txt`
rem  got EXECUTED by accident every time you ran this file.
rem  All user-facing Chinese text comes from the python side, which is safe.
rem ============================================================================
setlocal
cd /d "%~dp0"

rem Prefer the local venv, then the project's isolated env, then python on PATH.
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
  set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
) else (
  set "PY=python"
)

"%PY%" -X utf8 -u "qq-agent.py" %*
set RC=%ERRORLEVEL%
if %RC% NEQ 0 (
  echo.
  echo [X] Console exited with code %RC%
  pause
)
endlocal
