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
    15 前端脚本的语法与引用完整性（元素 id / 图标名 / node --check）
    16 隐藏桌面面板的接口（每个 GET 都要给 JSON，不许用「断连」当报错）
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

    # ---- reply_policy：界面拿它念出「这次会回复谁」 ----
    # 这一条是补的，因为**第一版字段名全是错的**（写成 chat.trigger_keywords /
    # aggregate.window_seconds，真实键是 chat.trigger_prefixes / always_reply /
    # group_requires_trigger），于是界面在一句**安全警告**里印了假话。
    # 当时没有任何测试盯着这个字段 —— 加字段而不加断言，就等于没加。
    rp = s.get("reply_policy")
    R.check("含 reply_policy", isinstance(rp, dict), str(rp)[:160])
    if isinstance(rp, dict):
        for k in ("private_chat_only", "always_reply", "group_requires_trigger"):
            R.check(f"reply_policy.{k} 是布尔（键名对得上）",
                    isinstance(rp.get(k), bool), repr(rp.get(k)))
        R.check("reply_policy.trigger_prefixes 是**非空**列表（键名对得上）",
                isinstance(rp.get("trigger_prefixes"), list)
                and len(rp["trigger_prefixes"]) > 0,
                repr(rp.get("trigger_prefixes")))
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
    """2026-09-28 起密钥收拢进 config.json（该文件被 .gitignore 覆盖，
    仓库里只留 config.example.json）；secrets.local.json 降级为只读后备。
    安全不变量改为两条：①接口/状态只回掩码；②config.json 必须被 gitignore。"""
    log("\n[4] 密钥收拢进 config.json 且该文件被 gitignore")
    cfg = json.load(open(P.CONFIG_PATH, encoding="utf-8"))
    R.check("config.json 里存了新 Key（主存储）",
            cfg.get("llm", {}).get("api_key") == "sk-test-roundtrip-1234567890")
    # 唯一存储：不该再往 secrets.local.json 写新 Key（那里只保留旧配置的后备值）
    sec = json.load(open(P.SECRETS_PATH, encoding="utf-8"))
    R.check("secrets.local.json 不再接收新 Key（后备不变）",
            sec.get("llm", {}).get("api_key") != "sk-test-roundtrip-1234567890")
    st, d = http("/api/state")
    R.check("state 里不出现明文 Key",
            "sk-test-roundtrip" not in json.dumps(d["state"]), "接口把密钥泄露出去了！")
    R.check("state 只报来源", d["state"]["llm"]["key_present"] and
            "config.json" in d["state"]["llm"]["key_source"])
    # 收拢后的安全闸：允许存 Key 的前提是文件绝不入库
    here = os.path.dirname(os.path.abspath(__file__))
    gi_path = os.path.join(os.path.dirname(here), ".gitignore")
    gi = open(gi_path, encoding="utf-8").read() if os.path.isfile(gi_path) else ""
    R.check("config.json 被 .gitignore 覆盖（pc-agent-demo*/config.json）",
            "pc-agent-demo*/config.json" in gi, f"{gi_path} 里没有对应条目")


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



def _top_level_ancestor_ranges(src: str) -> list[tuple[int, int]]:
    """
    找出所有「最外层函数」占据的行区间（1 起，含首尾）。

    做法：逐字符扫，维护一个「函数起始位置」的栈 —— 只在遇到 `function` 关键字
    时压栈（不追踪箭头函数，本项目这一层统一用 `function` 声明）。
    一次遇到 `function` 时，若还没进过任何函数，它就是一个最外层函数。
    配对的大括号决定它的结束行。

    只需要「够用」：用来判断某个调用点有没有落在正确的函数范围内。
    """
    import re as _re
    ranges: list[tuple[int, int]] = []
    depth = 0
    cur_top_start = None
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        # 跳过字符串与注释，避免里面的 {} 干扰配对
        if ch == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if ch == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch in "\"'`":
            quote = ch
            i += 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    break
                i += 1
            i += 1
            continue
        if src.startswith("function", i) and _re.match(r"function\s+[A-Za-z_$]", src[i:]):
            if depth == 0 and cur_top_start is None:
                cur_top_start = i
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and cur_top_start is not None:
                a = src[:cur_top_start].count("\n") + 1
                b = src[: i + 1].count("\n") + 1
                ranges.append((a, b))
                cur_top_start = None
        i += 1
    return ranges


