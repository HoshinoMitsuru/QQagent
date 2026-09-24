# -*- coding: utf-8 -*-
"""
hostd.py —— **常驻宿主进程**（daemon）：被启动到隐藏桌面上、一直活着的那一个

## 它和 hostagent.py 的分工

    hostagent  一次性：进去 → 抓图/读界面/点登录 → 写 JSON → 退出
    hostd      长活  ：进去 → 一直待着 → 跑回复主循环 → 直到收到停止哨兵

`hostagent` 那段说明里写的「壳对隐藏桌面是瞎的」在这里同样成立，而且更严重：
常驻意味着**没有人盯着**。所以 hostd 额外承担三件事：

    1. **持有桌面句柄** —— 桌面靠句柄活着（见 desktop.py 的 `_KEEP`）。
       一次性动作做完就退，桌面还能靠 QQ 撑着；常驻不行：QQ 一崩桌面就没了，
       而这件事在用户桌面上完全看不出来，表现是常驻静默停摆。
    2. **QQ 不在就拉起来、卡在登录页就点掉登录** —— 无人值守下没有第二个人
       能去点那个按钮。
    3. **写心跳** —— 壳看不到窗口、拿不到 stdout（CreateProcessW 没接管管道），
       文件是唯一通道。

## 为什么 agent 必须跑在**主线程**

硬性约定：全进程只有一条线程碰 UIA，且该线程必须是初始化 COM 的那条。
`agent.py` 在**模块导入期**就调 `auto.SetGlobalSearchTimeout()`，
它跑在哪条线程上，COM 公寓就建在哪条线程上。

所以 hostd 的结构是被这条约束定死的：

    主线程   : 桌面 → QQ → 登录 → agent.run_forever()（全程占用，阻塞）
    守护线程 : 只写心跳、只看哨兵文件 —— 绝不碰 UIA

反过来，如果为了「方便管理」把 agent 丢进子线程，uiautomation 会静默返回空树，
现象是「读不到任何消息」而日志里没有任何报错 —— 这是本项目踩过的坑。

## 停止协议（两段，别混）

    hostd.stop   让**整套**退：循环停 → 宿主退（界面上的「停止」按这个）
    state/STOP   只让**回复循环**退出（agent 自己的哨兵，宿主会去写它）

宿主收到 `hostd.stop` 后自己去写 `state/STOP`，给 agent 一次优雅收尾的机会
（把队列里已经生成的回复发完），等不到才强退。

## 用法（一般由 `host.start_daemon` 调用，不手跑）

    python -m app.hostd --desktop QQAgentHidden [--dry-run] [--no-agent]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback

# 作为被 spawn 的子进程，sys.path[0] 未必是仓库根 —— 显式补上，
# 否则 `import agent`（它在仓库根，不在 app 包里）根本不成立。
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ⚠️ 这个 import 顺序是**有语义的**，不要为了排版去调它：
# `app.paths` 在导入期就把 QQ_AGENT_HOME 定死并写进 os.environ，
# 而 agent.py 在**它自己**的导入期才去读 QQ_AGENT_HOME / _HEARTBEAT / _STOP。
# 顺序反了，agent 就会把上下文和心跳写到别处去。
from app import desktop, host, paths  # noqa: E402

os.environ["QQ_AGENT_HEARTBEAT"] = paths.HEARTBEAT_PATH
os.environ["QQ_AGENT_STOP"] = paths.STOP_PATH

PHASE_START = "启动"
PHASE_DESKTOP = "接管桌面"
PHASE_QQ = "等待 QQ"
PHASE_LOGIN = "等待登录"
PHASE_CHAT = "打开会话"
PHASE_IDLE = "待命（不跑回复）"
PHASE_RUN = "运行中"
PHASE_EXIT = "退出中"
PHASE_DONE = "已停止"

#: 心跳周期。1 秒是权衡：再快纯属浪费，再慢会让界面上的「宿主死了」来得更晚。
BEAT_INTERVAL = 1.0

_DONE = threading.Event()


# ============================================================ 输出
class _Tee:
    """
    把 stdout 同时写给日志文件与（可能存在的）原始 stdout。

    ## 为什么非得自己接管 stdout

    `desktop.spawn` 走 `CreateProcessW` 且**不继承句柄**，于是本进程的 stdout
    要么指向一个没人读的地方，要么干脆是 None —— 而 `print(None)` 会抛
    AttributeError，让一个本来能跑的常驻进程在第一次打日志时就死掉。

    同时，界面上的日志面板是靠壳 tail 这个日志文件工作的（见 `app/logtail.py`）。
    也就是说：**这个文件是隐藏桌面上发生的一切的唯一可见证据**，
    它比 stdout 重要得多，所以写失败也必须安静（不能因为日志把主循环拖死）。
    """

    def __init__(self, streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, text: str) -> int:
        for s in self._streams:
            try:
                s.write(text)
                s.flush()
            except Exception:
                pass
        return len(text or "")

    def flush(self) -> None:
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:
        return "utf-8"


def _install_stdio():
    """把 stdout/stderr 换成 tee。返回日志文件对象（退出时要关）。"""
    try:
        os.makedirs(paths.LOG_DIR, exist_ok=True)
        # 每次启动重写（"w"）：上一轮的日志已经没有意义了，留着只会让人看错时间线。
        f = open(host.HOSTD_LOG, "w", encoding="utf-8", buffering=1)
    except Exception:
        f = None
    tee = _Tee([f, sys.stdout])
    sys.stdout = tee
    sys.stderr = tee
    return f


def log(tag: str, msg: str) -> None:
    # 与 agent.log 完全一致的格式：壳 tail 这个文件喂进日志总线时，
    # 解析规则（[HH:MM:SS] TAG 正文）才能复用，级别着色和按标签过滤才不会散架。
    print(f"[{time.strftime('%H:%M:%S')}] {tag:<5} {msg}", flush=True)


# ============================================================ 心跳
_HB: dict = {}
_HB_LOCK = threading.Lock()


def beat(**fields) -> None:
    """
    写宿主心跳。

    ## 为什么心跳里要带「阶段」而不只是一个时间戳

    常驻停摆有两种：「进程死了」和「进程活着但卡住了」。只写时间戳的话，
    界面只能看出「心跳停更」，然后人去猜是不是崩了。带上阶段之后，
    界面可以直接说「卡在『等待登录』这个阶段」—— 那是完全不同的处置动作。

    ## 为什么用 os.replace 而不是原地覆盖

    壳随时可能在读这个文件。边写边读会读到半截 JSON，
    解析失败的表现是「状态一闪变成未运行」，非常像真故障。
    """
    with _HB_LOCK:
        _HB.update(fields)
        payload = {"ts": time.time()}
        payload.update(_HB)
    try:
        os.makedirs(paths.STATE_DIR, exist_ok=True)
        tmp = host.HOSTD_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, host.HOSTD_JSON)
    except Exception:
        pass        # 心跳是纯观测，失败绝不能影响主循环


def stop_requested() -> bool:
    return os.path.isfile(host.HOSTD_STOP)


def _ask_agent_to_stop() -> None:
    """
    代写 agent 的停止哨兵，让回复循环**优雅**退出（而不是被强杀）。

    这一步的价值全在 agent.shutdown() 里：它会把防抖缓冲结算掉、
    把队列里已经生成的回复在时限内发完、把没发出去的落盘。
    直接结束进程的话，「对方发了消息、我们的回复已经生成好却永远没发出去」
    就成了一种静默丢消息的路径。
    """
    try:
        os.makedirs(paths.STATE_DIR, exist_ok=True)
        with open(paths.STOP_PATH, "w", encoding="utf-8") as f:
            f.write(str(time.time()))
    except Exception as exc:
        log("ERR", f"写 agent 停止哨兵失败（{type(exc).__name__}: {exc}）："
                   f"回复循环不会优雅退出，稍后只能强退")


def _watchdog(started: float, grace: float) -> None:
    """
    守护线程：写心跳 + 盯停止哨兵。

    ## 为什么最后一步是 os._exit 而不是抛异常

    主线程正阻塞在 agent 的循环里，异常传不过去；而 `SystemExit` 在子线程里
    什么也不会做。等到优雅退出时限用尽，只剩「直接结束进程」这一个选项。
    代价要写进心跳：进程被硬结束时 agent 没机会落盘，所以这句 note 是
    事后判断「有没有丢消息」的依据。
    """
    while not _DONE.is_set():
        time.sleep(BEAT_INTERVAL)
        try:
            beat(uptime=round(time.time() - started, 1))
        except Exception:
            pass
        if not stop_requested():
            continue

        log("HOST", f"收到停止哨兵（{os.path.basename(host.HOSTD_STOP)}），"
                    f"给回复循环 {grace:.0f}s 优雅收尾")
        beat(phase=PHASE_EXIT, note="收到停止哨兵，正在等待回复循环收尾")
        _ask_agent_to_stop()
        deadline = time.time() + grace
        while time.time() < deadline and not _DONE.is_set():
            time.sleep(0.3)
        if _DONE.is_set():
            return
        beat(phase=PHASE_DONE, stopped=True,
             note=f"等满 {grace:.0f}s 仍未退出，已强制结束进程"
                  f"（agent 没机会落盘，队列里未发的回复以磁盘上次的为准）")
        log("ERR", "优雅退出超时，强制结束进程")
        try:
            sys.stdout.flush()
        except Exception:
            pass
        os._exit(3)


# ============================================================ QQ 保障
def _wait_window(timeout: float) -> tuple[int, str]:
    """
    等 QQ 主窗口出现在这张桌面上。只枚举窗口（user32），**不碰 UIA** ——
    这样即使将来把它挪到别的线程也不会违反「只有一条线程碰 UIA」。

    返回 `(hwnd, 错误)`。
    """
    from app import hostagent
    deadline = time.time() + timeout
    last_report = 0.0
    while time.time() < deadline:
        if stop_requested():
            return 0, "启动期就收到了停止哨兵"
        found = hostagent.find_qq_window()
        main = found.get("main")
        if main:
            log("HOST", f"QQ 窗口已出现：hwnd={main['hwnd']} "
                        f"pid={main['pid']} {main['rect']} title={main['title']!r}")
            return main["hwnd"], ""
        if time.time() - last_report > 8:
            last_report = time.time()
            log("HOST", f"窗口还没出现（本桌面内已枚举到 {found.get('count', 0)} 个候选），"
                        f"还需最多 {max(0, deadline - time.time()):.0f}s")
        time.sleep(1.0)
    return 0, ("E-DESK-003 等不到 QQ 窗口。三种可能：QQ 没被启动到这张桌面、"
               "它停在托盘没画窗口、或这是全新 profile 的首次启动（慢得多）。")


def _ensure_login(timeout: float, desktop_name: str) -> tuple[bool, str]:
    """
    确认已登录；停在登录页就把它点掉（免扫码）。

    ## 为什么这件事必须交给**一次性子进程**，而不是本进程直接调 UIA

    这是实测逼出来的结论（2026-09-25，第一版就是在主线程里直接读 + 点）：

        按登录失败：Invoke 失败：COMError (-2147220992)
        之后每一次 UIA 调用都失败：COMError (-2147220991, '事件无法调用任何订户')

    而**同一张桌面、同一个 QQ**，换一个全新的子进程去读，一切正常 ——
    也就是说那串报错不是「QQ 有问题」，是**本进程的 uiautomation 状态被一次失败的
    Invoke 弄坏了**，而且坏了之后不会自愈（后面每 8 秒重报一次同样的错，永远好不了）。

    这对宿主进程是致命的：它的主线程接下来要交给 agent 跑一整天。
    所以改成：**每次探测/点击都派一个一次性的 `hostagent` 子进程**，
    它无论成功还是把 COM 弄坏，进程一退就干净了 —— 下一次重试永远是新环境。

    顺带拿到两个实实在在的好处：

      · 宿主的**第一次碰 UIA 就是 agent 自己那一次**，与已经整条验证通过的
        U1 链路完全同形，不再有一条「先热身再交给 agent」的隐式耦合；
      · 每轮都会留下画面与界面证据（`host.SHOT_PNG` / `host.GRAB_JSON`），
        登录卡住时能直接看「它当时到底看到了什么」，而不是只有一行报错。

    关于那个「假失败」：Invoke 报错时元素往往**已经点中了**（登录页正在被销毁，
    Chromium 的 provider 在调用途中失效）。所以这里**不看 Invoke 的成败**，
    只看下一轮界面上有没有会话列表 —— 结论以界面为准，不以调用返回值为准。
    """
    deadline = time.time() + timeout
    settle = 2.5          # 第一轮多等一会儿：登录页刚画出来时按钮可能还没接上处理函数
    clicks = 0
    last_note = ""
    while time.time() < deadline:
        if stop_requested():
            return False, "登录等待期收到停止哨兵"

        # `state=True` 让子进程在点击**之前**先判一次登录态 —— 判据写在
        # `hostagent.login_state` 里，那边是唯一该理解「什么算登录了」的地方。
        r = host.grab(host.SHOT_PNG, desktop_name=desktop_name, wait=settle,
                      uia=True, login=True, state=True, timeout=60.0)
        st = r.get("login_state") or {}
        lg = r.get("login") or {}

        if st.get("ok"):
            beat(nickname=st.get("nickname") or "",
                 has_recent_list=bool((st.get("uia") or {}).get("has_recent_list")))
            if st.get("logged_in"):
                log("HOST", f"已登录：{st.get('nickname') or '(昵称未取到)'}")
                return True, ""

        if lg.get("clicked_via"):
            clicks += 1
            beat(login_click_via=lg["clicked_via"],
                 login_auto_toggled=bool(lg.get("auto_login_toggled")))
            log("HOST", f"停在登录页 —— 已按「登录」（走 {lg['clicked_via']}，"
                        f"自动登录勾选={lg.get('auto_login_toggled')}），等它进主界面")
            settle = 4.0          # 点过之后要多给它一点时间重建界面
            if clicks >= 4:
                last_note = "已经点了 4 次登录按钮仍没进主界面"
        elif lg.get("error"):
            # 登录按钮找不到是**正常**的（窗口可能还是登录页的骨架 / 已经登录了），
            # 只在连续多轮没结论时才值得报出来。
            note = f"这一轮没看到登录按钮：{lg['error']}"
            if note != last_note:
                last_note = note
                log("WARN", f"{note}（继续等）")
        elif r.get("error"):
            note = f"这一轮探测失败：{r['error']}"
            if note != last_note:
                last_note = note
                log("WARN", f"{note}（继续等）")

        time.sleep(3.0)

    return False, ("E-DESK-003 等不到登录完成"
                   + (f"（{last_note}）" if last_note else "")
                   + "。若是全新 profile 且从未登录过，隐藏桌面上没人能扫码 —— "
                     "先用 `python -m app.host start --visible` 在可见桌面登录一次，"
                     "再用 --hidden 起。")


def _ensure_chat_open(timeout: float, desktop_name: str,
                      chat: str = "") -> tuple[bool, str]:
    """
    确保那张桌面上**有一个打开着的会话**。

    ## 为什么这是「能不能跑起来」的最后一道门

    QQ 登录完停在会话列表页，而 agent 要用的锚点（`ml-list`、
    `ExEditor-qq-msg-editor`、`send-msg`）**只存在于聊天页**。
    实测（2026-09-25）树里 `recent-contact-list` 有、`ml-list` 是 0 ——
    于是 `dom_exposed()` 为 False，常驻直接以 `E-QQ-004` 退出，
    而那个码说的是「无障碍参数没生效」，按它去重启 QQ 是白折腾。

    在用户桌面上这件事一直是用户顺手做的（打开 QQ 自然点进某个聊天），
    所以藏了很久；隐藏桌面上没人能点，它就成了硬性前提。

    判据用**界面客观事实**（ml-list 在不在），不是「点了没有」——
    点击是控件级 Invoke，调用返回不代表生效，这是本项目的通用原则。
    """
    deadline = time.time() + timeout
    last_note = ""
    tried: list[str] = []
    index = 0
    while time.time() < deadline:
        if stop_requested():
            return False, "等会话打开期间收到停止哨兵"
        # 一次子进程同时干四件事：读界面（判据）、必要时点开一个会话、
        # 顺便报一次登录态、留下画面证据
        r = host.grab(host.SHOT_PNG, desktop_name=desktop_name, wait=1.0,
                      uia=True, state=True, open_chat=True, chat=chat,
                      chat_index=index, timeout=60.0)
        uia = r.get("uia") or {}
        oc = r.get("open_chat") or {}

        if uia.get("has_ml_list"):
            log("HOST", f"聊天页已就绪（ml-list 已在，目标={oc.get('target') or '(本来就开着)'!r}）")
            return True, ""

        if oc.get("already_open"):
            # 子进程看到 ml-list 已经在了，但这一轮 uia 没读到 —— 再确认一轮
            log("HOST", "子进程报告会话已是打开状态，再确认一轮")
        elif oc.get("clicked"):
            log("HOST", f"点开了 {oc.get('target')!r}（走 {oc.get('clicked')}），"
                        f"但聊天区没出现 —— 换下一条")
            tried.append(oc.get("target") or "?")
            beat(chat_clicked=oc.get("clicked"), chat_target=oc.get("target") or "",
                 chat_tried=tried)
            index += 1
        elif oc.get("error"):
            note = f"打开会话没成功：{oc['error']}"
            if note != last_note:
                last_note = note
                log("WARN", note)
            # 「列表里没有更多条目了」是终态，继续重试没意义
            if "取不到第" in note or "都找不到" in note:
                break
            index += 1
        elif r.get("error"):
            note = f"这一轮探测失败：{r['error']}"
            if note != last_note:
                last_note = note
                log("WARN", f"{note}（继续等）")

        time.sleep(2.5)
    return False, ("E-QQ-008 等不到聊天页就绪（ml-list 一直不出现）"
                   + (f"。试过的会话：{tried}" if tried else "")
                   + (f"。{last_note}" if last_note else "")
                   + "。这个号可能一个真实会话都没有（列表里全是公众号/服务号这类"
                     "没有聊天区的条目）—— 先让它有一个普通联系人。")


def _ensure_qq(desktop_name: str, profile: str, qq_exe: str) -> tuple[bool, str]:
    """QQ 不在就拉起来。返回 (是否已有/已拉起, 错误)。"""
    procs = host.own_processes(profile)
    if procs:
        pids = sorted(int(p["pid"]) for p in procs)
        log("HOST", f"本桌面上已有属于本工具的 QQ：{len(pids)} 个进程 {pids}")
        beat(qq_pids=pids)
        return True, ""
    log("HOST", "没有认领到本工具的 QQ 进程，现在拉起一个（独立 profile）")
    r = host.start(desktop_name=desktop_name, profile=profile, qq_exe=qq_exe)
    if not r["ok"]:
        return False, r["error"]
    log("HOST", f"已启动 pid={r['pid']}（profile {r['profile']}）")
    beat(qq_pids=[r["pid"]])
    return True, ""


# ============================================================ 主流程
def _idle_forever() -> int:
    """`--no-agent`：只把 QQ 稳在隐藏桌面上，不跑回复（用来观察/抓图）。"""
    log("HOST", "已按 --no-agent 启动：只维持 QQ 与桌面，不跑回复循环")
    beat(phase=PHASE_IDLE)
    while not stop_requested():
        time.sleep(1.0)
    return 0


def _run_agent(dry_run: bool, no_send: bool) -> int:
    """
    把主线程交给 agent 的常驻循环。

    ## 为什么直接构造 Agent 而不是复用 agent.main()

    `main()` 是命令行入口，它要 parse sys.argv、处理 --selftest/--peek 这些
    一次性诊断分支。常驻要的只是「跑循环」这一件事，而且需要拿到返回码
    （2 = 入口检查没过，比如附着失败）—— 那个码要写进心跳，
    否则界面上只看到「宿主已停止」，不知道是正常退出还是启动就失败了。
    """
    import agent as A
    cfg = A.load_config()
    ag = A.Agent(cfg, dry_run=dry_run, no_send=no_send)
    log("HOST", f"进入回复主循环（dry_run={dry_run} no_send={no_send}）"
                + ("　※ 只读模式，不会真的发送" if dry_run or no_send else ""))
    beat(phase=PHASE_RUN)
    return ag.run_forever()


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m app.hostd",
                                 description="隐藏桌面上的常驻宿主进程")
    ap.add_argument("--desktop", default=desktop.DEFAULT_NAME,
                    help="要驻守的桌面名（进程本身也应被启动到这张桌上）")
    ap.add_argument("--profile", default="", help="QQ 独立 profile 目录")
    ap.add_argument("--qq-exe", dest="qq_exe", default="", help="QQ.exe 路径")
    ap.add_argument("--dry-run", action="store_true", help="只读不发（透传给 agent）")
    ap.add_argument("--no-send", action="store_true",
                    help="彩排：走完整链路但不真的发出（透传给 agent）")
    ap.add_argument("--no-agent", action="store_true",
                    help="只维持桌面与 QQ，不跑回复循环")
    ap.add_argument("--stop-qq-on-exit", action="store_true",
                    help="退出时把本工具那份 QQ 一起结束（默认留着）")
    ap.add_argument("--ready-timeout", type=float, default=150.0,
                    help="等 QQ 窗口出现的最长秒数（全新 profile 首次启动很慢）")
    ap.add_argument("--login-timeout", type=float, default=90.0,
                    help="等登录完成的最长秒数")
    ap.add_argument("--chat-timeout", type=float, default=40.0,
                    help="等聊天页就绪（ml-list 出现）的最长秒数")
    ap.add_argument("--chat", default="",
                    help="优先点开名字含这个串的会话（留空=按列表顺序逐个试）")
    ap.add_argument("--stop-grace", type=float, default=20.0,
                    help="收到停止哨兵后，给回复循环优雅收尾的秒数")
    a = ap.parse_args()

    started = time.time()
    logf = _install_stdio()
    # ⚠️ `rc` 必须在**每条返回路径**上都被赋值。第一版只在「跑完 agent」那条路径上写它，
    # 于是启动期失败（返回 2）也报 `agent_rc=0` —— 界面上就成了一句看着很正常的
    # 「已停止」，把「启动就失败」这件事盖掉了。这正是本项目最忌讳的那种假诊断。
    rc = 0
    reason = ""
    try:
        # 1. 自报桌面 —— 「lpDesktop 真的生效」的唯一可信证据。
        #    命令行里写的是**请求**，桌面归属由内核在创建进程时决定，两者不一致的情况真的存在。
        self_desktop = desktop.current_name()
        beat(pid=os.getpid(), desktop=a.desktop, desktop_self=self_desktop,
             phase=PHASE_START, started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
             agent_enabled=not a.no_agent, dry_run=a.dry_run, no_send=a.no_send,
             log_path=host.HOSTD_LOG, profile=host.profile_dir(a.profile))
        log("HOST", f"宿主进程启动 pid={os.getpid()}　自报桌面={self_desktop!r}　"
                    f"目标桌面={a.desktop!r}")
        if self_desktop != a.desktop:
            log("WARN", "自报桌面与目标桌面不一致 —— lpDesktop 没生效？"
                        "（后续所有窗口操作都会找不到 QQ）")

        # ⚠️ 看门线程必须**在这里**就起来，不能等登录完了再起。
        # 启动期（等 QQ 画出窗口、等登录）本来就可能几十秒，而它同时承担
        # 「写心跳」与「响应停止哨兵」两件事 —— 晚起的话，界面在这段时间里
        # 会显示「心跳停更」（被误判成卡死），而且这段时间按「停止」没人接。
        threading.Thread(target=_watchdog, args=(started, a.stop_grace),
                         name="hostd-watch", daemon=True).start()

        def fail(code: str, msg: str) -> int:
            """启动阶段的统一失败出口：把码、原因、返回码一次写全，避免再出现「看着正常」的假状态。"""
            nonlocal rc, reason
            rc = 2
            reason = f"[{code}] {msg}"
            log("ERR", reason)
            return rc

        # 2. 接管桌面：把句柄登记进保活表，进程活着桌面就活着
        r = desktop.create(a.desktop, keep=True)
        if not r["ok"]:
            return fail("E-DESK-001", r["error"])
        beat(phase=PHASE_DESKTOP, desktop_existed=r["existed"])
        log("HOST", f"已接管桌面 {a.desktop}（建之前就存在={r['existed']}）")

        # 3. QQ
        beat(phase=PHASE_QQ)
        ok_q, err = _ensure_qq(a.desktop, a.profile, a.qq_exe)
        if not ok_q:
            return fail("E-QQ-002", err)

        # 4. 等窗口 → 等登录
        hwnd, err = _wait_window(a.ready_timeout)
        if not hwnd:
            return fail("E-DESK-003", err)
        beat(hwnd=hwnd, phase=PHASE_LOGIN)
        ok_l, err = _ensure_login(a.login_timeout, a.desktop)
        if not ok_l:
            return fail("E-DESK-003", err)
        beat(logged_in=True)

        # 5. 打开一个会话 —— 不做这一步的话 agent 连锚点都找不到（见该函数说明）
        beat(phase=PHASE_CHAT)
        ok_c, err = _ensure_chat_open(a.chat_timeout, a.desktop, a.chat)
        if not ok_c:
            return fail("E-QQ-008", err)
        beat(chat_open=True)

        # 6. 主线程交给 agent（必须主线程，见文件开头）
        if a.no_agent:
            rc = _idle_forever()
        else:
            rc = _run_agent(a.dry_run, a.no_send)
        reason = (f"agent 主循环返回 {rc}（0=正常退出，2=入口检查没过，看日志）"
                  if rc else "agent 主循环正常结束（多半是收到了停止哨兵）")
        return rc
    except KeyboardInterrupt:
        reason = "收到中断信号"
        return 0
    except Exception as exc:
        rc = 2
        reason = f"{type(exc).__name__}: {exc}"
        log("ERR", f"宿主进程异常：{reason}\n{traceback.format_exc()[-1200:]}")
        return rc
    finally:
        _DONE.set()
        # 字段叫 `rc` 而不是 `agent_rc`：它是**宿主整个进程**的返回码，
        # 启动期失败也走它。第一版叫 agent_rc，于是「启动就失败」被读成了
        # 「agent 正常返回 0」，界面上只剩一句看着正常的「已停止」。
        # `reason` 一律写（复盘用），`error` 只在失败时写 ——
        # 界面靠 error 决定「要不要把这次退出当成故障摆在最上面」，
        # 正常停止也写 error 的话，每次停止都会弹一条红框。
        beat(phase=PHASE_DONE, stopped=True, rc=rc, reason=reason,
             error=(reason if rc else ""),
             uptime=round(time.time() - started, 1))
        log("HOST", f"宿主退出 rc={rc}　{reason}")
        # 清掉两个停止哨兵：不留给下一次启动（否则下一次一上来就退出）
        for p in (host.HOSTD_STOP, paths.STOP_PATH):
            try:
                if os.path.isfile(p):
                    os.remove(p)
            except Exception:
                pass
        if a.stop_qq_on_exit:
            try:
                r = host.stop(a.profile)
                log("HOST", f"已按 --stop-qq-on-exit 结束 QQ：{r.get('killed')} 个进程")
            except Exception as exc:
                log("WARN", f"退出时结束 QQ 失败：{exc}")
        try:
            if logf:
                logf.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
