@echo off
rem ============================================================================
rem  build-cu.bat -- build qq-cu.exe (the CU CLI deliverable for agent callers)
rem
rem  Usage:
rem    build-cu.bat            build dist\qq-cu.exe
rem
rem  Why a separate script/spec from build.bat (qq-agent.exe):
rem    - qq-cu is a pure CLI: console=True and NO admin manifest (an elevated
rem      exe cannot be spawned by a non-elevated agent shell -> WinError 740).
rem    - qq-cu must KEEP Pillow (verifier needs BMP->PNG transcoding), while
rem      qq-agent.spec deliberately excludes it.
rem    - Separate spec = the two deliverables never clobber each other.
rem
rem  Output: dist\qq-cu.exe  (single-file, console, no UAC)
rem
rem  Data directory (paths.py resolution): QQ_AGENT_HOME env -> exe dir -> source
rem  dir. Point QQ_AGENT_HOME at a folder holding config.json + secrets.local.json
rem  to share state between source runs and the exe.
rem
rem  NOTE: keep this file ASCII-only (OEM codepage on Chinese Windows).
rem ============================================================================
setlocal
cd /d "%~dp0"

set "PY="
if defined QQAGENT_PY call :check "%QQAGENT_PY%"
if not defined PY if exist ".venv\Scripts\python.exe" call :check ".venv\Scripts\python.exe"
if not defined PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" (
  call :check "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
)
for /f "delims=" %%P in ('where python 2^>nul') do (
  if not defined PY call :check "%%P"
)
if not defined PY (
  echo [X] No python with PyInstaller found. Install deps first (see build.bat).
  exit /b 2
)

echo [i] Python: %PY%
if exist "build\qq-cu" rmdir /s /q "build\qq-cu" >nul 2>&1
if exist "dist\qq-cu.exe" del /q "dist\qq-cu.exe" >nul 2>&1

"%PY%" -m PyInstaller --noconfirm --clean qq-cu.spec || goto :fail

echo.
echo ============================================================================
echo [OK] Output: dist\qq-cu.exe
for %%F in ("dist\qq-cu.exe") do echo      Size: %%~zF bytes
echo.
echo Smoke test (hosted, read-only):
echo   dist\qq-cu.exe health --json
echo   dist\qq-cu.exe sessions --json
echo Data dir: set QQ_AGENT_HOME to a folder with config.json + secrets.local.json
echo (otherwise the exe reads/writes state next to ITSELF, i.e. dist\).
echo ============================================================================
endlocal
exit /b 0

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
