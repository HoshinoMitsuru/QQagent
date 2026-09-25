# -*- coding: utf-8 -*-
"""
host.py —— 用**独立 profile** 启动 / 认领 / 停止 QQ，可指定落在哪张桌面上

## 这个模块为什么存在（先说结论，因为它是一个被实测逼出来的设计）

2026-09-24 的侦察结论（证据在 `archive/two-accounts.json`）：

    本机同时登录的两个 QQ，主进程命令行**逐字相同**，
    而且所有子进程的 --user-data-dir 都指向同一个 `%APPDATA%\\QQ`。
    只读路径下连「自己这个号是多少」都读不到 —— 主界面上只有昵称/头像/签名/天气，
    扫完 894 个节点 × 8 类 UIA 属性，命中 0。

也就是说：**事后从界面或命令行反推「哪个进程是哪个号」这条路是不通的**
（能拿到昵称，但昵称可改，且拿不到 QQ 号）。

于是换一条路：让身份由**启动参数**保证，而不是事后去猜。

    命令行里带我们那份 --user-data-dir 的 QQ 进程 == 我们托管的小号。

这一条带来三个直接好处：

    1. 壳活在用户桌面上，**看不到**隐藏桌面的窗口（`EnumWindows` 只枚举本桌面），
       但**看得到进程** —— 进程枚举不受桌面限制。于是凭命令行就能认领自己的实例。
    2. 不会误伤用户自己的 QQ：停止时按 pid 精确结束，绝不按进程名批量杀。
    3. 独立 profile 正好也是 Electron 单实例锁的作用域 ——
       否则「再起一个 QQ」会被已有实例接管（只把窗口唤到前台、参数不生效），
       这正是 `qqctl.restart_qq` 注释里那个最容易白折腾半小时的坑。

**代价**：独立 profile 没有登录态 → 首次必须登录一次。
所以流程天然是两段：

    第一段  `start(--visible)`  —— 落在用户看得见的桌面，用户自己扫码登录小号
    第二段  `start(--hidden)`   —— 同一份 profile 起在隐藏桌面，复用登录态

这也是为什么「截图扫码」不该当主线：把登录这件麻烦事放在用户看得见的界面上，
让风控/验证码/设备锁这些意外由用户自己处理，比我们在看不见的桌面上猜要稳得多。

## 关于桌面句柄的存活（重要，别踩）

`desktop.create()` 建出的桌面，靠**句柄**活着。本模块是短命 CLI 时，
进程一退句柄就还了 —— 桌面靠启动上去的 QQ 自己持有而继续存在。
QQ 一旦退出，桌面随之消失，下次要重建（这没问题，`create()` 对同名是幂等的）。
**这条对一次性动作成立，对常驻不成立** —— 常驻下「QQ 崩了 → 桌面没了」这件事
在用户桌面上完全看不出来，所以常驻必须有一个长活进程一直握着句柄。

常驻场景则需要一个长活的宿主进程一直持有句柄 —— 那就是 `app/hostd.py`，
由本模块的 `start_daemon()` 丢进隐藏桌面。

## 用法

    python -m app.host status
    python -m app.host start --visible          # 首次登录用这个
    python -m app.host start --hidden           # 登录态有了之后用这个
    python -m app.host stop --yes

    python -m app.host daemon start             # 常驻：宿主 + QQ + 回复循环
    python -m app.host daemon start --dry-run   # 常驻但只读不发
    python -m app.host daemon status
    python -m app.host daemon stop
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import desktop, paths, platform_win as pw, qqctl
from .logbus import BUS

#: 显式指定 profile 目录时用的环境变量（最高优先级）
ENV_PROFILE = "QQ_AGENT_QQ_PROFILE"

#: profile 子目录名
PROFILE_DIR_NAME = "qq-profile"

PROCESS_NAMES = qqctl.QQ_PROCESS_NAMES
ACCESSIBILITY_FLAG = qqctl.ACCESSIBILITY_FLAG

#: 反节流四件套。
#:
#: R4 里窗口在那张桌面上是**可见且唯一**的，Chromium 结构性算不出遮挡，
#: 所以理论上这四条用不上。保留的理由是兜底：万一桌面被切到不可见状态
#: （锁屏、远程桌面接管），节流还是会来，而这四条没有任何副作用。
ANTI_THROTTLE_FLAGS = (
    "--disable-features=CalculateNativeWinOcclusion",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
)

#: 启动记录。壳靠它知道「上次起的是谁」；
#: 即便进程已经没了，也能据此判断 profile 是不是曾经登录过。
STATE_PATH = os.path.join(paths.STATE_DIR, "host.json")


# ============================================================ profile 目录
def profile_dir(custom: str = "") -> str:
    """
    定下 QQ 的独立 profile 目录。优先级：显式传参 > 环境变量 > 兜底。

    ## 为什么兜底落在 LOCALAPPDATA 而不是数据目录

    数据目录在源码运行模式下就是仓库根目录。把 profile 放那儿有三个坏处：

      · 体积 GB 级（实测用户现有 profile 2.3 GB），会被编辑器索引、
        备份工具、杀软扫描反复吃掉 IO
      · 它装的是**登录态**，属于机器私有数据，不该跟源码走
      · 换 exe 位置（便携模式）时不该丢登录态

    LOCALAPPDATA 三条都躲开了。

    ## 为什么绝不能落在 `%APPDATA%\\QQ`

    那是用户现在正在用的 profile。指过去会直接抢掉主号的登录态，
    而且两个进程写同一份 profile 会损坏数据 —— 这是本模块最该防的事，
    所以下面 `_is_forbidden_profile()` 会显式拦一道。
    """
    p = (custom or "").strip().strip('"')
    if p:
        return os.path.normpath(os.path.abspath(p))
    env = (os.environ.get(ENV_PROFILE) or "").strip().strip('"')
    if env:
        return os.path.normpath(os.path.abspath(env))
    return os.path.join(paths.local_appdata_dir(), PROFILE_DIR_NAME)


def _is_forbidden_profile(path: str) -> str:
    """
    拦住「指到用户现有 QQ profile」这种会毁数据的用法。

    返回空串 = 没问题；非空 = 禁止的原因。
    """
    low = os.path.normpath(os.path.abspath(path)).lower()
    appdata = (os.environ.get("APPDATA") or "").lower()
    for bad in (os.path.join(appdata, "qq") if appdata else "",
                os.path.join(appdata, "tencent", "qq") if appdata else ""):
        if bad and low == os.path.normpath(bad).lower():
            return (f"这个目录是 QQ 自己的 profile（{bad}）。指向它会抢掉现有登录态，"
                    f"且两个进程同时写会损坏数据。")
    return ""


# ============================================================ 启动参数
def launch_args(profile: str, extra_args: str = "") -> list[str]:
    """
    组装给 QQ 的参数。**不含 exe 自身** —— `desktop.spawn(exe, args)` 会把 exe
    拼到命令行最前面，这里再带一次就变成两个 exe 参数了。

    第一个参数是 `--force-renderer-accessibility`：没有它，Chromium 不暴露
    无障碍树，后面什么都读不到（这是 UIA 路线的前提，不是优化项）。
    """
    args = [ACCESSIBILITY_FLAG, f"--user-data-dir={profile}"]
    args += list(ANTI_THROTTLE_FLAGS)
    if (extra_args or "").strip():
        args += [a for a in extra_args.split() if a]
    return args


# ============================================================ 启动
def start(*, desktop_name: str = "", profile: str = "", qq_exe: str = "",
          extra_args: str = "", create: bool = True) -> dict:
    """
    启动一个用独立 profile 的 QQ。

    `desktop_name` 为空 = **不指定桌面**，进程落在调用者当前桌面
    （= 用户看得见，用于首次登录）。给了名字才是隐藏桌面常驻。

    返回 `{ok, pid, mode, desktop, profile, exe, args, error}`。不抛异常。
    """
    out = {"ok": False, "pid": 0, "mode": "visible", "desktop": desktop_name or "",
           "profile": "", "exe": "", "args": [], "error": ""}

    found = qqctl.find_qq_exe(qq_exe)
    exe = found.get("path", "")
    if not exe:
        out["error"] = (f"找不到 QQ.exe（已试 {len(found.get('candidates', []))} 个位置）。"
                        f"可以在界面里手动指定 QQ 路径。")
        return out
    out["exe"] = exe

    prof = profile_dir(profile)
    why = _is_forbidden_profile(prof)
    if why:
        out["error"] = why
        return out
    try:
        os.makedirs(prof, exist_ok=True)
    except Exception as exc:
        out["error"] = f"profile 目录建不起来（{prof}）：{type(exc).__name__}: {exc}"
        return out
    out["profile"] = prof

    if desktop_name:
        if create:
            r = desktop.create(desktop_name)
            if not r["ok"]:
                out["error"] = f"E-DESK-001 {r['error']}"
                return out
            if not desktop.wait_for_desktop_ready(desktop_name, timeout=2.0):
                out["error"] = ("E-DESK-001 桌面建出来了但打不开 —— "
                                "后续往它上面丢进程必然失败，先在这里报清楚")
                return out
        else:
            if not desktop.exists(desktop_name):
                out["error"] = (f"E-DESK-001 桌面 {desktop_name} 不存在"
                                f"（加 --create 才会新建）")
                return out
        out["mode"] = "hidden"

    args = launch_args(prof, extra_args)
    out["args"] = args
    spawned = desktop.spawn(exe, args, desktop=desktop_name or None,
                            cwd=os.path.dirname(exe))
    if not spawned["ok"]:
        out["error"] = f"E-DESK-002 {spawned['error']}"
        return out

    out["ok"] = True
    out["pid"] = spawned["pid"]
    _save_record({
        "pid": spawned["pid"],
        "mode": out["mode"],
        "desktop": desktop_name or "",
        "profile": prof,
        "exe": exe,
        "args": args,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return out


# ============================================================ 认领自己的实例
def own_processes(profile: str = "") -> list[dict]:
    """
    找出「我们起的那些 QQ 进程」—— 判据是命令行里带我们那份 profile 路径。

    ## 为什么这是唯一可行的认领方式

    壳在用户桌面上，`EnumWindows` 看不到隐藏桌面的窗口，所以**窗口这条路断了**。
    但进程枚举（`CreateToolhelp32Snapshot` / CIM）是**整机**的，不受桌面限制 ——
    于是「命令行里有没有我们那份 --user-data-dir」成了壳唯一拿得到的身份证据。

    ## 代价

    `process_command_lines` 走 PowerShell，约 1 秒。**不要放进状态轮询**，
    它只该在「启动 / 停止 / 体检」这些低频动作里调。

    注意子进程也会带上这个路径（QQ 会给渲染进程显式指定同一份 user-data-dir），
    所以返回的通常不止一个 pid —— 主进程是其中启动最早的那个，
    `status()` 里用记录里的 pid 做交叉核对。
    """
    prof = profile_dir(profile)
    marker = prof.lower().rstrip("\\/")
    out: list[dict] = []
    for row in pw.process_command_lines(PROCESS_NAMES):
        cmd = (row.get("cmdline") or "").lower().replace('"', "").replace("'", "")
        if marker and marker in cmd:
            out.append(row)
    return out


def _save_record(rec: dict) -> None:
    try:
        os.makedirs(paths.STATE_DIR, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def last_record() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ============================================================ 停止
def stop(profile: str = "", timeout: float = 12.0) -> dict:
    """
    只结束**我们起的**那个 QQ。

    ⚠️ 为什么绝不复用 `pw.kill_processes(("QQ.exe",))`：那个函数按名字批量杀，
    会把用户正在用的主号一起带走，正在打的字全丢。这里是按 pid 精确结束，
    而且 pid 来自「命令行带我们那份 profile」这个判据 —— 认领不到就一个都不杀。
    """
    out = {"ok": False, "killed": 0, "pids": [], "error": ""}
    procs = own_processes(profile)
    if not procs:
        out["error"] = "没有认领到任何属于本工具的 QQ 进程（它本来就没在跑）"
        out["ok"] = True
        return out

    rec = last_record()
    main = int(rec.get("pid") or 0)
    pids = [int(p["pid"]) for p in procs]
    # 先杀主进程（带进程树）：渲染进程是它的子进程，理论上会跟着走。
    order = ([main] if main in pids else []) + [p for p in pids if p != main]
    for pid in order:
        try:
            if pw.kill_pid(pid, tree=True):
                out["killed"] += 1
                out["pids"].append(pid)
        except Exception:
            pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not own_processes(profile):
            break
        time.sleep(0.4)
    out["ok"] = not own_processes(profile)
    if not out["ok"]:
        out["error"] = "结束超时：仍有进程残留（多半是子进程没跟着退出）"
    return out


# ============================================================ 状态
def status(profile: str = "") -> dict:
    """
    给界面和诊断报告用的一次性快照。注意 `own_processes` 约 1 秒，别轮询。
    """
    prof = profile_dir(profile)
    rec = last_record()
    procs = own_processes(profile)
    pids = sorted(int(p["pid"]) for p in procs)

    exists_profile = os.path.isdir(prof)
    # 「有没有登录态」的最朴素判据：profile 里出现账号数据目录。
    # 不做更深的猜测 —— 真的确认登录了没有，得靠宿主进程在那张桌面上读界面。
    subdirs = []
    if exists_profile:
        try:
            subdirs = sorted(d for d in os.listdir(prof)
                             if os.path.isdir(os.path.join(prof, d)))[:20]
        except Exception:
            pass

    return {
        "profile": prof,
        "profile_exists": exists_profile,
        "profile_subdirs": subdirs,
        "own_pid_count": len(pids),
        "own_pids": pids,
        "running": bool(pids),
        "last_record": rec,
        "record_pid_alive": int(rec.get("pid") or 0) in pids if rec else False,
        "desktop_default": desktop.DEFAULT_NAME,
        "desktop_exists": desktop.exists(desktop.DEFAULT_NAME),
        "current_desktop": desktop.current_name(),
        "profile_forbidden": _is_forbidden_profile(prof),
    }


def describe() -> dict:
    return {
        "env_profile": ENV_PROFILE,
        "profile_dir": profile_dir(),
        "state_path": STATE_PATH,
        "desktop": desktop.describe(),
        "status": status(),
    }


# ============================================================ 派子进程
#: 宿主进程的产出文件。壳读它，才知道隐藏桌面上到底发生了什么。
GRAB_JSON = os.path.join(paths.STATE_DIR, "host-grab.json")

#: 模块名 → 冻结后的子命令。R4 的两个角色都是包内模块，不能靠 `-m`。
_FROZEN_SUBCMD = {
    "app.hostagent": "--run-hostagent",
    "app.hostd": "--run-hostd",
}


def child_args(module: str) -> list[str]:
    """
    组装「让本程序再跑一个自己」时该用的参数前缀。

    ## 为什么不能一律用 `-m`

    源码模式：`python -m app.hostagent` 最直接，`sys.executable` 就是解释器。
    冻结之后 `sys.executable` 是 **exe 自己**，而 exe 不认识 `-m` ——
    必须换成 exe 自己的子命令（`--run-hostagent` / `--run-hostd`），
    再由 `app/main.py` 转发回同一份代码。

    这是打包版最容易漏的一环：**源码下一切正常，exe 里报的却是一句
    「无法识别的参数」**，然后人会去查 QQ 路径。所以这里把两种形态收在同一个
    函数里，谁新增一个宿主角色都只改这一处（`_FROZEN_SUBCMD` 也一起加）。
    """
    if getattr(sys, "frozen", False):
        cmd = _FROZEN_SUBCMD.get(module)
        if not cmd:
            # 没登记就按约定推一个 —— 与其静默用 `-m`（必然失败），
            # 不如让失败信息直接指出「这里漏登记了」。
            cmd = "--run-" + module.split(".")[-1]
        return [cmd]
    return ["-m", module]


def grab(png_path: str = "", desktop_name: str = "", *, wait: float = 0.0,
         uia: bool = True, tree_path: str = "", login: bool = False,
         dry_run: bool = False, state: bool = False, open_chat: bool = False,
         chat: str = "", chat_index: int = 0, timeout: float = 45.0,
         list_sessions: bool = False, read_messages: int = 0,
         send_text: str = "") -> dict:
    """
    派一个宿主进程进隐藏桌面，把那边的窗口画面抓回来。

    ## 为什么必须绕这一圈

    壳在用户桌面上，`EnumWindows` 只枚举本桌面 —— 它对隐藏桌面的窗口是瞎的。
    所以「找窗口 + 抓图」这两步只能交给**同样在那张桌面上**的进程去做，
    也就是 `app/hostagent.py`。这是 R4 里「宿主进程」这个概念的最小可用形态。

    ## 链路

        spawn(python -m app.hostagent, desktop=NAME)
            → 子进程自报桌面名 / 找 QQ 窗口 / PrintWindow 抓图 / 写 JSON
            → 本函数等它结束，读回 JSON

    走文件不走 stdout：`CreateProcessW` 没接管管道（见 desktop.spawn 的说明）。

    冻结成 exe 后 `sys.executable` 是 exe 自己，接不了 `-m app.hostagent` ——
    这一点由 `child_args()` 统一处理（换成 `--run-hostagent`），
    调用方不需要关心当前是源码还是打包。

    ## CU 执行面新增的三个动作（cu.hosted 专用）

    `list_sessions` / `read_messages` / `send_text` 让一次性宿主子进程顺带
    完成语义原语的读写。子进程是**全新**的，做完就退 —— 这正是「宿主进程
    里不要提前碰 UIA」铁律的安全形态：UIA 的任何坏状态跟着进程一起消失。
    结果分别写在返回 JSON 的 `cu.sessions` / `cu.messages` / `cu.send` 里。
    """
    out = {"ok": False, "desktop": desktop_name or desktop.DEFAULT_NAME,
           "json": GRAB_JSON, "png": "", "error": ""}
    name = desktop_name or desktop.DEFAULT_NAME
    if not desktop.exists(name):
        out["error"] = (f"E-DESK-001 桌面 {name} 不存在（QQ 没起在上面，或上次已退出）")
        return out

    png = png_path or SHOT_PNG
    out["png"] = png
    args = child_args("app.hostagent") + ["--out", GRAB_JSON, "--png", png]
    if uia:
        args.append("--uia")
    if tree_path:
        args += ["--tree", tree_path]
    if state:
        args.append("--state")
    if login:
        args.append("--login")
        if dry_run:
            args.append("--dry-run")
    if open_chat:
        args.append("--open-chat")
        if chat:
            # ⚠️ 用 `=` 形式：`--chat 苏霖韵` 这种带中文值的写法本身没问题，
            # 但值以 `-` 开头时 argparse 会把它当成另一个选项（踩过同类坑）。
            args.append(f"--chat={chat}")
        if chat_index:
            args += ["--chat-index", str(int(chat_index))]
    if wait > 0:
        args += ["--wait", f"{wait:g}"]
    # ---- CU 执行面动作（详见 docstring）----
    if list_sessions:
        args.append("--list-sessions")
    if read_messages and int(read_messages) > 0:
        args += ["--read-messages", str(int(read_messages))]
    if send_text:
        # `=` 形式：消息文本里可能有空格/以 - 开头的内容（理由同上 --chat）
        args.append(f"--send-text={send_text}")

    # 先删掉上一次的结果：否则子进程还没写完，我们就读到了旧内容，
    # 表现成「抓图成功」但图是上一次的 —— 这种假成功最难查。
    try:
        if os.path.isfile(GRAB_JSON):
            os.remove(GRAB_JSON)
    except Exception:
        pass

    r = desktop.spawn(sys.executable, args, desktop=name, cwd=paths.exe_dir())
    if not r["ok"]:
        out["error"] = f"E-DESK-002 宿主进程启动失败：{r['error']}"
        return out
    out["host_pid"] = r["pid"]

    waited = desktop.wait(r["hproc"], timeout)
    desktop.close_handle(r["hproc"])
    out["waited"] = waited
    if waited != 0:
        out["error"] = "宿主进程超时未结束（QQ 可能还在慢慢起来，加大 --wait 再试）"
        return out

    try:
        with open(GRAB_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        out["error"] = (f"读不到宿主结果（{GRAB_JSON}）：{type(exc).__name__}: {exc}"
                        f" —— 子进程可能启动即崩")
        return out
    out.update(data)
    return out


# ============================================================ 常驻宿主进程（hostd）
#
# 到这里为止，本模块干的都是「一次性动作」：起一个 QQ、抓一张图、点一下登录。
# 那些动作做完进程就退了，隐藏桌面靠**启动上去的 QQ 自己持有**而继续存在
# （见文件开头「关于桌面句柄的存活」）。
#
# 常驻不行：QQ 一旦自己退出（崩溃、被更新拉起重启），桌面就跟着没了，
# 而「QQ 没了」这件事在用户桌面上是完全看不见的 —— 表现是常驻静默停摆，
# 界面上却什么都没变。所以常驻形态需要一个**长活的宿主进程**一直待在那张桌面上：
#
#     hostd 负责：持有桌面句柄 / QQ 不在了就重新拉起 / 卡在登录页就点掉登录
#                 / 在那张桌面上跑 agent 的主循环 / 写心跳让壳知道它还活着
#
#: 宿主心跳。宿主自己写，壳只读 —— 这是壳**唯一**能拿到那边状态的通道
#: （窗口看不到、stdout 没有管道，只剩文件）。
HOSTD_JSON = os.path.join(paths.STATE_DIR, "hostd.json")
#: 宿主日志。`desktop.spawn` 不接管 stdout 管道，宿主把 stdout 落到这里，
#: 壳再 tail 进界面的日志面板 —— 否则隐藏桌面上发生的一切对界面都是黑的。
HOSTD_LOG = os.path.join(paths.LOG_DIR, "hostd.log")
#: 宿主的停止哨兵。与 `paths.STOP_PATH` 分开，见 `stop_daemon` 的说明。
HOSTD_STOP = os.path.join(paths.STATE_DIR, "hostd.stop")
#: 抓图落盘位置。**固定文件名**是刻意的：界面上的 <img> 靠一个不会被猜到的
#: 路径 + 令牌访问它，而服务端只允许这一个文件被读出去（见 `server._shot`），
#: 这样就不需要为「按路径读任意文件」开一个口子 —— 那种接口早晚会变成目录穿越。
#:
#: 宿主做免扫码登录时的探测也落在这里：登录卡住时，用户最需要看的就是
#: 「它当时到底看到了什么」，把它落在同一个地方正好。
SHOT_PNG = os.path.join(paths.STATE_DIR, "desktop-shot.png")
#: 心跳超过这个秒数没更新，就认为宿主可能已经不在了。
#: 取 20s 而不是更长：agent 一轮轮询是 0.8s，正常心跳 1s 一次，
#: 20s 足够容忍一次长模型调用，又不至于让「宿主已死」拖太久才被发现。
HOSTD_STALE_SECONDS = 20.0


def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def last_grab() -> dict:
    """
    上一次 `grab()` 的完整结果（界面用它显示「那张桌面上现在是什么样」）。

    注意它可能来自**上一次运行**：这个文件不会自己过期。界面必须把
    `saved.at` 一起显示出来，否则「截图上 QQ 还在登录页」会被当成现状 ——
    那种误判会让人去修一个已经不存在的问题。
    """
    return _read_json(GRAB_JSON)


def daemon_status(stale: float = HOSTD_STALE_SECONDS) -> dict:
    """
    常驻宿主的**轻量**快照 —— 可以放心地每两秒轮询一次。

    ## 为什么必须另做一个轻量版

    `status()` 里那个 `own_processes()` 走 PowerShell，约 1 秒。
    界面的状态流每 2 秒推一次全量状态，把它塞进去等于让界面永远慢半拍。
    而这里只需要三个很便宜的动作：读一个几 KB 的 JSON、问一次「这个 pid 还在吗」、
    看一眼日志文件的大小。

    ## 「活着」的判据为什么是心跳而不是 pid

    pid 会被复用：宿主崩了、别的进程恰好拿到同一个 pid，界面就会显示「运行中」。
    所以主判据是**心跳的新旧**，pid 只作为辅助信息展示。
    反过来，pid 不在了而心跳还新（进程正在退出的那一瞬），也算不在了 ——
    所以最终判据是「pid 活着 **且** 心跳没过期」。
    """
    hb = _read_json(HOSTD_JSON)
    pid = int(hb.get("pid") or 0)
    ts = float(hb.get("ts") or 0.0)
    age = round(time.time() - ts, 1) if ts else -1.0
    alive = pw.pid_alive(pid) if pid else False
    not_stale = 0 <= age <= stale
    return {
        "running": bool(pid) and alive and not_stale,
        "alive": alive,
        "stale": bool(pid) and alive and not not_stale,
        "age": age,
        "stale_after_seconds": stale,
        "pid": pid,
        "desktop": hb.get("desktop") or "",
        "desktop_self": hb.get("desktop_self") or "",
        "phase": hb.get("phase") or "",
        "uptime": hb.get("uptime"),
        "logged_in": bool(hb.get("logged_in")),
        "nickname": hb.get("nickname") or "",
        "qq_pids": hb.get("qq_pids") or [],
        "hwnd": hb.get("hwnd") or 0,
        # 宿主进程的退出码（0=正常，2=启动阶段失败），只在它已经退出时有意义
        "rc": hb.get("rc"),
        "stopped": bool(hb.get("stopped")),
        "error": hb.get("error") or "",
        # 退出原因。`error` 只在失败时写（界面据此决定要不要弹红框），
        # 而 `reason` 每次都写 —— 「上一次是**怎么**停的」这个问题，
        # 在正常停止时同样需要答案（否则只看到一句「已停止」，无从判断
        # 是用户点的、还是它自己退出来的）。
        "reason": hb.get("reason") or "",
        "note": hb.get("note") or "",
        "stop_requested": os.path.isfile(HOSTD_STOP),
        "heartbeat_path": HOSTD_JSON,
        "heartbeat_present": bool(hb),
        "log_path": HOSTD_LOG,
        "log_exists": os.path.isfile(HOSTD_LOG),
        "log_bytes": os.path.getsize(HOSTD_LOG) if os.path.isfile(HOSTD_LOG) else 0,
    }


def start_daemon(*, desktop_name: str = "", profile: str = "", qq_exe: str = "",
                 dry_run: bool = False, no_send: bool = False,
                 with_agent: bool = True, stop_qq_on_exit: bool = False,
                 chat: str = "", wait_ready: float = 0.0) -> dict:
    """
    把常驻宿主进程丢进隐藏桌面。返回 `{ok, pid, desktop, profile, args, error}`。

    ## 为什么宿主要由壳用 lpDesktop 丢进去，而不是自己切过去

    `SetThreadDesktop` 有两个硬限制：线程必须还没建过窗口、还没装过钩子。
    而我们要跑的 agent 一上来就要建 COM 对象、读 UIA。用 `CreateProcessW` 的
    `lpDesktop` 让进程**一出生就在那张桌面上**，没有这些限制 ——
    这也是 `desktop.spawn` 存在的全部理由（见 desktop.py 的模块说明）。

    ## 为什么要先删掉旧心跳

    和 `grab()` 里删旧 JSON 是同一个理由：不删的话，子进程还没写完我们就读到了
    上一次的内容，表现成「启动成功」，但状态是旧的。这种假成功最难查。
    """
    out = {"ok": False, "pid": 0, "desktop": "", "profile": "", "args": [],
           "error": ""}
    st = daemon_status()
    if st["running"]:
        out["error"] = f"E-PROC-001 常驻宿主已经在运行（pid {st['pid']}，已运行 {st['uptime']}s）"
        return out

    # 清哨兵必须在**拉起来之前**：上一次停止留下的 state/STOP 会让新起的
    # 回复循环一进循环就退出（现象是「点启动，转一圈就变已停止」）。
    clear_stop_sentinels("本次启动之前")

    name = desktop_name or desktop.DEFAULT_NAME
    r = desktop.create(name)
    if not r["ok"]:
        out["error"] = f"E-DESK-001 {r['error']}"
        return out
    if not desktop.wait_for_desktop_ready(name, timeout=2.0):
        out["error"] = "E-DESK-001 桌面建出来了但打不开 —— 丢进程上去必然失败"
        return out

    # 清掉上一轮的三个残留：停止哨兵（否则宿主一启动就退出）、
    # 旧心跳（否则读到假状态）、agent 自己的停止哨兵（否则 agent 一轮就退出）。
    for p in (HOSTD_STOP, HOSTD_JSON, paths.STOP_PATH):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass

    args = child_args("app.hostd") + ["--desktop", name]
    if profile:
        args += ["--profile", profile]
    if qq_exe:
        args += ["--qq-exe", qq_exe]
    if dry_run:
        args.append("--dry-run")
    if no_send:
        args.append("--no-send")
    if not with_agent:
        args.append("--no-agent")
    if stop_qq_on_exit:
        args.append("--stop-qq-on-exit")
    if chat.strip():
        args += ["--chat", chat.strip()]

    spawned = desktop.spawn(sys.executable, args, desktop=name,
                            cwd=paths.exe_dir())
    if not spawned["ok"]:
        out["error"] = f"E-DESK-002 {spawned['error']}"
        return out

    out.update({"ok": True, "pid": spawned["pid"], "desktop": name,
                "profile": profile_dir(profile), "args": args})
    desktop.close_handle(spawned["hproc"])

    if wait_ready > 0:
        deadline = time.time() + wait_ready
        while time.time() < deadline:
            time.sleep(0.5)
            st = daemon_status()
            if st["running"] and st["phase"] not in ("启动", ""):
                break
        out["ready"] = daemon_status()
    return out


def clear_stop_sentinels(reason: str = "") -> list[str]:
    """
    清掉两个停止哨兵，返回真正被清掉的文件名。

    ## 为什么必须有这一步（实测会「启动就停」）

    停止流程是两段式的：宿主收到 `hostd.stop` → **自己写 `state/STOP`**
    让 agent 优雅收尾 → 然后自己退出。问题是那两个哨兵**退出时不会被清**：

      · `HOSTD_STOP` 由 `stop_daemon` 在确认宿主退掉之后删（这一条是对的）；
      · `state/STOP` **谁都不删** —— 只有 V1 那条路（`app/supervisor.py`）会清。

    于是隐藏桌面这条路：停一次 → `state/STOP` 留在磁盘上 → **下次启动
    `agent.run_forever()` 一进循环就看到哨兵，立刻退出**。
    用户看到的现象是「点启动，它转一圈就变成已停止」，而日志里只有一句
    「收到停止哨兵」——完全联想不到是上一次留下的文件。

    所以：**启动前清一次、停止后也清一次**。启动前那次是为了消化
    「上次异常退出留下的」；停止后那次是为了让下一次「无论从哪进来」都干净
    （比如有人直接 `python -m app.hostd` 手工拉起，绕过了 start_daemon）。
    """
    cleared: list[str] = []
    for path, label in ((paths.STOP_PATH, "state/STOP（回复循环）"),
                        (HOSTD_STOP, "hostd.stop（宿主）")):
        try:
            if os.path.isfile(path):
                os.remove(path)
                cleared.append(label)
        except Exception:
            pass
    if cleared and reason:
        BUS.emit(f"已清掉上次留下的停止哨兵：{'、'.join(cleared)}（{reason}）",
                 tag="INFO", source="host")
    return cleared


def stop_daemon(timeout: float = 20.0) -> dict:
    """
    让常驻宿主优雅退出。

    ## 为什么宿主要用自己的停止哨兵，而不是复用 `paths.STOP_PATH`

    两个哨兵的语义不一样，混用会造成一种很难查的半死状态：

      · `paths.STOP_PATH`（agent 的）  只让**回复循环**退出。宿主还在，QQ 还在。
      · `HOSTD_STOP`（宿主的）         让**整个隐藏桌面这套**退：循环停 → 宿主退。

    所以停止流程是两段：宿主收到 `HOSTD_STOP` 后**自己去写** `STOP_PATH`，
    给 agent 一个优雅收尾的机会（把队列里已经生成的回复发完），
    等不到才由宿主强退。界面上「停止」按的是后者。
    """
    st = daemon_status()
    out = {"ok": False, "pid": st["pid"], "waited": 0.0, "killed": False,
           "error": ""}
    if not st["pid"] or not st["alive"]:
        try:
            if os.path.isfile(HOSTD_STOP):
                os.remove(HOSTD_STOP)
        except Exception:
            pass
        out["ok"] = True
        out["error"] = "常驻宿主本来就没在跑"
        return out

    try:
        os.makedirs(paths.STATE_DIR, exist_ok=True)
        with open(HOSTD_STOP, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception as exc:
        out["error"] = f"E-PATH-001 写停止哨兵失败（{type(exc).__name__}: {exc}）"
        return out

    t0 = time.time()
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        if not pw.pid_alive(st["pid"]):
            out["ok"] = True
            break
    out["waited"] = round(time.time() - t0, 1)

    if not out["ok"]:
        # 优雅退出等不到（agent 卡在模型调用里最常见）→ 结束进程树。
        # 代价要说清楚：队列里已生成的回复会留在磁盘上，下次启动继续发。
        if pw.kill_pid(st["pid"], tree=True):
            out["killed"] = True
            out["ok"] = True
            out["error"] = f"等满 {timeout:.0f}s 未退出，已强制结束"
    try:
        if os.path.isfile(HOSTD_STOP):
            os.remove(HOSTD_STOP)
    except Exception:
        pass
    out["final"] = daemon_status()
    # 收尾时把 agent 的停止哨兵也清掉：下一次启动不该继承这一次的「停止意图」。
    clear_stop_sentinels("上次停止的收尾")
    return out


# ============================================================ 命令行
def _cmd_status(_a) -> int:
    s = status(_a.profile)
    print(f"profile        : {s['profile']}")
    print(f"  目录存在     : {s['profile_exists']}")
    if s["profile_subdirs"]:
        print(f"  子目录       : {', '.join(s['profile_subdirs'])}")
    if s["profile_forbidden"]:
        print(f"  [!] {s['profile_forbidden']}")
    print(f"本工具实例     : {s['own_pid_count']} 个进程 {s['own_pids'] if s['own_pids'] else ''}")
    print(f"  在跑         : {s['running']}")
    rec = s["last_record"]
    if rec:
        print(f"上次启动       : pid={rec.get('pid')} 模式={rec.get('mode')} "
              f"桌面={rec.get('desktop') or '(可见桌面)'} {rec.get('started_at')}")
        print(f"  那个 pid 还在: {s['record_pid_alive']}")
    print(f"当前线程桌面   : {s['current_desktop']}")
    print(f"隐藏桌面存在   : {s['desktop_exists']}（默认名 {s['desktop_default']}）")
    return 0


def _cmd_start(a) -> int:
    # 三条互斥的意图，按优先级排：--visible（当前桌面）> --hidden（默认隐藏桌面）
    # > --desktop NAME（指定名字，显式）。都没给时按 hidden 处理 ——
    # 因为这是常驻形态的默认值，而「起在用户眼前」是需要特意说明的例外。
    if a.visible:
        name = ""
    elif a.hidden or not a.desktop:
        name = a.desktop or desktop.DEFAULT_NAME
    else:
        name = a.desktop
    r = start(desktop_name=name, profile=a.profile, qq_exe=a.qq_exe,
              extra_args=a.extra_args or "", create=True)
    if not r["ok"]:
        print(f"[X] 启动失败：{r['error']}")
        return 2
    print(f"[OK] 已启动 pid={r['pid']} 模式={r['mode']} "
          f"桌面={r['desktop'] or '(当前/可见桌面)'}")
    print(f"    profile : {r['profile']}")
    print(f"    exe     : {r['exe']}")
    print(f"    args    : {' '.join(r['args'])}")
    if r["mode"] == "visible":
        print("\n这一步是**首次登录**：QQ 窗口现在就在你眼前，请登录小号。")
        print("登录完成后关掉它，再用 --hidden 起第二个（同一份 profile 会复用登录态）。")
    else:
        print("\n它现在在隐藏桌面上，你看不见 —— 这是预期的。")
        print("用 `python -m app.host status` 确认进程还在。")
    return 0


def _cmd_stop(a) -> int:
    s = status(a.profile)
    if not s["running"]:
        print("[i] 没有在跑的本工具实例，无事可做")
        return 0
    if not a.yes:
        print(f"将要结束 {s['own_pid_count']} 个进程：{s['own_pids']}")
        print(f"它们都属于 profile：{s['profile']}")
        print("只会结束命令行里带这份 profile 的 QQ，你自己的 QQ 不受影响。")
        print("加 --yes 才会真的执行。")
        return 1
    r = stop(a.profile)
    print(f"[{'OK' if r['ok'] else 'X'}] 结束 {r['killed']} 个 {r['error']}")
    return 0 if r["ok"] else 2


def _cmd_grab(a) -> int:
    r = grab(a.png, a.desktop, wait=a.wait, uia=not a.no_uia, tree_path=a.tree,
             login=a.login, dry_run=a.dry_run, state=True,
             open_chat=a.open_chat, chat=a.chat, chat_index=a.chat_index)
    print(f"宿主进程自报桌面 : {r.get('desktop') or r.get('error')}")
    if r.get("host_pid"):
        print(f"宿主 pid         : {r['host_pid']}（waited={r.get('waited')}）")
    wins = r.get("windows") or {}
    print(f"那张桌面上的 QQ 窗口 : {wins.get('count', 0)} 个")
    for c in (wins.get("candidates") or [])[:5]:
        print(f"    hwnd={c['hwnd']:<10} pid={c['pid']:<8} vis={int(c['visible'])} "
              f"rect={c['rect']} title={c['title']!r}")
    cap = r.get("capture") or {}
    if cap:
        print(f"抓图统计         : {cap.get('w')}x{cap.get('h')} "
              f"颜色种类={cap.get('distinct_colors')} "
              f"平均亮度={cap.get('mean_luma')} 非黑占比={cap.get('nonblack_ratio')}")
    saved = r.get("saved") or {}
    if saved.get("ok"):
        print(f"[OK] 画面已存    : {saved.get('path')}（{saved.get('bytes')} 字节）")
    else:
        print(f"[X] 落盘失败     : {saved.get('error') or r.get('error')}")
    lg = r.get("login") or {}
    if lg:
        print(f"登录按钮         : {lg.get('button') or lg.get('error')}")
        if lg.get("patterns"):
            print(f"  Pattern 可用性 : {lg['patterns']}")
        if lg.get("auto_login_checkbox"):
            print(f"  自动登录勾选框 : {lg['auto_login_checkbox']}")
            print(f"    可用性       : {lg.get('auto_login_patterns')}")
        if lg.get("note"):
            print(f"  {lg['note']}")
        if lg.get("clicked_via"):
            print(f"  已点击         : 走 {lg['clicked_via']}"
                  f"（自动登录勾选={lg.get('auto_login_toggled')}）")
        if lg.get("error"):
            print(f"  [X] {lg['error']}")
    st = r.get("login_state") or {}
    if st.get("ok"):
        print(f"登录态           : {'已登录' if st.get('logged_in') else '未确认'}"
              f"　昵称={st.get('nickname')!r}"
              f"　会话列表={(st.get('uia') or {}).get('has_recent_list')}")
    oc = r.get("open_chat") or {}
    if oc:
        print(f"打开会话         : {oc.get('clicked') or oc.get('note') or oc.get('error')}"
              f"　目标={oc.get('target')!r}　候选={oc.get('candidates')}")
    uia = r.get("uia") or {}
    if uia:
        if uia.get("ok"):
            nick = uia.get("nickname") or "(无)"
            print(f"界面             : 昵称={nick!r} "
                  f"会话列表={uia.get('has_recent_list')} "
                  f"消息区={uia.get('has_ml_list')} 节点={uia.get('nodes_scanned')}")
            for s in (uia.get("text_samples") or [])[:8]:
                print(f"    文本: {s!r}")
        else:
            print(f"界面读取失败     : {uia.get('error')}")
    if r.get("traceback"):
        print(f"宿主 traceback:\n{r['traceback']}")
    return 0 if r.get("ok") else 2


def _cmd_daemon(a) -> int:
    # `daemon start|stop|status` 是主写法；`start-hostd` / `stop-hostd` 是
    # 写得更快的两条别名，两者必须落到同一个动作上，别各走一套。
    action = a.action if a.cmd == "daemon" else a.cmd.replace("-hostd", "")
    if action == "status":
        s = daemon_status()
        print(f"宿主在运行     : {s['running']}（pid={s['pid'] or '-'} "
              f"pid 还活着={s['alive']}）")
        if s["heartbeat_present"]:
            print(f"心跳           : {s['heartbeat_path']}（{s['age']}s 前更新，"
                  f"超过 {s['stale_after_seconds']}s 视为停摆"
                  f"{'　[!] 已过期' if s['stale'] else ''}）")
            print(f"  阶段         : {s['phase'] or '-'}　已运行 {s['uptime']}s")
            print(f"  桌面         : {s['desktop'] or '-'}"
                  f"（宿主自报 {s['desktop_self'] or '-'}）")
            print(f"  已登录       : {s['logged_in']}　昵称={s['nickname'] or '-'}"
                  f"　QQ 进程 {len(s['qq_pids'])} 个 {s['qq_pids']}")
            if s["rc"] is not None:
                print(f"  退出码       : {s['rc']}（0=正常，2=启动阶段失败）")
            if s["error"]:
                print(f"  [X] {s['error']}")
            if s["reason"]:
                print(f"  上次退出原因 : {s['reason']}")
            if s["note"]:
                print(f"  附注         : {s['note']}")
        else:
            print("心跳           : 还没有（宿主从未启动过）")
        print(f"待停止         : {s['stop_requested']}")
        print(f"宿主日志       : {s['log_path']}（{s['log_bytes']} 字节）")
        return 0

    if action == "start":
        r = start_daemon(desktop_name=a.desktop, profile=a.profile,
                         qq_exe=a.qq_exe, dry_run=a.dry_run,
                         no_send=a.no_send, with_agent=not a.no_agent,
                         stop_qq_on_exit=a.stop_qq,
                         wait_ready=float(a.wait or 0.0))
        if not r["ok"]:
            print(f"[X] 启动失败：{r['error']}")
            return 2
        print(f"[OK] 宿主已启动 pid={r['pid']} 桌面={r['desktop']}")
        print(f"    profile : {r['profile']}")
        print(f"    args    : {' '.join(r['args'])}")
        if a.wait:
            st = r.get("ready") or {}
            print(f"    阶段     : {st.get('phase') or '(还没写出)'}"
                  f"　已登录={st.get('logged_in')}")
        print("\n它现在在隐藏桌面上，你看不见 —— 这是预期的。")
        print("用 `python -m app.host daemon status` 看它还在不在。")
        return 0

    r = stop_daemon(timeout=float(a.timeout or 20.0))
    tag = "OK" if r["ok"] else "X"
    print(f"[{tag}] 已停止 pid={r['pid']} 等了 {r['waited']}s"
          f"{'（强制结束）' if r['killed'] else ''} {r['error']}")
    return 0 if r["ok"] else 2


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m app.host",
                                 description="独立 profile 的 QQ 启动 / 认领 / 停止")
    ap.add_argument("cmd", choices=("status", "start", "stop", "grab", "describe",
                                    "daemon", "start-hostd", "stop-hostd"))
    ap.add_argument("--profile", default="", help="覆盖 profile 目录")
    ap.add_argument("--qq-exe", dest="qq_exe", default="", help="覆盖 QQ.exe 路径")
    ap.add_argument("--extra-args", dest="extra_args", default="", help="追加给 QQ 的参数")
    ap.add_argument("--visible", action="store_true",
                    help="start：落在当前可见桌面（首次登录用）")
    ap.add_argument("--hidden", action="store_true",
                    help="start：落在隐藏桌面（默认名 QQAgentHidden，可用 --desktop 改名）")
    ap.add_argument("--desktop", default="", help="start：指定桌面名")
    ap.add_argument("--yes", action="store_true", help="stop：确认执行")
    ap.add_argument("--png", default="", help="grab：画面落盘路径")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="grab：派出去后先等几秒，给 QQ 把窗口画出来")
    ap.add_argument("--no-uia", action="store_true", help="grab：不读界面，只抓图")
    ap.add_argument("--tree", default="", help="grab：把整棵 UIA 树 dump 到这个文件")
    ap.add_argument("--login", action="store_true",
                    help="grab：在隐藏桌面的登录页上按「登录」（免扫码）")
    ap.add_argument("--open-chat", dest="open_chat", action="store_true",
                    help="grab：点开一个会话（没打开会话时 agent 连锚点都找不到）")
    ap.add_argument("--chat", default="",
                    help="grab --open-chat：优先点开名字含这个串的会话")
    ap.add_argument("--chat-index", dest="chat_index", type=int, default=0,
                    help="grab --open-chat：取会话列表的第几条（0 起）")
    ap.add_argument("--dry-run", action="store_true",
                    help="grab --login：只报可行性，不点击")
    ap.add_argument("action", nargs="?", default="status",
                    choices=("status", "start", "stop"),
                    help="daemon 的子动作（默认 status）")
    ap.add_argument("--no-send", action="store_true",
                    help="daemon start：彩排，走完整链路但不真的发出")
    ap.add_argument("--no-agent", action="store_true",
                    help="daemon start：只维持桌面与 QQ，不跑回复循环")
    ap.add_argument("--stop-qq", action="store_true",
                    help="daemon start：宿主退出时把本工具那份 QQ 一起结束")
    ap.add_argument("--timeout", type=float, default=20.0,
                    help="daemon stop：优雅退出的等待秒数")
    a = ap.parse_args()

    if a.cmd == "status":
        return _cmd_status(a)
    if a.cmd == "start":
        return _cmd_start(a)
    if a.cmd == "stop":
        return _cmd_stop(a)
    if a.cmd == "grab":
        return _cmd_grab(a)
    if a.cmd in ("daemon", "start-hostd", "stop-hostd"):
        return _cmd_daemon(a)
    import pprint
    pprint.pp(describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
