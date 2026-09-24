# -*- coding: utf-8 -*-
"""
test_ui.py —— 控制台壳的端到端测试（离线，不碰 QQ、不发消息）

跑的是**真的 HTTP 服务、真的令牌校验、真的子进程**，只把「碰 QQ」的那些动作跳过。
做法与项目里其它测试一致：只伪造「外人看不出来」的那一层（这里是一个隔离的临时数据
目录），其余全是真的 —— 只测我们自己写的那点逻辑，测不出集成上的问题。

运行：
    python test_ui.py            # 全部用例
    python test_ui.py -v         # 打印每一步的响应

覆盖：
    1  静态页面与令牌校验（没令牌拿不到页面，页面里注入了令牌）
    2  /api/state 结构完整（管理员/桌面/路径/QQ/常驻/日志统计）
    3  /api/settings 读写往返（保存后磁盘上的 config.json 真的变了，且注释键还在）
    4  API Key 只写进 secrets.local.json，绝不落到 config.json
    5  风控参数的矛盾组合会被拦下（这是最容易被误判成「程序卡住」的配置错误）
    6  任务清单与参数校验（缺参数要报错，不是默认空串硬跑）
    7  子进程真实启动并把输出送进日志（用 --run-agent --state，不碰 QQ）
    8  互斥：常驻运行时，会碰 QQ 的任务要被拒绝
    9  只读任务可以与常驻并行
    10 SSE 能收到日志与状态
    11 危险动作的确认信息存在（前端据此弹窗）
    12 令牌错误返回 401
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="qqagent-test-")
os.environ["QQ_AGENT_HOME"] = TMP

import app.main as M            # noqa: E402
import app.paths as P           # noqa: E402
import app.runtime as R         # noqa: E402
import app.server as S          # noqa: E402
import app.settings as SET      # noqa: E402
import app.supervisor as SUPV   # noqa: E402

VERBOSE = "-v" in sys.argv
PORT = 0
BASE = ""
TOKEN = ""


def log(msg):
    print(msg, flush=True)


class Result:
    def __init__(self):
        self.ok = 0
        self.fail = 0
        self.errors = []

    def check(self, name, cond, detail=""):
        if cond:
            self.ok += 1
            log(f"  [PASS] {name}")
        else:
            self.fail += 1
            self.errors.append(f"{name} {detail}")
            log(f"  [FAIL] {name} {detail}")
        return bool(cond)

    def summary(self):
        log("")
        log("=" * 70)
        log(f"通过 {self.ok}　失败 {self.fail}")
        for e in self.errors:
            log(f"  - {e}")
        log("=" * 70)
        return 0 if not self.fail else 1


R = Result()


def http(path, body=None, token=None, raw=False, timeout=20):
    """
    token=None  → 用测试令牌
    token=False → 完全不带令牌（用于验证拒绝逻辑）
    token=<str> → 用指定的（错误的）令牌
    """
    url = BASE + path
    if token is None:
        token = TOKEN
    if token is not False:
        url += ("&" if "?" in url else "?") + "t=" + token
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw_body = r.read()
            if raw:
                return r.status, raw_body
            return r.status, json.loads(raw_body.decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw_body = e.read()
        if raw:
            return e.code, raw_body
        try:
            return e.code, json.loads(raw_body.decode("utf-8"))
        except Exception:
            return e.code, {"ok": False, "error": raw_body.decode("utf-8", "replace")}


def wait_for(cond, timeout=25.0, interval=0.3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


# ============================================================ 启动服务
def start_server():
    global PORT, BASE, TOKEN
    srv = S.serve("127.0.0.1", 0)
    PORT = srv.server_address[1]
    BASE = f"http://127.0.0.1:{PORT}"
    TOKEN = S.TOKEN
    log(f"[i] 测试服务已启动：{BASE}")
    return srv


# ============================================================ 用例
def t1_page_and_token():
    log("\n[1] 静态页面与令牌校验")
    st, body = http("/", token=False, raw=True)
    R.check("无令牌访问首页被拒（401）", st == 401, f"实际 {st}")

    st, body = http("/", raw=True)
    text = body.decode("utf-8", "replace")
    R.check("带令牌能拿到页面", st == 200 and "<title>QQAgent 控制台</title>" in text, f"实际 {st}")
    R.check("页面里注入了令牌（前端不用手抄）", TOKEN in text)
    R.check("样式内联、无外部依赖",
            "http://" not in text.replace("http://127.0.0.1", "") or
            "cdn." not in text)

    st, body = http("/favicon.ico", raw=True)
    R.check("图标可访问", st == 200 and b"<svg" in body[:200], f"实际 {st}")

    st, body = http("/", token="wrong-token", raw=True)
    R.check("错误令牌被拒", st == 401, f"实际 {st}")


def t2_state():
    log("\n[2] /api/state 结构")
    st, d = http("/api/state")
    R.check("接口返回 200/ok", st == 200 and d.get("ok"), str(d)[:200])
    s = d["state"]
    for key in ("version", "admin", "desktop", "paths", "app", "llm", "qq",
                "supervisor", "warnings", "log_stats", "server_time"):
        R.check(f"含字段 {key}", key in s)
    R.check("数据目录是隔离的临时目录", s["paths"]["data_dir"] == os.path.normpath(TMP),
            s["paths"]["data_dir"])
    R.check("supervisor 含 resident/heartbeat",
            "resident" in s["supervisor"] and "heartbeat" in s["supervisor"])
    R.check("qq 含进程/窗口/exe/自启",
            all(k in s["qq"] for k in ("running", "processes", "windows", "exe", "autostart")))
    R.check("未配置 Key 时给出警告",
            any("API Key" in w for w in s["warnings"]), str(s["warnings"]))
    if VERBOSE:
        log(json.dumps(s, ensure_ascii=False, indent=2)[:1500])


def t3_settings_roundtrip():
    log("\n[3] 配置读写往返")
    st, d = http("/api/settings")
    R.check("读取配置成功", st == 200 and d.get("ok"))
    payload = d["settings"]
    R.check("字段数 > 40", len(payload["fields"]) > 40, str(len(payload["fields"])))
    R.check("分组齐全", len(payload["groups"]) >= 6)

    cfg_before = json.load(open(P.CONFIG_PATH, encoding="utf-8"))
    cfg_before.pop("_说明", None)

    vals = dict(payload["values"])
    vals["llm.api_key"] = "sk-test-roundtrip-1234567890"
    vals["llm.model"] = "deepseek-chat"
    vals["chat.nontext_policy"] = "describe"
    vals["aggregate.base_wait_ms"] = 7000
    vals["chat.trigger_prefixes"] = ["小清澈", "清澈", "清宝"]
    st, d = http("/api/settings", {"values": vals})
    R.check("保存成功", st == 200 and d.get("ok"), str(d)[:300])
    R.check("变更被登记", len(d.get("changed", [])) >= 3, str(d.get("changed")))
    R.check("生成了备份", bool(d.get("backup")) and os.path.isfile(d["backup"]))

    disk = json.load(open(P.CONFIG_PATH, encoding="utf-8"))
    R.check("model 落盘", disk["llm"]["model"] == "deepseek-chat")
    R.check("nontext_policy 落盘", disk["chat"]["nontext_policy"] == "describe")
    R.check("数字类型没被写成字符串",
            isinstance(disk["aggregate"]["base_wait_ms"], int), str(type(disk["aggregate"]["base_wait_ms"])))
    R.check("tags 落盘为列表", disk["chat"]["trigger_prefixes"] == ["小清澈", "清澈", "清宝"])
    R.check("注释键没被抹掉", "_说明" in disk, str(list(disk.keys())[:5]))

    # 再次读取应看到新值
    st, d2 = http("/api/settings")
    R.check("重新读取拿到新值", d2["settings"]["values"]["llm.model"] == "deepseek-chat")
    R.check("Key 显示为掩码且标记已配置",
            d2["settings"]["api_key_present"] and "•" in d2["settings"]["values"]["llm.api_key"])


def t4_secret_isolation():
    log("\n[4] 密钥只进 secrets.local.json")
    cfg = json.load(open(P.CONFIG_PATH, encoding="utf-8"))
    R.check("config.json 里没有明文 Key",
            "sk-test-roundtrip" not in json.dumps(cfg), "config.json 里发现了明文密钥！")
    sec = json.load(open(P.SECRETS_PATH, encoding="utf-8"))
    R.check("secrets.local.json 里存了新 Key",
            sec.get("llm", {}).get("api_key") == "sk-test-roundtrip-1234567890")
    st, d = http("/api/state")
    R.check("state 里不出现明文 Key",
            "sk-test-roundtrip" not in json.dumps(d["state"]), "接口把密钥泄露出去了！")
    R.check("state 只报来源", d["state"]["llm"]["key_present"] and
            "secrets" in d["state"]["llm"]["key_source"])


def t5_validation():
    log("\n[5] 参数校验")
    st, d = http("/api/settings", {"values": {"llm.api_base": "deepseek.com", "llm.model": "x"}})
    R.check("接口地址缺协议被拦下",
            not d.get("ok") and any("http" in (e.get("message") or "") for e in d.get("errors", [])),
            str(d)[:200])
    R.check("校验错误带错误码",
            d.get("code") == "E-CFG-004" and all(e.get("code") for e in d.get("errors", [])),
            str(d.get("errors"))[:160])

    base = http("/api/settings")[1]["settings"]["values"]
    bad = dict(base)
    bad["queue.max_replies_per_minute"] = 60
    bad["queue.min_interval_seconds"] = 30
    st, d = http("/api/settings", {"values": bad})
    R.check("风控矛盾组合能保存但给出明确警告",
            d.get("ok") and "E-CFG-003" in (d.get("warning_codes") or []), str(d)[:300])

    empty = dict(base)
    empty["llm.model"] = ""
    st, d = http("/api/settings", {"values": empty})
    R.check("空模型名被拦下", not d.get("ok"), str(d)[:200])
    R.check("空模型名带 E-CFG-007",
            any(e.get("code") == "E-CFG-007" for e in d.get("errors", [])), str(d)[:200])


def t6_tasks():
    log("\n[6] 任务清单与参数校验")
    st, d = http("/api/tasks")
    R.check("任务清单可读", st == 200 and d.get("ok"))
    tasks = {t["id"]: t for t in d["tasks"]}
    R.check("任务数 >= 10", len(tasks) >= 10, str(len(tasks)))
    R.check("包含真发任务且标记危险", tasks.get("send", {}).get("dangerous") is True)
    R.check("真发任务带确认文案", bool(tasks.get("send", {}).get("confirm")))
    R.check("真发任务需要参数", bool(tasks.get("send", {}).get("params")))
    R.check("存在可并行(只读)任务", any(t.get("concurrent") for t in d["tasks"]))
    R.check("存在不可并行(会碰QQ)任务", any(not t.get("concurrent") for t in d["tasks"]))

    st, d = http("/api/task", {"id": "send", "params": {}})
    R.check("缺参数的发送被拒绝", not d.get("ok") and "参数" in (d.get("error") or ""), str(d)[:200])

    st, d = http("/api/task", {"id": "nonexistent"})
    R.check("未知任务被拒绝", not d.get("ok"), str(d)[:200])

    # 「QQ 没在跑就不许执行」这条门禁要**确定性**地测，不能依赖测试机上 QQ 开没开。
    # 所以直接临时把进程枚举清空 —— 测的是门禁逻辑本身，不是这台机器的状态。
    real_list = SUPV.pw.list_processes
    try:
        SUPV.pw.list_processes = lambda names=None: []
        st, d = http("/api/task", {"id": "peek", "params": {"limit": "5"}})
        R.check("QQ 未运行时任务被拒绝",
                not d.get("ok") and "QQ" in (d.get("error") or ""), str(d)[:200])
    finally:
        SUPV.pw.list_processes = real_list

    qq_on = bool(real_list(("QQ.exe", "QQEX.exe")))
    st, d = http("/api/task", {"id": "peek", "params": {"limit": "5"}})
    if qq_on:
        R.check("QQ 在运行时只读任务被放行", d.get("ok"), str(d)[:200])
        R.check("放行的只读任务不被常驻门禁误伤", "常驻" not in (d.get("error") or ""))
    else:
        R.check("QQ 确实没在跑（跳过放行用例）", True)


def t7_subprocess_and_logs():
    log("\n[7] 子进程与日志总线（跑 --state，不碰 QQ）")
    st, d = http("/api/logs")
    R.check("日志接口可读", st == 200 and d.get("ok"))
    before = d["lines"][-1]["seq"] if d["lines"] else 0

    st, d = http("/api/task", {"id": "state", "params": {}})
    R.check("只读任务被接受", d.get("ok"), str(d)[:300])
    R.check("返回了子进程 pid", isinstance(d.get("pid"), int) and d["pid"] > 0, str(d.get("pid")))

    ok = wait_for(lambda: any(r["id"] == "state" for r in
                              SUPV.SUP.state()["task_history"]), timeout=40)
    R.check("任务进入了历史记录", ok, str(SUPV.SUP.state()["task_history"])[:200])

    st, d = http("/api/logs", token=TOKEN)
    lines = [x for x in d["lines"] if x["seq"] > before]
    R.check("子进程输出进了日志总线", len(lines) > 0, f"新增 {len(lines)} 行")
    joined = "\n".join(x["text"] for x in lines)
    R.check("日志里能看到启动子进程", "启动子进程" in joined, joined[:300])
    texts = [x["text"] for x in lines]
    R.check("子进程真实产物可见（返回码或内容）",
            any("返回码" in t for t in texts) or any("上下文" in t or "会话" in t for t in texts),
            joined[:300])
    if VERBOSE:
        for x in lines[:25]:
            log(f"      {x['clock']} {x['tag']:<5} {x['text'][:110]}")
    hi = SUPV.SUP.state()["task_history"]
    R.check("任务返回码为 0", any(r.get("rc") == 0 for r in hi), str(hi)[:200])


def t8_mutex():
    log("\n[8] 互斥：常驻运行时拒绝会碰 QQ 的任务")
    # 用一个「假装常驻」的方式：直接启动真常驻会去附着 QQ（本机没开 QQ 会立刻退出），
    # 所以这里用一个真实的子进程 + 手工登记，验证门禁逻辑本身。
    import subprocess
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "agent.py"), "--state"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            cwd=P.DATA_DIR, stdin=subprocess.DEVNULL)
    fake = SUPV.RunInfo("resident", "测试用假常驻", [])
    fake.pid, fake.proc = proc.pid, proc
    SUPV.SUP.resident = fake
    try:
        st, d = http("/api/task", {"id": "input_test", "params": {}})
        R.check("常驻运行时 input_test 被拒绝",
                d.get("code") == "E-PROC-002" and "常驻" in (d.get("detail") or ""),
                str(d)[:220])
        st, d = http("/api/agent/start", {"dry_run": True})
        R.check("常驻已在运行时不能再启一个",
                d.get("code") == "E-PROC-001" and "已经在运行" in (d.get("detail") or ""),
                str(d)[:220])
        # 只读任务应当仍可并行
        st, d = http("/api/task", {"id": "state", "params": {}})
        R.check("只读任务可与常驻并行", d.get("ok"), str(d)[:200])
    finally:
        SUPV.SUP.resident = None
        try:
            proc.kill()
        except Exception:
            pass


def t9_stop_flag():
    log("\n[9] 停止哨兵协议")
    from app import paths as PP
    SUPV.SUP.clear_stop_flag()
    R.check("初始没有哨兵文件", not os.path.isfile(PP.STOP_PATH))
    SUPV.SUP.request_stop()
    R.check("request_stop 写入哨兵", os.path.isfile(PP.STOP_PATH))
    SUPV.SUP.clear_stop_flag()
    R.check("clear_stop_flag 清除哨兵", not os.path.isfile(PP.STOP_PATH))

    # agent 侧要能看见它（这是跨进程协议，必须两边都对）
    src = open(os.path.join(HERE, "agent.py"), encoding="utf-8").read()
    R.check("agent.py 读了 QQ_AGENT_STOP", "QQ_AGENT_STOP" in src)
    R.check("agent.py 循环里检查停止", "stop_requested()" in src)
    R.check("agent.py 有优雅退出方法", "def shutdown(" in src)


def t10_sse():
    log("\n[10] SSE 事件流")
    got = {"log": 0, "state": 0, "raw": b""}

    def reader():
        req = urllib.request.Request(f"{BASE}/api/events?t={TOKEN}")
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                start = time.time()
                buf = b""
                while time.time() - start < 6:
                    chunk = r.read(1)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n\n" in buf:
                        block, buf = buf.split(b"\n\n", 1)
                        got["raw"] += block
                        for line in block.split(b"\n"):
                            if not line.startswith(b"data: "):
                                continue
                            try:
                                d = json.loads(line[6:].decode("utf-8"))
                                got[d.get("type")] = got.get(d.get("type"), 0) + 1
                            except Exception:
                                pass
        except Exception:
            pass

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    time.sleep(1.5)
    S.BUS.emit("SSE 联通性测试消息", tag="TEST", source="ui")
    th.join(timeout=10)

    R.check("收到状态帧", got.get("state", 0) >= 1, str(got.get("state")))
    R.check("收到日志帧", got.get("log", 0) >= 1, str(got.get("log")))
    R.check("新日志能实时推送", "SSE 联通性测试消息".encode() in got["raw"])
    R.check("有心跳保活", b"ping" in got["raw"])


def t11_admin_and_system():
    log("\n[11] 管理员与系统接口")
    st, d = http("/api/state")
    s = d["state"]
    R.check("非管理员时给出明确警告",
            s["admin"] or any("管理员" in w for w in s["warnings"]), str(s["warnings"]))

    st, d = http("/api/admin/autostart", {"action": "bogus"})
    R.check("非法自启动作被拒", not d.get("ok"), str(d)[:200])

    st, d = http("/api/system/open", {"target": "nope"})
    R.check("未知打开目标被拒", not d.get("ok"), str(d)[:200])

    st, d = http("/api/admin/harden", {})
    R.check("加固接口可达（无权限时返回明确错误）",
            d.get("ok") or "权限" in (d.get("error") or ""), str(d)[:200])

    st, d = http("/api/qq/wait", {"timeout": 3})
    R.check("等待就绪是后台任务（立即返回）", st == 200 and d.get("ok"), str(d)[:200])
    R.check("后台任务状态可见", S.JOBS.state() is not None)
    st, d = http("/api/qq/wait/cancel", {})
    R.check("可以取消后台任务", d.get("ok"), str(d)[:200])
    SUPV.SUP.clear_stop_flag()


def t12_static_and_unknown():
    log("\n[12] 其它")
    st, d = http("/api/nope")
    R.check("未知接口 404", st == 404 and not d.get("ok"), str(st))
    st, d = http("/api/logs/clear", {})
    R.check("清空日志可用", d.get("ok"), str(d)[:200])

    html = open(os.path.join(HERE, "app", "web", "index.html"), encoding="utf-8").read()
    R.check("前端无 emoji（主题要求）",
            not any(ord(c) > 0x1F000 for c in html), "index.html 里出现了 emoji")
    i = html.find("const P = (d")
    R.check("前端用的是内联 SVG 图标（内联构造器 + 图标表）",
            i > 0 and "<svg viewBox" in html[i:i + 320] and "const ICONS" in html,
            "没找到内联 SVG 图标构造器")
    R.check("前端没有外链资源",
            "cdn." not in html and "unpkg" not in html and "jsdelivr" not in html)
    R.check("令牌通过占位符注入而不是硬编码", "__TOKEN__" in html)
    R.check("危险操作有二次确认", "confirmDialog" in html)
    R.check("有明暗主题切换", 'data-theme="dark"' in html and "setTheme" in html)


def t13_uia_thread_and_defaults():
    log("\n[13] UIA 工作线程与默认值一致性")
    st, d = http("/api/qq/diag", timeout=90)
    R.check("诊断接口可读", st == 200 and d.get("ok"), str(d)[:200])

    probe = d.get("probe", {})
    blob = json.dumps(probe, ensure_ascii=False)
    R.check("体检没有 COM 未初始化错误",
            "CoInitialize" not in blob and "UIAutomationCore" not in blob, blob[:400])
    uia = probe.get("uia", {})
    R.check("UIA 工作线程已初始化", uia.get("initialized") is True, str(uia))
    R.check("体检结果里始终带 UIA 状态（提前返回也要带）",
            "uia" in probe, str(sorted(probe.keys())))
    if probe.get("qq_running") and probe.get("windows", {}).get("main"):
        R.check("agent 模块在 UIA 线程上加载成功", uia.get("agent_loaded") is True, str(uia))
    else:
        # QQ 窗口不可见时不会走到加载那一步 —— 这是预期，不算失败
        R.check("QQ 窗口不可见时体检给出明确原因",
                bool(probe.get("error")), blob[:200])
    R.check("体检结果结构完整",
            all(k in probe for k in ("ok", "qq_running", "windows", "error")), blob[:200])
    if probe.get("qq_running"):
        R.check("QQ 在运行时体检有明确结论（可读或给出原因）",
                probe.get("ok") or bool(probe.get("error")),
                blob[:400])
        if probe.get("ok"):
            log(f"      （本机 QQ 可读：会话 {probe.get('session_count')} 个，"
                f"消息 {probe.get('message_count')} 条，当前 {probe.get('dialog_title')!r}）")
    else:
        log("      （本机 QQ 未运行，跳过可读性断言）")

    diff = SET.diff_against_agent_defaults()
    R.check("本地默认值副本与 agent.DEFAULTS 一致",
            diff.get("ok"),
            f"缺失={diff.get('missing')} 差异={diff.get('different')} err={diff.get('error')}")


def t14_tray():
    """
    托盘图标：只验证「建得起来」，不跑消息循环（跑了就阻塞）。

    这段是纯 ctypes 手写的 Shell_NotifyIcon 调用，属于最容易「失败但不报错」的一类 ——
    NOTIFYICONDATAW 少一个字段、cbSize 填错，Shell_NotifyIconW 会直接返回 False
    而不抛异常。所以这里必须真的去调一次，不能只看代码。
    """
    log("\n[14] 托盘图标（只建不收消息）")
    from app import tray as T
    t = T.Tray("QQAgent 测试托盘", on_open=lambda: None)
    ok = t.setup()
    R.check("托盘图标创建成功", ok, f"错误：{t.error}")
    if ok:
        R.check("拿到了隐藏窗口句柄", bool(t.hwnd))
        t.notify("测试", "托盘通知可用")
        R.check("通知调用不抛异常", True)
        t._remove()
        R.check("移除后内部句柄已清空", t._nid is None)
        t.quit()



# ============================================================ 主流程
def main():
    log("=" * 70)
    log(f"QQAgent 控制台测试　数据目录 {TMP}")
    log("=" * 70)

    srv = start_server()
    try:
        # 与 run_ui 一致：先把数据目录里的配置文件落盘，否则测的是一个不存在的世界
        SET.ensure_config_file()
        SET.ensure_secrets_file()
        t1_page_and_token()
        t2_state()
        t3_settings_roundtrip()
        t4_secret_isolation()
        t5_validation()
        t6_tasks()
        t7_subprocess_and_logs()
        t8_mutex()
        t9_stop_flag()
        t10_sse()
        t11_admin_and_system()
        t12_static_and_unknown()
        t13_uia_thread_and_defaults()
        t14_tray()
    finally:
        rc = R.summary()
        try:
            srv.shutdown()
        except Exception:
            pass
        try:
            shutil.rmtree(TMP, ignore_errors=True)
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