def t15_frontend_script():
    """
    前端脚本的**语法与引用完整性**。

    ## 为什么这件事必须自动测

    单文件 index.html 里的那一段 JS，后端**完全看不见它**。写错一个引号、
    少定义一个图标、`$("#xxx")` 指到一个不存在的 id，后果是整页白屏 ——
    而所有接口测试照样全绿（后端确实是对的）。用户看到的是「界面打不开」，
    我们手里的证据却是「服务正常」，这类故障最费时间。

    2026-09-25 真的写出来过一个：一个字符串用了双引号却跨了行，
    `node --check` 一句话就指到了行号和位置。

    所以这里固定做三件事：
      1. 用 node 做一次真正的语法检查（没有 node 就跳过，不能因为缺工具而失败）
      2. 所有 `$("#id")` / `getElementById("id")` 引用的 id 必须真的存在
      3. 所有 `icon("name")` / `ICONS.name` 用到的图标必须有定义
    """
    log("\n[15] 前端脚本：语法与引用完整性")
    import re
    import shutil as _sh
    import subprocess

    html = open(os.path.join(HERE, "app", "web", "index.html"), encoding="utf-8").read()
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    R.check("页面里有且只有一段主脚本", len(blocks) == 1, f"找到 {len(blocks)} 段")
    src = blocks[-1] if blocks else ""

    ids = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', src))
    used |= set(re.findall(r'getElementById\("([^"]+)"\)', src))
    missing = sorted(u for u in used if u not in ids)
    R.check("脚本引用的元素 id 都存在", not missing, f"缺：{missing}")

    defined = set(re.findall(r"^\s*([a-zA-Z]+):\s*P\(", src, re.M))
    wanted = set(re.findall(r'icon\("([a-zA-Z]+)"', src))
    wanted |= set(re.findall(r"ICONS\.([a-zA-Z]+)", src))
    R.check("脚本用到的图标都有定义", not (wanted - defined),
            f"缺：{sorted(wanted - defined)}")

    # 找 node：PATH → 常见安装位置。找不到就不做这一步（不能因为缺一个工具
    # 把测试判失败，那会让人去修一个不存在的问题），上面两项引用检查仍然生效。
    # ---- 函数作用域：声明在某个函数**里面**的函数，别处调不到 ----
    # 这一条是 2026-09-25 补的，因为我**正好犯了这个错**：
    # 把一个 `policyText()` 声明写在了 renderRun() 内部，却在点击处理器里调用它。
    # `node --check` 只验语法，完全查不出这种问题 —— 表现是**点那个按钮时
    # ReferenceError**，页面上其它一切正常（所以很难第一时间联想到作用域）。
    #
    # 判据（保守但够用）：如果某个函数名在全文只声明过一次、且它声明在另一个函数
    # **内部**，那么对它的调用必须落在「最外层那个祖先函数」的范围内；
    # 落在别处就是调不到。重名的函数一律跳过（可能是合法的遮蔽）。
    decls = {}
    for m in re.finditer(r"(?m)^\s*function\s+([A-Za-z_$][\w$]*)\s*\(", src):
        name = m.group(1)
        decls.setdefault(name, []).append(src[:m.start()].count("\n") + 1)
    top_ancestor = _top_level_ancestor_ranges(src)
    bad_calls = []
    for name, lines in decls.items():
        if len(lines) != 1:
            continue
        decl_line = lines[0]
        ancestor = None
        for (a, b) in top_ancestor:
            if a <= decl_line <= b:
                ancestor = (a, b)
                break
        if not ancestor:
            continue                      # 声明在模块级：任何地方都能调
        for m in re.finditer(rf"(?<![\w$.]){re.escape(name)}\s*\(", src):
            call_line = src[:m.start()].count("\n") + 1
            if call_line == decl_line:
                continue
            if not (ancestor[0] <= call_line <= ancestor[1]):
                bad_calls.append((name, decl_line, call_line))
    R.check("没有「声明在函数内部、却在别处调用」的函数", not bad_calls,
            "；".join(f"{n} 声明于第 {d} 行，却在第 {c} 行调用"
                      for n, d, c in bad_calls[:4]))

    node = _sh.which("node") or ""
    if not node:
        for cand in (os.path.join(os.environ.get("ProgramFiles", ""), "nodejs", "node.exe"),
                     os.path.join(os.environ.get("ProgramFiles(x86)", ""), "nodejs", "node.exe"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs", "node.exe")):
            if cand and os.path.isfile(cand):
                node = cand
                break
    if not node:
        log("      （本机找不到 node，跳过真正的语法检查 —— 上面两项引用检查仍在把关）")
        return
    tmp = os.path.join(TMP, "ui-check.js")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(src)
    r = subprocess.run([node, "--check", tmp], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    detail = (r.stdout or "") + (r.stderr or "")
    R.check("node --check 通过（整页白屏级错误）", r.returncode == 0,
            detail.strip().splitlines()[:4])


def t16_host_panel():
    """
    隐藏桌面面板：接口必须答得出话，**而且不能靠「连不上」来报错**。

    GET 分支原先没有统一信封，某个路由里写错一个名字时，表现是连接被直接掐断
    （浏览器只看到 RemoteDisconnected），用户读到的是「后端挂了」——
    真相却只是少了一行 `app = settings.load_app_settings()`。
    所以这里逐个 GET 接口都要拿到带 ok 字段的 JSON，而不是一个异常。
    """
    log("\n[16] 隐藏桌面面板接口")

    def probe(path, body=None):
        """连不上时返回 (0, {...}) 而不是把异常抛出去 —— 断连本身就是被测的一项。"""
        try:
            return http(path, body)
        except Exception as exc:
            return 0, {"_disconnected": f"{type(exc).__name__}: {exc}"}

    for path in ("/api/host/status", "/api/state"):
        st, res = probe(path)
        R.check(f"{path} 返回 JSON 且带 ok（不是断连）",
                st == 200 and isinstance(res, dict) and "ok" in res,
                f"HTTP {st} {str(res)[:160]}")

    st, res = probe("/api/host/status")
    R.check("host/status 带 daemon 段", "daemon" in (res or {}), str(res)[:160])
    d = (res or {}).get("daemon") or {}
    R.check("daemon 段字段齐全",
            all(k in d for k in ("running", "stale", "age", "phase", "pid",
                                 "heartbeat_path", "log_path")), str(list(d))[:200])
    R.check("没跑宿主时状态是「未运行」而不是报错", d.get("running") is False,
            str(d.get("running")))
    R.check("host/status 带 last_grab 与桌面信息",
            "last_grab" in (res or {}) and "desktop_target" in (res or {}),
            str(list(res or {}))[:200])

    st, res = probe("/api/host/shot.png")
    R.check("没抓过图时 shot.png 给的是带码的错误而不是断连",
            st in (404, 500) and isinstance(res, dict) and bool(res.get("code")),
            f"HTTP {st} {str(res)[:160]}")

    st, res = probe("/api/host/stop", {})
    R.check("没跑宿主时 stop 是幂等的成功",
            st == 200 and (res or {}).get("ok") is True,
            f"HTTP {st} {str(res)[:160]}")


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
        t15_frontend_script()
        t16_host_panel()
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
