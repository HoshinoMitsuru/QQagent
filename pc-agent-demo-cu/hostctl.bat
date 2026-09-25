@echo off
rem ============================================================================
rem  hostctl.bat -- control the hidden-desktop daemon (R4 / V2)
rem
rem  WHY THIS SCRIPT EXISTS
rem    Typing `python -m app.host ...` by hand hits two traps:
rem      1) `-m app.host` resolves the `app` package from the CURRENT directory,
rem         so you must cd here first;
rem      2) the python on PATH has no uiautomation/comtypes installed -- only the
rem         project's isolated env does. A bare `python` gives ModuleNotFoundError.
rem    This script does both for you: it cd's itself, and picks a python that
rem    actually has the dependencies.
rem
rem  USAGE
rem    hostctl                   status
rem    hostctl start             hidden desktop + QQ + reply loop (SENDS real messages)
rem    hostctl start --no-agent  just put QQ on that desktop, no reply loop
rem    hostctl start --dry-run   full flow, but never actually sends
rem    hostctl start --no-send   rehearsal: types the text, does not press send
rem    hostctl stop              stop daemon + reply loop (keeps the QQ on that desktop)
rem    hostctl stop-qq           also close that desktop's QQ (the desktop then goes away)
rem    hostctl log               dump the host log (for live output use the web UI)
rem    hostctl open              open the console UI (prefers the exe in dist\)
rem
rem  You normally do NOT need this script: double-click dist\qq-agent.exe and use
rem  the buttons on the "hidden desktop" card -- they do all of the above.
rem
rem  NOTE: keep this file ASCII-only. A .bat containing UTF-8 Chinese gets
rem  mis-decoded by the OEM codepage on Chinese Windows: a multi-byte sequence at
rem  end-of-line swallows the CR, lines merge, and cmd then EXECUTES the garbage
rem  (this actually happened -- run_ui.bat was running `pip install` by accident).
rem  All user-facing Chinese text comes from the python side, which is safe.
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

rem ---- pick a python that has the dependencies --------------------------------
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
  set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
)
if not defined PY for /f "delims=" %%P in ('where python 2^>nul') do (
  if not defined PY set "PY=%%P"
)

if not defined PY (
  echo [X] No python found. Install Python 3.11+ , or just use the packaged
  echo     exe instead -- dist\qq-agent.exe needs no python at all.
  exit /b 2
)

"!PY!" -c "import uiautomation, comtypes, requests, pyperclip" >nul 2>&1
if errorlevel 1 (
  echo [X] This python is missing dependencies:
  echo       !PY!
  echo     Install them once:
  echo       "!PY!" -m pip install -r requirements.txt
  echo     Or skip python entirely:  dist\qq-agent.exe  --  no python needed
  exit /b 2
)

if "%~1"=="" goto :status
if /i "%~1"=="status"   goto :status
if /i "%~1"=="start"    goto :start
if /i "%~1"=="stop"     goto :stop
if /i "%~1"=="stop-qq"  goto :stopqq
if /i "%~1"=="log"      goto :log
if /i "%~1"=="open"     goto :open
echo [X] Unknown subcommand: %~1
echo     Available: status / start / stop / stop-qq / log / open
exit /b 2

:status
echo [i] python: !PY!
"!PY!" -X utf8 -m app.host daemon status
goto :end

:start
shift
echo [i] python: !PY!
echo [i] Starting: create desktop -^> launch QQ -^> login -^> open a chat -^> reply loop
"!PY!" -X utf8 -m app.host daemon start %1 %2 %3 %4 %5
if errorlevel 1 (
  echo.
  echo [X] Start failed. Read the host log with:  hostctl log
  exit /b 2
)
echo.
echo [i] status: hostctl status      logs: hostctl log      stop: hostctl stop
goto :end

:stop
"!PY%" -X utf8 -m app.host daemon stop
goto :end

:stopqq
"!PY%" -X utf8 -m app.host daemon stop
echo [i] Now closing that desktop's QQ (only the processes using OUR profile;
echo     your own QQ is never touched) ...
"!PY%" -X utf8 -m app.host stop --yes
goto :end

:log
if not exist "logs\hostd.log" (
  echo [i] No logs\hostd.log yet -- the host has never been started.
  exit /b 0
)
type "logs\hostd.log"
goto :end

:open
rem Prefer the packaged exe (it starts the server and opens the browser itself).
if exist "dist\qq-agent.exe" (
  echo [i] Launching dist\qq-agent.exe  ^(will ask for admin rights^)
  start "" "dist\qq-agent.exe"
  goto :end
)
if exist "run_ui.bat" (
  echo [i] dist\qq-agent.exe not found, falling back to source mode
  call "run_ui.bat"
  goto :end
)
echo [X] Neither dist\qq-agent.exe nor run_ui.bat exists
exit /b 2

:end
endlocal
exit /b 0
