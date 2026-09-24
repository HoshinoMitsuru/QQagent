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
rem    dist\qq-agent.exe                    (single-file build, the deliverable)
rem    dist\qq-agent-noadmin.exe            (nadmin switch)
rem    dist\qq-agent-console.exe            (console switch)
rem    dist\qq-agent-noadmin-console.exe    (both switches)
rem    dist\qq-agent\                       (folder build)
rem
rem  Why the noadmin variant matters: the default exe carries a
rem  requireAdministrator manifest, and CreateProcess CANNOT elevate -- so a
rem  non-elevated process (including test_exe.py, or the shell spawning itself)
rem  gets WinError 740 when it tries to start it. The noadmin build is what you
rem  run the automated exe tests against.
rem
rem  NOTE: keep this file ASCII-only. Non-ASCII inside a .bat gets mangled by the
rem  OEM codepage on Chinese Windows and the script then misbehaves silently.
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "QQAGENT_CONSOLE=0"
set "QQAGENT_ONEDIR=0"
set "QQAGENT_NO_ADMIN=0"

:parse
if "%~1"=="" goto afterparse
if /i "%~1"=="console" set "QQAGENT_CONSOLE=1"
if /i "%~1"=="diag"    set "QQAGENT_CONSOLE=1"
if /i "%~1"=="dir"     set "QQAGENT_ONEDIR=1"
if /i "%~1"=="onedir"  set "QQAGENT_ONEDIR=1"
rem NOTE: both spellings are accepted on purpose. This flag used to be written
rem as "nadmin" in the usage text but matched only "noadmin" here, so following
rem the docs produced a silently WRONG build (still requireAdministrator) and a
rem confusing WinError 740 the first time you tried to launch it.
if /i "%~1"=="nadmin"  set "QQAGENT_NO_ADMIN=1"
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
rem NOTE: build_icon.py lives in archive\ (probes and build helpers were all
rem moved there). It resolves the project root itself, so the path below is the
rem only thing that has to stay in sync. A missing icon is NOT fatal for
rem PyInstaller -- but it would silently produce an exe without one, so we fail.
if not "%QQAGENT_NO_ICON%"=="1" (
  echo [i] Generating icon ...
  "%PY%" "archive\build_icon.py" || goto :fail
  if not exist "app\assets\qq-agent.ico" (
    echo [X] Icon was not produced at app\assets\qq-agent.ico
    goto :fail
  )
)

rem ---- artifact name ----------------------------------------------------------
rem PyInstaller always emits dist\qq-agent.exe (the spec hard-codes that name),
rem so the three variants used to overwrite each other and had to be renamed by
rem hand. That is how a "console build" could silently clobber the admin build.
rem The switch decides the file name:
rem   (none)                 dist\qq-agent.exe              <- the deliverable
rem   nadmin                 dist\qq-agent-noadmin.exe      <- no UAC prompt
rem   console                dist\qq-agent-console.exe      <- keeps a console
rem   nadmin console         dist\qq-agent-noadmin-console.exe
rem
rem This block MUST run before the cleanup below: the cleanup has to know which
rem file it is allowed to delete. It used to delete dist\qq-agent.exe
rem unconditionally, so building the default first and a variant second
rem **deleted the deliverable** and left only the variant.
set "SUFFIX="
if "%QQAGENT_NO_ADMIN%"=="1" set "SUFFIX=noadmin"
if "%QQAGENT_CONSOLE%"=="1" (
  if defined SUFFIX (set "SUFFIX=!SUFFIX!-console") else (set "SUFFIX=console")
)
if "%QQAGENT_ONEDIR%"=="1" (
  set "TARGET=dist\qq-agent"
  set "TARGET_EXE=dist\qq-agent\qq-agent.exe"
) else if defined SUFFIX (
  set "TARGET=dist\qq-agent-!SUFFIX!.exe"
  set "TARGET_EXE=!TARGET!"
) else (
  set "TARGET=dist\qq-agent.exe"
  set "TARGET_EXE=dist\qq-agent.exe"
)

rem ---- clean previous artefacts ----------------------------------------------
if exist "build\qq-agent" rmdir /s /q "build\qq-agent" >nul 2>&1
if exist "!TARGET_EXE!" del /q "!TARGET_EXE!" >nul 2>&1

rem ---- build ------------------------------------------------------------------
echo.
echo [i] Building (console=%QQAGENT_CONSOLE% onedir=%QQAGENT_ONEDIR% name=!TARGET!) ...
echo.
"%PY%" -m PyInstaller --noconfirm --clean qq-agent.spec || goto :fail

rem NOTE: no renaming here on purpose. The spec names the artefact from the same
rem switches (see APP_NAME there), so PyInstaller writes the final file directly.
rem An earlier version built dist\qq-agent.exe and then renamed it -- which
rem silently overwrote the default deliverable whenever a variant was built.

echo.
echo ============================================================================
if "%QQAGENT_ONEDIR%"=="1" (
  echo [OK] Output folder: dist\qq-agent\
  echo      Run: dist\qq-agent\qq-agent.exe
) else (
  echo [OK] Output file: !TARGET!
  for %%F in ("!TARGET!") do echo      Size: %%~zF bytes
  if defined SUFFIX (
    echo.
    echo [i] This is a VARIANT build ^(not the default deliverable^).
    echo     Variants exist so you can run/test without an elevated shell:
    echo       nadmin  - no UAC prompt, so non-elevated processes can start it
    echo                 ^(this is what test_exe.py needs^)
    echo       console - keeps a console window for live output
  )
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
