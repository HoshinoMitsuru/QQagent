# -*- mode: python ; coding: utf-8 -*-
"""
qq-agent.spec —— PyInstaller 打包配置

一个 exe 里同时装着「界面壳」和「agent 本体」，靠第一个参数区分（见 app/main.py）。
这样做的原因很实际：冻结之后只有一个可执行文件，如果壳去调用系统的 python，
那台机器上就必须先装 Python 和一整套依赖，「拷过去双击就能用」这个前提就没了。

## 构建开关（用环境变量控制，因为 spec 文件本身不接受命令行参数）

    QQAGENT_CONSOLE=1    构建带控制台的诊断版（能看到实时输出，排错用）
    QQAGENT_ONEDIR=1     构建目录版（启动快，但交付物是一个文件夹）
    QQAGENT_NO_ADMIN=1   不申请管理员权限（默认会申请，双击即弹 UAC）
    QQAGENT_NO_ICON=1    不使用自定义图标

日常构建请直接跑 build.bat，它会把上面这些开关翻译好。
"""

import os
from PyInstaller.utils.hooks import collect_all

HERE = os.path.dirname(os.path.abspath(SPEC))          # noqa: F821  (PyInstaller 注入)
CONSOLE = os.environ.get("QQAGENT_CONSOLE") == "1"
ONEDIR = os.environ.get("QQAGENT_ONEDIR") == "1"
NO_ADMIN = os.environ.get("QQAGENT_NO_ADMIN") == "1"
NO_ICON = os.environ.get("QQAGENT_NO_ICON") == "1"

# ---------------------------------------------------------------- 产物名
# ⚠️ 产物名必须在这里决定，不能「先构建成 qq-agent.exe 再改名」。
#
# PyInstaller 只会按 `name=` 写文件。名字写死成 `qq-agent` 的话，构建任何变体
# 都会**先覆盖掉 dist\qq-agent.exe**（默认交付版），然后再被改名成变体 ——
# 于是「先建默认版、再建变体」的结果是：默认版产物不见了。
# 这件事实测遇到过：建完 nadmin 之后 `dist\qq-agent.exe` 就没了。
#
# 所以让开关直接决定文件名，PyInstaller 写哪个就是哪个，互不干扰：
#   (none)          qq-agent
#   nadmin          qq-agent-noadmin
#   console         qq-agent-console
#   nadmin console  qq-agent-noadmin-console
_SUFFIX = ""
if NO_ADMIN:
    _SUFFIX += "-noadmin"
if CONSOLE:
    _SUFFIX += "-console"
APP_NAME = "qq-agent" + _SUFFIX

# ---------------------------------------------------------------- 资源
datas = [
    (os.path.join(HERE, "app", "web"), "app/web"),          # WebUI 静态文件
]
icon_path = os.path.join(HERE, "app", "assets", "qq-agent.ico")
if not NO_ICON and os.path.isfile(icon_path):
    datas.append((icon_path, "app/assets"))

binaries = []

# ---------------------------------------------------------------- 隐式导入
# `uiautomation` 与 `comtypes` 都有动态生成/加载的子模块，PyInstaller 的静态分析
# 看不到它们。漏掉的典型症状是：打包成功、启动也成功，一调 UIA 就报
# `ModuleNotFoundError: No module named 'comtypes.stream'` —— 只在运行时才炸。
hiddenimports = [
    "app.main", "app.server", "app.supervisor", "app.qqctl", "app.settings",
    "app.tray", "app.platform_win", "app.logbus", "app.paths", "app.runtime",
    "app.errors", "app.diagnose",
    # ---- R4（独立桌面托管）专有 ----
    # 这四个**必须显式列**，因为它们没有任何静态 import 能指向它们：
    #   app.host     server 有 import，写上只是求稳
    #   app.hostd    只由 app/host.py 以**子命令**方式拉起，静态分析看不到
    #   app.hostagent 同上（由 host.grab / hostd 拉起）
    #   app.logtail  main.run_ui 里 import，能自动找到；列出来是防止将来挪位置
    # 漏掉的症状非常典型：源码模式全绿，exe 里点「抓一张画面」报
    # ModuleNotFoundError: No module named 'app.hostagent'。
    "app.host", "app.hostd", "app.hostagent", "app.logtail",
    "app.desktop", "app.winmsg",
    # 错误码目录：agent 与 app 共用同一份。虽然两边都是静态 import（能被自动分析到），
    # 还是显式列出来 —— 它一旦漏掉，整个「带码报错」的能力会静默退化成裸异常。
    "error_codes",
    # 这三个是通过 importlib 在运行时按名字加载的，静态分析看不到
    "agent", "qqid", "reply_queue",
    "comtypes.stream", "comtypes.client", "comtypes.typeinfo",
    "winreg", "pyperclip", "requests", "uiautomation",
    # QQX 认领根治（2026-09-26 从 CU 同步）：own_processes 走 psutil 读 PEB，
    # CIM 通道对 QQ 主进程返回空 CommandLine 导致 stop 杀不干净
    "psutil",
]

for pkg in ("uiautomation", "comtypes"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# ---------------------------------------------------------------- 排除
# 只排那些「确定用不到、而且体积不小」的。宁少勿多：
# 排错一个库的代价是运行时 ImportError，比多几 MB 严重得多。
excludes = [
    "tkinter", "matplotlib", "numpy", "scipy", "pandas", "PIL", "cv2",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "pytest", "pip", "setuptools", "wheel",
    "IPython", "jupyter", "notebook",
]

# ---------------------------------------------------------------- 分析与产物
a = Analysis(
    [os.path.join(HERE, "qq-agent.py")],
    pathex=[HERE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe_kwargs = dict(
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX 会和 uiautomation 的 COM 加载打架，别开
    console=CONSOLE,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=not NO_ADMIN,    # 双击即申请管理员权限
)
if not NO_ICON and os.path.isfile(icon_path):
    exe_kwargs["icon"] = icon_path

if ONEDIR:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **exe_kwargs)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
              exclude_binaries=False, **exe_kwargs)
