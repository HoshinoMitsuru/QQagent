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

from . import desktop, errors as E, host
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
    # 只算一次：这个函数虽然便宜（读一个 JSON + 一次 OpenProcess），
    # 但状态流每 2 秒推一次全量，没必要在一份状态里算三遍。
    hd = host.daemon_status()

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

    if hd["stale"]:
        warnings.append("隐藏桌面的常驻宿主**心跳已过期**：它的进程还在，但已经 "
                        f"{hd['age']}s 没更新状态了。多半是卡在一次很慢的 UIA 调用或模型调用上。")
    if hd["heartbeat_present"] and not hd["running"] and hd["error"]:
        warnings.append(f"常驻宿主已停止：{hd['error']}")

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
        "host": hd,
        # 上一次的抓图结果。读一个小 JSON，很便宜 —— 放进状态流是为了让面板
        # 在「抓图任务结束后」自己就能刷出图，不用额外拉一次接口。
        "grab_last": host.last_grab(),
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


def _hidden_desktop(app: dict) -> str:
    """隐藏桌面名：设置里可改，留空一律回到默认名（不能空着 —— 空名等于不指定桌面）。"""
    return str((app or {}).get("hidden_desktop") or "").strip() or desktop.DEFAULT_NAME


#: 抓图**请求**的落盘位置。定义在 `host` 里 —— 宿主做免扫码登录时的探测也落同一个文件，
#: 两处各写一份文件名迟早会漂移（那时界面会显示一张永远不更新的旧图）。
#:
#: ⚠️ 这只是「想要的路径」，**不等于最终文件名**：没有 Pillow 时 `winmsg.grab_any`
#: 会把后缀换成 `.bmp`（打包版就是这样，因为 spec 排除了 PIL）。
#: 要发图请用 `_shot_path()` 去问上一次抓图的结果。
SHOT_PNG = host.SHOT_PNG

#: 允许被发出去的图片后缀 → MIME。白名单而不是「按后缀猜」：
#: 这条路由的输入来自磁盘上的一个文件路径，任何一条能读任意文件的路径都是漏洞。
SHOT_MIME = {".png": "image/png", ".bmp": "image/bmp",
             ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}


def _shot_path() -> str:
    """
    上一次抓图**实际**写出来的那个文件；没有就返回空串。

    判据分三层，缺一不可：
      1. 抓图结果里记了 `saved.path`（hostagent 写进 host-grab.json 的）
      2. 那个文件现在还在（别发一个已经不存在的路径）
      3. 后缀在 `SHOT_MIME` 白名单里（同时也是「路径没有跑到别处去」的一道闸）
    """
    try:
        saved = (host.last_grab() or {}).get("saved") or {}
        p = str(saved.get("path") or "")
    except Exception:
        return ""
    if not p or not os.path.isfile(p):
        return ""
    if os.path.splitext(p)[1].lower() not in SHOT_MIME:
        return ""
    return p


