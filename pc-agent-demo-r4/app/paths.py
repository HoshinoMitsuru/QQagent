# -*- coding: utf-8 -*-
"""
paths.py —— 数据目录与资源目录的解析

打包成 exe 之后，`__file__` 会指向 PyInstaller 的临时解压目录（_MEIPASS），
往那里写配置和状态，程序一退出就全丢了。所以这里把三类路径彻底分开：

    资源目录 (resource)  —— 只读，随 exe 一起打包。1 版里没有需要的资源，保留接口。
    数据目录 (data)      —— 可写，config.json / secrets / state / logs 都在这。
    程序目录 (exe dir)   —— 冻结时是 exe 所在目录，源码运行时是仓库根目录。

数据目录的搜索顺序：

    1. 环境变量 QQ_AGENT_HOME（显式指定，最高优先级）
    2. exe 所在目录 —— 前提是**可写**（portable 场景：整个目录拷到 U 盘/VM 就能跑）
    3. %LOCALAPPDATA%\\QQAgent —— 兜底（exe 被放在 Program Files 这类只读位置时）

第 3 条不是可选项：用户完全可能顺手把 exe 丢进 Program Files，那时如果还硬写
exe 目录，只会在第一次保存配置时报一个让人看不懂的 PermissionError。
"""

from __future__ import annotations

import os
import sys

APP_DIR_NAME = "QQAgent"

# 环境变量名（agent.py 也读这一个，两边必须一致）
ENV_HOME = "QQ_AGENT_HOME"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def exe_dir() -> str:
    """冻结时 = exe 所在目录；源码运行时 = 仓库根目录（app/ 的上一级）。"""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resource_dir() -> str:
    """只读资源目录。PyInstaller 单文件模式下是解压出来的临时目录。"""
    if is_frozen():
        return getattr(sys, "_MEIPASS", exe_dir())
    return os.path.dirname(os.path.abspath(__file__))


def _writable(path: str) -> bool:
    """真的去写一个文件来验证，而不是看 os.access —— Windows 上后者对 ACL 不敏感。"""
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write-probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except Exception:
        return False


# 对外别名：诊断模块要用它来回答「数据目录到底能不能写」
is_writable = _writable


def local_appdata_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") \
        or os.path.expanduser("~")
    return os.path.join(base, APP_DIR_NAME)


def resolve_data_dir() -> tuple[str, str]:
    """
    定下数据目录，返回 (路径, 决策原因)。

    每次启动都重新算：用户把 exe 从只读位置挪到可写位置后，应该自动回到便携模式。
    """
    env = (os.environ.get(ENV_HOME) or "").strip()
    if env:
        path = os.path.normpath(os.path.abspath(env))
        os.makedirs(path, exist_ok=True)
        return path, "环境变量 QQ_AGENT_HOME 指定"

    portable = exe_dir()
    if _writable(portable):
        return portable, "exe 所在目录可写（便携模式）"

    fallback = local_appdata_dir()
    os.makedirs(fallback, exist_ok=True)
    return fallback, "exe 所在目录不可写，已回退到 LOCALAPPDATA"


DATA_DIR, DATA_DIR_REASON = resolve_data_dir()

# 下面几个都要能被 agent.py 看见，所以在 import 期就把环境变量定死。
# agent.py 的 _resolve_home() 第一个读的就是它 —— 这样两边绝对不会各写各的。
os.environ[ENV_HOME] = DATA_DIR

CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
SECRETS_PATH = os.path.join(DATA_DIR, "secrets.local.json")
STATE_DIR = os.path.join(DATA_DIR, "state")
LOG_DIR = os.path.join(DATA_DIR, "logs")
UI_LOG_PATH = os.path.join(LOG_DIR, "ui.log")
AGENT_LOG_PATH = os.path.join(LOG_DIR, "agent.log")
HEARTBEAT_PATH = os.path.join(STATE_DIR, "heartbeat.json")
# 停止哨兵：一个文件的存在与否，就是「请优雅退出」的全部协议。
# 之所以不用 Ctrl+C —— 子进程是 CREATE_NO_WINDOW 起的，没有控制台，信号送不进去。
STOP_PATH = os.path.join(STATE_DIR, "STOP")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def ensure_dirs() -> None:
    for d in (DATA_DIR, STATE_DIR, LOG_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


def describe() -> dict:
    return {
        "data_dir": DATA_DIR,
        "data_dir_reason": DATA_DIR_REASON,
        "exe_dir": exe_dir(),
        "resource_dir": resource_dir(),
        "frozen": is_frozen(),
        "config_path": CONFIG_PATH,
        "secrets_path": SECRETS_PATH,
        "heartbeat_path": HEARTBEAT_PATH,
        "stop_path": STOP_PATH,
        "state_dir": STATE_DIR,
        "log_dir": LOG_DIR,
    }
