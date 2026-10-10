# -*- coding: utf-8 -*-
"""
paths.py —— 数据目录与资源目录的解析

打包成 exe 之后，`__file__` 会指向 PyInstaller 的临时解压目录（_MEIPASS），
往那里写配置和状态，程序一退出就全丢了。所以这里把三类路径彻底分开：

    资源目录 (resource)  —— 只读，随 exe 一起打包。1 版里没有需要的资源，保留接口。
    数据目录 (data)      —— 可写，config.json / secrets / state / logs 都在这。
    程序目录 (exe dir)   —— 冻结时是 exe 所在目录，源码运行时是仓库根目录。

数据目录的搜索顺序（2026-10-10 起，第 3 条是新增的，理由见 `resolve_data_dir`）：

    1. 环境变量 QQ_AGENT_HOME（显式指定，最高优先级）
    2. exe 所在目录 —— **且那里已经有一份我们的 config.json**（便携场景：
       整个目录拷到 U 盘/VM 就能跑）
    3. exe 所在目录的**上一级** —— 且那里有一份我们的 config.json

       打包后 exe 在 `dist` 目录里，而「这个项目的配置」在人眼里是**项目根目录**
       那份 `config.json`。原来两者毫无关联：人在项目根填好 Key，双击 dist 里的
       exe 却读到一份新建的空配置，日志只有一句「没有可用的 API Key」——
       没有一个字告诉他「你填的那份不是我在读的那份」。2026-10-10 实测复现过。

    4. exe 所在目录 —— 只要**可写**（全新安装：在 exe 旁边新建配置）
    5. %LOCALAPPDATA%\\QQAgent —— 兜底（exe 被放在 Program Files 这类只读位置时）

第 5 条不是可选项：用户完全可能顺手把 exe 丢进 Program Files，那时如果还硬写
exe 目录，只会在第一次保存配置时报一个让人看不懂的 PermissionError。

第 2 条优先于第 3 条：exe 旁边就有配置 = 这是一台便携部署，
不该被上一级的旧文件抢走。
"""

from __future__ import annotations

import json
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


def _has_our_config(dir_path: str) -> bool:
    """
    这个目录里是否有一份**我们的** config.json。

    ## 为什么不能只看「文件在不在」

    第 3 条新规则（用上一级目录）必须防一手：用户把 exe 丢进任何一个恰好放着
    别人的 `config.json` 的目录（下载文件夹、桌面……），都会被那份无关配置悄悄
    接管 —— 那种故障比现在这个「读不到」更难查，因为它不报错，只是行为诡异。

    所以只认「能解析成 JSON 对象、并且长得像我们写的」：
    有 `llm` 段，或有我们特有的 `_xxx说明` 注释键。

    解析失败（半截文件 / 不是 JSON）也当「不是我们的」—— 那种文件本来也不该被采用。
    """
    path = os.path.join(dir_path, "config.json")
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if isinstance(data.get("llm"), dict):
        return True
    return any(isinstance(k, str) and k.startswith("_") for k in data)


def resolve_data_dir() -> tuple[str, str]:
    """
    定下数据目录，返回 (路径, 决策原因)。

    每次启动都重新算：用户把 exe 从只读位置挪到可写位置后，应该自动回到便携模式。

    ## 为什么多了「上一级目录」这一条（2026-10-10）

    原来只有「env → exe 目录可写就用 → LOCALAPPDATA」。打包版 exe 在 `dist` 目录，
    于是**在项目根目录编辑 config.json 对它是完全无效的**，而且不报「你填错地方了」：
    实测 `sys.frozen=True` + `sys.executable=<树>/dist/qq-agent.exe` →
    `DATA_DIR=<树>/dist`、`dist/config.json` 不存在 → `E-CFG-005` + `E-LLM-001`
    「没有可用的 API Key」。用户真实用法是「把整个目录拷到另一台机器 → 双击 exe」，
    踩的正是这条。

    新规则把「配置已经存在」当成比「exe 在哪」更强的信号：
    先看 exe 旁边、再看上一级，都没有才在 exe 旁边新建。**只有 exe 旁边没有
    config.json 时才会去看上一级** —— 所以已经跑起来的部署（`dist/config.json`
    已存在）行为完全不变。
    """
    env = (os.environ.get(ENV_HOME) or "").strip()
    if env:
        path = os.path.normpath(os.path.abspath(env))
        os.makedirs(path, exist_ok=True)
        return path, "环境变量 QQ_AGENT_HOME 指定"

    portable = exe_dir()
    if _has_our_config(portable):
        return portable, "exe 所在目录里已有 config.json（便携模式）"

    parent = os.path.dirname(portable)
    if parent and parent != portable and _has_our_config(parent) and _writable(parent):
        return parent, (f"exe 所在目录没有 config.json，改用上一级目录 "
                        f"「{os.path.basename(parent) or parent}」里的那一份")

    if _writable(portable):
        return portable, "exe 所在目录可写（便携模式，将在此新建 config.json）"

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
