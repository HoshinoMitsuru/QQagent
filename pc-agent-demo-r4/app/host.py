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

常驻场景则需要一个长活的宿主进程一直持有句柄，那是后续要补的东西。

## 用法

    python -m app.host status
    python -m app.host start --visible          # 首次登录用这个
    python -m app.host start --hidden           # 登录态有了之后用这个
    python -m app.host stop --yes
"""

from __future__ import annotations

import argparse
import json
import os
import time

from . import desktop, paths, platform_win as pw, qqctl

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
    name = "" if a.visible else (a.desktop or desktop.DEFAULT_NAME)
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


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m app.host",
                                 description="独立 profile 的 QQ 启动 / 认领 / 停止")
    ap.add_argument("cmd", choices=("status", "start", "stop", "describe"))
    ap.add_argument("--profile", default="", help="覆盖 profile 目录")
    ap.add_argument("--qq-exe", dest="qq_exe", default="", help="覆盖 QQ.exe 路径")
    ap.add_argument("--extra-args", dest="extra_args", default="", help="追加给 QQ 的参数")
    ap.add_argument("--visible", action="store_true",
                    help="start：落在当前可见桌面（首次登录用）")
    ap.add_argument("--desktop", default="", help="start：指定桌面名（默认 QQAgentHidden）")
    ap.add_argument("--yes", action="store_true", help="stop：确认执行")
    a = ap.parse_args()

    if a.cmd == "status":
        return _cmd_status(a)
    if a.cmd == "start":
        return _cmd_start(a)
    if a.cmd == "stop":
        return _cmd_stop(a)
    import pprint
    pprint.pp(describe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
