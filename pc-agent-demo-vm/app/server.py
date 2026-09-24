# -*- coding: utf-8 -*-
"""
server.py —— WebUI 的服务端

刻意只用标准库（`http.server`）而不是 FastAPI/uvicorn：这个程序的全部价值在于
**能在别人的虚拟机上双击就跑**，而每多一个第三方 Web 框架，打包体积、版本冲突
和「在这台机器上装不上」的概率就多一分。这里需要的能力只有「几个 JSON 接口 + 一条
SSE 日志流」，标准库足够。

## 安全模型

    监听地址     默认 127.0.0.1 —— 只有本机能连
    访问令牌     每次启动随机生成，必须带在 URL 或请求头里

令牌不是走过场：这个界面能读你的聊天记录、能冒充你发消息、还能结束你的 QQ 进程。
没有令牌时，同一台机器上任何一个网页里的一行 fetch 都能把上面这些做完。
界面首屏也必须带令牌才能打开，所以「不知道令牌」= 连 HTML 都拿不到。
"""

from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import errors as E
from . import paths, platform_win as pw, qqctl, settings
from .logbus import BUS
from .supervisor import SUP, TASKS

TOKEN = secrets.token_urlsafe(24)

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
}


# ============================================================ 后台任务
class Jobs:
    """
    那种「要等很久、但必须让界面看到进度」的动作（比如等 QQ 登录）。

    做成后台任务而不是让 HTTP 请求一直挂着：RDP 断开、浏览器超时、用户刷新页面
    都会掐掉连接，但「等 QQ 就绪」这件事应该继续做完。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._job: dict | None = None

    def state(self) -> dict | None:
        with self._lock:
            return dict(self._job) if self._job else None

    def running(self, name: str) -> bool:
        with self._lock:
            return bool(self._job and self._job["name"] == name and self._job["status"] == "running")

    def start(self, name: str, label: str, fn) -> dict:
        with self._lock:
            if self._job and self._job["status"] == "running":
                return E.envelope("E-PROC-001", f"「{self._job['label']}」还在进行中",
                                  {"已进行": round(self._job.get("elapsed") or 0, 1)})
            self._job = {"name": name, "label": label, "status": "running",
                         "started_at": time.time(), "elapsed": 0.0, "error": "",
                         "progress": ""}
            job = self._job

        def progress(elapsed, last):
            with self._lock:
                if self._job is job:
                    self._job["elapsed"] = elapsed
                    self._job["progress"] = last.get("error") or "等待中…"

        def runner():
            try:
                res = fn(progress)
                with self._lock:
                    if self._job is job:
                        self._job["status"] = "ok" if res.get("ok") else "failed"
                        self._job["error"] = res.get("error", "")
                        self._job["elapsed"] = res.get("elapsed", self._job["elapsed"])
            except Exception as exc:
                with self._lock:
                    if self._job is job:
                        self._job["status"] = "failed"
                        self._job["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                with self._lock:
                    if self._job is job:
                        self._job["ended_at"] = time.time()

        threading.Thread(target=runner, name=f"job-{name}", daemon=True).start()
        return {"ok": True}

    def cancel(self) -> dict:
        with self._lock:
            if not self._job or self._job["status"] != "running":
                return E.envelope("E-PROC-001", "没有正在进行的后台任务")
            self._job["status"] = "cancelled"
        return {"ok": True}


JOBS = Jobs()
_QQ_WAIT_CANCEL = threading.Event()


# ============================================================ 状态汇总
def _qq_summary(app: dict, with_probe: bool = False) -> dict:
    found = qqctl.find_qq_exe(app.get("qq_exe_path", ""))
    win = qqctl.qq_window_state()
    running = bool(pw.list_processes(qqctl.QQ_PROCESS_NAMES))
    out = {
        "running": running,
        "processes": pw.list_processes(qqctl.QQ_PROCESS_NAMES),
        "windows": win,
        "exe": found,
        "autostart": pw.autostart_status(),
        "cdp": qqctl.cdp_status(int(app.get("cdp_port") or 9222)) if app.get("cdp_enabled") else
               {"ok": False, "enabled": False},
        "cdp_enabled": bool(app.get("cdp_enabled")),
    }
    if with_probe:
        out["probe"] = qqctl.check_accessibility(app.get("qq_exe_path", ""))
    return out


def build_state(with_probe: bool = False, with_cmdlines: bool = False) -> dict:
    app = settings.load_app_settings()
    agent_cfg = settings.load_merged()
    key = settings.resolve_api_key_display()
    st = SUP.state()
    qq = _qq_summary(app, with_probe=with_probe)

    warnings: list[str] = []
    if not pw.is_admin():
        warnings.append("当前**没有管理员权限**：自启注册、VM 加固、结束 QQ 进程会失败。"
                        "点右上角「以管理员重启」可以提权。")
    if paths.DATA_DIR_REASON.endswith("回退到 LOCALAPPDATA"):
        warnings.append(f"exe 所在目录不可写，数据已放到 {paths.DATA_DIR}。"
                        f"想便携使用，请把 exe 移到可写目录。")
    if not key["present"]:
        warnings.append("还没有配置 API Key，模型调不通。请在「模型接入」里填好再启动常驻。")
    if qq["running"] and not qq.get("probe", {}).get("ok") and with_probe:
        warnings.append("QQ 在跑，但**读不到它的界面**。多半是没有以无障碍模式启动 —— "
                        "用「重启 QQ 到可读状态」修一下（登录态会保留）。")
    if pw.desktop_state() == "locked":
        warnings.append("检测到桌面处于锁屏状态。读取还能工作，但切换会话和发送会失败。"
                        "这正是选用虚拟机路线要解决的问题。")
    if app.get("web_host") == "0.0.0.0":
        warnings.append("WebUI 正在监听所有网卡。跨机访问请只在可信内网使用。")

    return {
        "version": _version(),
        "token_hint": TOKEN[:6],
        "admin": pw.is_admin(),
        "system_user": pw.is_system_user(),
        "desktop": pw.desktop_state(),
        "foreground_title": pw.foreground_title(),
        "paths": paths.describe(),
        "app": app,
        "llm": {
            "model": agent_cfg.get("llm", {}).get("model", ""),
            "api_base": agent_cfg.get("llm", {}).get("api_base", ""),
            "key_present": key["present"],
            "key_source": key["source"],
        },
        "qq": qq,
        "supervisor": st,
        "job": JOBS.state(),
        "warnings": warnings,
        "log_stats": BUS.counts(),
        "memory": pw.memory_str(),
        "lan_ip": pw.lan_ip(),
        "server_time": time.time(),
    }


def _version() -> str:
    from . import __version__
    return __version__


# ============================================================ 请求处理
class Handler(BaseHTTPRequestHandler):
    server_version = "QQAgent"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------- 基础工具
    def log_message(self, fmt, *args):        # 默认会往 stderr 狂刷，这里降级
        if self.path and self.path.startswith("/api/events"):
            return
        BUS.emit(f"{self.address_string()} {fmt % args}", tag="HTTP", source="http",
                 level="debug")

    def _token_ok(self, query: dict) -> bool:
        if self.headers.get("X-Token") == TOKEN:
            return True
        if query.get("t", [""])[0] == TOKEN:
            return True
        cookie = self.headers.get("Cookie", "")
        return f"qat={TOKEN}" in cookie

    def _deny(self):
        # 令牌无效也走统一信封 —— 界面据此显示「重新打开控制台」而不是一句干巴巴的 401
        self._json(E.envelope("E-WEB-001", "请求没有携带正确的访问令牌",
                              {"提示": "从托盘菜单「打开控制台界面」重新打开"}), 401)

    def _json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _file(self, rel: str):
        full = os.path.join(paths.WEB_DIR, rel)
        # 目录穿越防护：拼出来的路径必须仍在 web 目录里
        if not os.path.abspath(full).startswith(os.path.abspath(paths.WEB_DIR)):
            return self._json(E.envelope("E-WEB-003", f"非法路径：{rel}"), 400)
        if not os.path.isfile(full):
            return self._json(E.envelope("E-WEB-003", f"静态资源不存在：{rel}",
                                         {"查找目录": paths.WEB_DIR}), 404)
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            data = f.read()
        if ext == ".html":
            # 令牌直接注入页面：UI 后续的 fetch 都带着它，用户不需要手抄
            text = data.decode("utf-8").replace("__TOKEN__", TOKEN)
            data = text.encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            if ext == ".html":
                self.send_header("Set-Cookie", f"qat={TOKEN}; Path=/; SameSite=Strict")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    # ---------------------------------------------------- GET
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            if not self._token_ok(query):
                return self._deny()
            return self._file("index.html")
        if route == "/favicon.ico":
            return self._file("favicon.svg")

        if not self._token_ok(query):
            return self._deny()

        if route == "/api/state":
            probe = query.get("probe", ["0"])[0] in ("1", "true")
            return self._json({"ok": True, "state": build_state(with_probe=probe)})
        if route == "/api/settings":
            return self._json({"ok": True, "settings": settings.as_ui_payload()})
        if route == "/api/tasks":
            return self._json({"ok": True, "tasks": TASKS, "supervisor": SUP.state()})
        if route == "/api/logs":
            since = int(query.get("since", ["0"])[0] or 0)
            return self._json({"ok": True, "lines": BUS.since(since),
                               "counts": BUS.counts()})
        if route == "/api/qq/diag":
            app = settings.load_app_settings()
            return self._json({
                "ok": True,
                "found": qqctl.find_qq_exe(app.get("qq_exe_path", "")),
                "processes": pw.list_processes(qqctl.QQ_PROCESS_NAMES),
                "cmdlines": pw.process_command_lines(qqctl.QQ_PROCESS_NAMES),
                "windows": qqctl.qq_window_state(),
                "probe": qqctl.check_accessibility(app.get("qq_exe_path", "")),
                "cdp": qqctl.cdp_status(int(app.get("cdp_port") or 9222)),
                "desktop": pw.desktop_state(),
                "admin": pw.is_admin(),
                "autostart": pw.autostart_status(),
                "memory": pw.memory_str(),
            })
        if route == "/api/events":
            return self._sse()
        if route == "/api/diagnose":
            # 体检会跑网络请求和 UIA，两个都可以按需关掉（默认都开）
            from . import diagnose
            with_llm = query.get("llm", ["1"])[0] not in ("0", "false")
            with_uia = query.get("uia", ["1"])[0] not in ("0", "false")
            return self._json(diagnose.run(with_llm=with_llm, with_uia=with_uia))
        if route == "/api/errors":
            # 错误码目录（界面上的「错误码速查」用它）
            return self._json({"ok": True, "catalog": E.EC.CATALOG,
                               "domains": E.EC.DOMAINS,
                               "summary": E.catalog_summary()})
        return self._json(E.envelope("E-WEB-003", f"未知接口 {route}"), 404)

    # ---------------------------------------------------- POST
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/")
        query = urllib.parse.parse_qs(parsed.query)
        if not self._token_ok(query):
            return self._deny()
        data = self._body()
        try:
            return self._dispatch_post(route, data)
        except Exception as exc:
            BUS.emit(f"接口 {route} 抛异常：{type(exc).__name__}: {exc}",
                     tag="ERR", source="ui")
            env = E.from_exception(exc, "E-WEB-003", {"接口": route})
            return self._json(env, 500)

    def _dispatch_post(self, route: str, data: dict):
        app = settings.load_app_settings()

        if route == "/api/settings":
            res = settings.save(data.get("values") or {})
            if res.get("ok"):
                BUS.emit("配置已保存" + (f"，变更 {len(res['changed'])} 项" if res["changed"] else "（无变化）"),
                         tag="CFG", source="ui")
                if res.get("backup"):
                    BUS.emit(f"改动前的配置已备份：{os.path.basename(res['backup'])}",
                             tag="CFG", source="ui")
            return self._json(res)

        if route == "/api/settings/reset":
            return self._json(settings.reset_to_defaults())

        if route == "/api/task":
            res = SUP.run_task(str(data.get("id") or ""), data.get("params") or {})
            return self._json(res)

        if route == "/api/task/kill":
            return self._json(SUP.kill_task(str(data.get("id") or "")))

        if route == "/api/agent/start":
            return self._json(SUP.start_resident(dry_run=bool(data.get("dry_run"))))

        if route == "/api/agent/stop":
            return self._json(SUP.stop_resident())

        if route == "/api/agent/restart":
            dry = data.get("dry_run")
            return self._json(SUP.restart_resident(dry_run=None if dry is None else bool(dry)))

        if route == "/api/qq/launch":
            found = qqctl.find_qq_exe(app.get("qq_exe_path", ""))
            if not found["path"]:
                return self._json(E.envelope(
                    "E-QQ-001", "没找到 QQ.exe",
                    {"试过的位置": "; ".join(found["candidates"][:8]),
                     "动作": "在「运行环境」里手动填写完整路径"}))
            res = qqctl.launch_qq(found["path"], bool(app.get("cdp_enabled")),
                                  int(app.get("cdp_port") or 9222),
                                  str(app.get("qq_extra_args") or ""))
            BUS.emit(f"已启动 QQ（{found['source']}）：{found['path']}", tag="QQ", source="ui")
            if not res.get("ok"):
                res = E.envelope("E-QQ-002", res.get("error", "启动失败"),
                                 {"路径": found["path"], "参数": res.get("args")})
            return self._json(res)

        if route == "/api/qq/restart":
            found = qqctl.find_qq_exe(app.get("qq_exe_path", ""))
            if not found["path"]:
                return self._json(E.envelope(
                    "E-QQ-001", "没找到 QQ.exe",
                    {"动作": "在「运行环境」里手动填写完整路径"}))
            BUS.emit("正在重启 QQ 到可读状态：先完全退出（启动参数只在首次启动生效），再带参数拉起",
                     tag="QQ", source="ui")
            res = qqctl.restart_qq(found["path"], bool(app.get("cdp_enabled")),
                                   int(app.get("cdp_port") or 9222),
                                   str(app.get("qq_extra_args") or ""))
            if res.get("ok"):
                BUS.emit(f"QQ 已重新启动，结束了 {res.get('killed', 0)} 个旧进程。"
                         f"登录态保留，若弹出登录窗口请手动登录。", tag="QQ", source="ui")
            else:
                res = E.envelope("E-QQ-005", res.get("error", "重启失败"),
                                 {"路径": found["path"], "结束的旧进程": res.get("killed")})
                for line in E.EC.describe("E-QQ-005").splitlines():
                    BUS.emit(line, tag="ERR", source="ui")
            return self._json(res)

        if route == "/api/qq/wait":
            _QQ_WAIT_CANCEL.clear()
            timeout = float(data.get("timeout") or 180)

            def job(progress):
                return qqctl.wait_ready(timeout=timeout, should_stop=_QQ_WAIT_CANCEL.is_set,
                                        on_progress=progress)
            return self._json(JOBS.start("qq_wait", "等待 QQ 就绪", job))

        if route == "/api/qq/wait/cancel":
            _QQ_WAIT_CANCEL.set()
            JOBS.cancel()
            return self._json({"ok": True})

        if route == "/api/admin/elevate":
            if pw.is_admin():
                return self._json({"ok": True, "note": "已经是管理员权限"})
            ok = pw.relaunch_as_admin()
            if not ok:
                return self._json(E.envelope(
                    "E-ENV-003", "提权请求被拒绝或失败",
                    {"提示": "UAC 窗口被点了「否」，或系统策略禁止提权"}))
            BUS.emit("已请求以管理员权限重新启动（请在弹出的 UAC 窗口点「是」，然后重新打开界面）",
                     tag="ADM", source="ui")
            return self._json({"ok": True, "exit": True})

        if route == "/api/admin/autostart":
            action = str(data.get("action") or "")
            if action == "install":
                if not pw.is_admin():
                    return self._json(E.envelope("E-ENV-003", "安装开机自启需要管理员权限"))
                target = paths.exe_dir() + r"\qq-agent.exe" if paths.is_frozen() else \
                    os.path.join(paths.exe_dir(), "run_ui.bat")
                res = pw.install_autostart(target)
                if not res.get("ok"):
                    return self._json(E.envelope("E-ENV-003",
                                                 res.get("error", "注册计划任务失败"),
                                                 {"目标": target}))
                return self._json(res)
            if action == "remove":
                res = pw.remove_autostart()
                if not res.get("ok"):
                    return self._json(E.envelope("E-ENV-003",
                                                 res.get("error", "删除计划任务失败")))
                return self._json(res)
            return self._json(E.envelope("E-WEB-003",
                                         f"action 必须是 install 或 remove，收到 {action!r}"))

        if route == "/api/admin/harden":
            res = pw.apply_vm_hardening()
            if not res.get("ok"):
                return self._json(E.envelope("E-ENV-003",
                                             res.get("error", "加固失败"),
                                             {"已尝试": res.get("applied")}))
            return self._json(res)

        if route == "/api/system/open":
            target = str(data.get("target") or "data")
            mapping = {"data": paths.DATA_DIR, "log": paths.LOG_DIR,
                       "config": paths.CONFIG_PATH, "state": paths.STATE_DIR}
            path = mapping.get(target)
            if not path:
                return self._json(E.envelope("E-WEB-003",
                                             f"未知的打开目标 {target!r}",
                                             {"可用": ", ".join(mapping)}))
            if not pw.open_in_explorer(path):
                return self._json(E.envelope("E-WEB-003", f"系统拒绝打开 {path}"))
            return self._json({"ok": True, "path": path})

        if route == "/api/system/quit":
            from . import runtime
            BUS.emit("界面请求退出程序", tag="BOOT", source="ui")
            runtime.request_quit()
            return self._json({"ok": True, "note": "正在退出"})

        if route == "/api/logs/clear":
            BUS.clear()
            BUS.emit("日志已清空", tag="UI", source="ui")
            return self._json({"ok": True})

        return self._json(E.envelope("E-WEB-003", f"未知接口 {route}"), 404)

    # ---------------------------------------------------- SSE
    def _sse(self):
        q = BUS.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            # 首屏把已有日志补上，否则刚打开页面会是一片空白，看起来像程序没在用
            for row in BUS.tail(120):
                self._sse_send(json.dumps({"type": "log", "line": row}, ensure_ascii=False))
            self._sse_send(json.dumps({"type": "state", "state": build_state()},
                                      ensure_ascii=False, default=str))

            last_state = 0.0
            while True:
                try:
                    payload = q.get(timeout=1.0)
                    self._sse_send(payload if payload.startswith("{") else
                                   json.dumps({"type": "raw", "data": payload}))
                except queue.Empty:
                    # 每秒一次心跳注释，防止反代/浏览器把空闲连接掐掉
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                now = time.time()
                if now - last_state > 2.0:
                    last_state = now
                    self._sse_send(json.dumps({"type": "state", "state": build_state()},
                                              ensure_ascii=False, default=str))
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass
        finally:
            BUS.unsubscribe(q)
            self.close_connection = True

    def _sse_send(self, text: str) -> None:
        self.wfile.write(f"data: {text}\n\n".encode("utf-8"))
        self.wfile.flush()


# ============================================================ 启动
class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(host: str, port: int) -> Server:
    httpd = Server((host, port), Handler)
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    return httpd