# ============================================================ 请求处理
class Handler(BaseHTTPRequestHandler):
    server_version = "QQAgent"
    protocol_version = "HTTP/1.1"

    #: 「响应头是否已经发出去了」。异常信封必须知道这件事：
    #: 已经开写（SSE、静态文件）之后再补一条 JSON 错误，只会把响应弄成垃圾。
    _responded = False

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
        self._responded = True
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

    def _shot(self):
        """
        把上一次抓到的隐藏桌面画面发出去。

        ## 为什么要一个专门的路由

        `_file()` 的根目录是 `app/web/`，而截图落在数据目录的 `state/` 里 ——
        两者不在同一棵树。与其把 web 目录的边界放宽（那正是目录穿越开始的地方），
        不如让这条路由只认**上一次抓图结果里记下的那一个文件**。

        ## ⚠️ 为什么不能假定它是 `.png`（打包版踩过）

        抓图优先写 PNG，但**没有 Pillow 时会退成 BMP**（`winmsg.grab_any`）。
        而 `qq-agent.spec` 刻意排除了 PIL —— 于是：

            源码模式：Pillow 在 → 落 `desktop-shot.png` → 界面正常
            打包版：  Pillow 被排除 → 落 `desktop-shot.bmp` → 这条路由死找 .png
                      → 界面永远 404，「抓一张画面」看着像坏了（其实图早就抓到了）

        所以正确做法是：**问上一次抓图的结果要文件名**，并按真实后缀给 MIME。
        这也顺手让「以后换成 jpg/webp」不需要再改这里。
        """
        path = _shot_path()
        if not path:
            return self._json(E.envelope(
                "E-PATH-002", "还没有抓过隐藏桌面的画面",
                {"动作": "点面板上的「抓一张现在的画面」",
                 "预期产出": SHOT_PNG + "（没有 Pillow 时会退成 .bmp）"}), 404)
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as exc:
            return self._json(E.from_exception(exc, "E-PATH-001",
                                               {"文件": path}), 500)
        ext = os.path.splitext(path)[1].lower()
        self._responded = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", SHOT_MIME.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

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
        self._responded = True
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
        """
        所有 GET 都套一层统一信封。

        ## 为什么这一层不能省

        POST 那边一开始就有（见 `do_POST`），GET 这边一直没有 —— 于是在 GET 分支里
        写错一个名字，表现是：**连接被直接掐断**，浏览器只报一句
        `RemoteDisconnected` / `连不上控制台服务`。用户看到的是「后端挂了」，
        而真相只是某个路由里少了一行 `app = settings.load_app_settings()`。

        这正是本项目反复在防的那类假诊断：一句「连不上」会把排查方向整个带偏。
        """
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)
        try:
            return self._dispatch_get(route, query)
        except Exception as exc:
            BUS.emit(f"接口 {route} 抛异常：{type(exc).__name__}: {exc}",
                     tag="ERR", source="ui")
            env = E.from_exception(exc, "E-WEB-003", {"接口": route,
                                                      "方法": "GET"})
            if self._responded:
                # 已经发过响应头（SSE / 静态文件）—— 这时候再写 JSON 只会把响应弄坏，
                # 唯一的动作是留痕。日志里那条 ERR 就是留给这种情况的。
                return
            return self._json(env, 500)

    def _dispatch_get(self, route: str, query: dict):
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
        if route == "/api/host/status":
            app = settings.load_app_settings()
            return self._json({"ok": True, "daemon": host.daemon_status(),
                               "last_grab": host.last_grab(),
                               "desktop_default": desktop.DEFAULT_NAME,
                               "desktop_exists": desktop.exists(_hidden_desktop(app)),
                               "desktop_target": _hidden_desktop(app),
                               "shot_path": _shot_path()})
        # `/api/host/shot.png` 作为别名保留：界面和 bookmark 都可能还带着它，
        # 而真正会变的是后缀（打包版落 .bmp），所以不给它另开一条语义。
        if route in ("/api/host/shot", "/api/host/shot.png"):
            return self._shot()
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

        # -------------------------------------------------- 隐藏桌面（R4）
        #
        # 这一组和上面 `/api/agent/*` **不是同一套东西**，别混：
        #   /api/agent/*   V1：壳在自己桌面上起一个 agent 子进程，抢前台发消息
        #   /api/host/*    V2：把常驻宿主丢进另一张桌面，全程不碰用户前台
        # 两者都会占用 QQ 的输入框，所以**不能同时开**（见下面 start 的互斥检查）。
        if route == "/api/host/start":
            if SUP.state()["resident"]["running"]:
                return self._json(E.envelope(
                    "E-PROC-002", "V1 常驻（壳内子进程）正在运行，不能同时开隐藏桌面常驻",
                    {"原因": "两者都会同时操作同一个 QQ 输入框，会互相踩",
                     "动作": "先在上面的「常驻运行」里停止 V1 常驻"}))
            name = _hidden_desktop(app)
            hd = host.daemon_status()
            if hd["running"]:
                return self._json(E.envelope(
                    "E-PROC-001", "隐藏桌面常驻已经在运行了",
                    {"pid": hd["pid"], "已运行": f"{hd['uptime']}s", "阶段": hd["phase"]}))
            res = host.start_daemon(
                desktop_name=name,
                qq_exe=str(app.get("qq_exe_path") or ""),
                dry_run=bool(data.get("dry_run")),
                no_send=bool(data.get("no_send")),
                with_agent=not bool(data.get("no_agent")),
                stop_qq_on_exit=bool(data.get("stop_qq")),
                chat=str(app.get("hidden_desktop_chat") or ""),
                wait_ready=float(data.get("wait") or 0.0))
            if res.get("ok"):
                mode = ("只读模式，不会发送任何消息"
                        if (data.get("dry_run") or data.get("no_send"))
                        else "会真的回复并发送消息")
                BUS.emit(f"已把常驻宿主丢进隐藏桌面 {name}（pid {res.get('pid')}）—— {mode}。"
                         f"它在那张桌面上有自己的 QQ，与你自己正在用的那个互不影响。",
                         tag="RUN", source="ui")
            else:
                BUS.emit(f"启动隐藏桌面常驻失败：{res.get('error')}", tag="ERR", source="ui")
            return self._json(res)

        if route == "/api/host/stop":
            BUS.emit("正在停止隐藏桌面常驻：先给回复循环留时间把队列发完，再退宿主",
                     tag="RUN", source="ui")
            res = host.stop_daemon()
            if res.get("killed"):
                BUS.emit("优雅退出超时，已强制结束宿主进程。"
                         "队列里没发出去的回复已落盘，下次启动会继续发。",
                         tag="WARN", source="ui")
            BUS.emit("隐藏桌面常驻已停止" if res.get("ok") else
                     f"停止失败：{res.get('error')}", tag="RUN", source="ui")
            return self._json(res)

        if route == "/api/host/grab":
            # 抓图要派一个进程进那张桌面（PrintWindow 是唯一可靠路径），
            # 单次可能几秒到几十秒 —— 所以走后台任务，别让 HTTP 请求一直挂着。
            name = _hidden_desktop(app)
            wait = float(data.get("wait") or 1.5)

            def job(progress):
                t0 = time.time()
                # 这一步是同步的（要派一个进程进那张桌面走 PrintWindow），
                # 没法边做边报进度。但**结束时要报一次真实耗时** ——
                # 不报的话 Jobs 里的 elapsed 会一直停在 0.0，界面上显示成
                # 「已进行 0s」，看着像卡住了，实际它正在干活。
                r = host.grab(SHOT_PNG, desktop_name=name, wait=wait, uia=True)
                progress(time.time() - t0, {"error": ""})
                return r
            return self._json(JOBS.start("host_grab", "抓取隐藏桌面画面", job))

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
        self._responded = True
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
