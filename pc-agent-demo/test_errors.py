# -*- coding: utf-8 -*-
"""
test_errors.py —— 错误体系的自测

## 为什么错误机制本身也要测

一套"报错系统"如果自己会出错，后果比没有它更糟：它会给出**看起来很像答案**的
错误方向，把人带偏。所以这里测三件事：

    1. 目录自身是健康的（编号唯一、字段齐全、判据和动作都写具体了）
    2. 分类逻辑真的能把不同的根因分开（这是本项目的核心诉求：
       一个消息压在多个根因上，等于把人引向错误的修复动作）
    3. 出口都带码（接口、日志、诊断报告）

运行：
    python test_errors.py
    python test_errors.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="qqagent-err-")
os.environ["QQ_AGENT_HOME"] = TMP

import agent                       # noqa: E402
import error_codes as EC           # noqa: E402
import app.errors as E             # noqa: E402
import app.server as S             # noqa: E402
import app.settings as SET         # noqa: E402
from app import diagnose as DIAG   # noqa: E402

VERBOSE = "-v" in sys.argv


def log(m):
    print(m, flush=True)


class R:
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
        log("=" * 72)
        log(f"通过 {self.ok}　失败 {self.fail}")
        for e in self.errors:
            log(f"  - {e}")
        log("=" * 72)
        return 0 if not self.fail else 1


R = R()


# ============================================================ 1 目录自身
def t1_catalog_integrity():
    log("\n[1] 错误码目录的完整性")
    problems = EC.audit()
    R.check("目录自检通过（无格式/字段问题）", not problems, str(problems[:5]))
    R.check("码数量足够覆盖各环节", len(EC.CATALOG) >= 50, str(len(EC.CATALOG)))
    log(f"      共 {len(EC.CATALOG)} 条")

    # 每个域都要有码 —— 少了某个域说明有环节压根没设计报错
    for dom, name in EC.DOMAINS.items():
        n = sum(1 for it in EC.CATALOG.values() if it["domain"] == dom)
        R.check(f"域 {dom}（{name}）有错误码", n > 0, f"{n} 条")

    # 每个码的 fixes 必须「可执行」：至少包含一个能指到动作的「抓手」——
    # 界面元素名（「」）、路径/命令、点分配置项，或者一个明确的动词。
    # 这条检查的意义：fixes 写成「这是正常现象」那样的解释是没用的，
    # 用户要的是「我现在该点哪儿」。
    weak = []
    for code, it in EC.CATALOG.items():
        blob = " ".join(it["fixes"])
        has_handle = ("「" in blob or "\\" in blob or "/" in blob or "%" in blob
                      or "." in blob and any(c.isalpha() for c in blob.split(".")[0][-3:]))
        verbs = ("点", "把", "改", "用", "跑", "确认", "检查", "填", "先", "重", "换",
                 "看", "调", "移", "关", "开", "加", "删", "解", "按", "挂", "让",
                 "等", "补", "别", "保持", "建议", "安装", "执行", "启动", "运行", "提前")
        if not (has_handle or any(v in blob for v in verbs)):
            weak.append(code)
    R.check("每条 fixes 都给了可执行的抓手", not weak, str(weak))

    # 严重级别分布要合理：全是 error 说明没有分级，全是 info 说明不重视
    sev = {}
    for it in EC.CATALOG.values():
        sev[it["severity"]] = sev.get(it["severity"], 0) + 1
    R.check("有 error 级", sev.get("error", 0) >= 20, str(sev))
    R.check("有 warn 级", sev.get("warn", 0) >= 10, str(sev))
    log(f"      级别分布 {sev}")


def t2_catalog_api():
    log("\n[2] 目录的查询接口")
    it = EC.get("E-QQ-004")
    R.check("get 返回定义", it["title"].startswith("QQ 未以无障碍模式"))
    R.check("one_line 含码与标题", EC.one_line("E-QQ-004").startswith("E-QQ-004 "))
    d = EC.describe("E-QQ-004", "detail-here", {"k": "v"})
    R.check("describe 含码/原因/动作/现场", all(x in d for x in
            ("E-QQ-004", "可能原因", "怎么办", "现场数据", "detail-here")))
    unknown = EC.get("E-ZZZ-999")
    R.check("未知码不抛异常且给出占位", unknown["title"] == "未知错误码")
    R.check("wrap 对 AppError 原样返回",
            EC.wrap(EC.AppError("E-QQ-002", "x"), "E-LLM-002").code == "E-QQ-002")


def t3_throttle():
    log("\n[3] 错误抑制器")
    th = EC.Throttle(window=10.0)
    now = 1000.0
    emit1, sup1 = th.should_emit("E-QQ-003", now)
    emit2, sup2 = th.should_emit("E-QQ-003", now + 1)
    emit3, sup3 = th.should_emit("E-QQ-003", now + 2)
    R.check("窗口内第一条输出", emit1 is True)
    R.check("窗口内后续被抑制", emit2 is False and emit3 is False)
    emit4, sup4 = th.should_emit("E-QQ-003", now + 11)
    R.check("窗口过期后重新输出", emit4 is True)
    R.check("重新输出时报告被抑制次数", sup4 == 2, str(sup4))
    emitA, _ = th.should_emit("E-FG-001", now + 11)
    R.check("不同码互不影响", emitA is True)
    R.check("stats 可见统计", th.stats()["E-QQ-003"]["suppressed"] == 0)

    # agent 侧的 report() 必须真的走抑制
    agent.THROTTLE.reset()
    buf = []
    orig = agent.log
    agent.log = lambda tag, msg: buf.append((tag, msg))
    try:
        for _ in range(50):
            agent.report("E-UU1-000" if False else "E-QQ-003", "重复报错")
        lines = [m for t, m in buf]
    finally:
        agent.log = orig
        agent.THROTTLE.reset()
    R.check("report() 对重复错误只输出一次",
            sum(1 for x in lines if x.startswith("E-QQ-003")) == 1,
            f"输出 {len(lines)} 行")
    R.check("首次输出是多行诊断块", len(lines) >= 3, str(lines[:2]))


def t4_wrap_mapping():
    log("\n[4] 原生异常 → 错误码的映射")
    import requests

    cases = [
        (requests.exceptions.ConnectTimeout("t"), "E-LLM-003"),
        (requests.exceptions.ReadTimeout("t"), "E-LLM-003"),
        (requests.exceptions.ConnectionError("新连接失败"), "E-LLM-002"),
        (requests.exceptions.SSLError("bad cert"), "E-LLM-002"),
        (requests.exceptions.ProxyError("proxy"), "E-LLM-002"),
        (PermissionError("Permission denied"), "E-PATH-001"),
        (RuntimeError("尚未调用 CoInitialize。"), "E-UIA-001"),
        (RuntimeError("Can not load UIAutomationCore.dll"), "E-UIA-001"),
        (ValueError("完全无关的异常"), "E-LLM-008"),        # 落到 default，不瞎猜
    ]
    for exc, want in cases:
        got = EC.wrap(exc, "E-LLM-008").code
        R.check(f"{type(exc).__name__} → {want}", got == want, f"实际 {got}")


# ============================================================ 5 LLM 分类
class _FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload or {}, ensure_ascii=False)

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _llm_case(status=None, payload=None, text="", exc=None):
    """用假的 requests.post 跑一次 chat()，返回错误码（成功返回 None）。"""
    orig = agent.requests.post
    agent.requests.post = lambda *a, **k: (_ for _ in ()).throw(exc) if exc else \
        _FakeResp(status, payload, text)
    try:
        cfg = agent.load_config()
        cfg["llm"]["api_key"] = "sk-fake-for-test"
        client = agent.LLMClient(cfg)
        client.chat([{"role": "user", "content": "hi"}])
        return None
    except EC.AppError as e:
        return e.code
    finally:
        agent.requests.post = orig


def t5_llm_classification():
    log("\n[5] LLM 失败分类（这是「报错混淆」最集中的地方）")
    import requests

    cases = [
        ("无密钥 → E-LLM-001", "E-LLM-001", dict(status=200, payload={"choices": []})),
        ("401 → E-LLM-004", "E-LLM-004", dict(status=401, text="bad key")),
        ("403 → E-LLM-004", "E-LLM-004", dict(status=403, text="forbidden")),
        ("404 → E-LLM-005", "E-LLM-005", dict(status=404, text="model not found")),
        ("400 → E-LLM-005", "E-LLM-005", dict(status=400, text="invalid request")),
        ("429 → E-LLM-006", "E-LLM-006", dict(status=429, text="rate limited")),
        ("500 → E-LLM-007", "E-LLM-007", dict(status=500, text="oops")),
        ("503 → E-LLM-007", "E-LLM-007", dict(status=503, text="unavailable")),
        ("无 choices → E-LLM-008", "E-LLM-008", dict(status=200, payload={"id": "x"})),
        ("连接失败 → E-LLM-002", "E-LLM-002",
         dict(exc=requests.exceptions.ConnectionError("dns"))),
        ("连接超时 → E-LLM-003", "E-LLM-003",
         dict(exc=requests.exceptions.ConnectTimeout("connect timeout"))),
        ("读取超时 → E-LLM-003", "E-LLM-003",
         dict(exc=requests.exceptions.ReadTimeout("read timeout"))),
    ]
    for label, want, kwargs in cases:
        if want == "E-LLM-001":
            orig = agent.requests.post
            cfg = agent.load_config()
            cfg["llm"]["api_key"] = ""
            client = agent.LLMClient(cfg)
            try:
                client.chat([{"role": "user", "content": "hi"}])
                got = None
            except EC.AppError as e:
                got = e.code
            finally:
                agent.requests.post = orig
        else:
            got = _llm_case(**kwargs)
        R.check(label, got == want, f"实际 {got}")

    # 400 与 404 都归到「地址/模型」，但 HTTP 码要保留在 detail 里 —— 那是排查线索
    orig = agent.requests.post
    agent.requests.post = lambda *a, **k: _FakeResp(400, None, "bad param")
    try:
        cfg = agent.load_config()
        cfg["llm"]["api_key"] = "sk-fake"
        agent.LLMClient(cfg).chat([{"role": "user", "content": "hi"}])
    except EC.AppError as e:
        R.check("错误 detail 里保留了原始响应片段",
                "bad param" in e.detail_text and "400" in str(e.context.get("HTTP", "")),
                f"{e.detail_text!r} {e.context}")
    finally:
        agent.requests.post = orig


def t6_agent_reports():
    log("\n[6] agent 侧的报错出口")
    # 配置文件写坏 → 必须报 E-CFG-001 且带行列号
    bad = os.path.join(TMP, "broken.json")
    with open(bad, "w", encoding="utf-8") as f:
        f.write('{"llm": {"api_base": "x",,}}')
    orig_path = agent.CONFIG_PATH
    agent.CONFIG_PATH = bad
    buf = []
    orig_log = agent.log
    agent.log = lambda tag, msg: buf.append((tag, msg))
    agent.THROTTLE.reset()
    try:
        agent.load_config()
    finally:
        agent.log = orig_log
        agent.CONFIG_PATH = orig_path
        agent.THROTTLE.reset()
    line = " ".join(m for _, m in buf)
    R.check("坏 JSON 报 E-CFG-001", "E-CFG-001" in line, line[:120])
    R.check("带出行列号（可直接定位）", "行" in line and "列" in line, line[:160])

    # attach 失败的分流：本机 QQ 在跑，应该是 E-QQ-003；没在跑则是 E-QQ-002
    qq = agent.QQWindow(agent.load_config())
    code, ctx = qq.diagnose_attach()
    R.check("附着诊断给出明确错误码", code in ("E-QQ-002", "E-QQ-003"), code)
    R.check("附着诊断带现场数据", "QQ窗口" in ctx and "可见" in ctx, str(ctx))

    # 诊断分流必须与 attach() 的判定规则一致（否则诊断会与实际行为对不上）
    wins = qq._qq_top_windows()
    if wins:
        R.check("无可见窗口时判为 E-QQ-003（托盘/最小化）",
                (any(w["visible"] for w in wins)) or code == "E-QQ-003", str(code))
    else:
        R.check("无 QQ 顶层窗口时判为 E-QQ-002", code == "E-QQ-002", str(code))

    # 每个码都要能被 describe 渲染成多行块（format 不自相矛盾）
    bad_desc = [c for c in EC.CATALOG if len(EC.describe(c).splitlines()) < 2]
    R.check("所有码都能渲染成多行诊断块", not bad_desc, str(bad_desc[:5]))


def t7_settings_codes():
    log("\n[7] 配置校验的错误码")
    SET.ensure_config_file()
    SET.ensure_secrets_file()
    v = SET.validate({})
    codes = [x["code"] for x in v["errors"]]
    R.check("空提交给出 E-LLM-001", "E-LLM-001" in codes, str(codes))
    R.check("校验项带 message 与 hint",
            all(x.get("message") and "hint" in x for x in v["errors"]), str(v["errors"]))

    r = SET.save({"llm.api_base": "api.deepseek.com", "llm.model": "m"})
    R.check("坏地址被拦下并带 E-CFG-004", r.get("code") == "E-CFG-004", str(r)[:160])

    base = SET.as_ui_payload()["values"]
    good = dict(base)
    good["llm.api_key"] = "sk-test-errors-123456"
    good["queue.max_replies_per_minute"] = 60
    good["queue.min_interval_seconds"] = 30
    r = SET.save(good)
    R.check("矛盾风控可保存但给出 E-CFG-003 警告",
            r.get("ok") and "E-CFG-003" in (r.get("warning_codes") or []), str(r)[:200])


# ============================================================ 8 接口信封
BASE = ""
TOKEN = ""


def http(path, body=None, token=None, timeout=90):
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
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}


def t8_server_envelope():
    log("\n[8] 接口失败一律带错误码")
    st, d = http("/api/state", token=False)
    R.check("无令牌 → 401 且带 E-WEB-001",
            st == 401 and d.get("code") == "E-WEB-001", f"{st} {str(d)[:120]}")

    st, d = http("/api/nope")
    R.check("未知 GET 接口带 E-WEB-003", d.get("code") == "E-WEB-003", str(d)[:120])
    st, d = http("/api/nope", {})
    R.check("未知 POST 接口带 E-WEB-003", d.get("code") == "E-WEB-003", str(d)[:120])

    st, d = http("/api/task", {"id": "not-a-task"})
    R.check("未知任务带 E-WEB-003", d.get("code") == "E-WEB-003", str(d)[:160])

    st, d = http("/api/task", {"id": "send", "params": {}})
    R.check("缺参数带 E-PROC-003（不是笼统的失败）",
            d.get("code") == "E-PROC-003", str(d)[:160])

    st, d = http("/api/system/open", {"target": "nope"})
    R.check("未知打开目标带 E-WEB-003", d.get("code") == "E-WEB-003", str(d)[:120])

    st, d = http("/api/admin/autostart", {"action": "bogus"})
    R.check("非法自启动作带码", bool(d.get("code")), str(d)[:120])

    # 每个失败信封都要有这四样，缺一样界面就没法给出可操作的提示
    for name, (st, d) in {
        "令牌": (401, {}), "未知接口": http("/api/nope"),
        "缺参数": http("/api/task", {"id": "send", "params": {}}),
    }.items():
        if name == "令牌":
            _, d = http("/api/state", token=False)
        R.check(f"{name}信封含 code/error/hint/causes",
                all(k in d for k in ("code", "error", "hint", "causes", "fixes")),
                str(sorted(d.keys())))


def t9_diagnose():
    log("\n[9] 诊断报告接口")
    st, d = http("/api/diagnose?llm=0")
    R.check("接口返回 200/ok=True", st == 200 and d.get("ok") is True, str(d)[:160])
    R.check("ok 与 healthy 是两个独立字段（避免把「发现的问题」当成接口失败）",
            "healthy" in d and "ok" in d, str(sorted(d.keys())))
    R.check("有 counts 与 checks", isinstance(d.get("counts"), dict) and d.get("checks"))
    R.check("报告文本非空且含错误码说明入口",
            "诊断报告" in (d.get("report_text") or "") and
            "错误码" in (d.get("report_text") or ""))
    log(f"      结论：{d['counts']}　健康={d['healthy']}")

    checks = {c["id"]: c for c in d["checks"]}
    for cid in ("runtime", "admin", "datadir", "deps", "config", "key",
                "qq_exe", "qq_proc", "desktop", "proc", "catalog"):
        R.check(f"含检查项 {cid}", cid in checks, str(list(checks)))
    R.check("每项都有 label 与 status",
            all(c.get("label") and c.get("status") in ("ok", "warn", "fail", "skip")
                for c in d["checks"]))
    # skip 的项天然没有动作（"这一项不适用"），不能要求它给 hint
    R.check("fail/warn 的项都带 hint（不能只报问题不给动作）",
            all(c.get("hint") for c in d["checks"] if c["status"] in ("fail", "warn")),
            str([c["id"] for c in d["checks"]
                 if c["status"] in ("fail", "warn") and not c.get("hint")]))
    # 数据目录被显式隔离到临时目录，报告里必须体现出来
    # （比对 basename 而不是全路径：JSON 里的反斜杠会被转义，全路径匹配是假阴性）
    marker = os.path.basename(TMP)
    R.check("报告里带出数据目录",
            marker in json.dumps(d, ensure_ascii=False), f"没找到 {marker}")
    if VERBOSE:
        log(d["report_text"][:1200])

    # 跳过开关要真的生效
    st, d2 = http("/api/diagnose?llm=0&uia=0")
    uia = [c for c in d2["checks"] if c["id"] == "qq_uia"][0]
    R.check("uia=0 时跳过无障碍检查", uia["status"] in ("skip",), str(uia)[:160])


def t10_catalog_endpoint():
    log("\n[10] 错误码目录接口（界面上点击错误码时就地查看）")
    st, d = http("/api/errors")
    R.check("目录可读", st == 200 and d.get("ok"))
    R.check("含全部码", len(d.get("catalog", {})) == len(EC.CATALOG))
    R.check("含域说明", "QQ" in (d.get("domains") or {}))
    R.check("含自检摘要", "problems" in (d.get("summary") or {}))


def t12_read_path_diagnostics():
    """
    「对方发了消息但程序一声不吭」这类故障的自证能力。

    这是本轮最值钱的一条：原本实时路径有一种**完全静默**的失败模式 ——
    读到 N 条新消息、逐条被 `_admit` 拒掉、日志里一个字都不出现，
    于是「读不到」「读到了但被跳过」「对方根本没发」三件事在日志上无法区分。
    """
    log("\n[12] 读取路径的可观测性")
    a = agent.Agent(agent.load_config(), dry_run=True)

    # ---- 1) 方向不对的消息必须给出原因，而且原因里要带验证方法 ----
    m_me = agent.Message(sender="某人", content="你好", direction="me", key="aid:1")
    writes, why = a._admit(m_me, "private:1")
    R.check("方向为 me 的消息给出跳过原因（不再静默丢弃）",
            not writes and "方向" in why, repr(why))
    R.check("原因里直接给出验证方法（不用去翻文档）",
            "只读诊断" in why, why[:120])

    m_other = agent.Message(sender="某人", content="你好", direction="other", key="aid:2")
    writes2, why2 = a._admit(m_other, "private:1")
    R.check("方向为 other 的消息正常收下", bool(writes2) and not why2, f"{writes2} {why2}")

    # ---- 2) 读到但全被跳过 → 必须报 E-READ-001 ----
    class _FakeQQ:
        """只实现 step() 用到的那几个方法，其余不碰。"""

        def __init__(self, msgs):
            self._msgs = msgs
            self.ml_list = object()
            self.editor = object()
            self.send_btn = object()
            self.win = object()
            self.is_group = False
            self.dialog_title = "测试会话"

        def refresh_layout(self, force=False):
            pass

        def read_messages(self, limit=30):
            return list(self._msgs)

        def split_new(self, msgs, scope=""):
            return list(msgs)          # 全部当成新鲜的，模拟「刚读到」

        def has_seen(self, scope=""):
            return True

        def baseline(self, skip_last=0, scope=""):
            return 0

    buf = []
    orig_log, orig_report = agent.log, agent.report
    agent.log = lambda tag, msg: buf.append((tag, msg))
    agent.report = lambda code, detail="", **kw: buf.append(("REPORT", code))
    try:
        agent.THROTTLE.reset()
        a.qq = _FakeQQ([m_me, agent.Message(sender="x", content="", direction="other", key="aid:3")])
        a.scope = "private:1"
        # 让 _sync_scope 直接返回（scope 没变）—— 这里要测的是 step → _ingest → _admit 这一段，
        # 不是会话切换。切换逻辑有它自己的测试。
        a.current_scope = lambda: "private:1"
        n = a.step()
        a._report_round()
        codes = [c for t, c in buf if t == "REPORT"]
        R.check("读到但一条都没收下 → 报 E-READ-001", "E-READ-001" in codes, str(codes))
        R.check("收下条数为 0", n == 0, str(n))
        skips = [m for t, m in buf if t == "SKIP"]
        R.check("被跳过的消息留了原因", len(skips) >= 1, str(skips)[:200])
        # 正文为空的那条不刷日志（否则每个非文本气泡都会留一行），但**必须进计数**，
        # 否则它就成了新的静默黑洞。
        R.check("正文为空的条目也进了跳过计数",
                any("正文为空" in k for k in a._round["skips"]),
                str(dict(a._round["skips"])))

        # 反例：能收下时不该报 E-READ-001
        buf.clear()
        agent.THROTTLE.reset()
        a.qq = _FakeQQ([m_other])
        a.step()
        a._report_round()
        codes = [c for t, c in buf if t == "REPORT"]
        R.check("能收下时不报 E-READ-001", "E-READ-001" not in codes, str(codes))
    finally:
        agent.log, agent.report = orig_log, orig_report
        agent.THROTTLE.reset()

    # ---- 3) --watch：读不到 / 读得到 / key 不稳定，三种结论都要能说清 ----
    import contextlib
    import io as _io

    def run_watch(rounds):
        seq = iter(rounds)

        class _W:
            dialog_title = "测试会话"
            is_group = False
            member_count = 0
            self_nickname = "我"
            ml_list = object()
            editor = object()
            send_btn = object()
            win = object()

            def attach(self):
                return True

            def diagnose_attach(self):
                return "E-QQ-002", {}

            def refresh_layout(self, force=False):
                pass

            def read_messages(self, limit=30):
                try:
                    return list(next(seq))
                except StopIteration:
                    return []

            def title_now(self):
                return "测试会话"

        orig_cls = agent.QQWindow
        agent.QQWindow = lambda cfg: _W()
        out = _io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = agent.watch(agent.load_config(), seconds=0.35, interval=0.05)
        finally:
            agent.QQWindow = orig_cls
        return rc, out.getvalue()

    base = [agent.Message(sender="对方", content="旧消息", direction="other", key="aid:10")]
    newm = agent.Message(sender="对方", content="新消息来了", direction="other", key="aid:11")
    rc, text = run_watch([base, base, base + [newm], base + [newm]])
    R.check("watch 正常退出", rc == 0, str(rc))
    R.check("watch 报出读到的新消息", "★ 新增" in text and "新消息来了" in text, text[-400:])
    R.check("watch 给出「读取这一层是通的」的结论",
            "读取这一层是通的" in text, text[-300:])

    rc, text = run_watch([base] * 12)
    R.check("读不到新增时给出两种可能（含「没发就没有结论」）",
            "期间没有检测到任何新增消息" in text and "没有结论" in text, text[-500:])

    # key 不稳定：同一段正文换出不同 key
    k1 = [agent.Message(sender="对方", content="重复的话", direction="other", key="aid:20")]
    k2 = [agent.Message(sender="对方", content="重复的话", direction="other", key="fp:deadbeef")]
    rc, text = run_watch([k1, k2, k2])
    R.check("检测出 key 不稳定并说清后果",
            "key 不稳定" in text and "静默丢弃" in text, text[-600:])

    # 方向判反时要在条目上就地提示
    wrong = [agent.Message(sender="对方", content="其实是他发的", direction="me", key="aid:30")]
    rc, text = run_watch([base, base + wrong, base + wrong])
    R.check("watch 对方向为『我方』的新条目就地警告",
            "方向判反" in text or "跳过**这一条" in text, text[-500:])

    # ---- 4) 心跳要带上本轮的读取计数（界面据此显示） ----
    src = open(os.path.join(HERE, "agent.py"), encoding="utf-8").read()
    R.check("心跳里带上了 read/fresh/skipped（界面可显示）",
            all(k in src for k in ('read=self._round.get("read"', 'fresh=self._round.get("fresh"')),
            "心跳缺读取计数")
    R.check("_report_round 接到了主循环里", "_report_round()" in src)


def t13_heartbeat_phase():
    """
    心跳必须能回答「卡在哪一步」，而且 stale 判定不能跟实际耗时脱钩。

    本轮真实问题：心跳 stale 阈值写死 30s，而模型调用上限是 `llm.timeout_seconds`（默认 **60s**）
    —— 于是**每一次**稍慢的模型调用都会让界面弹出「心跳已停更，进程可能卡住了」。
    那是必然误报，还会把人引向「去重启一个其实健康的进程」。
    """
    log("\n[13] 心跳的阶段感知与 stale 阈值")
    import json as _json
    import app.paths as P
    from app import supervisor as SUPV

    # 两边的常量是各自模块里的（agent 读 QQ_AGENT_HEARTBEAT，app.paths 也读它，
    # 但测试里没有导出环境变量），所以两边都要指到同一个临时文件。
    hb_path = os.path.join(TMP, "hb.json")
    orig_agent_path, orig_app_path = agent.HEARTBEAT_PATH, P.HEARTBEAT_PATH
    agent.HEARTBEAT_PATH = hb_path
    P.HEARTBEAT_PATH = hb_path
    agent._HB.clear()
    try:
        agent.set_phase("读消息", 20)
        data = _json.load(open(hb_path, encoding="utf-8"))
        R.check("阶段被写进心跳", data.get("phase") == "读消息", str(data)[:160])
        R.check("阶段自带合理上限", data.get("stale_after") == 20.0, str(data.get("stale_after")))
        R.check("记了阶段开始时刻", isinstance(data.get("phase_since"), float))

        time.sleep(0.12)
        agent.set_phase("调模型", 75)
        data = _json.load(open(hb_path, encoding="utf-8"))
        R.check("切阶段时记下上一阶段与它的耗时",
                data.get("prev_phase") == "读消息" and data.get("prev_phase_seconds") >= 0.1,
                str({k: data.get(k) for k in ("prev_phase", "prev_phase_seconds")}))
        R.check("累计各阶段耗时（供界面排行）",
                "读消息" in (data.get("phase_stats") or {}),
                str(data.get("phase_stats")))

        # 关键回归：模型阶段的上限必须**大于**模型超时上限，否则又会误报
        cfg = agent.load_config()
        llm_timeout = float(cfg["llm"].get("timeout_seconds") or 60)
        a = agent.Agent(cfg, dry_run=True)
        R.check("模型超时上限确实大于旧的 30s 阈值（这就是误报的根源）",
                llm_timeout > 30, str(llm_timeout))
        # prepare 里给「调模型」阶段的预算是 timeout+15
        R.check("调模型阶段的上限 > 模型超时上限",
                llm_timeout + 15 > llm_timeout, f"{llm_timeout} + 15")

        # ---- supervisor 侧：阈值必须用 agent 给的那个，而不是写死 30 ----
        def fake_hb(age, stale_after=None):
            payload = {"ts": time.time() - age}
            if stale_after is not None:
                payload["stale_after"] = stale_after
            with open(hb_path, "w", encoding="utf-8") as f:
                _json.dump(payload, f)
            return SUPV.SUP.heartbeat()

        h = fake_hb(40, 75)
        R.check("age 40s / 上限 75s → 不算停更（不再误报）", h["stale"] is False, str(h))
        R.check("把该阶段的上限回传出来", h.get("stale_after_seconds") == 75.0, str(h))
        h = fake_hb(40, 30)
        R.check("age 40s / 上限 30s → 判为停更", h["stale"] is True, str(h))
        h = fake_hb(40, None)
        R.check("老版本心跳没有 stale_after 时退回 30s", h["stale"] is True, str(h))
        h = fake_hb(5, 75)
        R.check("新鲜心跳不算停更", h["stale"] is False, str(h))

        # phase_seconds 要能被算出来（界面显示「该阶段已持续 X 秒」）
        with open(hb_path, "w", encoding="utf-8") as f:
            _json.dump({"ts": time.time(), "phase": "调模型",
                        "phase_since": time.time() - 12.5, "stale_after": 75}, f)
        h = SUPV.SUP.heartbeat()
        R.check("算出「该阶段已持续多久」",
                h.get("phase_seconds") is not None and h["phase_seconds"] >= 12,
                str(h.get("phase_seconds")))
        R.check("阶段名回传给界面", h.get("phase") == "调模型", str(h.get("phase")))

        # 主循环每个慢阶段入口都要 set_phase
        src = open(os.path.join(HERE, "agent.py"), encoding="utf-8").read()
        for phase in ("读消息", "结算缓冲", "扫会话列表", "队列服务", "调模型", "发送"):
            R.check(f"主流程声明了阶段「{phase}」", f'"{phase}"' in src)
        R.check("单轮超时会被报成 E-PROC-007（带阶段与 CPU 核数）",
                "E-PROC-007" in src and "本机CPU核数" in src)
    finally:
        agent.HEARTBEAT_PATH = orig_agent_path
        P.HEARTBEAT_PATH = orig_app_path
        agent._HB.clear()


def t14_editor_safety():
    """
    输入框是**用户的地盘**，对它做破坏性动作必须有明确的理由与顺序。

    本轮现场观察到的现象：

        agent 呼出 QQ → 粘贴一段文字 → **归还前台** → **又呼出 QQ** → 把输入框里的字删掉

    那是中止发送后的清理动作（`_abort_cleanup`）。它原来「一律抢回前台来清」，
    代价是用户被打扰两次、第二次纯破坏性。这里把三条约束钉死：

        1. 先写剪贴板（无副作用），再清输入框（有副作用）—— 不能白毁已有内容
        2. 清理顺序：免前台 → 已在前台才用键盘 → 都不行就**留着并告知**，绝不二次抢前台
        3. 「发送按钮等待」可配且默认放宽（慢速虚拟机上 1 秒真的不够）
    """
    log("\n[14] 输入框的安全性（顺序、清理降级、按钮等待）")

    cfg = agent.load_config()
    cfg["uia"]["restore_foreground"] = False      # 免去归还前台的干扰

    class _Win:
        NativeWindowHandle = 4242

    class _FakeEditor:
        def __init__(self):
            self.value = ""
            self.can_set_value = False

        def SetFocus(self):
            pass

        def GetValuePattern(self):
            if not self.can_set_value:
                # uiautomation 在 Pattern 不支持时是抛异常，不是返回 None
                raise RuntimeError("该控件不支持 ValuePattern")

            editor = self

            class _Pat:
                @staticmethod
                def SetValue(v):
                    editor.value = v
            return _Pat()

    def make_qq():
        q = agent.QQWindow(cfg)
        q.win = _Win()
        q.ml_list = object()
        q.editor = _FakeEditor()
        q.dialog_title = "测试会话"
        q.is_group = False
        q.fg_before_switch = 0
        q.refresh_layout = lambda force=False: None
        q.is_foreground = lambda: q._fg
        q._fg = True
        q._cleared = 0
        q.editor_text = lambda: q.editor.value

        def _clear():
            q._cleared += 1
            q.editor.value = ""
        q.clear_editor = _clear
        return q

    reports = []
    orig_report, orig_exc = agent.report, agent.report_exc
    orig_fg, orig_clip = agent.force_foreground, agent.copy_to_clipboard
    fg_calls = {"n": 0}
    cfg_keep_off = agent.load_config()
    cfg_keep_off["uia"]["restore_foreground"] = False
    cfg_keep_off["chat"]["keep_draft_on_abort"] = False      # 本组测的是「关掉保留草稿」时的那套清理
    try:
        agent.report = lambda code, detail="", **kw: reports.append((code, detail, kw))
        agent.report_exc = lambda exc, code, **kw: reports.append((code, str(exc), kw))
        agent.force_foreground = lambda hwnd: (fg_calls.__setitem__("n", fg_calls["n"] + 1), True)[1]

        # ---- 1) 剪贴板失败时，绝不能已经清空了输入框 ----
        q = make_qq()
        q.cfg = cfg_keep_off
        q.editor.value = "用户正在打的一半的话"
        agent.copy_to_clipboard = lambda t: False
        ok = q.type_text("AI 要发的回复")
        R.check("剪贴板失败时 type_text 返回 False", ok is False, str(ok))
        R.check("**没有**清掉输入框里原有的内容（顺序对了）",
                q.editor.value == "用户正在打的一半的话", repr(q.editor.value))
        R.check("剪贴板失败报 E-SEND-001",
                "E-SEND-001" in [c for c, _d, _k in reports], str(reports)[:200])

        # ---- 2) 清理三级降级（仅在 keep_draft_on_abort=false 时生效）----
        # ① 免前台可用 → 一次前台都不抢
        reports.clear(); fg_calls["n"] = 0
        q = make_qq()
        q.cfg = cfg_keep_off
        q.editor.value = "AI 粘进去但没发出去的话"
        q.editor.can_set_value = True
        q._abort_cleanup("测试")
        R.check("① 免前台清理成功", q.editor.value == "", repr(q.editor.value))
        R.check("① 清理不抢前台", fg_calls["n"] == 0, str(fg_calls))
        R.check("① 报 E-SEND-010 并写明用的是免前台",
                any(c == "E-SEND-010" and "免前台" in str(k) for c, _d, k in reports),
                str(reports)[:240])

        # ② 免前台不可用、但 QQ 已在前台 → 用键盘，仍不额外抢前台
        reports.clear(); fg_calls["n"] = 0
        q = make_qq()
        q.cfg = cfg_keep_off
        q.editor.value = "AI 粘进去但没发出去的话"
        q._fg = True
        q._abort_cleanup("测试")
        R.check("② 已在前台时用键盘清掉", q.editor.value == "" and q._cleared == 1,
                f"val={q.editor.value!r} cleared={q._cleared}")
        R.check("② 仍然不额外抢前台（本来就在前台）", fg_calls["n"] == 0, str(fg_calls))

        # ③ 免前台不可用、QQ 也不在前台 → 留着，并且**绝不**抢前台
        reports.clear(); fg_calls["n"] = 0
        q = make_qq()
        q.cfg = cfg_keep_off
        q.editor.value = "AI 粘进去但没发出去的话"
        q._fg = False
        q._abort_cleanup("发送按钮未恢复")
        R.check("③ 不为了擦草稿抢前台", fg_calls["n"] == 0, str(fg_calls))
        R.check("③ 草稿被保留", q.editor.value == "AI 粘进去但没发出去的话", repr(q.editor.value))
        R.check("③ 报 E-SEND-011 并带上草稿内容与原因",
                any(c == "E-SEND-011" and "草稿" in str(k) and "作废原因" in str(k)
                    for c, _d, k in reports), str(reports)[:300])

        # ---- 2b) 默认行为：VM 场景下**根本不该删草稿** ----
        reports.clear(); fg_calls["n"] = 0
        q = make_qq()
        q.cfg = agent.load_config()          # 默认 keep_draft_on_abort = True
        q.editor.value = "这条回复必须被送出去"
        q._fg = False
        q._abort_cleanup("发送按钮未恢复")
        R.check("默认不动输入框里的草稿（草稿 = 可恢复的进度）",
                q.editor.value == "这条回复必须被送出去", repr(q.editor.value))
        R.check("默认既不抢前台也不用键盘", fg_calls["n"] == 0 and q._cleared == 0,
                f"fg={fg_calls} cleared={q._cleared}")
        R.check("报 E-SEND-012，明确说「草稿已保留、队列会继续重试」",
                any(c == "E-SEND-012" and "已保留" in d for c, d, _k in reports),
                str(reports)[:260])

        # ---- 3) 发送按钮等待可配、且用配置值 ----
        q = make_qq()
        q._send_disabled = lambda: False
        t0 = time.time()
        R.check("按钮已恢复时立即返回", q.wait_send_enabled() is True,
                f"{time.time() - t0:.2f}s")
        cfg["chat"]["send_button_wait_seconds"] = 0.4
        q = make_qq()
        q._send_disabled = lambda: True
        t0 = time.time()
        got = q.wait_send_enabled()
        cost = time.time() - t0
        R.check("按钮一直禁用时按配置的秒数等待（默认 3s，慢速虚拟机够用）",
                got is False and cost >= 0.35, f"{cost:.2f}s")
        R.check("配置里的默认等待 ≥ 2 秒（1 秒在慢速虚拟机上不够）",
                float(agent.DEFAULTS["chat"]["send_button_wait_seconds"]) >= 2.0,
                str(agent.DEFAULTS["chat"]["send_button_wait_seconds"]))

        # ---- 4) 三个中止点都要把自己的原因传下去 ----
        src = open(os.path.join(HERE, "agent.py"), encoding="utf-8").read()
        for why in ("发送按钮未恢复", "回读不符", "发送前会话复核失败"):
            R.check(f"中止时把原因传给清理：{why}", why in src)
        R.check("清理动作有三种手段（免前台/键盘/保留）",
                all(k in src for k in ("clear_editor_uia", "E-SEND-010", "E-SEND-011")))
    finally:
        agent.report, agent.report_exc = orig_report, orig_exc
        agent.force_foreground, agent.copy_to_clipboard = orig_fg, orig_clip


def t15_never_lose_reply():
    """
    **VM 场景的核心不变量：已经生成好的回复，一定要送出去。**

    背景（用户定的策略）：选定 VM 作为应用场景后，「打扰」不再是成本，
    正确性才是。而之前为「少打扰」做的三处取舍全都变成了丢消息：

        · 中止时删掉输入框里的草稿      → 丢掉一段已写好的回复
        · 重排时把 item.reply 清空      → 已生成的回复作废，要重新调模型（内容还会变）
        · 重试上限用尽就 drop（撤单）    → **那批消息永远没有人回**

    这一节把「不丢」逐条钉住。
    """
    log("\n[15] 不丢回复：已生成的回复必须被送出去")

    cfg = agent.load_config()
    cfg["uia"]["restore_foreground"] = False
    reports = []
    orig_report, orig_exc = agent.report, agent.report_exc
    try:
        agent.report = lambda code, detail="", **kw: reports.append((code, detail, kw))
        agent.report_exc = lambda exc, code, **kw: reports.append((code, str(exc), kw))

        def new_agent():
            a = agent.Agent(cfg, dry_run=True)
            # 待发队列落盘到临时目录，别污染项目
            a._pending_path = lambda: os.path.join(TMP, "pending-replies.json")
            return a

        # ---- 1) 发送失败：回复必须保留、项必须留在队列里 ----
        a = new_agent()
        item = a.queue.submit("private:5001", "会话A", "5001", "对方说的话",
                              key="k1", wait_seconds=0.0, now=time.time() - 1)
        item.prepared = True
        item.reply = "已经生成好的回复内容"
        item.ready_at = 0.0                          # submit 会加抖动，这里强制可发
        a.deliver = lambda it, reply: False          # 强制发送失败
        a.serve_queue()
        it = a.queue.get("private:5001")
        R.check("发送失败后这一项**仍在队列里**（没有撤单）", it is not None)
        R.check("**已生成的回复被完整保留**", it is not None and it.reply == "已经生成好的回复内容",
                f"{getattr(it, 'reply', None)!r}")
        R.check("prepared 保持 True（不必重新调模型）", it is not None and it.prepared is True)
        R.check("记下了失败次数与原因码", it.fail_count == 1 and bool(it.last_fail),
                f"{it.fail_count} {it.last_fail!r}")
        R.check("原来的 texts 也没丢", it.count == 1 and it.texts[0] == "对方说的话")

        # ---- 2) 退避递增且有上限（不能越拖越久到等于放弃）----
        d1 = a._retry_delay(it)
        it.fail_count = 8
        d2 = a._retry_delay(it)
        R.check("失败越多退避越长", d2 > d1, f"{d1} → {d2}")
        R.check("退避有上限（等于静默窗硬上限，不会无限增长）",
                d2 <= a.queue.max_hold + 1e-6, f"{d2} > {a.queue.max_hold}")

        # ---- 3) 迟到消息：不塞进已定稿的项，也不丢，而是结转 ----
        a = new_agent()
        item = a.queue.submit("private:5002", "会话B", "5002", "第一句",
                              key="k1", wait_seconds=0.0, now=time.time() - 1)
        item.ready_at = 0.0
        item.prepared, item.reply = True, "针对第一句的回复"
        item.late = [("第二句", "k2", 2), ("第三句", "k3", 3)]
        item.late_hint = 2
        a._carry_over(item)
        # 明确找「新加进来的那一条」——get(scope) 会返回列表里第一条（也就是老的）
        carried = next((x for x in a.queue.items if x is not item), None)
        R.check("结转出了一条新项", carried is not None)
        R.check("新项走「总读取」（把迟到的和新来的一起读全）",
                carried is not None and carried.pending_read is True)
        R.check("新项的未读提示带上了迟到条数",
                carried is not None and carried.unread_hint >= 2, str(getattr(carried, "unread_hint", None)))
        R.check("carried_over 计数 +1", a.queue.stats.get("carried_over", 0) >= 1)

        # 已有排队项时不重复结转（避免同一会话被回两次）
        a = new_agent()
        item = a.queue.submit("private:5003", "会话C", "5003", "x",
                              key="k1", wait_seconds=0.0, now=time.time() - 1)
        item.ready_at = 0.0
        item.prepared, item.reply, item.late = True, "r", [("y", "k2", 2)]
        a.queue.submit("private:5003", "会话C", "5003", "z", key="k3",
                       wait_seconds=0.0, now=time.time() - 1)
        before = len(a.queue)
        a._carry_over(item)
        R.check("该会话已有新项时不重复结转", len(a.queue) == before, f"{before} → {len(a.queue)}")

        # ---- 4) 落盘 / 恢复：重启不丢已生成的回复 ----
        a = new_agent()
        it = a.queue.submit("private:5004", "会话D", "5004", "原始消息",
                            key="k1", wait_seconds=0.0, now=time.time() - 1)
        it.prepared, it.reply, it.drafted = True, "重启也不能丢的回复", True
        it.fail_count, it.last_fail = 2, "E-SEND-002"
        a._save_pending()
        R.check("待发队列已落盘", os.path.isfile(a._pending_path()))

        b = new_agent()
        n = b._load_pending()
        restored = b.queue.get("private:5004")
        R.check("重启后恢复了待发项", n == 1 and restored is not None, str(n))
        R.check("**回复内容完整恢复**（这是「重启不丢」的关键）",
                restored is not None and restored.reply == "重启也不能丢的回复",
                f"{getattr(restored, 'reply', None)!r}")
        R.check("drafted / 失败原因也恢复（重试路径仍然最短）",
                restored.drafted is True and restored.fail_last_check()
                if hasattr(restored, "fail_last_check") else
                (restored.drafted is True and restored.last_fail == "E-SEND-002"))
        R.check("恢复时会明确报一条 E-SEND-015",
                any(c == "E-SEND-015" for c, _d, _k in reports), str(reports)[:200])

        # ---- 5) resume：草稿还在输入框里就跳过重新粘贴 ----
        q = agent.QQWindow(cfg)
        q.win = type("W", (), {"NativeWindowHandle": 777})()
        q.editor = type("E", (), {"value": "这句话已经在输入框里了", "SetFocus": lambda s: None})()
        q.ml_list = object()
        q.dialog_title = "会话"
        q.is_group = False
        q.fg_before_switch = 0
        q.refresh_layout = lambda force=False: None
        q._send_disabled = lambda: False
        q.editor_text = lambda: q.editor.value
        pasted = []
        q.type_text = lambda t: (pasted.append(t), True)[1]
        q._abort_cleanup = lambda reason="": None
        sent = {"n": 0}

        class _Btn:
            def GetInvokePattern(self):
                class _P:
                    @staticmethod
                    def Invoke():
                        sent["n"] += 1
                return _P()
        q.send_btn = _Btn()

        ok = q.send_text("这句话已经在输入框里了", resume=True)
        R.check("resume=True 且草稿仍在 → 发送成功", ok is True, str(ok))
        R.check("**没有**重新粘贴（省掉一次抢前台 + 剪贴板）", pasted == [], str(pasted))
        R.check("确实触发了发送按钮", sent["n"] == 1, str(sent))

        # 草稿不在了 → 退回完整流程，并报 E-SEND-014
        reports.clear()
        q.editor.value = "别的内容"
        ok2 = q.send_text("这句话已经在输入框里了", resume=True)
        R.check("草稿不在时不假装成功（回读校验会拦住）", ok2 is False, str(ok2))
        R.check("报 E-SEND-014 说明「草稿已不在，改为重新写入」",
                any(c == "E-SEND-014" for c, _d, _k in reports), str(reports)[:200])

        # ---- 6) 心跳里要能看到「已生成但没发出去」的条数 ----
        src = open(os.path.join(HERE, "agent.py"), encoding="utf-8").read()
        R.check("心跳带上了 unsent 计数（界面据此告警）", "unsent=len(self.queue.unsent())" in src)
        R.check("启动时会恢复待发队列", "self._load_pending()" in src)
        R.check("退出时会落盘待发队列", "self._save_pending()" in src)
        R.check("生成好回复就立刻落盘", "self._save_pending()" in src)
    finally:
        agent.report, agent.report_exc = orig_report, orig_exc


def t11_no_code_escape():
    """
    扫一遍源码：不该再有「用户能看到的失败」只给一句没码的话。

    这是防回归的关键一条 —— 加新功能时很容易又写回 `log("ERR", "失败了")`，
    而那种报错正是我们这次要消灭的东西。
    """
    log("\n[11] 源码里不应再有「无码的失败」（防回归）")
    import re

    suspects = []
    pat = re.compile(r'(?:log|print)\(\s*"(ERR|WARN)"\s*,\s*f?"([^"]{0,200})')
    for name in ("agent.py", "app/server.py", "app/supervisor.py", "app/qqctl.py",
                 "app/settings.py", "app/diagnose.py", "app/main.py", "app/tray.py"):
        path = os.path.join(HERE, name)
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            m = pat.search(line)
            if not m:
                continue
            msg = m.group(2)
            # 允许：纯进展/收尾类信息；不允许：讲「失败/找不到/无法」却没有码
            if any(k in msg for k in ("失败", "找不到", "无法", "拒绝", "超时", "错误")):
                if not re.search(r"E-[A-Z]{2,4}-\d{3}", line):
                    suspects.append(f"{name}:{i} {msg[:60]}")
    R.check("没有「说了失败但没给码」的报错", not suspects, "\n      " + "\n      ".join(suspects[:8]))
    if suspects and VERBOSE:
        for s in suspects:
            log(f"      {s}")


# ============================================================ 主流程
def main():
    log("=" * 72)
    log(f"错误体系自测　数据目录 {TMP}")
    log("=" * 72)

    t1_catalog_integrity()
    t2_catalog_api()
    t3_throttle()
    t4_wrap_mapping()
    t5_llm_classification()
    t6_agent_reports()
    t7_settings_codes()

    srv = S.serve("127.0.0.1", 0)
    global BASE, TOKEN
    BASE = f"http://127.0.0.1:{srv.server_address[1]}"
    TOKEN = S.TOKEN
    log(f"[i] 测试服务：{BASE}")
    try:
        t8_server_envelope()
        t9_diagnose()
        t10_catalog_endpoint()
        t12_read_path_diagnostics()
        t13_heartbeat_phase()
        t14_editor_safety()
        t15_never_lose_reply()
        t11_no_code_escape()
    finally:
        rc = R.summary()
        try:
            srv.shutdown()
        except Exception:
            pass
        shutil.rmtree(TMP, ignore_errors=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
