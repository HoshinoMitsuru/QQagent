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
