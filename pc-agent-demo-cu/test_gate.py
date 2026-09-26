# -*- coding: utf-8 -*-
"""
test_gate.py —— cu/gate.py（执行面安全闸，A1+C1+B）离线自检

覆盖：
1. 匹配语义与配置提取
2. 委托（name / require_confirmation / 其余属性）
3. 目标闸：attach/hosted 两面 × 留空/非空 × name/index × 标题探测
4. 读取闸 B1（留空=方便优先）/ B2（非空=安全优先）× 失败关闭
5. list_sessions 永不拦
6. 探测失败原样上抛（真根因优先于闸）

不碰 QQ、不碰 UIA。用法：python test_gate.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from cu.base import Executor, ExecutorError, SessionInfo  # noqa: E402
from cu.gate import GatedExecutor, gates_from_cfg, target_allowed  # noqa: E402

OK = FAIL = 0


def case(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def expect_cu004(fn, name: str):
    try:
        fn()
        case(name, False, "没抛")
    except ExecutorError as exc:
        case(name, exc.code == "E-CU-004", f"码={exc.code} {exc.detail}")
    except Exception as exc:                       # noqa: BLE001
        case(name, False, f"错型 {type(exc).__name__}: {exc}")


class FakeInner(Executor):
    """内层执行面替身：记录调用、可配置标题与面名。"""

    def __init__(self, face: str = "hosted", require_confirmation: bool = False,
                 title: str = "", title_exc: ExecutorError | None = None):
        self.name = face
        self.require_confirmation = require_confirmation
        self._title = title
        self._title_exc = title_exc
        self.calls: list[str] = []
        self.probes = 0                      # current_chat_title 调用次数

    # 六原语
    def list_sessions(self):
        self.calls.append("list_sessions")
        return [SessionInfo(index=0, name="测试目标A", unread=0, is_group=False),
                SessionInfo(index=1, name="测试群B", unread=2, is_group=True)]

    def open_chat(self, name: str = "", index: int = -1) -> dict:
        self.calls.append(f"open_chat({name!r},{index})")
        return {"ok": True, "opened": name or f"index={index}"}

    def read_recent(self, limit: int = 12):
        self.calls.append(f"read_recent({limit})")
        return []

    def send_text(self, text: str, *, armed: bool | None = None):
        self.calls.append(f"send_text({text!r},armed={armed})")
        if self.require_confirmation and armed is not True:
            raise ExecutorError("E-CU-004", "（内层确认锁）发送必须人工确认")
        return "sent"

    def screenshot(self, path: str = ""):
        self.calls.append("screenshot")
        return "shot"

    def health(self):
        self.calls.append("health")
        return "health"

    def current_chat_title(self) -> str:
        self.probes += 1
        if self._title_exc is not None:
            raise self._title_exc
        return self._title


# ---------------------------------------------------------------------------
print("[1] 匹配语义与配置提取")

case("互为子串命中（名单项 ⊆ 目标）", target_allowed("测试目标A", ["测试目标A"]), "")
case("互为子串命中（目标 ⊆ 名单项）", target_allowed("测试目标A（备注）", ["测试目标A"]), "")
case("名单外拒绝", not target_allowed("张三", ["测试目标A", "测试群B"]), "")
case("空目标拒绝（fail-closed 原料）", not target_allowed("", ["测试目标A"]), "")
case("空名单拒绝一切", not target_allowed("测试目标A", []), "")

open_a, read_a = gates_from_cfg({"cu": {"open_chat_allow": ["测试目标A"],
                                        "read_allow": ["测试群B"]}})
case("gates_from_cfg 提取两名单", open_a == ["测试目标A"] and read_a == ["测试群B"], "")
open_a, read_a = gates_from_cfg({})
case("缺 cu 节 = 两闸均空", open_a == [] and read_a == [], "")
open_a, read_a = gates_from_cfg({"cu": {"open_chat_allow": ["", "  ", "测试目标A", 3]}})
case("非字符串/空白项被剔除", open_a == ["测试目标A"], str(open_a))

# ---------------------------------------------------------------------------
print("[2] 委托")

inner = FakeInner(face="attach", require_confirmation=True)
g = GatedExecutor(inner, open_allow=[], read_allow=[])
case("name 委托", g.name == "attach", g.name)
case("require_confirmation 委托", g.require_confirmation is True, "")
case("未配置的闸零开销：read 不做标题探测",
     (g.read_recent(5) == [] and inner.probes == 0), str(inner.probes))

# ---------------------------------------------------------------------------
print("[3] 目标闸 · attach 面")

# 留空 = 放行（确认锁兜底）
inner = FakeInner(face="attach", require_confirmation=True)
g = GatedExecutor(inner, open_allow=[], read_allow=[])
g.open_chat(name="任何人")
try:
    g.send_text("hi")
    case("attach 空名单：open/send 放行", False, "send 没抛（应被内层确认锁拦）")
except ExecutorError as exc:
    case("attach 空名单：open 放行、send 到内层确认锁",
         any("open_chat" in c for c in inner.calls) and exc.code == "E-CU-004"
         and "确认" in exc.detail, str(inner.calls))

# 非空：名单内放行 / 名单外拦
inner = FakeInner(face="attach", require_confirmation=True)
g = GatedExecutor(inner, open_allow=["测试目标A"], read_allow=[])
g.open_chat(name="测试目标A")
case("attach 非空名单：名单内 open 放行", "open_chat('测试目标A',-1)" in inner.calls, str(inner.calls))
expect_cu004(lambda: GatedExecutor(FakeInner(face="attach"),
                                   open_allow=["测试目标A"]).open_chat(name="张三"),
             "attach 非空名单：名单外 open 拦截（E-CU-004）")

# index → 名字解析
g = GatedExecutor(FakeInner(face="attach"), open_allow=["测试群B"])
g.open_chat(index=1)
case("attach：index 解析到名单内名字 → 放行", True, "")
expect_cu004(lambda: g.open_chat(index=0), "attach：index 解析到名单外名字 → 拦截")
expect_cu004(lambda: g.open_chat(index=99), "attach：index 解析不到名字 → fail-closed")

# ---------------------------------------------------------------------------
print("[4] 目标闸 · hosted 面（空名单 = fail-closed）")

expect_cu004(lambda: GatedExecutor(FakeInner(face="hosted"),
                                   open_allow=[]).open_chat(name="测试目标A"),
             "hosted 空名单：open 一律拒")
expect_cu004(lambda: GatedExecutor(FakeInner(face="hosted"),
                                   open_allow=[]).send_text("hi"),
             "hosted 空名单：send 一律拒")

inner = FakeInner(face="hosted", title="测试目标A")
g = GatedExecutor(inner, open_allow=["测试目标A"])
r = g.send_text("在吗")
case("hosted 非空名单：当前会话在名单内 → 发送放行",
     r == "sent" and inner.probes == 1, f"probes={inner.probes}")

inner = FakeInner(face="hosted", title="路人丙")
g = GatedExecutor(inner, open_allow=["测试目标A"])
expect_cu004(lambda: g.send_text("hi"), "hosted：当前会话不在名单 → send 拦")

inner = FakeInner(face="hosted", title="")
g = GatedExecutor(inner, open_allow=["测试目标A"])
expect_cu004(lambda: g.send_text("hi"), "hosted：探不到标题 → send fail-closed")

expect_cu004(lambda: GatedExecutor(FakeInner(face="hosted"), open_allow=["测试群B"])
             .open_chat(name="测试目标A"),
             "hosted：open 名单外 → 拦（互为子串也不豁免名单外）")
g = GatedExecutor(FakeInner(face="hosted"), open_allow=["测试目标"])
g.open_chat(name="测试目标A")
case("hosted：名单项是目标的子串 → 放行（互为子串语义）", True, "")

# ---------------------------------------------------------------------------
print("[5] 读取闸 B1/B2")

# B1（read_allow 空）：不探标题直接放（见 [2] 零开销用例）
# B2（非空）：
inner = FakeInner(face="hosted", title="测试群B")
g = GatedExecutor(inner, open_allow=[], read_allow=["测试群B"])
case("B2：当前会话在 read_allow → read 放行", g.read_recent() == [] and inner.probes == 1, "")
case("B2：screenshot 同受 read_allow 约束", g.screenshot() == "shot", "")

inner = FakeInner(face="hosted", title="测试群B")
g = GatedExecutor(inner, open_allow=[], read_allow=["测试目标A"])
expect_cu004(lambda: g.read_recent(), "B2：当前会话不在 read_allow → read 拦")
expect_cu004(lambda: g.screenshot(), "B2：screenshot 同拦")

inner = FakeInner(face="hosted", title="")
g = GatedExecutor(inner, open_allow=[], read_allow=["测试目标A"])
expect_cu004(lambda: g.read_recent(), "B2：探不到标题 → read fail-closed")

inner = FakeInner(face="attach", title="测试群B")
g = GatedExecutor(inner, open_allow=[], read_allow=["测试群B"])
case("B2：attach 面 read 同样受闸", g.read_recent() == [], "")

inner = FakeInner(face="hosted", title="路人丙")
g = GatedExecutor(inner, open_allow=[], read_allow=["测试目标A"])
case("B2：list_sessions 永不拦", [s.name for s in g.list_sessions()][0] == "测试目标A",
     "")

# ---------------------------------------------------------------------------
print("[6] 探测失败：真根因原样上抛（不是 E-CU-004）")

inner = FakeInner(face="hosted", title_exc=ExecutorError("E-QQ-003", "附着失败"))
g = GatedExecutor(inner, open_allow=["测试目标A"], read_allow=["测试目标A"])
try:
    g.send_text("hi")
    case("send 标题探测失败 → 上抛真根因", False, "没抛")
except ExecutorError as exc:
    case("send 标题探测失败 → 上抛真根因", exc.code == "E-QQ-003", exc.code)
try:
    g.read_recent()
    case("read 标题探测失败 → 上抛真根因", False, "没抛")
except ExecutorError as exc:
    case("read 标题探测失败 → 上抛真根因", exc.code == "E-QQ-003", exc.code)

print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
