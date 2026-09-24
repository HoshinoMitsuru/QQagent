# -*- coding: utf-8 -*-
"""
supervisor.py —— 进程托管

界面里的每个按钮，最后都归结为「起一个子进程、把它的输出接进日志总线、等它结束」。
这里管两件事：

    常驻进程 (resident)  —— `agent.py` 不带参数，就是那个自动回复的主循环
    一次性任务 (task)     —— 自检、只读诊断、输入测试、彩排、真发一条

## 为什么任务要串行

同时跑两个会碰 QQ 的进程是这套方案里最严重的错误之一：两边都在切会话、都在写输入框，
轻则互相把文字打进错误的会话，重则把 A 的回复发进 B 的窗口。
所以除了「纯读取」的任务（自检、看上下文、只读诊断），其余任务一律要求
**常驻进程先停下来 + 同一时刻只有一个**。用 `concurrent` 这一个标志表达，
而不是散落在一堆 if 里 —— 这种安全约束最怕的就是漏判一处。

## 为什么用 subprocess 起自己，而不是 import agent 后在线程里跑

    1. UIA 的 COM 对象要绑定在创建它的公寓线程上，和 Web 服务共用线程池必然出问题；
    2. exe 冻结后只有一个可执行文件，`exe --run-agent` 复用它，不必再带一份解释器；
    3. 行为与命令行完全一致 —— 不会出现「界面里能跑、命令行里不行」的漂移。

## 停止一个常驻进程为什么不用 Ctrl+C

子进程是用 CREATE_NO_WINDOW 起的，**它没有控制台**，所以控制台控制事件
（CTRL_C / CTRL_BREAK）根本送不到它那里。这里改用「停止哨兵文件」：
supervisor 写 `state/STOP`，agent 的循环每轮看一眼，看到了就自己走收尾流程。
这条路不依赖控制台，也不依赖信号语义在各版本 Python 上的差异。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from typing import Optional

from . import errors as E
from . import paths, platform_win as pw
from .logbus import BUS

MAX_TASK_HISTORY = 60


def _emit_diag(code: str, detail: str = "", ctx: dict | None = None,
               tag: str = "ERR") -> None:
    """
    把错误码展开成多行日志发给日志总线。

    逐行发而不是整体发：控制台的日志面板**按行着色**，整体塞进一条会让
    后续行丢掉级别 —— 那些行恰好是「怎么办」，正是最该被看见的部分。
    """
    for line in E.EC.describe(code, detail, ctx).splitlines():
        BUS.emit(line, tag=tag, source="ui")


def _child_command(mode: str, args: list[str]) -> list[str]:
    """
    拼出子进程命令行。

    冻结后只有一个 exe，靠第一个参数区分「我是壳」还是「我是 agent」；
    源码运行时则是 `python agent.py ...`。
    """
    frozen = paths.is_frozen()
    script = {"agent": "agent.py", "qqid": "qqid.py"}.get(mode)
    if not script:
        raise ValueError(f"未知的任务模式：{mode}")
    if frozen:
        return [sys.executable, f"--run-{mode}", *args]
    return [sys.executable, os.path.join(paths.exe_dir(), script), *args]


# ============================================================ 任务定义
def _p(name: str, label: str, *, placeholder: str = "", required: bool = True,
       default: str = "", multiline: bool = False) -> dict:
    return {"name": name, "label": label, "placeholder": placeholder,
            "required": required, "default": default, "multiline": multiline}


TASKS: list[dict] = [
    {
        "id": "selftest", "label": "自检", "mode": "agent", "args": ["--selftest"],
        "concurrent": True, "needs_qq": False, "dangerous": False, "group": "验证",
        "desc": "检查配置、API Key、模型连通性、调教解析。不碰 QQ。",
    },
    {
        "id": "replay", "label": "离线回放", "mode": "agent", "args": ["--replay", "{text}"],
        "concurrent": True, "needs_qq": False, "dangerous": False, "group": "验证",
        "desc": "把一句话当成刚收到的消息，跑一遍「生成回复」的链路。不碰 QQ、不发消息。",
        "params": [_p("text", "模拟收到的消息", placeholder="在吗", default="在吗")],
    },
    {
        "id": "peek", "label": "只读诊断", "mode": "agent", "args": ["--peek", "{limit}"],
        "concurrent": True, "needs_qq": True, "dangerous": False, "group": "验证",
        "desc": "解析并打印最近几条消息，核对「我方/对方」有没有认反、正文有没有被截断。",
        "params": [_p("limit", "读取条数", placeholder="10", default="10")],
    },
    {
        "id": "sessions", "label": "会话列表体检", "mode": "agent", "args": ["--sessions"],
        "concurrent": True, "needs_qq": True, "dangerous": False, "group": "验证",
        "desc": "打印「发现」视图：会话列表、未读徽标、指纹变化。能看到 agent 眼里的会话长什么样。",
    },
    {
        "id": "state", "label": "查看上下文", "mode": "agent", "args": ["--state"],
        "concurrent": True, "needs_qq": False, "dangerous": False, "group": "维护",
        "desc": "把持久化下来的每个会话的上下文打印出来。不碰 QQ。",
    },
    {
        "id": "watch", "label": "实时读取监视器", "mode": "agent",
        "args": ["--watch", "{seconds}"],
        # 只读：不写上下文、不发送、不切会话，所以可以和常驻并行
        "concurrent": True, "needs_qq": True, "dangerous": False, "group": "验证",
        "desc": "盯着**当前打开的那个会话**，把每一条新读到的消息连同方向判定一起打出来。"
                "专门用来判定「新消息到底有没有被读到」—— 启动后请去 QQ 里发一条消息。",
        "params": [_p("seconds", "运行秒数", placeholder="60", default="60")],
    },
    {
        "id": "input_test", "label": "输入框写入测试", "mode": "agent", "args": ["--input-test"],
        "concurrent": False, "needs_qq": True, "dangerous": False, "group": "验证",
        "desc": "写入一段文字 → 回读 → 清空，**绝不发送**。验证中文能不能进输入框。",
    },
    {
        "id": "enroll_all", "label": "给所有会话取号", "mode": "qqid", "args": ["--enroll-all"],
        "concurrent": False, "needs_qq": True, "dangerous": False, "group": "维护",
        "desc": "逐个打开会话读资料卡，把「昵称 → QQ 号」写进缓存。发送前的身份复核靠它；"
                "会逐个切换会话并抢前台，耗时较长。",
    },
    {
        "id": "dry_once", "label": "彩排：完整读一轮", "mode": "agent",
        "args": ["--once", "--dry-run"],
        "concurrent": False, "needs_qq": True, "dangerous": False, "group": "实弹",
        "desc": "读消息 → 生成回复 → 三重复核，最后一步不发出。会切换会话，但不会写输入框。",
    },
    {
        "id": "no_send_once", "label": "彩排：写到输入框为止", "mode": "agent",
        "args": ["--once", "--no-send"],
        "concurrent": False, "needs_qq": True, "dangerous": False, "group": "实弹",
        "desc": "和常驻完全同一条链路（含切会话、写输入框、签名复核），只在最后一击前收手。"
                "比「完整读一轮」更接近真实运行。",
    },
    {
        "id": "send", "label": "真发一条", "mode": "agent", "args": ["--send", "{text}"],
        "concurrent": False, "needs_qq": True, "dangerous": True, "group": "实弹",
        "desc": "向**当前打开的会话**真的发一条消息。会出现在对方聊天窗口里，撤不回来。",
        "params": [_p("text", "要发送的内容", placeholder="这是一条测试消息", multiline=True)],
        "confirm": "这会真的把消息发出去，对方能看到。确认发送？",
    },
    {
        "id": "forget", "label": "彻底忘记某会话", "mode": "agent",
        "args": ["--forget", "{scope}"],
        "concurrent": True, "needs_qq": False, "dangerous": True, "group": "维护",
        "desc": "把某个会话的本地记忆清干净：**上下文 + 已生成但没发出的回复 + 轮询基线**。"
                "三样都要清 —— 少清一样，被清掉的内容会从别的通道回来（例如那条"
                "「基于旧上下文生成的回复」会在重启后照原样发出去，看起来像没清干净）。"
                "输入**昵称**或**会话键**都可以；匹配不到时会列出对照表。",
        "params": [_p("scope", "昵称或会话键", placeholder="光みつる 或 private:3302676083")],
        "confirm": "确认彻底忘记这个会话？会清掉它的本地上下文、"
                   "并**丢弃**基于该上下文生成、还没发出去的回复。此操作不可撤销。",
    },
]

TASK_BY_ID = {t["id"]: t for t in TASKS}


def _fill(template: list[str], params: dict) -> list[str]:
    out = []
    for a in template:
        if "{" in a and "}" in a:
            key = a[a.index("{") + 1:a.index("}")]
            val = str(params.get(key, "")).strip()
            if not val:
                raise ValueError(f"缺少参数：{key}")
            out.append(a.replace("{" + key + "}", val))
        else:
            out.append(a)
    return out


class RunInfo:
    def __init__(self, run_id: str, label: str, args: list[str]):
        self.id = run_id
        self.label = label
        self.args = args
        self.started_at = time.time()
        self.ended_at: Optional[float] = None
        self.rc: Optional[int] = None
        self.pid: Optional[int] = None
        self.status = "running"          # running | ok | failed | killed
        self.proc: Optional[subprocess.Popen] = None

    def alive(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "label": self.label, "args": self.args,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "elapsed": round((self.ended_at or time.time()) - self.started_at, 1),
            "rc": self.rc, "pid": self.pid, "status": self.status,
            "alive": self.alive(),
        }


class Supervisor:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.resident: Optional[RunInfo] = None
        self._resident_dry = False
        self._tasks: dict[str, RunInfo] = {}
        self._history: list[RunInfo] = []

    # ------------------------------------------------------ 子进程
    def _env(self) -> dict:
        env = dict(os.environ)
        env["QQ_AGENT_HOME"] = paths.DATA_DIR
        env["QQ_AGENT_HEARTBEAT"] = paths.HEARTBEAT_PATH
        env["QQ_AGENT_STOP"] = paths.STOP_PATH
        # 不加这两条，Python 输出到管道会按块缓冲，日志卡在缓冲区里几十行才吐一次，
        # 看起来就像程序死了（这个坑我们真的踩过，白等了六分钟）。
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _spawn(self, mode: str, args: list[str], *, source: str) -> subprocess.Popen:
        cmd = _child_command(mode, args)
        BUS.emit("启动子进程：" + " ".join(_pretty(cmd)), tag="EXEC", source="ui")
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(
            cmd, cwd=paths.DATA_DIR, env=self._env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=creation,
        )
        threading.Thread(target=self._pump, args=(proc, source), daemon=True).start()
        threading.Thread(target=self._reap, args=(proc,), daemon=True).start()
        return proc

    def _pump(self, proc: subprocess.Popen, source: str) -> None:
        """
        把子进程输出按行喂进日志总线。

        读的是**二进制**再自己按 UTF-8 解码：如果让 Python 按系统区域设置（中文 Windows
        下多半是 GBK）去解，agent 输出的中文会变成一堆乱码。
        """
        stream = proc.stdout
        if stream is None:
            return
        while True:
            try:
                raw = stream.readline()
            except Exception:
                break
            if not raw:
                break
            BUS.feed(raw.decode("utf-8", "replace"), source=source)

    def _reap(self, proc: subprocess.Popen) -> None:
        try:
            proc.wait()
        except Exception:
            pass

    # ------------------------------------------------------ 常驻
    def start_resident(self, dry_run: bool = False) -> dict:
        with self._lock:
            if self.resident and self.resident.alive():
                return E.envelope("E-PROC-001", "常驻进程已经在运行了",
                                  {"已在运行": self.resident.pid})
            busy = [r for r in self._tasks.values()
                    if not TASK_BY_ID.get(r.id, {}).get("concurrent")]
            if busy:
                return E.envelope("E-PROC-001",
                                  f"「{busy[0].label}」正在运行，请等它结束再启动常驻",
                                  {"占用中": busy[0].id})

        self.clear_stop_flag()
        args = ["--dry-run"] if dry_run else []
        label = "常驻自动回复" + ("（只读不发）" if dry_run else "")
        run = RunInfo("resident", label, args)
        try:
            proc = self._spawn("agent", args, source="resident")
        except Exception as exc:
            return E.from_exception(exc, "E-PROC-004", {"模式": "agent", "参数": args})
        run.pid, run.proc = proc.pid, proc
        with self._lock:
            self.resident = run
            self._resident_dry = dry_run
        BUS.emit("常驻进程已启动 —— " + ("只读模式，不会发送任何消息" if dry_run
                                     else "会真的回复并发送消息"), tag="RUN", source="ui")
        threading.Thread(target=self._supervise, args=(run, proc, "resident"),
                         daemon=True).start()
        return {"ok": True, "pid": proc.pid}

    def stop_resident(self, grace: float = 12.0) -> dict:
        with self._lock:
            run = self.resident
        if not run or not run.proc:
            return E.envelope("E-PROC-001", "常驻进程没有在运行")
        if not run.alive():
            with self._lock:
                self.resident = None
            return {"ok": True, "note": "进程已经退出了"}

        BUS.emit(f"正在停止常驻进程：先放停止哨兵，给它 {grace:.0f}s 把队列发完",
                 tag="RUN", source="ui")
        run.status = "killed"
        self.request_stop()
        try:
            run.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            _emit_diag("E-PROC-005", f"等满 {grace:.0f}s 仍未退出，强制结束进程树",
                       tag="WARN")
            _kill_tree(run.proc.pid)
            try:
                run.proc.wait(timeout=5)
            except Exception:
                pass
        self.clear_stop_flag()
        with self._lock:
            self.resident = None
        return {"ok": True}

    def restart_resident(self, dry_run: Optional[bool] = None) -> dict:
        with self._lock:
            dry = self._resident_dry if dry_run is None else dry_run
        self.stop_resident()
        time.sleep(0.6)
        return self.start_resident(dry_run=dry)

    # ------------------------------------------------------ 停止哨兵
    def request_stop(self) -> None:
        try:
            os.makedirs(os.path.dirname(paths.STOP_PATH), exist_ok=True)
            with open(paths.STOP_PATH, "w", encoding="utf-8") as f:
                f.write(str(time.time()))
        except Exception as exc:
            # 写不进哨兵 = 常驻进程收不到停止请求 → 只能强杀。必须说清后果。
            _emit_diag("E-PATH-001", f"写停止哨兵失败（{type(exc).__name__}: {exc}）",
                       {"回退": "将不得不强制结束常驻进程（队列里已生成的回复会留在磁盘上）"},
                       tag="WARN")

    def clear_stop_flag(self) -> None:
        try:
            if os.path.isfile(paths.STOP_PATH):
                os.remove(paths.STOP_PATH)
        except Exception:
            pass

    # ------------------------------------------------------ 一次性任务
    def run_task(self, task_id: str, params: dict | None = None) -> dict:
        task = TASK_BY_ID.get(task_id)
        if not task:
            return E.envelope("E-WEB-003", f"未知任务 id：{task_id}",
                              {"可用的任务": ", ".join(sorted(TASK_BY_ID))})
        params = params or {}

        if task.get("needs_qq") and not pw.list_processes(("QQ.exe", "QQEX.exe")):
            return E.envelope("E-QQ-002", f"「{task['label']}」需要 QQ 在运行",
                              {"任务": task_id})

        with self._lock:
            if task_id in self._tasks:
                return E.envelope("E-PROC-001", f"「{task['label']}」已经在跑了")
            if not task.get("concurrent"):
                others = [r for r in self._tasks.values()
                          if not TASK_BY_ID.get(r.id, {}).get("concurrent")]
                if others:
                    return E.envelope(
                        "E-PROC-001",
                        f"「{others[0].label}」正在运行。会碰 QQ 的任务必须串行。",
                        {"占用中": others[0].id})
                if self.resident and self.resident.alive():
                    return E.envelope(
                        "E-PROC-002",
                        f"「{task['label']}」会切换会话并写输入框，常驻运行时不允许执行",
                        {"冲突任务": task_id})

        try:
            args = _fill(task["args"], params)
        except ValueError as exc:
            return E.envelope("E-PROC-003", str(exc),
                              {"任务": task_id, "需要参数":
                               ", ".join(p["name"] for p in task.get("params") or [])})

        run = RunInfo(task_id, task["label"], args)
        try:
            proc = self._spawn(task["mode"], args, source=f"task:{task_id}")
        except Exception as exc:
            return E.from_exception(exc, "E-PROC-004", {"任务": task_id, "参数": args})
        run.pid, run.proc = proc.pid, proc
        with self._lock:
            self._tasks[task_id] = run
        threading.Thread(target=self._supervise, args=(run, proc, f"task:{task_id}"),
                         daemon=True).start()
        return {"ok": True, "pid": proc.pid, "args": args}

    def kill_task(self, task_id: str) -> dict:
        with self._lock:
            run = self._tasks.get(task_id)
        if not run or not run.proc:
            return E.envelope("E-PROC-001", f"任务 {task_id} 不在运行")
        run.status = "killed"
        _kill_tree(run.proc.pid)
        return {"ok": True}

    def _supervise(self, run: RunInfo, proc: subprocess.Popen, source: str) -> None:
        """等子进程退出，然后登记结果。心跳只在常驻退出时清 ——
        否则一个只读诊断任务结束，会把正在运行的常驻进程的心跳一起删掉。"""
        rc = proc.wait()
        run.rc = rc
        run.ended_at = time.time()
        if run.status == "running":
            run.status = "ok" if rc == 0 else "failed"
        if rc == 0:
            BUS.emit(f"{run.label} 结束，返回码 0（耗时 {run.to_dict()['elapsed']}s）",
                     tag="EXIT", source="ui")
        else:
            # 非零退出只是**结论**，根因在它上面的输出里。必须把这句话说清楚，
            # 否则用户会拿着「返回码 2」这一行来问「为什么是 2」。
            _emit_diag("E-PROC-006",
                       f"{run.label} 退出码 {rc}（耗时 {run.to_dict()['elapsed']}s）",
                       {"命令": " ".join(run.args or []),
                        "根因": "上面第一条带错误码的报错才是根因"})
        if source == "resident":
            self.clear_stop_flag()
            self._clear_heartbeat()
            with self._lock:
                self.resident = None
        else:
            with self._lock:
                self._tasks.pop(run.id, None)
                self._history.append(run)
                del self._history[:-MAX_TASK_HISTORY]

    def _clear_heartbeat(self) -> None:
        """进程一退出就把心跳删掉，否则界面会一直显示「运行中」的假象。"""
        try:
            if os.path.isfile(paths.HEARTBEAT_PATH):
                os.remove(paths.HEARTBEAT_PATH)
        except Exception:
            pass

    # ------------------------------------------------------ 状态
    def heartbeat(self) -> dict:
        """
        读 agent 写的心跳文件。文件不存在 = 还没进循环，或已经退出了。

        ## stale 阈值为什么不能写死

        原来这里是 `stale = age > 30`。但主循环是**串行**的，而 `调模型` 这一步
        最长要 `llm.timeout_seconds`（默认 **60 秒**）—— 于是一次正常的模型调用
        就足以让界面弹出「心跳已停更，进程可能卡住了」。

        那个提示是**必然误报**，而且会把人往错误方向带（去重启一个其实健康的进程）。

        现在阈值由 agent 按**当前阶段自己的合理上限**写进心跳（`stale_after`），
        这里只负责用。阶段名（`phase`）也一并带出来，于是提示能直接说
        「卡在『调模型』已 42 秒（上限 75 秒）」而不是一句「进程可能卡住了」。
        """
        import json
        try:
            if not os.path.isfile(paths.HEARTBEAT_PATH):
                return {"present": False}
            with open(paths.HEARTBEAT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["present"] = True
            now = time.time()
            data["age"] = round(now - float(data.get("ts") or 0), 1)
            # agent 给的阶段上限；老版本心跳里没有这个字段时退回 30s
            try:
                limit = float(data.get("stale_after") or 0) or 30.0
            except (TypeError, ValueError):
                limit = 30.0
            data["stale_after_seconds"] = round(limit, 1)
            data["stale"] = data["age"] > limit
            since = data.get("phase_since")
            data["phase_seconds"] = round(now - float(since), 1) if since else None
            return data
        except Exception as exc:
            return {"present": False, "error": str(exc)}

    def state(self) -> dict:
        with self._lock:
            resident = self.resident
            running = [r.to_dict() for r in self._tasks.values()]
            history = [r.to_dict() for r in self._history[-20:]][::-1]
            dry = self._resident_dry
        resident_state = {"running": bool(resident and resident.alive()), "dry_run": dry}
        if resident:
            resident_state.update(resident.to_dict())
            resident_state["running"] = resident.alive()
        return {
            "resident": resident_state,
            "running_tasks": running,
            "task_history": history,
            "heartbeat": self.heartbeat(),
        }


def _kill_tree(pid: int) -> None:
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def _pretty(cmd: list[str]) -> list[str]:
    """日志里别把一长串绝对路径全显示出来，只留可执行文件名和参数。"""
    if not cmd:
        return cmd
    out = [os.path.basename(cmd[0])]
    for a in cmd[1:]:
        out.append(os.path.basename(a) if a.lower().endswith(".py") else a)
    return out


SUP = Supervisor()
