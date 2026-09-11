# -*- coding: utf-8 -*-
"""
qqctl.py —— QQ 的发现、体检与托管

这个模块回答四个问题：

    1. QQ 装在哪？          —— 注册表 / 常见目录 / 盘根（本机就是盘根 D:\\QQ.exe）
    2. QQ 在跑吗？跑成什么样？ —— 进程 + 顶层窗口（纯 ctypes，不依赖 uiautomation）
    3. 无障碍参数生效了吗？    —— **以「UIA 树读不读得到」为准**，不看命令行猜
    4. 需要重启它吗？          —— 只在用户明确点击时执行

第 3 条是这里最要紧的设计决定。判断「QQ 有没有带 --force-renderer-accessibility」有两条路：
查命令行、或者直接去读它的无障碍树。**只有后者是可信的** —— 参数写对了但没生效
（QQ 已经在跑，再执行一次带参数的命令只会把窗口唤到前台，参数根本不应用）时，
查命令行会告诉你「没问题」，而实际什么都读不到。我们只认能读到东西。

关于 UIA 调用为什么必须固定在一个线程上：comtypes 在 import 时对**导入它的那个线程**
做 CoInitialize，跨线程调用 COM 接口在有些场景下会静默返回空数据。
Web 服务是多线程的，所以这里专门开一条 UIA 工作线程，所有 UIA 调用都排队交给它。
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
from ctypes import wintypes

from . import paths, platform_win as pw
from .logbus import BUS

QQ_PROCESS_NAMES = ("QQ.exe", "QQEX.exe")
ACCESSIBILITY_FLAG = "--force-renderer-accessibility"
UNSET = object()

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)


# ============================================================ 定位 QQ
def _registry_candidates() -> list[str]:
    out: list[str] = []
    try:
        import winreg
    except Exception:
        return out

    keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\QQNT", ("Install", "InstallPath", "Path")),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Tencent\QQNT", ("Install", "InstallPath", "Path")),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\QQNT", ("Install", "InstallPath", "Path")),
        (winreg.HKEY_CURRENT_USER, r"Software\Tencent\QQ", ("Install", "InstallPath", "Path")),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Tencent\QQ2009", ("Install", "InstallPath")),
    ]
    for hive, sub, names in keys:
        try:
            with winreg.OpenKey(hive, sub) as k:
                for name in names:
                    try:
                        val, _ = winreg.QueryValueEx(k, name)
                    except OSError:
                        continue
                    if isinstance(val, str) and val.strip():
                        cand = val.strip().strip('"')
                        if cand.lower().endswith(".exe"):
                            out.append(cand)
                        else:
                            out.append(os.path.join(cand, "QQ.exe"))
        except OSError:
            continue

    # 卸载项里 DisplayName 带 QQ 的（第三方安装器最爱写这里）
    for hive, root in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
                       (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")):
        try:
            with winreg.OpenKey(hive, root) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        name = winreg.EnumKey(k, i)
                        with winreg.OpenKey(k, name) as sk:
                            try:
                                disp, _ = winreg.QueryValueEx(sk, "DisplayName")
                            except OSError:
                                continue
                            if not isinstance(disp, str) or "QQ" not in disp.upper():
                                continue
                            try:
                                loc, _ = winreg.QueryValueEx(sk, "InstallLocation")
                            except OSError:
                                continue
                            if isinstance(loc, str) and loc.strip():
                                out.append(os.path.join(loc.strip(), "QQ.exe"))
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def _common_paths() -> list[str]:
    out = []
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", ""),
                 os.environ.get("APPDATA", "")):
        if not base:
            continue
        out += [
            os.path.join(base, "Tencent", "QQNT", "QQ.exe"),
            os.path.join(base, "Tencent", "QQ", "Bin", "QQ.exe"),
            os.path.join(base, "Tencent", "QQ", "QQ.exe"),
            os.path.join(base, r"Programs\Tencent\QQNT\QQ.exe"),
        ]
    out.append(r"C:\QQ.exe")
    return out


def _drive_root_paths() -> list[str]:
    """盘根目录。本机的 QQ 就在 D:\\QQ.exe —— 非标准路径，只能这样兜。"""
    out = []
    try:
        drives = [f"{c}:\\" for c in "CDEFGH" if os.path.exists(f"{c}:\\")]
    except Exception:
        drives = []
    for d in drives:
        out.append(os.path.join(d, "QQ.exe"))
    return out


def _find_qq_in_processes() -> str:
    """最后兜底：QQ 正在跑，那就直接从它的进程路径反推安装位置。"""
    for row in pw.list_processes(QQ_PROCESS_NAMES):
        p = pw.process_path(row["pid"])
        if p and os.path.isfile(p):
            return p
    return ""


def find_qq_exe(configured: str = "") -> dict:
    """
    按可靠性从高到低的顺序找 QQ.exe，返回 {path, source, candidates}。

    configured 非空且文件存在时直接采用 —— 用户手填的永远优先于我们的猜测。
    """
    tried: list[str] = []

    cand = (configured or "").strip().strip('"')
    if cand:
        tried.append(cand)
        if os.path.isfile(cand):
            return {"path": cand, "source": "手动指定", "candidates": tried}

    for label, gen in (("注册表", _registry_candidates),
                       ("常见安装目录", _common_paths),
                       ("盘根目录", _drive_root_paths)):
        for p in gen():
            if p in tried:
                continue
            tried.append(p)
            if os.path.isfile(p):
                return {"path": p, "source": label, "candidates": tried}

    p = _find_qq_in_processes()
    if p:
        tried.append(p)
        return {"path": p, "source": "运行中的进程", "candidates": tried}

    return {"path": "", "source": "", "candidates": tried}


# ============================================================ 窗口枚举（纯 ctypes）
class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def top_windows() -> list[dict]:
    out: list[dict] = []

    def cb(hwnd, _lparam):
        try:
            length = _user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 2)
            _user32.GetWindowTextW(hwnd, buf, length + 1)

            cls = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(hwnd, cls, 256)

            pid = wintypes.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rect = _RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(rect))
            out.append({
                "hwnd": int(hwnd),
                "title": buf.value,
                "class": cls.value,
                "pid": int(pid.value),
                "visible": bool(_user32.IsWindowVisible(hwnd)),
                "minimized": bool(_user32.IsIconic(hwnd)),
                "rect": [rect.left, rect.top, rect.right, rect.bottom],
            })
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_WNDENUMPROC(cb), 0)
    except Exception:
        pass
    return out


_QQ_CLASSES = ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0", "TXGuiFoundation")


def qq_windows() -> list[dict]:
    """属于 QQ 进程的顶层窗口。主窗口判定沿用 agent.attach 的规则：最大且可见。"""
    pids = {row["pid"] for row in pw.list_processes(QQ_PROCESS_NAMES)}
    out = []
    for w in top_windows():
        if w["class"] not in _QQ_CLASSES:
            continue
        if pids and w["pid"] not in pids:
            continue
        out.append(w)
    return out


def qq_window_state() -> dict:
    wins = qq_windows()
    visible = [w for w in wins if w["visible"]]
    main = None
    best = 0
    for w in visible:
        r = w["rect"]
        area = max(0, r[2] - r[0]) * max(0, r[3] - r[1])
        if area > best:
            best, main = area, w
    return {
        "window_count": len(wins),
        "visible_count": len(visible),
        "main": main,
        "main_title": (main or {}).get("title", ""),
        "minimized": bool(main.get("minimized")) if main else None,
    }


# ============================================================ UIA 工作线程
class UiaWorker:
    """
    把 UIA 调用固定到一条线程上。

    ## 为什么必须这样

    `uiautomation` 内部的 `CUIAutomation` 是**进程级单例**：它在第一次被用到时执行
    `comtypes.client.CreateObject(...)`，而 COM 对象是**属于创建它的那个线程的公寓**的。
    一旦这个单例被建在 A 线程、又在 B 线程里用，轻则 `尚未调用 CoInitialize` 报错，
    重则静默返回空数据 —— 后者会表现成「有时候读得到有时候读不到」，极难归因。

    所以：**全进程只有这一条线程碰 UIA**，并且在这条线程启动时显式做一次
    `CoInitializeEx`（`InitializeUIAutomationInCurrentThread`）。光 import uiautomation
    是**不够的** —— import 不会创建单例，真正的坑在第一次碰控件的时候。
    """

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._module = UNSET
        self._lock = threading.Lock()
        self._import_error = ""
        self._init_error = ""

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._loop, name="uia-worker", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        initializer = None
        try:
            import uiautomation as auto
            initializer = auto.UIAutomationInitializerInThread(debug=False)
        except Exception as exc:
            self._init_error = f"{type(exc).__name__}: {exc}"
            BUS.emit(f"UIA 线程初始化失败：{self._init_error}", tag="WARN", source="ui")
        try:
            while True:
                item = self._q.get()
                if item is None:
                    return
                fn, box, evt = item
                try:
                    box["result"] = fn()
                except Exception as exc:
                    box["error"] = f"{type(exc).__name__}: {exc}"
                finally:
                    evt.set()
        finally:
            if initializer is not None:
                try:
                    initializer.Uninitialize()
                except Exception:
                    pass

    def call(self, fn, timeout: float = 25.0):
        """在 UIA 线程上执行 fn()，超时抛 TimeoutError。"""
        self._ensure_thread()
        box: dict = {}
        evt = threading.Event()
        self._q.put((fn, box, evt))
        if not evt.wait(timeout):
            raise TimeoutError("UIA 调用超时（QQ 无响应？）")
        if "error" in box:
            raise RuntimeError(box["error"])
        return box.get("result")

    def agent_module(self):
        """
        懒加载 agent 模块（含 uiautomation）。

        **必须在 UIA 线程上调用**，否则会在当前线程建起那个单例，
        把它锁死在错误的公寓里。`load_agent()` 已经把它挪到线程上执行。
        """
        if self._module is UNSET:
            try:
                import sys
                root = paths.exe_dir()
                if root not in sys.path:
                    sys.path.insert(0, root)
                import agent
                self._module = agent
            except Exception as exc:
                self._module = None
                self._import_error = f"{type(exc).__name__}: {exc}"
        return self._module

    def load_agent(self):
        """在 UIA 线程上加载 agent，返回模块或 None。"""
        try:
            return self.call(self.agent_module, timeout=40.0)
        except Exception as exc:
            self._import_error = f"{type(exc).__name__}: {exc}"
            return None

    def status(self) -> dict:
        return {
            "thread_alive": bool(self._thread and self._thread.is_alive()),
            "initialized": not self._init_error,
            "init_error": self._init_error,
            "import_error": self._import_error,
            "agent_loaded": self._module is not UNSET and self._module is not None,
        }


UIA = UiaWorker()


def check_accessibility(configured_qq: str = "") -> dict:
    """
    体检 QQ 的无障碍树：能不能附着、能不能读到消息列表、当前是哪个会话。

    返回结构里的 `ok` 是唯一可信的「无障碍参数生效」证据（见模块开头说明）。

    整个体检**跑在 UIA 工作线程上**（包括 agent 模块的加载），因为一旦在别的线程里
    碰了 uiautomation，那个进程级单例就被钉死在错误的公寓上了。
    """
    win = qq_window_state()
    result = {
        "qq_running": bool(pw.list_processes(QQ_PROCESS_NAMES)),
        "windows": win,
        "attached": False,
        "ok": False,
        "dialog_title": "",
        "is_group": False,
        "message_count": 0,
        "ml_list_found": False,
        "editor_found": False,
        "send_btn_found": False,
        "error": "",
        # 每一条返回路径都要带上它。少了这一个字段的后果不小：
        # 界面和诊断报告里就看不到「UIA 线程到底初始化了没」——
        # 而那恰好是排查「读不到界面」时最该先看的一行。
        "uia": UIA.status(),
    }
    if not result["qq_running"]:
        result["error"] = "QQ 没有在运行"
        return result
    if not win["main"]:
        result["error"] = "没找到可见的 QQ 主窗口（可能在托盘里，或被最小化）"
        return result

    def job() -> dict:
        agent = UIA.agent_module()
        if agent is None:
            return {"error": f"agent 模块不可用：{UIA._import_error or '未知原因'}"}
        cfg = agent.load_config()
        q = agent.QQWindow(cfg)
        if not q.attach():
            return {"error": "附着失败：没找到可用的 QQ 窗口"}
        out = {
            "attached": True,
            "dialog_title": q.dialog_title,
            "is_group": bool(q.is_group),
            "ml_list_found": q.ml_list is not None,
            "editor_found": q.editor is not None,
            "send_btn_found": q.send_btn is not None,
        }
        try:
            msgs = q.read_messages(limit=10)
            out["message_count"] = len(msgs)
            out["last_direction"] = msgs[-1].direction if msgs else ""
        except Exception as exc:
            out["error"] = f"读消息失败：{type(exc).__name__}: {exc}"
        # 会话列表能读到，才算无障碍树整体可用
        try:
            import qqid
            sessions = qqid.list_sessions(q.win)
            out["session_count"] = len(sessions)
        except Exception as exc:
            out["session_count"] = 0
            out.setdefault("error", f"读会话列表失败：{type(exc).__name__}: {exc}")
        return out

    try:
        data = UIA.call(job, timeout=45.0)
        result.update(data)
        result["ok"] = bool(data.get("ml_list_found")) or bool(data.get("session_count"))
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["uia"] = UIA.status()      # 加载 agent 之后再刷一次，此时能看到真实结果
    return result


# ============================================================ 启动 / 重启
def build_launch_args(qq_exe: str, cdp_enabled: bool, cdp_port: int,
                      extra_args: str = "") -> list[str]:
    args = [qq_exe, ACCESSIBILITY_FLAG]
    if cdp_enabled:
        args += [f"--remote-debugging-port={int(cdp_port)}", "--remote-allow-origins=*"]
    if extra_args.strip():
        args += [a for a in extra_args.split() if a]
    return args


def launch_qq(qq_exe: str, cdp_enabled: bool = False, cdp_port: int = 9222,
              extra_args: str = "") -> dict:
    if not qq_exe or not os.path.isfile(qq_exe):
        return {"ok": False, "error": f"QQ 可执行文件不存在：{qq_exe or '(空)'}"}
    args = build_launch_args(qq_exe, cdp_enabled, cdp_port, extra_args)
    pid = pw.launch_detached(args, cwd=os.path.dirname(qq_exe))
    return {"ok": pid != 0, "pid": pid, "args": args,
            "error": "" if pid else "启动失败"}


def restart_qq(qq_exe: str, cdp_enabled: bool = False, cdp_port: int = 9222,
               extra_args: str = "") -> dict:
    """
    完全退出 QQ 再用正确参数拉起。

    为什么必须「完全退出」：启动参数只在主进程**首次启动**时生效。QQ 已经在跑的时候
    再执行一次带参数的命令，它只会把已有窗口唤到前台，参数不应用 —— 这是最容易
    白折腾半小时的坑。登录态会保留，不用重新扫码。
    """
    if not qq_exe or not os.path.isfile(qq_exe):
        return {"ok": False, "error": f"QQ 可执行文件不存在：{qq_exe or '(空)'}"}
    killed = pw.kill_processes(QQ_PROCESS_NAMES, wait_seconds=8.0)
    time.sleep(1.0)
    res = launch_qq(qq_exe, cdp_enabled, cdp_port, extra_args)
    res["killed"] = killed
    return res


def wait_ready(timeout: float = 180.0, interval: float = 2.5,
               should_stop=None, on_progress=None) -> dict:
    """
    等 QQ 起好并进入可读状态。用户可能要在这个窗口里扫码/登录，所以超时给得比较宽。

    on_progress(elapsed, last) 每轮回调一次，界面据此显示倒计时和最近一次失败原因。
    """
    started = time.time()
    last: dict = {}
    while time.time() - started < timeout:
        if should_stop and should_stop():
            return {"ok": False, "error": "已被取消", "elapsed": time.time() - started}
        last = check_accessibility()
        if last.get("ok"):
            return {"ok": True, "elapsed": round(time.time() - started, 1), "probe": last}
        if on_progress:
            try:
                on_progress(round(time.time() - started, 1), last)
            except Exception:
                pass
        time.sleep(interval)
    return {"ok": False, "error": last.get("error") or "超时仍不可读",
            "elapsed": round(time.time() - started, 1), "probe": last}


def cdp_status(port: int = 9222) -> dict:
    """探一下 CDP 端口是不是真的 CDP（QQ 自己有个 9211 的 JWT 服务，别被它骗了）。"""
    import json
    import urllib.request
    url = f"http://127.0.0.1:{int(port)}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return {"ok": True, "browser": data.get("Browser", ""), "url": url}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "url": url}
