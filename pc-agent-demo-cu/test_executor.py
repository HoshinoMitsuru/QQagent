# -*- coding: utf-8 -*-
"""
test_executor.py —— CU 执行面抽象层（cu/ 包）的自检

## 测什么

F2 的交付是「双执行面抽象」：接口契约、两条后端的选择与映射逻辑、
以及**最要紧的安全锁**。全部离线完成：

    - 不附着真 QQ（stub 掉 qqid / agent / host.grab）
    - 不发送任何消息
    - 不碰 UIA（这本来就是 hosted 面的纪律）

## 为什么安全锁的测试最重要

attach 面的 send_text 必须在 armed=True 之前**连 QQWindow 都不构造**——
这条防线如果依赖「上层记得检查」，就一定会被某次重构忘掉。所以测试里
专门断言：未解锁调用 send_text 后，executor._win 仍是 None。

用法：
    python test_executor.py
"""

from __future__ import annotations

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import error_codes as EC                      # noqa: E402
from cu.base import (ChatMessage, Executor, ExecutorError, Health,  # noqa: E402
                     MODES, SendReceipt, SessionInfo, Shot, create_executor)

OK = FAIL = 0


def case(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def expect_code(fn, code: str) -> bool:
    try:
        fn()
        return False
    except ExecutorError as exc:
        return exc.code == code


def fake_card(idx: int, name: str, unread: int = 0, group: bool = False):
    return types.SimpleNamespace(index=idx, display_name=name,
                                 unread=unread, looks_group=group)


# ============================================================ 错误码目录
print("§1 错误码目录：CU 域注册完整")
for code in ("E-CU-001", "E-CU-002", "E-CU-003", "E-CU-004", "E-CU-005"):
    case(f"{code} 已注册且可 describe",
         code in EC.CATALOG and bool(EC.get(code)["title"]), "")
case("CU 域进入 DOMAINS", "CU" in EC.DOMAINS, "")
case("目录自检无问题", EC.audit() == [], str(EC.audit()[:3]))

# ============================================================ 工厂与契约
print("§2 工厂：模式分发与非法值拒绝")
ex_attach = create_executor("attach", {})
case("attach 模式返回 AttachExecutor", type(ex_attach).__name__ == "AttachExecutor", "")
case("AttachExecutor 是 Executor 子类", isinstance(ex_attach, Executor), "")
case("attach 面要求人工确认", ex_attach.require_confirmation is True, "")
ex_hosted = create_executor("hosted", {})
case("hosted 模式返回 HostedExecutor", type(ex_hosted).__name__ == "HostedExecutor", "")
case("hosted 面全自主", ex_hosted.require_confirmation is False, "")
case("非法模式抛 E-CU-002",
     expect_code(lambda: create_executor("nonsense", {}), "E-CU-002"), "")
case("MODES 只有两种", MODES == ("attach", "hosted"), str(MODES))

# ============================================================ 确认锁（最重要）
print("§3 安全锁：attach.send_text 未解锁时连 QQ 都不许碰")
ex = create_executor("attach", {})
case("未 armed 抛 E-CU-004",
     expect_code(lambda: ex.send_text("你好"), "E-CU-004"), "")
case("armed=False 同样拒绝",
     expect_code(lambda: ex.send_text("你好", armed=False), "E-CU-004"), "")
case("拒绝发生在触碰 QQ 之前（_win 仍是 None）", ex._win is None,
     "确认锁必须先于一切 QQ 交互")

# ============================================================ attach 面映射
print("§4 attach 面：list_sessions / open_chat 的映射与错误码")
import cu.attach as ca                        # noqa: E402

_stub_qqid = types.SimpleNamespace(
    find_qq_main_window=lambda: ("desc", 4321),
    list_sessions=lambda win: [fake_card(0, "苏霖韵", 2),
                               fake_card(1, "小清澈群", 0, group=True)],
    switch_session=lambda card, win: True,
)
_saved_qqid = ca.qqid
_saved_agent = ca.agent
ca.qqid = _stub_qqid
ca.agent = types.SimpleNamespace(control_from_hwnd=lambda hwnd: object())

cards = ex_attach.list_sessions()
case("list_sessions 条数一致", len(cards) == 2, str(cards))
case("字段映射正确",
     cards[0] == SessionInfo(index=0, name="苏霖韵", unread=2, is_group=False)
     and cards[1].is_group is True, str(cards))
case("open_chat 按名字子串命中",
     ex_attach.open_chat("霖韵").get("opened") == "苏霖韵", "")
case("open_chat index=0 是合法序号（列表第一条）",
     ex_attach.open_chat(index=0).get("opened") == "苏霖韵", "")
case("open_chat 名字无匹配抛 E-CU-005",
     expect_code(lambda: ex_attach.open_chat("不存在的人"), "E-CU-005"), "")
case("open_chat 无 name 且无 index 抛 E-CU-005",
     expect_code(lambda: ex_attach.open_chat(), "E-CU-005"), "")
def override_ns(base, **kw):
    """复制 stub 命名空间并覆盖若干属性（SimpleNamespace 不允许 kwarg 重复）。"""
    d = dict(vars(base))
    d.update(kw)
    return types.SimpleNamespace(**d)


ca.qqid = override_ns(_stub_qqid, switch_session=lambda card, win: False)
case("切换失败（标题没变）抛 E-FG-004",
     expect_code(lambda: ex_attach.open_chat("霖韵"), "E-FG-004"), "")
ca.qqid = override_ns(_stub_qqid, find_qq_main_window=lambda: ("desc", 0))
case("主窗口找不到抛 E-QQ-003",
     expect_code(lambda: ex_attach.list_sessions(), "E-QQ-003"), "")
ca.qqid = override_ns(_stub_qqid, list_sessions=lambda win: [])
case("会话列表为空抛 E-QQ-007",
     expect_code(lambda: ex_attach.list_sessions(), "E-QQ-007"), "")
ca.qqid, ca.agent = _saved_qqid, _saved_agent

# ============================================================ hosted 面
print("§5 hosted 面：子进程结果的解析与错误映射")
import app.host as host_mod                   # noqa: E402
import cu.hosted as ch                        # noqa: E402

_saved_grab = host_mod.grab


def grab_ok(**kw) -> dict:
    return {"ok": True, "desktop": "QQAgentHidden", "png": "state/shot.png",
            "cu": {"attached": True, "title": "苏霖韵",
                   "sessions": [{"index": 0, "name": "苏霖韵",
                                 "unread": 3, "is_group": False}],
                   "messages": [{"sender": "苏霖韵", "content": "在吗",
                                 "direction": "other", "kind": "text",
                                 "ts": "", "dir_src": "class",
                                 "key": "k1", "rect": [1, 2, 3, 4]}],
                   "send": {"ok": True, "route": "wmchar",
                            "title": "苏霖韵"}},
            "open_chat": {"ok": True, "target": "苏霖韵",
                          "already_open": False, "clicked": "InvokePattern"},
            "saved": {"ok": True, "file": "state/shot.png"},
            "login_state": {"ok": True, "is_login_page": False,
                            "logged_in": True, "nickname": "小号",
                            "uia": {"ok": True, "has_ml_list": True}},
            "hwnd": 111}


host_mod.grab = grab_ok
hx = create_executor("hosted", {})
case("hosted list_sessions 映射",
     hx.list_sessions() == [SessionInfo(0, "苏霖韵", 3, False)], "")
case("hosted open_chat 回传目标",
     hx.open_chat("霖韵").get("opened") == "苏霖韵", "")
msgs = hx.read_recent(12)
case("hosted read_recent 过滤掉 key/rect 等内部字段",
     len(msgs) == 1 and msgs[0] == ChatMessage(
         sender="苏霖韵", content="在吗", direction="other",
         kind="text", ts="", dir_src="class"), str(msgs))
case("hosted send_text 回执带路由",
     hx.send_text("好").route == "wmchar", "")
case("hosted 空文本拒绝", expect_code(lambda: hx.send_text(""), "E-CU-001"), "")
case("hosted screenshot 取 saved.file",
     hx.screenshot().path == "state/shot.png", "")
h = hx.health()
case("hosted health：已登录+聊天页 → ok",
     h.ok and h.chat_open and h.mode == "hosted", h.to_dict().__str__())

host_mod.grab = lambda **kw: {"ok": False, "error":
                              "E-DESK-001 桌面 QQAgentHidden 不存在（QQ 没起在上面，或上次已退出）"}
case("桌面不存在 → E-DESK-001",
     expect_code(lambda: hx.list_sessions(), "E-DESK-001"), "")
host_mod.grab = lambda **kw: {"ok": False, "error":
                              "读不到宿主结果（state/grab.json）：FileNotFoundError"}
case("结果 JSON 不可读 → E-CU-003",
     expect_code(lambda: hx.list_sessions(), "E-CU-003"), "")
host_mod.grab = lambda **kw: {"ok": False, "error": "完全没见过的失败"}
case("未分类失败 → E-CU-001（兜底不断言根因）",
     expect_code(lambda: hx.list_sessions(), "E-CU-001"), "")
host_mod.grab = lambda **kw: {"ok": True, "cu": {"attached": False,
                                                 "code": "E-QQ-003",
                                                 "error": "附着 QQ 主窗口失败",
                                                 "ctx": {}}}
case("宿主侧附着失败透传精确码",
     expect_code(lambda: hx.list_sessions(), "E-QQ-003"), "")
host_mod.grab = lambda **kw: {"ok": True, "hwnd": 111,
                              "login_state": {"ok": True,
                                              "is_login_page": True,
                                              "logged_in": False,
                                              "nickname": "",
                                              "uia": {"ok": True,
                                                      "has_ml_list": False}}}
h2 = hx.health()
case("hosted health：登录页 → 不 ok 且给出 E-QQ-004",
     (not h2.ok) and h2.code == "E-QQ-004" and h2.chat_open is False,
     h2.to_dict().__str__())

host_mod.grab = _saved_grab

# ============================================================ 信封形状
print("§6 ExecutorError 的 JSON 信封与数据类序列化")
err = ExecutorError("E-CU-005", "没有匹配目标", {"want": "张三"})
env = err.envelope()
case("信封是 ok=False 结构",
     env["ok"] is False and env["error"]["code"] == "E-CU-005", str(env))
case("SessionInfo.to_dict 可 JSON 化",
     SessionInfo(0, "苏", 1, False).to_dict() ==
     {"index": 0, "name": "苏", "unread": 1, "is_group": False}, "")
case("SendReceipt.to_dict 可 JSON 化",
     SendReceipt(True, "wmchar", "苏").to_dict()["route"] == "wmchar", "")
case("Shot.to_dict 可 JSON 化", Shot(True, "a.png").to_dict()["ok"] is True, "")
case("Health.to_dict 可 JSON 化",
     Health(True, "attach").to_dict()["mode"] == "attach", "")

# ============================================================
print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
