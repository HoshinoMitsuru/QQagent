# -*- coding: utf-8 -*-
"""
test_text_port.py — 离线冒烟测试：验证「小清澈3.0.js → PC 端」移植的三项文本能力。

不碰 QQ、不联网、不发送任何消息。跑法：

    python -X utf8 test_text_port.py

覆盖：
    1) 防抖聚合：连发多条 → 合并成一次模型调用；等待时长与插件的自适应算法一致
    2) 调教回插：调教条目立即写入后，被压在缓冲里的更早用户消息仍排在它前面
    3) 上下文持久化：按会话隔离、存档/重载一致、损坏文件不致命
    4) 连续对话：开口即激活、活跃期续期、超时自动退出
    5) 端到端（dry-run）：Agent.step() 只入队，flush() 才结算
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agent as A  # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"    {'✓' if cond else '✗'} {name}" + (f"   {extra}" if extra else ""))


def make_cfg(tmpdir: str) -> dict:
    """一份完全不联网、状态写到临时目录的配置。"""
    cfg = A._deep_merge(A.DEFAULTS, {
        "persist": {"file": os.path.join(tmpdir, "conversations.json")},
        # 轮转状态也要隔离：它会记「哪些会话建过基线」，
        # 写到真实文件里会污染下一次运行（实测踩过）。
        "discovery": {"state_file": os.path.join(tmpdir, "rotation.json")},
    })
    cfg["llm"]["system_prompt"] = "（测试用 system prompt）"
    return cfg


# ------------------------------------------------------------------ 1. 防抖聚合
def test_debouncer(cfg: dict) -> None:
    print("\n[1] 防抖聚合（移植自 setupDebounceTimer）")
    d = A.Debouncer(cfg)

    check("默认基础等待 = 5s", d.base == 5000.0)

    t1 = time.time()
    w1 = d.add(A.PendingMessage("小明", "在吗", "k1", 1), "s")
    check("首条进入缓冲", len(d.pending) == 1 and not d.ready)
    # 插件的习惯初值是 lastTime = now-120s → 首条被判为冷启动，turns=1，惩罚 6000*(1-1/4)=4500
    check("首条等待 = 基础 5s + 冷启动 4.5s", abs(w1 - 9.5) < 0.01, f"实测 {w1}s")

    t2 = time.time()
    w2 = d.add(A.PendingMessage("小明", "你在干嘛", "k2", 2), "s")
    check("第二条继续进缓冲", len(d.pending) == 2)
    check("间隔很短 → 冷启动惩罚归零，只等基础 5s", abs(w2 - 5.0) < 0.01, f"实测 {w2}s")
    # 防抖的核心：「收到新消息就清掉旧定时器、从新消息重新起算」
    check("到期时间按最后一条重算（不是叠加）",
          abs((d.due_at - t2) - 5.0) < 0.2 and d.due_at < t1 + 9.5,
          f"due-t2={d.due_at - t2:.2f}s")

    # 习惯性额外停顿：模拟「上一轮间隔 14s」→ (14-5)*0.3 = 2.7s 额外等待（EMA）
    d3 = A.Debouncer(cfg)
    d3._habits["小明"] = {"last_ms": (time.time() - 14.0) * 1000.0, "extra": 0.0, "turns": 0}
    w3 = d3.add(A.PendingMessage("小明", "嗯", "k3", 1), "s")
    check("间隔 14s 的节奏 → 额外等待 2.7s（EMA 0.3）", abs(w3 - 7.7) < 0.2, f"实测 {w3}s")

    batch = d.take()
    check("结算后缓冲清空、内容完整",
          len(batch) == 2 and [p.content for p in batch] == ["在吗", "你在干嘛"])
    check("结算后 ready=False", not d.ready)

    # 关闭防抖 → 立即到期
    cfg_off = A._deep_merge(cfg, {"aggregate": {"enabled": False}})
    d2 = A.Debouncer(cfg_off)
    d2.add(A.PendingMessage("小明", "你好", "k9", 1), "s")
    check("enabled=false 时立即到期", d2.ready)


# ------------------------------------------------------------------ 1b. 两档自适应
def test_adaptive_base(cfg: dict) -> None:
    print("\n[1b] 基础等待两档自适应（连续 3 轮单条 → 2s）")
    d = A.Debouncer(cfg)
    check("初始为常规档 5s", d.current_base("s1") == 5000.0)

    d.note_round("s1", 1)
    d.note_round("s1", 1)
    check("连续 2 轮单条还没降档", d.current_base("s1") == 5000.0)
    check("计数进行中 2/3", d.summary("s1").endswith("2/3）"), d.summary("s1"))

    d.note_round("s1", 1)
    check("第 3 轮单条 → 降到 2s", d.current_base("s1") == 2000.0)
    check("另一个会话不受影响（按会话独立）", d.current_base("s2") == 5000.0)

    # 快档下的实际等待：新用户 + 冷启动 = 2000+4500
    w = d.add(A.PendingMessage("小红", "嗯", "kf", 1), "s1")
    check("快档 + 冷启动 = 6.5s", abs(w - 6.5) < 0.01, f"实测 {w}s")

    d.note_round("s1", 3)
    check("出现 3 条连发 → 回到 5s 且计数归零",
          d.current_base("s1") == 5000.0 and d.summary("s1").endswith("0/3）"), d.summary("s1"))

    d.note_round("s1", 1)
    d.note_round("s1", 1)
    check("重新计数（2/3 而非直接再降档）",
          d.current_base("s1") == 5000.0 and d.summary("s1").endswith("2/3）"), d.summary("s1"))


# ------------------------------------------------------------------ 2. 调教回插
def test_teach_reorder(cfg: dict) -> None:
    print("\n[2] 调教条目的因果回插（push_before_teach）")
    h = A.History(max_entries=40, system_prompt="sp")

    seq_a = h.next_seq()                     # 缓冲里的用户消息先占号
    seq_b = h.next_seq()
    h.push("assistant", "我在这儿呢", source="teach", teach=True)   # 调教立即写入
    h.push_before_teach(min(seq_a, seq_b), "user", "【小明】：在吗\n【小明】：你在干嘛")

    roles = [(it["role"], it["content"][:12]) for it in h._items]
    check("用户消息排在调教条目之前", roles[0][0] == "user", f"{roles}")
    check("调教条目仍在列表里", any(it.get("teach") for it in h._items))
    msgs = h.build_messages()
    check("build_messages 首条是 system", msgs[0]["role"] == "system")
    check("元数据不上行", all(set(m) == {"role", "content"} for m in msgs))

    h2 = A.History(max_entries=40, system_prompt="")
    h2.push("assistant", "教1", teach=True)
    h2.push("assistant", "教2", teach=True)
    check("drop_last_teach 摘的是最后一条", h2.drop_last_teach() == "教2")
    check("clear_teach 返回条数", h2.clear_teach() == 1)

    h3 = A.History(max_entries=3, system_prompt="")
    for i in range(6):
        h3.push("user", f"m{i}")
    check("按条数裁剪保留最近的", [it["content"] for it in h3._items] == ["m3", "m4", "m5"])


# ------------------------------------------------------------------ 3. 持久化
def test_persist(cfg: dict) -> None:
    print("\n[3] 上下文持久化（按会话隔离）")
    s1 = A.ConversationStore(cfg)
    check("文件还不存在时不报错", not os.path.isfile(cfg["persist"]["file"]))

    ha = s1.history("private:小明")
    ha.push("user", "你好", source="incoming")
    ha.push("assistant", "嗨～", source="auto")
    s1.history("group:桌游群").push("user", "【小红】：开团吗")
    s1.activate_continuous("private:小明", time.time())
    s1.save(force=True)

    check("文件已落盘", os.path.isfile(cfg["persist"]["file"]))
    raw = json.load(open(cfg["persist"]["file"], encoding="utf-8"))
    check("两个会话各自独立", set(raw["scopes"]) == {"private:小明", "group:桌游群"})

    s2 = A.ConversationStore(cfg)
    check("重载后消息条数一致", len(s2.history("private:小明")) == 2)
    check("重载后群会话未串味", len(s2.history("group:桌游群")) == 1)
    check("重载后内容一致",
          s2.history("private:小明")._items[1]["content"] == "嗨～")
    check("重载后 seq 接着涨", s2.history("private:小明").next_seq() == 3)
    check("重载后连续对话仍是激活态",
          s2.is_continuous("private:小明", time.time(), 1800))

    check("forget 能删掉一个会话", s2.forget("group:桌游群"))
    s3 = A.ConversationStore(cfg)
    check("forget 的结果已落盘", "group:桌游群" not in s3._book)

    # 损坏文件：不崩、自动备份
    with open(cfg["persist"]["file"], "w", encoding="utf-8") as f:
        f.write("{ 这不是合法 JSON")
    s4 = A.ConversationStore(cfg)
    check("损坏文件不致命（从空开始）", len(s4._book) == 0)
    check("损坏文件已备份", os.path.isfile(cfg["persist"]["file"] + ".bad"))


# ------------------------------------------------------------------ 4. 连续对话
def test_continuous(cfg: dict) -> None:
    print("\n[4] 连续对话状态机")
    s = A.ConversationStore(cfg)
    now = time.time()
    check("初始未激活", not s.is_continuous("private:小明", now, 1800))
    s.activate_continuous("private:小明", now)
    check("激活后命中", s.is_continuous("private:小明", now + 10, 1800))
    s.refresh_continuous("private:小明", now + 1000)
    check("续期后超时窗口顺延", s.is_continuous("private:小明", now + 2000, 1800))
    check("超过窗口自动退出", not s.is_continuous("private:小明", now + 1000 + 1801, 1800))
    check("退出后状态已落为 inactive",
          not s._book["private:小明"]["cont"]["active"])
    check("未激活时 refresh 不起作用",
          s.refresh_continuous("private:小明", now) is False)


# ------------------------------------------------------------------ 5. 端到端 dry-run
class FakeQQ:
    """假的 QQ 窗口：喂进去什么就按顺序吐出来。"""

    def __init__(self, title="小明", is_group=False):
        self.dialog_title = title
        self.is_group = False
        self.member_count = 0
        self.ml_list = object()
        self._msgs: list[A.Message] = []
        self._seen: dict[str, set] = {}     # scope -> 已读 key 集合（与真实实现一样按会话隔离）
        self.win = None                      # 发现路径不用它（本测试不跑 discover）

    def refresh_layout(self, force: bool = False) -> None:
        pass

    def _seen_of(self, scope: str = "") -> set:
        return self._seen.setdefault(scope or "", set())

    def has_seen(self, scope: str = "") -> bool:
        return (scope or "") in self._seen

    def baseline(self, skip_last: int = 0, scope: str = "") -> int:
        target = self._msgs[: len(self._msgs) - skip_last] if skip_last > 0 else self._msgs
        d = self._seen_of(scope)
        for m in target:
            d.add(m.key)
        return len(target)

    def feed(self, sender: str, content: str, direction="other", kind="text") -> None:
        self._msgs.append(A.Message(
            sender=sender, content=content, direction=direction,
            key=f"k{len(self._msgs)}", kind=kind))

    def read_messages(self, limit: int = 12) -> list[A.Message]:
        return self._msgs[-limit:]

    def split_new(self, msgs, scope: str = "") -> list[A.Message]:
        d = self._seen_of(scope)
        fresh = [m for m in msgs if m.key not in d]
        for m in msgs:
            d.add(m.key)
        return fresh

    def send_text(self, text: str) -> bool:
        raise AssertionError("dry-run 不应该走到发送")


def test_end_to_end(cfg: dict) -> None:
    print("\n[5] 端到端（dry-run，不联网不发送）")
    ag = A.Agent(cfg, dry_run=True)
    ag.qq = FakeQQ()
    ag.scope = ag.current_scope()

    ag.qq.feed("小明", "在吗")
    ag.qq.feed("小明", "你在干嘛")
    ag.qq.feed("小明", "图片一张", kind="nontext")

    n = ag.step()
    check("step 只入队，不调用模型", n == 2 and len(ag.deb.pending) == 2)
    check("非文本被跳过", not any(p.content == "图片一张" for p in ag.deb.pending))
    check("未到点时不结算", ag.flush() is False)

    did = ag.flush(force=True)
    hist = ag.store.history(ag.scope)
    check("flush 结算成功", did and not ag.deb.pending)
    check("合并成一条 user 消息（含双方格式）",
          len(hist) == 1 and "【小明】：在吗" in hist._items[0]["content"]
          and "你在干嘛" in hist._items[0]["content"])
    check("dry-run 不产生 assistant 回复", len(hist) == 1)

    # 调教：立即写入，不进缓冲
    ag.qq.feed("小明", "小清澈：我今天超开心的")
    ag.step()
    check("调教不触发模型", len(ag.deb.pending) == 0)
    check("调教以 assistant 写入并带标记",
          hist._items[-1]["role"] == "assistant" and hist._items[-1]["teach"])

    # 连续对话：dry-run 下不激活（因为没真的调模型）
    check("dry-run 不会激活连续对话", not ag._continuous_now(ag.scope))

    # 换会话 → 自动切 scope 并给新会话建基线
    ag.qq.dialog_title = "桌游群"
    ag.qq.is_group = True
    ag.step()
    check("换会话后 scope 跟着变", ag.scope == "group:桌游群")
    ag.qq.feed("小红", "开团吗")
    ag.step()
    check("新会话独立缓冲", len(ag.deb.pending) == 1)
    check("旧会话上下文没被污染", len(ag.store.history("private:小明")) == 2)

    # 两档自适应走完整链路：连续 3 轮「单条成一轮」→ 降档；出现连发 → 回档
    ag.flush(force=True)                      # 先把「开团吗」结算掉，别混进单条计数
    for i in range(3):
        ag.qq.feed("小红", f"单条第 {i} 轮")
        ag.step()
        ag.flush(force=True)
    check("连续 3 轮单条 → 基础等待降到 2s（端到端）",
          ag.deb.current_base(ag.scope) == 2000.0, ag.deb.summary(ag.scope))
    ag.qq.feed("小红", "又开始连发 1")
    ag.qq.feed("小红", "又开始连发 2")
    ag.step()
    ag.flush(force=True)
    check("出现连发 → 基础等待回到 5s（端到端）",
          ag.deb.current_base(ag.scope) == 5000.0, ag.deb.summary(ag.scope))


# ------------------------------------------------------------------ main
def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="pcagent-test-")
    print("=" * 72)
    print(f"离线冒烟测试（临时目录 {tmpdir}）")
    print("=" * 72)
    try:
        cfg = make_cfg(tmpdir)
        test_debouncer(make_cfg(tmpdir))
        test_adaptive_base(cfg)
        test_teach_reorder(cfg)
        test_persist(cfg)
        test_continuous(cfg)
        test_end_to_end(cfg)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    print("\n" + "=" * 72)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for name in FAIL:
            print(f"  ✗ {name}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
