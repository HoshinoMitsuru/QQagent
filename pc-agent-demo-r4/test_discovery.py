# -*- coding: utf-8 -*-
"""
test_discovery.py — 「发现 + 延迟总读取」降级方案的离线单测

## 为什么要有这个测试

桌面 UI 上每个会话**只保留最新一条消息的节选**，摘要是被截断的、也拿不到上下文。
所以整个方案是：

    首次红点（或摘要变化）→ 只占位排队（不碰前台、不读正文）
      → 静默窗（5s 阈值）关闭后 → 切过去做一次「上下文总读取」
      → 按准入判定写进该会话上下文 → 生成回复 → 排到风控额度就发送

这条链路以前没有任何自动化覆盖，只能靠真机手工试。而它有三个很容易回归的点：

    1. 首见会话建基线时 **skip_last 要用上未读条数**，否则要么回灌历史、要么吞掉新消息；
    2. 「总读取没拿到新消息」必须**撤单**，否则会为一条根本不存在的新消息回一句；
    3. 自己发出的回复会改会话预览，**发送后必须重采指纹**，否则形成自我叫醒循环。

本测试把 QQ 完全 fake 掉（包括 `qqid.list_sessions` / `qqid.switch_session`），
**不需要 QQ、不需要网络、不发送任何东西**。

用法：
    python test_discovery.py
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time

import agent as A
import qqid as Q

OK = FAIL = 0


def case(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ------------------------------------------------------------------ 假 QQ
def mkmsg(sender: str, content: str, idx: int, kind: str = "text") -> A.Message:
    return A.Message(sender=sender, content=content, direction="other",
                     key=f"m{idx}", rect=(0, 0, 0, 0), kind=kind, ts="")


class FakeQQ:
    """
    假 QQ 窗口。每个会话各自一份消息列表 + 各自一份已读集合（和真实实现同构）。

    `switch_session` 被 monkeypatch 成直接设 `cur`，所以切换是「瞬时」的，
    但仍要经过 `_ensure_session` 的签名稳定等待 —— 那条路径也要被测到。
    """

    def __init__(self):
        self.win = object()
        self.ml_list = object()
        # 这两个字段要跟真实 QQWindow 对齐：_ensure_session 会读 hwnd 来决定
        # 「切换前后台怎么记、怎么还」。0 表示"没有真窗口"，于是那套前台逻辑自动跳过。
        self.hwnd = 0
        self.fg_before_switch = 0
        self.sessions: dict = {}      # name -> {"msgs": [...], "is_group": bool}
        self.cur: str = ""
        self.is_group = False
        self.member_count = 0
        self._seen: dict = {}
        self.clicks: list = []
        self.sent: list = []

    # ---- 窗口接口
    @property
    def dialog_title(self) -> str:
        return self.cur

    def title_now(self) -> str:
        return self.cur

    def refresh_layout(self, force: bool = False) -> None:
        self.is_group = bool((self.sessions.get(self.cur) or {}).get("is_group"))

    def read_messages(self, limit: int = 30) -> list:
        return list((self.sessions.get(self.cur) or {}).get("msgs", []))[-limit:]

    def chat_signature(self, force: bool = True) -> tuple:
        msgs = self.read_messages(8)
        return (self.cur, self.is_group, tuple(m.key for m in msgs))

    def send_text(self, text: str, guard=None) -> bool:
        if guard is not None and not guard():
            return False
        self.sent.append((self.cur, text))
        return True

    # ---- 已读集合（与 QQWindow 同语义：按 scope 隔离、只在内存）
    def _seen_of(self, scope: str = "") -> set:
        return self._seen.setdefault(scope or "", set())

    def has_seen(self, scope: str = "") -> bool:
        return (scope or "") in self._seen

    def mark_seen(self, msgs, scope: str = "") -> None:
        d = self._seen_of(scope)
        for m in msgs:
            d.add(m.key)

    def split_new(self, msgs, scope: str = "") -> list:
        d = self._seen_of(scope)
        fresh = [m for m in msgs if m.key not in d]
        for m in msgs:
            d.add(m.key)
        return fresh

    def baseline(self, skip_last: int = 0, scope: str = "") -> int:
        msgs = self.read_messages()
        target = msgs[: len(msgs) - skip_last] if skip_last > 0 else msgs
        self.mark_seen(target, scope)
        return len(target)


# ------------------------------------------------------------------ 工具
def make_cfg(tmpdir: str) -> dict:
    return A._deep_merge(A.DEFAULTS, {
        "persist": {"file": os.path.join(tmpdir, "conversations.json")},
        "identity": {"store": os.path.join(tmpdir, "uid-map.json")},
        "discovery": {"state_file": os.path.join(tmpdir, "rotation.json")},
        "queue": {"jitter_seconds": 0.0, "min_interval_seconds": 0.0,
                  "max_replies_per_minute": 600},
    })


def build(tmpdir: str) -> A.Agent:
    """装好假 QQ + 假模型 + 假会话列表的 Agent。"""
    cfg = make_cfg(tmpdir)
    ag = A.Agent(cfg, no_send=True)
    ag.qq = FakeQQ()
    # 模型不联网
    ag.generate_reply = lambda scope: f"（测试回复 → {scope}）"
    # 会话列表也 fake 掉：discover 只依赖 list_sessions 的返回值
    ag._fake_cards = []
    Q.list_sessions = lambda win: list(ag._fake_cards)

    def fake_click(session, win, prefer_noop=True):
        ag.qq.clicks.append(session.display_name)
        ag.qq.cur = session.display_name
        ag.qq.refresh_layout(force=True)
        return True

    Q.switch_session = fake_click
    return ag


def card(name: str, preview=("在吗",), unread: int = 0) -> Q.SessionCard:
    return Q.SessionCard(index=0, display_name=name, preview_texts=tuple(preview),
                         ts="12:00", selected=False, rect=(0, 0, 0, 0), unread=unread)


def seed_session(ag: A.Agent, name: str, is_group: bool, uin: str,
                 msgs: list, cur: bool = False) -> None:
    ag.qq.sessions[name] = {"msgs": msgs, "is_group": is_group}
    if cur:
        ag.qq.cur = name
        ag.qq.refresh_layout(force=True)
    ag._uids().put(name, Q.UidInfo(uin=uin, is_group=is_group, card_name=name))


# ------------------------------------------------------------------ 用例
def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="discovery-test-")
    print("=" * 78)
    print(f"发现 / 延迟总读取 离线单测（临时目录 {tmpdir}）")
    print("=" * 78)

    def sub(name: str) -> str:
        """
        每个用例一个独立子目录。

        必须隔离：`rotation.json` 里存着「哪些会话建过基线 + 上次的指纹快照」，
        共用目录会让上一个用例留下的快照把下一个用例的「首见」变成「有变化」，
        于是触发的时机整体错位（实测踩过，表现为莫名其妙的 FAIL）。
        """
        p = os.path.join(tmpdir, name)
        os.makedirs(p, exist_ok=True)
        return p

    try:
        test_discover_triggers(sub("t1"))
        test_total_read(sub("t2"))
        test_no_new_messages_aborts(sub("t3"))
        test_nontext_policy(sub("t4"))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 78)
    print(f"结果：{OK} 通过 / {FAIL} 失败")
    print("=" * 78)
    return 0 if FAIL else 1


def test_discover_triggers(tmpdir: str) -> None:
    print("\n[1] 发现：谁在等我（只扫列表，不碰前台）")
    ag = build(tmpdir)
    ag.qq.cur = "自己人"
    ag.qq.sessions["自己人"] = {"msgs": [], "is_group": False}
    seed_session(ag, "阿离", False, "1001", [mkmsg("阿离", "在吗", 1)])
    seed_session(ag, "群A", True, "2002", [mkmsg("甲", "开团", 2)])

    # 第一次扫描：全是首见 → 只记快照。未读为 0 就不该动作
    ag._fake_cards = [card("阿离"), card("群A", preview=("甲：", "开团"))]
    n = ag.discover()
    case("首见且无未读 → 不入队", n == 0 and len(ag.queue) == 0, f"n={n} len={len(ag.queue)}")
    case("但指纹快照已记下", len(ag._rot().fingerprints) == 2,
         f"{ag._rot().fingerprints}")
    case("没有切会话（发现阶段不许碰前台）", ag.qq.clicks == [], f"{ag.qq.clicks}")

    # 第二次扫描：还是原样 → 不该重复入队
    n = ag.discover()
    case("无变化 → 不入队", n == 0 and len(ag.queue) == 0)

    # 阿离来了新消息（摘要变化 + 未读 1）→ 占位入队
    ag._fake_cards = [card("阿离", preview=("在吗", "在忙吗"), unread=1),
                      card("群A", preview=("甲：", "开团"))]
    ag._last_scan = 0.0
    n = ag.discover()
    case("红点 → 占位入队", n == 1 and len(ag.queue) == 1, f"n={n} len={len(ag.queue)}")
    it = ag.queue.get("private:1001")
    case("scope 用 QQ 号，且群/私聊判对", it is not None and it.scope == "private:1001",
         f"{ag.queue.scopes()}")
    case("占位项不带正文（摘要不当上下文）", it is not None and it.count == 0,
         f"count={it.count if it else None}")
    case("标记为待总读", it is not None and it.pending_read is True)
    case("未读条数已记下（后面建基线要用）", it is not None and it.unread_hint == 1)
    case("仍然没切会话", ag.qq.clicks == [], f"{ag.qq.clicks}")

    # 再来一次同样的卡 → 不重复占位
    ag._last_scan = 0.0
    n = ag.discover()
    case("同一份新动静不重复占位", n == 0 and len(ag.queue) == 1, f"n={n}")

    # 群聊也要能发现（scope 前缀不同）
    ag._fake_cards = [card("阿离", preview=("在吗", "在忙吗"), unread=1),
                      card("群A", preview=("甲：", "开团了"), unread=3)]
    ag._last_scan = 0.0
    n = ag.discover()
    case("群聊红点也入队，scope 带 group:", ag.queue.get("group:2002") is not None,
         f"{ag.queue.scopes()}")

    # 当前打开的会话必须跳过（交给 step 的实时路径，否则会回两次）
    ag2 = build(tmpdir)
    seed_session(ag2, "自己人", False, "3003", [mkmsg("自己人", "喂", 9)], cur=True)
    ag2._fake_cards = [card("自己人", preview=("喂",), unread=5)]
    ag2.discover()
    case("当前打开的会话被跳过", len(ag2.queue) == 0, f"{ag2.queue.scopes()}")

    # 单轮上限（先让两轮都是「首见且无未读」以免混入未读触发）
    ag3 = build(tmpdir)
    ag3._fake_cards = [card(f"用户{i}", preview=("x", f"新{i}"), unread=0) for i in range(6)]
    ag3.discover()                     # 首轮只记快照
    ag3._fake_cards = [card(f"用户{i}", preview=("x", f"更新{i}"), unread=1) for i in range(6)]
    ag3._last_scan = 0.0
    n = ag3.discover()
    case("单轮上限 max_enqueue_per_scan=3 生效", n == 3, f"n={n} len={len(ag3.queue)}")


def test_total_read(tmpdir: str) -> None:
    print("\n[2] 延迟总读取：切过去 → 读全 → 只保留未读那几条")
    ag = build(tmpdir)
    ag.qq.cur = "自己人"
    ag.qq.sessions["自己人"] = {"msgs": [], "is_group": False}
    # 阿离有 8 条历史 + 2 条新消息（未读 2）
    msgs = [mkmsg("阿离", f"历史{i}", i) for i in range(8)]
    msgs += [mkmsg("阿离", "新消息一", 100), mkmsg("阿离", "新消息二", 101)]
    seed_session(ag, "阿离", False, "1001", msgs)

    ag._fake_cards = [card("阿离", preview=("新消息二",), unread=2)]
    case("首见就带未读 → 首次红点即排队", ag.discover() == 1,
         f"len={len(ag.queue)}")

    it = ag.queue.get("private:1001")
    it.ready_at = time.time() - 1.0     # 视为静默窗已关闭
    ok = ag.serve_queue()
    case("第 1 次 serve_queue：读+生成，不发送", ok is False)
    case("为读正文切了一次会话", ag.qq.clicks == ["阿离"], f"{ag.qq.clicks}")
    case("回复已备好", bool(it.prepared and it.reply), f"reply={it.reply!r}")

    hist = ag.store.history("private:1001")
    rows = hist.to_dict() if hasattr(hist, "to_dict") else []
    txt = "\n".join((r.get("content") or "") for r in rows)
    case("新消息一进了上下文", "新消息一" in txt)
    case("新消息二进了上下文", "新消息二" in txt)
    case("历史消息没被回灌", "历史0" not in txt and "历史7" not in txt, f"{txt[:120]!r}")
    case("只写进去 2 条对方消息",
         sum(1 for r in rows if r.get("role") == "user") == 2,
         f"{[r.get('content') for r in rows]}")

    ok = ag.serve_queue()
    case("第 2 次 serve_queue：排到风控额度 → 发出", ok is True)
    case("队列已清空", len(ag.queue) == 0)
    case("发送后指纹被重采（防止自我叫醒）",
         ag._rot().fp_of("阿离") is not None)


def test_no_new_messages_aborts(tmpdir: str) -> None:
    print("\n[3] 总读取没拿到新消息 → 撤单（不为不存在的新消息回一句）")
    ag = build(tmpdir)
    ag.qq.cur = "自己人"
    ag.qq.sessions["自己人"] = {"msgs": [], "is_group": False}
    seed_session(ag, "阿离", False, "1001", [mkmsg("阿离", "唯一一条", 1)])

    ag._fake_cards = [card("阿离", preview=("旧",), unread=0)]
    ag.discover()
    # 摘要变了但未读为 0 —— 很可能是「我自己发的」或纯重渲染
    ag._fake_cards = [card("阿离", preview=("变了",), unread=0)]
    ag._last_scan = 0.0
    case("摘要变化（无未读）也触发（兜底）", ag.discover() == 1, f"{len(ag.queue)}")

    it = ag.queue.get("private:1001")
    # 假装这一条已经被别处读过（比如用户自己打开看了）
    ag.qq.mark_seen(ag.qq.sessions["阿离"]["msgs"], "private:1001")
    it.ready_at = time.time() - 1.0
    ag.serve_queue()
    case("读不到新消息 → 撤单，不占用队列", len(ag.queue) == 0, f"{ag.queue.scopes()}")
    case("没有生成回复", it.reply == "", f"{it.reply!r}")
    rows = ag.store.history("private:1001").to_dict()
    case("上下文没被写入", not any(r.get("role") == "user" for r in rows),
         f"{[r.get('content') for r in rows]}")


def test_nontext_policy(tmpdir: str) -> None:
    """
    非文本未读（只发了个表情）在两种策略下的行为。

    这是真机跑出来的一个坑：对方只发了一个动画表情，未读徽标亮着（发现链路
    完美工作），但总读取阶段因为「非文本一律跳过」拿到 0 条可读消息，
    于是整条链路正确撤单 —— 表现为「有红点却一声不吭」。
    对陪伴场景来说这多半不是想要的，所以做成可配置项并在此固定两种行为。
    """
    print("\n[4] 非文本未读：skip 撤单 / describe 正常回")

    d1 = os.path.join(tmpdir, "a")
    d2 = os.path.join(tmpdir, "b")
    d3 = os.path.join(tmpdir, "c")
    for p in (d1, d2, d3):
        os.makedirs(p, exist_ok=True)

    # ---- 4a. 默认 skip：撤单，且不写上下文 ----
    ag = build(d1)
    ag.qq.cur = "自己人"
    ag.qq.sessions["自己人"] = {"msgs": [], "is_group": False}
    seed_session(ag, "阿离", False, "1001", [mkmsg("阿离", "[动画表情]", 1, kind="nontext")])
    case("默认策略是 skip", ag.cfg["chat"]["nontext_policy"] == "skip")

    ag._fake_cards = [card("阿离", preview=("[动画表情]",), unread=1)]
    case("表情红点照样触发发现", ag.discover() == 1, f"{len(ag.queue)}")

    it = ag.queue.get("private:1001")
    it.ready_at = time.time() - 1.0
    ag.serve_queue()
    case("skip：总读取 0 条 → 撤单", len(ag.queue) == 0, f"{ag.queue.scopes()}")
    rows = ag.store.history("private:1001").to_dict()
    case("skip：上下文没被写入", not any(r.get("role") == "user" for r in rows),
         f"{[r.get('content') for r in rows]}")

    # ---- 4b. describe：占位串当正文，正常走完 read → generate ----
    ag2 = build(d2)
    ag2.cfg["chat"]["nontext_policy"] = "describe"
    ag2.qq.cur = "自己人"
    ag2.qq.sessions["自己人"] = {"msgs": [], "is_group": False}
    seed_session(ag2, "阿离", False, "1001",
                 [mkmsg("阿离", "[动画表情]", 1, kind="nontext")])

    ag2._fake_cards = [card("阿离", preview=("[动画表情]",), unread=1)]
    case("describe：发现入队", ag2.discover() == 1, f"{len(ag2.queue)}")

    it2 = ag2.queue.get("private:1001")
    it2.ready_at = time.time() - 1.0
    ok = ag2.serve_queue()
    case("describe：第 1 次 serve_queue 不发送", ok is False)
    case("describe：回复已备好（没被当成 0 条撤单）",
         bool(it2.prepared and it2.reply), f"prepared={it2.prepared} reply={it2.reply!r}")
    rows2 = ag2.store.history("private:1001").to_dict()
    txt2 = "\n".join((r.get("content") or "") for r in rows2)
    case("describe：占位串进了上下文", "[动画表情]" in txt2, f"{txt2[:120]!r}")

    case("describe：第 2 次 serve_queue 发出去", ag2.serve_queue() is True)
    case("describe：队列已清空", len(ag2.queue) == 0)

    # ---- 4c. describe 不改变纯文本与调教的原有语义 ----
    ag3 = build(d3)
    ag3.cfg["chat"]["nontext_policy"] = "describe"
    ag3.qq.cur = "自己人"
    ag3.qq.sessions["自己人"] = {"msgs": [], "is_group": False}
    t = mkmsg("阿离", "小清澈：你要叫我哥哥", 1)
    writes, why = ag3._admit(t, "private:1001")
    case("describe 下纯文本调教语句仍走 teach 分支",
         len(writes) == 1 and writes[0][0] == "assistant", f"{writes} {why}")

    # 非文本的描述串不能被当成调教语句（否则一个表情能改人设）
    nt = mkmsg("阿离", "[小清澈：你要叫我哥哥]", 2, kind="nontext")
    writes2, _ = ag3._admit(nt, "private:1001")
    case("非文本描述串不参与调教解析",
         len(writes2) == 1 and writes2[0][0] == "user", f"{writes2}")

    # 描述串为空白（解析不出占位文本）→ 跳过，不能写空消息进上下文
    # 注意：这里必须用「非空但 strip 后为空」的串。真正 content == "" 的消息
    # 在 _admit 开头那条 `if not m.content` 就被静默丢掉了，根本走不到非文本分支。
    blank = mkmsg("阿离", "   ", 3, kind="nontext")
    writes3, why3 = ag3._admit(blank, "private:1001")
    case("空白描述的非文本仍跳过", writes3 == [] and "没有可用描述" in why3, f"{why3!r}")

    empty = mkmsg("阿离", "", 4, kind="nontext")
    writes4, why4 = ag3._admit(empty, "private:1001")
    case("content 为空的条目被静默丢弃（不写理由）", writes4 == [] and why4 == "",
         f"{writes4} {why4!r}")


if __name__ == "__main__":
    raise SystemExit(main())