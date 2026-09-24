@echo off
rem ============================================================================
rem  build.bat -- build qq-agent.exe from source
rem
rem  Usage:
rem    build.bat                build the single-file windowed exe (deliverable)
rem    build.bat console        same, but keep a console window (troubleshooting)
rem    build.bat dir            build a folder version (starts faster, many files)
rem    build.bat nadmin         do NOT request administrator rights
rem    build.bat console dir    combine switches
rem
rem  Python selection:
rem    The script walks a list of candidates and picks the FIRST one that already
rem    has all build+runtime dependencies. If none does, it installs them into the
rem    best candidate. Override with:  set QQAGENT_PY=C:\path\to\python.exe
rem
rem  Output:
rem    dist\qq-agent.exe        (single-file build)
rem    dist\qq-agent\           (folder build)
rem
rem  NOTE: keep this file ASCII-only. Non-ASCII inside a .bat gets mangled by the
rem  OEM codepage on Chinese Windows and the script then misbehaves silently.
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "QQAGENT_CONSOLE=0"
set "QQAGENT_ONEDIR=0"

:parse
if "%~1"=="" goto afterparse
if /i "%~1"=="console" set "QQAGENT_CONSOLE=1"
if /i "%~1"=="diag"    set "QQAGENT_CONSOLE=1"
if /i "%~1"=="dir"     set "QQAGENT_ONEDIR=1"
if /i "%~1"=="onedir"  set "QQAGENT_ONEDIR=1"
if /i "%~1"=="noadmin" set "QQAGENT_NO_ADMIN=1"
shift
goto parse
:afterparse

rem ---- pick a python that already has everything ------------------------------
set "PY="
if defined QQAGENT_PY call :check "%QQAGENT_PY%"
if not defined PY if exist ".venv\Scripts\python.exe" call :check ".venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
  call :check "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
)
for /f "delims=" %%P in ('where python 2^>nul') do (
  if not defined PY call :check "%%P"
)
for /f "delims=" %%P in ('where python3 2^>nul') do (
  if not defined PY call :check "%%P"
)

if not defined PY (
  echo [i] No interpreter has the full dependency set yet. Installing into a fresh venv...
  for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PY set "PY=%%P"
  )
  if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
    set "PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
  )
  if not defined PY (
    echo [X] Python not found at all. Install Python 3.11+ and re-run.
    exit /b 2
  )
  echo [i] Base interpreter: !PY!
  if not exist ".venv\Scripts\python.exe" (
    "!PY!" -m venv .venv || goto :fail
  )
  set "PY=.venv\Scripts\python.exe"
  echo [i] Installing dependencies into .venv ...
  "!PY!" -m pip install --quiet --upgrade pip
  "!PY!" -m pip install --quiet -r requirements.txt || goto :fail
  "!PY!" -m pip install --quiet pyinstaller || goto :fail
)

echo [i] Python: %PY%
"%PY%" -c "import PyInstaller, uiautomation, comtypes, requests, pyperclip; print('[i] deps OK, PyInstaller', PyInstaller.__version__)" || goto :fail

rem ---- icon -------------------------------------------------------------------
if not "%QQAGENT_NO_ICON%"=="1" (
  echo [i] Generating icon ...
  "%PY%" build_icon.py || goto :fail
)

rem ---- clean previous artefacts ----------------------------------------------
if exist "build\qq-agent" rmdir /s /q "build\qq-agent" >nul 2>&1
if exist "dist\qq-agent.exe" del /q "dist\qq-agent.exe" >nul 2>&1

rem ---- build ------------------------------------------------------------------
echo.
echo [i] Building (console=%QQAGENT_CONSOLE% onedir=%QQAGENT_ONEDIR%) ...
echo.
"%PY%" -m PyInstaller --noconfirm --clean qq-agent.spec || goto :fail

echo.
echo ============================================================================
if "%QQAGENT_ONEDIR%"=="1" (
  echo [OK] Output folder: dist\qq-agent\
  echo      Run: dist\qq-agent\qq-agent.exe
) else (
  echo [OK] Output file: dist\qq-agent.exe
  for %%F in ("dist\qq-agent.exe") do echo      Size: %%~zF bytes
)
echo.
echo Copy that file anywhere writable and double-click it.
echo It creates config.json / secrets.local.json / state\ / logs\ next to itself.
echo ============================================================================
endlocal
exit /b 0

rem ---- helper: set PY if this interpreter has all deps ------------------------
:check
if "%~1"=="" exit /b 1
if not exist "%~1" exit /b 1
"%~1" -c "import PyInstaller, uiautomation, comtypes, requests, pyperclip" >nul 2>&1
if errorlevel 1 exit /b 1
set "PY=%~1"
exit /b 0

:fail
echo.
echo [X] Build failed. Scroll up for the first error message.
endlocal
exit /b 1
