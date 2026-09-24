@echo off
rem ============================================================
rem  run_ui.bat —— 源码模式启动控制台（不打包，改完代码立刻能试）
rem
rem  首次使用请先装依赖：
rem    python -m pip install -r requirements.txt
rem ============================================================
setlocal
cd /d "%~dp0"

rem 如果装了本地 venv 就优先用它；否则回退到 PATH 里的 python
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

"%PY%" -X utf8 -u "qq-agent.py" %*
set RC=%ERRORLEVEL%
if %RC% NEQ 0 (
  echo.
  echo [X] 控制台退出，返回码 %RC%
  pause
)
endlocal
