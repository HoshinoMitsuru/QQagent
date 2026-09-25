# -*- coding: utf-8 -*-
"""
test_tools.py —— 工具化层（cu/tools.py）的自检

## 测什么

F3 的交付是「六原语 → OpenAI tool calling 工具」的包装层。全部离线：

    - schema 合法性（OpenAI 格式、字段自洽）
    - **armed 不出现在给模型的 schema 里**（模型不能给自己解锁）
    - dispatch 的信封形状、参数校验与宽松矫正
    - 确认锁在 dispatch 层的表现（E-CU-004，且原语根本没被调用）
    - 异常兜底（Brain 主循环不被炸掉）

不附着真 QQ、不发送、不碰 UIA。

用法：
    python test_tools.py
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from cu.base import (ChatMessage, Executor, ExecutorError, Health,  # noqa: E402
                     SendReceipt, SessionInfo, Shot)
from cu.tools import TOOLS, dispatch, tool_schemas  # noqa: E402

OK = FAIL = 0


def case(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


class StubExecutor(Executor):
    """六原语全部可编程的桩。记录调用，供断言「有没有真的被调」。"""

    name = "stub"
    require_confirmation = False

    def __init__(self):
        self.calls: list[tuple] = []
        self.raise_on: dict[str, Exception] = {}

    def _maybe_raise(self, tag: str):
        if tag in self.raise_on:
            raise self.raise_on[tag]

    def list_sessions(self):
        self.calls.append(("list_sessions",))
        self._maybe_raise("list_sessions")
        return [SessionInfo(0, "本人", 2, False),
                SessionInfo(1, "小清澈群", 0, True)]

    def open_chat(self, name="", index=0):
        self.calls.append(("open_chat", name, index))
        self._maybe_raise("open_chat")
        return {"ok": True, "opened": name or f"#{index}"}

    def read_recent(self, limit=12):
        self.calls.append(("read_recent", limit))
        self._maybe_raise("read_recent")
        return [ChatMessage("本人", "在吗", "other", dir_src="class")]

    def send_text(self, text, *, armed=None):
        self.calls.append(("send_text", text, armed))
        self._maybe_raise("send_text")
        return SendReceipt(True, route="wmchar", chat_title="本人")

    def screenshot(self, path=""):
        self.calls.append(("screenshot", path))
        self._maybe_raise("screenshot")
        return Shot(True, path or "state/auto.png")

    def health(self):
        self.calls.append(("health",))
        self._maybe_raise("health")
        return Health(True, mode=self.name, chat_open=True)


# ============================================================ schema
print("§1 注册表与 schema 合法性")
expect = {"list_sessions", "open_chat", "read_recent",
          "send_text", "screenshot", "health"}
case("注册表正好六个工具且名字对上", set(TOOLS) == expect, str(set(TOOLS)))
for name, t in TOOLS.items():
    fn = t.schema.get("function", {})
    params = fn.get("parameters", {})
    ok = (t.schema.get("type") == "function"
          and fn.get("name") == name
          and isinstance(fn.get("description"), str) and fn["description"]
          and params.get("type") == "object"
          and set(params.get("required", [])) <= set(params.get("properties", {})))
    case(f"{name}: schema 自洽", bool(ok), json.dumps(t.schema, ensure_ascii=False)[:120])
blob = json.dumps(tool_schemas(), ensure_ascii=False)
case("给模型的 schema 里没有 armed（模型不能自己解锁）", "armed" not in blob, "")
case("tool_schemas 返回的是深拷贝",
     (tool_schemas()[0]["function"].__setitem__("name", "hacked"),
      tool_schemas()[0]["function"]["name"] != "hacked")[-1], "")
case("每条描述都给模型写了用法",
     all(len(t.schema["function"]["description"]) >= 30 for t in TOOLS.values()), "")

# ============================================================ dispatch 基本流
print("§2 dispatch：成功路径与信封形状")
ex = StubExecutor()
r = dispatch(ex, "list_sessions", {})
case("list_sessions 信封",
     r["ok"] and r["tool"] == "list_sessions" and r["count"] == 2
     and r["sessions"][1]["is_group"] is True, str(r))
r = dispatch(ex, "open_chat", {"name": "本"})
case("open_chat 透传 name（index 缺省 = -1 未指定）",
     r["ok"] and ex.calls[-1] == ("open_chat", "本", -1) and r["opened"] == "本", str(r))
r = dispatch(ex, "open_chat", {"index": 2})
case("open_chat 透传 index", ex.calls[-1] == ("open_chat", "", 2), str(ex.calls[-1]))
r = dispatch(ex, "open_chat", {"index": 0})
case("index=0 是合法序号不是缺省",
     ex.calls[-1] == ("open_chat", "", 0), str(ex.calls[-1]))
r = dispatch(ex, "read_recent", {"limit": 30})
case("read_recent 信封",
     r["ok"] and r["count"] == 1 and r["messages"][0]["direction"] == "other", str(r))
case("limit 矫正：字符串 \"7\" 也接受",
     dispatch(ex, "read_recent", {"limit": "7"})["ok"]
     and ex.calls[-1] == ("read_recent", 7), str(ex.calls[-1]))
r = dispatch(ex, "send_text", {"text": "你好"})
case("hosted 面（无需确认）send_text 直达",
     r["ok"] and r["route"] == "wmchar" and ex.calls[-1] == ("send_text", "你好", False),
     str(r))
r = dispatch(ex, "screenshot", {})
case("screenshot 信封带路径", r["ok"] and r["path"] == "state/auto.png", str(r))
r = dispatch(ex, "health", {})
case("health 信封", r["ok"] and r["chat_open"] is True, str(r))

# ============================================================ 确认锁
print("§3 dispatch 层的确认锁（E-CU-004）")
ex = StubExecutor()
ex.require_confirmation = True        # 模拟 attach 面
r = dispatch(ex, "send_text", {"text": "这条要先给人看"})
case("未 armed → needs_confirmation 信封（与错误信封同形）",
     r["ok"] is False and r["error"]["code"] == "E-CU-004"
     and r["needs_confirmation"] is True and r["text"] == "这条要先给人看", str(r))
case("锁拦截时原语根本没被调用",
     not any(c[0] == "send_text" for c in ex.calls), str(ex.calls))
r = dispatch(ex, "send_text", {"text": "已确认"}, armed=True)
case("armed=True 人工放行后真正发送",
     r["ok"] and ex.calls[-1] == ("send_text", "已确认", True), str(r))

# ============================================================ 校验与错误码
print("§4 dispatch：未知工具 / 坏参数 / 矫正")
ex = StubExecutor()
case("未知工具 → E-CU-006",
     dispatch(ex, "delete_everything", {})["error"]["code"] == "E-CU-006", "")
case("坏 JSON 串 → E-CU-007",
     dispatch(ex, "read_recent", "{limit: 3")["error"]["code"] == "E-CU-007", "")
case("arguments 是数组 → E-CU-007",
     dispatch(ex, "read_recent", [1])["error"]["code"] == "E-CU-007", "")
case("缺必填参数（send_text 无 text）→ E-CU-007",
     dispatch(ex, "send_text", {})["error"]["code"] == "E-CU-007", "")
case("未知参数 → E-CU-007",
     dispatch(ex, "health", {"force": True})["error"]["code"] == "E-CU-007", "")
case("类型错误（limit=数组）→ E-CU-007",
     dispatch(ex, "read_recent", {"limit": [1]})["error"]["code"] == "E-CU-007", "")
case("合法 JSON 串也能吃",
     dispatch(ex, "read_recent", "{\"limit\": 5}")["ok"] is True, "")
case("None arguments 当空对象",
     dispatch(ex, "health", None)["ok"] is True, "")
case("空串 arguments 当空对象", dispatch(ex, "health", "")["ok"] is True, "")
case("错误信封带 tool 字段",
     dispatch(ex, "read_recent", {"limit": [1]}).get("tool") == "read_recent", "")

# ============================================================ 异常兜底
print("§5 dispatch：执行面异常的兜底（Brain 主循环不许被炸掉）")
ex = StubExecutor()
ex.raise_on["list_sessions"] = ExecutorError("E-QQ-007", "会话列表为空")
r = dispatch(ex, "list_sessions", {})
case("ExecutorError 原样转信封（码不变）",
     r["ok"] is False and r["error"]["code"] == "E-QQ-007"
     and r["tool"] == "list_sessions", str(r))
ex.raise_on.clear()
ex.raise_on["health"] = ValueError("意外异常")
r = dispatch(ex, "health", {})
case("意外异常兜底 E-CU-001",
     r["ok"] is False and r["error"]["code"] == "E-CU-001"
     and "ValueError" in r["error"]["detail"], str(r))

# ============================================================
print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
