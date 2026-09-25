# -*- coding: utf-8 -*-
"""
test_brain.py —— Brain 规划层（cu/brain.py）的离线自检

## 测什么

    - 配置合并 / Key 三级解析
    - 循环机制：tool_calls 派发 → role=tool 回灌 → 最终答复
    - 确认锁的三个分支（同意 / 拒绝 / 未传回调=fail-closed）
    - open_chat 白名单闸（含 index → 名字翻译、无缓存时的拦截）
    - 步数上限（E-CU-008）、HTTP 状态码分类（E-LLM-004/005/006/007）

全部离线：poster 替身替代 HTTP，StubExecutor 替代执行面。不碰网络、不碰 QQ。

用法：
    python test_brain.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import error_codes as EC                                   # noqa: E402
from cu.base import (ChatMessage, Executor, Health,         # noqa: E402
                     SendReceipt, SessionInfo, Shot)
from cu.brain import CU_DEFAULTS, Brain, load_cu_config     # noqa: E402

OK = FAIL = 0


def case(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def ai_msg(content=None, tool_calls=None) -> dict:
    m = {"role": "assistant", "content": content if content is not None else ""}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


def tc(tid: str, name: str, args: dict | str) -> dict:
    return {"id": tid, "type": "function",
            "function": {"name": name,
                         "arguments": args if isinstance(args, str)
                         else json.dumps(args, ensure_ascii=False)}}


def find_tool_msg(messages: list[dict], tid: str) -> dict | None:
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id") == tid:
            return m
    return None


class StubExecutor(Executor):
    name = "stub"
    require_confirmation = False

    def __init__(self):
        self.calls: list[tuple] = []

    def list_sessions(self):
        self.calls.append(("list_sessions",))
        return [SessionInfo(0, "苏霖韵", 1, False),
                SessionInfo(1, "我，我们", 0, True)]

    def open_chat(self, name="", index=0):
        self.calls.append(("open_chat", name, index))
        return {"ok": True, "opened": name or f"#{index}"}

    def read_recent(self, limit=12):
        self.calls.append(("read_recent", limit))
        return [ChatMessage("苏霖韵", "在吗", "other", dir_src="class")]

    def send_text(self, text, *, armed=None):
        self.calls.append(("send_text", text, armed))
        return SendReceipt(True, route="wmchar", chat_title="苏霖韵")

    def screenshot(self, path=""):
        self.calls.append(("screenshot", path))
        return Shot(True, path or "state/auto.png")

    def health(self):
        self.calls.append(("health",))
        return Health(True, mode="stub", chat_open=True,
                      extra={"window": {"title": "苏霖韵"}})


def make_brain(replies: list[dict], allow=None, **kw) -> tuple[Brain, StubExecutor, list]:
    """poster 替身：按序吐回复并记录每次收到的 payload。"""
    ex = StubExecutor()
    payloads: list[dict] = []
    seq = list(replies)

    def poster(payload: dict) -> dict:
        payloads.append(payload)
        return {"choices": [{"message": seq.pop(0)}]}

    cu = dict(CU_DEFAULTS)
    cu["api_key"] = "sk-test-12345678"
    if allow is not None:
        cu["open_chat_allow"] = allow
    cu.update(kw)
    return Brain(cu, {}, poster=poster), ex, payloads


# ============================================================ 配置
print("§1 配置合并与 Key 解析")
c = load_cu_config({})
case("缺 cu 节 = 全默认",
     c["base_url"] == "https://api.deepseek.com" and c["model"] == "deepseek-flash"
     and c["max_steps"] == 8, str(c))
c = load_cu_config({"cu": {"model": "my-model", "max_steps": 3}})
case("文件配置覆盖默认",
     c["model"] == "my-model" and c["max_steps"] == 3
     and c["base_url"] == "https://api.deepseek.com", str(c))
tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
tmp.write("sk-file-key-98765432")
tmp.close()
from cu.brain import _resolve_key                        # noqa: E402
key, src = _resolve_key({"api_key_file": tmp.name}, {})
case("Key 二级：api_key_file 生效", key == "sk-file-key-98765432" and "api_key_file" in src,
     f"{key} {src}")
key, src = _resolve_key({"api_key": "sk-direct-111"}, {})
case("Key 一级：cu.api_key 最优先", key == "sk-direct-111" and src == "cu.api_key", f"{src}")
os.unlink(tmp.name)

# ============================================================ 循环
print("§2 循环机制：tool 派发与回灌")
b, ex, payloads = make_brain([
    ai_msg(tool_calls=[tc("t1", "list_sessions", {})]),
    ai_msg(content="列表里有 2 个会话。"),
])
r = b.run(ex, "看看现在有哪些会话")
case("两步循环给出最终答复",
     r["ok"] and r["answer"] == "列表里有 2 个会话。" and r["usage_steps"] == 2, str(r)[:200])
tool_msg = find_tool_msg(payloads[1]["messages"], "t1")
case("结果以 role=tool 回灌且带 tool_call_id",
     tool_msg is not None and json.loads(tool_msg["content"])["count"] == 2,
     str(tool_msg)[:200])
case("list_sessions 结果被缓存（供白名单翻译）",
     len(b._last_sessions) == 2 and b._last_sessions[0]["name"] == "苏霖韵", "")

b, ex, payloads = make_brain([
    ai_msg(tool_calls=[tc("a", "list_sessions", {}),
                       tc("b", "health", {})]),
    ai_msg(content="体检完毕。"),
])
r = b.run(ex, "体检")
case("并行 tool_calls 全部派发",
     r["ok"] and ("list_sessions",) in ex.calls and ("health",) in ex.calls, str(ex.calls))

b, ex, payloads = make_brain([ai_msg(content="直接答复，不需要工具。")])
r = b.run(ex, "你好")
case("无工具直接答复", r["ok"] and r["answer"] == "直接答复，不需要工具。", str(r)[:120])

b, ex, payloads = make_brain([ai_msg(content="")])
r = b.run(ex, "你好")
case("空内容答复报 E-LLM-009",
     (not r["ok"]) and r["error"]["code"] == "E-LLM-009", str(r)[:200])

b, ex, payloads = make_brain([
    ai_msg(tool_calls=[tc("x", "delete_everything", {})]),
    ai_msg(content="抱歉，没有这个工具。"),
])
r = b.run(ex, "删库")
tool_msg = find_tool_msg(payloads[1]["messages"], "x")
case("幻觉工具名以 E-CU-006 回灌，循环继续",
     r["ok"] and json.loads(tool_msg["content"])["error"]["code"] == "E-CU-006", str(r)[:200])

b, ex, payloads = make_brain([ai_msg(tool_calls=[tc(f"t{i}", "list_sessions", {})])
                              for i in range(10)])
r = b.run(ex, "打转任务", )
case("步数超限报 E-CU-008",
     (not r["ok"]) and r["error"]["code"] == "E-CU-008"
     and sum(1 for s in r["steps"] if s["type"] == "tool_call") == 8, str(r)[:200])

b, ex, payloads = make_brain([])
b._poster = lambda payload: (_ for _ in ()).throw(EC.AppError("E-LLM-003", "超时"))
r = b.run(ex, "任务")
case("模型接口异常转错误信封（循环不抛出）",
     (not r["ok"]) and r["error"]["code"] == "E-LLM-003" and r["steps"] == [], str(r)[:200])

# ============================================================ 确认锁
print("§3 确认锁：人点头才 armed=True")
b, ex, payloads = make_brain([
    ai_msg(tool_calls=[tc("s1", "send_text", {"text": "你好呀"})]),
    ai_msg(content="已发送。"),
])
ex.require_confirmation = True
asked: list[dict] = []
r = b.run(ex, "给苏霖韵发「你好呀」",
          confirm=lambda info: (asked.append(info), True)[1])
case("确认回调收到了拟发文本",
     asked and asked[0]["text"] == "你好呀" and asked[0]["chat_title"] == "苏霖韵",
     str(asked))
case("人同意后以 armed=True 真发",
     r["ok"] and ("send_text", "你好呀", True) in ex.calls, str(ex.calls))
tool_msg = find_tool_msg(payloads[1]["messages"], "s1")
case("回灌给模型的是发送成功回执",
     json.loads(tool_msg["content"])["ok"] is True, str(tool_msg)[:150])

b2, ex2, payloads2 = make_brain([
    ai_msg(tool_calls=[tc("s2", "send_text", {"text": "再发一条"})]),
    ai_msg(content="明白，不发了。"),
])
ex2.require_confirmation = True          # 必须在 make_brain 之后设（它返回新桩）
r = b2.run(ex2, "任务", confirm=lambda info: False)
tool_msg = find_tool_msg(payloads2[1]["messages"], "s2")
case("人拒绝 → 回灌「用户拒绝，不要重试」",
     json.loads(tool_msg["content"])["error"]["code"] == "E-CU-004"
     and "拒绝" in json.loads(tool_msg["content"])["error"]["detail"], str(tool_msg)[:150])
case("拒绝后原语从未被真发",
     not any(c[0] == "send_text" and c[2] is True for c in ex2.calls), str(ex2.calls))

b3, ex3, payloads3 = make_brain([
    ai_msg(tool_calls=[tc("s3", "send_text", {"text": "没人确认"})]),
    ai_msg(content="好的。"),
])
ex3.require_confirmation = True          # 必须在 make_brain 之后设（它返回新桩）
r = b3.run(ex3, "任务")     # 不传 confirm = fail-closed
case("未传 confirm 回调 = 一律拒绝（fail-closed）",
     not any(c[0] == "send_text" and c[2] is True for c in ex3.calls)
     and r["ok"], str(ex3.calls))

# ---- 确认提示标题：两面 health extra 形态都要认（2026-09-25 真机：
# attach 面弹层一直显示「（未知）」—— extra 是中文平铺键，代码只认英文）----
b4, ex4, _ = make_brain([])
ex4.health = lambda: Health(True, mode="stub", chat_open=True,
                            extra={"窗口": "我，我们", "消息列表": True})
case("确认提示标题：attach 面（中文平铺键）取到标题",
     b4._current_chat_hint(ex4) == "我，我们", "")
case("确认提示标题：hosted 面（英文 window.title）取到标题",
     b4._current_chat_hint(StubExecutor()) == "苏霖韵", "")


class EmptyHealthEx(StubExecutor):
    def health(self):
        return Health(True, mode="stub", chat_open=True, extra={})


case("确认提示标题：extra 什么都没有 → 空串（上层显示「未知」）",
     b4._current_chat_hint(EmptyHealthEx()) == "", "")

# ============================================================ 白名单
print("§4 open_chat 白名单闸（实测授权：仅「我，我们」「苏霖韵」）")
ALLOW = ["我，我们", "苏霖韵"]
b, ex, payloads = make_brain([
    ai_msg(tool_calls=[tc("o1", "open_chat", {"name": "嗅尘紫蝶"})]),
    ai_msg(content="被拦了。"),
], allow=ALLOW)
r = b.run(ex, "找嗅尘紫蝶")
tool_msg = find_tool_msg(payloads[1]["messages"], "o1")
case("名单外目标被拦（E-CU-004）且原语没被调",
     json.loads(tool_msg["content"])["error"]["code"] == "E-CU-004"
     and not any(c[0] == "open_chat" for c in ex.calls), str(ex.calls))

b, ex, payloads = make_brain([
    ai_msg(tool_calls=[tc("o2", "open_chat", {"name": "霖韵"})]),
    ai_msg(content="已切过去。"),
], allow=ALLOW)
r = b.run(ex, "找苏霖韵")
case("名单内目标（子串）放行",
     r["ok"] and ("open_chat", "霖韵", -1) in ex.calls, str(ex.calls))

b, ex, payloads = make_brain([
    ai_msg(tool_calls=[tc("l1", "list_sessions", {})]),
    ai_msg(tool_calls=[tc("o3", "open_chat", {"index": 1})]),   # index 1 = 我，我们
    ai_msg(content="切到群了。"),
], allow=ALLOW)
r = b.run(ex, "去测试群")
tool_msg = find_tool_msg(payloads[2]["messages"], "o3")
case("index 经缓存翻译后命中白名单 → 放行",
     r["ok"] and ("open_chat", "", 1) in ex.calls, str(ex.calls))

b, ex, payloads = make_brain([
    ai_msg(tool_calls=[tc("o4", "open_chat", {"index": 5})]),
    ai_msg(content="知道了。"),
], allow=ALLOW)
r = b.run(ex, "去第6个会话")
tool_msg = find_tool_msg(payloads[1]["messages"], "o4")
case("index 无缓存可翻译 → 拦截并提示先 list_sessions",
     json.loads(tool_msg["content"])["error"]["code"] == "E-CU-004"
     and "list_sessions" in json.loads(tool_msg["content"])["error"]["detail"], str(tool_msg)[:200])

b, ex, payloads = make_brain([ai_msg(content="。")])   # 不设白名单
r = b.run(ex, "自由任务")
case("白名单为空 = 不限制", r["ok"], str(r)[:120])

# ============================================================ HTTP 状态分类
print("§5 HTTP 状态码分类（与 LLMClient 同口径）")


def fake_resp(code: int, text: str = "") -> types.SimpleNamespace:
    return types.SimpleNamespace(status_code=code, text=text,
                                 json=lambda: (_ for _ in ()).throw(ValueError()))


for code, want in ((401, "E-LLM-004"), (403, "E-LLM-004"), (404, "E-LLM-005"),
                   (429, "E-LLM-006"), (500, "E-LLM-007"), (503, "E-LLM-007"),
                   (418, "E-LLM-012")):
    try:
        from cu.brain import _raise_for_status                # noqa: E402
        _raise_for_status(fake_resp(code))
        case(f"HTTP {code} → {want}", False, "没抛")
    except EC.AppError as exc:
        case(f"HTTP {code} → {want}", exc.code == want, exc.code)

# ============================================================
print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
