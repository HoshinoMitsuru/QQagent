# -*- mode: python ; coding: utf-8 -*-
"""
qq-cu.spec —— PyInstaller 打包配置（qq-cu.exe：Agent 可调用的 CU CLI）

与 qq-agent.spec（V2 控制台壳）是**两份独立 spec、两个产物**，原因：
1. 形态不同：qq-cu 是纯 CLI（console=True、免 UAC），qq-agent 是窗口壳；
2. 体积取舍不同：qq-cu 的 verifier 依赖 Pillow（BMP→PNG 转码），**必须带 PIL**；
   qq-agent 刻意排除它。

## 冻结形态的三条铁律在这里的落点（详见 build.bat 头注释与项目记忆）

1. host.grab 冻结后会 spawn exe 自己 + `--run-hostagent` —— 所以入口
   qq-cu.py 必须认识这个子命令（见 qq-cu._frozen_dispatch），
   而 app.hostagent 必须进 hiddenimports（静态分析看不到它）。
2. agent / qqid 由 importlib 式路径引用，同样显式列。
3. uiautomation / comtypes 的动态子模块照旧 collect_all。
"""

import os
from PyInstaller.utils.hooks import collect_all

HERE = os.path.dirname(os.path.abspath(SPEC))          # noqa: F821  (PyInstaller 注入)

APP_NAME = "qq-cu"          # 固定名：这是给 agent 调的稳定契约，不做变体

icon_path = os.path.join(HERE, "app", "assets", "qq-agent.ico")

datas = []
binaries = []

hiddenimports = [
    # ---- hosted 链路（冻结后由 exe 自己 spawn 自己，静态分析看不到）----
    "app.hostagent", "app.host", "app.desktop", "app.winmsg",
    "app.paths", "app.platform_win", "app.qqctl",
    # ---- attach 链路 ----
    "agent", "qqid", "error_codes", "reply_queue",
    # ---- CU 包本体 ----
    "cu.base", "cu.attach", "cu.hosted", "cu.tools", "cu.brain", "cu.verifier",
    # ---- UIA/COM 运行时 ----
    "comtypes.stream", "comtypes.client", "comtypes.typeinfo",
    "uiautomation", "pyperclip", "requests", "winreg",
    # host.own_processes 走 psutil 读 PEB 命令行（认领自己的 QQ 实例）
    "psutil",
    # ---- verifier 依赖：BMP→PNG 转码（qq-agent.spec 排除它，cu 不能排）----
    "PIL", "PIL.Image",
]

for pkg in ("uiautomation", "comtypes"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

excludes = [
    "tkinter", "matplotlib", "numpy", "scipy", "pandas", "cv2",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
    "pytest", "pip", "setuptools", "wheel",
    "IPython", "jupyter", "notebook",
]

a = Analysis(
    [os.path.join(HERE, "qq-cu.py")],
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
    console=True,              # CLI：agent 要读 stdout，必须带控制台
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=False,           # 免 UAC：agent 的非提权 shell 必须能拉起它
)
if os.path.isfile(icon_path):
    exe_kwargs["icon"] = icon_path

exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          exclude_binaries=False, **exe_kwargs)
