# -*- coding: utf-8 -*-
"""
test_pipeline.py — 端到端彩排：从「收到消息」到「准备发出」的完整链路

**不会真的发出任何消息**（Agent 用 no_send=True，最后一步只打日志）。
由于它会对真实 QQ 做只读操作（切会话/读签名），请确保 QQ 已打开。

覆盖的链路：
    构造一条对方消息
      → _ingest（准入判定 / 身份解析 / 5s 阈值合并判定）
      → flush（聚合成一个待回复项入队）
      → serve_queue ×2（第 1 次「读+生成」，第 2 次「切会话 → 三重复核 → 彩排止步」）

额外验证 §9 的「5s 阈值合并」两条分支都能在真实 Agent 上走通。

⚠️ 需要**已解锁**的桌面：坐标点击在锁屏/屏保下点不到 QQ，第 4 步会全部失败。

隔离措施：
    上下文写到一个**独立的** state 文件（跑完删掉），不污染真实会话记忆。

用法：
    python test_pipeline.py
    python test_pipeline.py --user 寂静挽歌
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import agent as A
import reply_queue as RQ
import qqid as Q

OK = FAIL = 0
TEST_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "state", "_pipeline_test.json")


def case(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def mkmsg(sender: str, content: str, key: str) -> A.Message:
    return A.Message(sender=sender, content=content, direction="other",
                     key=key, rect=(0, 0, 0, 0), kind="text", ts="")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default=None, help="模拟哪个会话发来的消息（默认用当前打开的会话）")
    args = ap.parse_args()

    cfg = A.load_config()
    cfg.setdefault("persist", {})["file"] = "state/_pipeline_test.json"
    cfg.setdefault("queue", {})["jitter_seconds"] = 0.0        # 让测试可复现
    cfg["queue"]["max_replies_per_minute"] = 60                # 测试里不卡风控
    cfg["queue"]["min_interval_seconds"] = 0.0
    # 发现/轮转状态同样要隔离：它会记「哪些会话建过基线」，
    # 写进真实文件会污染之后每一次运行（实测踩过）。
    cfg.setdefault("discovery", {})["state_file"] = "state/_pipeline_rotation_test.json"
    ROT_TEST = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "state", "_pipeline_rotation_test.json")

    if os.path.exists(TEST_STATE):
        os.remove(TEST_STATE)

    agent = A.Agent(cfg, no_send=True)
    if not agent.qq.attach():
        # 走与常驻完全相同的分流：这一步有三种根因（没启动 / 窗口不可见 / 无障碍没生效），
        # 处理动作完全不同 —— 直接把它们区分出来，别让人猜。
        code, ctx = agent.qq.diagnose_attach()
        A.diag(code, "彩排需要 QQ 主窗口可见", ctx)
        return 2

    agent.scope = agent.current_scope()
    kept = agent.qq.baseline(scope=agent.scope)
    name = args.user or agent.qq.dialog_title
    uin = agent._uids().uin_of(name)
    print("=" * 74)
    print("端到端彩排（不会真的发出）")
    print("=" * 74)
    print(f"[i] 当前会话 = {agent.qq.dialog_title!r}，假设消息来自 {name!r}（QQ {uin or '未取号'}）")
    print(f"[i] 忽略已有 {kept} 条历史；测试上下文写到 {TEST_STATE}")
    print(f"[i] scope = {agent.scope}")

    try:
        # ---------------- 1. 单条消息 → 入队 ----------------
        print("\n[1] 收到一条消息 → 入队")
        n = agent._ingest([mkmsg(name, "在吗，今天有空吗", "t:1")])
        case("消息被接纳", n == 1, f"got {n}")
        case("防抖缓冲里有 1 条", len(agent.deb.pending) == 1, f"{len(agent.deb.pending)}")

        agent.flush(force=True)
        case("已聚合为 1 个待回复项", len(agent.queue) == 1, f"{len(agent.queue)}")
        it = agent.queue.get(agent.scope)
        case("队列项带了 QQ 号", bool(it and it.uin), f"uin={it.uin if it else None}")
        case("队列项文本带发送者前缀",
             bool(it) and "在吗，今天有空吗" in it.texts[0], f"{it.texts if it else None}")

        # ---------------- 2. 无人排队时 → 不合并 ----------------
        print("\n[2] 无人排队（wait≈0）时补充消息 → close，另排一次")
        before = len(agent.queue)
        n = agent._ingest([mkmsg(name, "算了，晚上再说", "t:2")])
        case("判定为「另排一次」：队列项数不变", len(agent.queue) == before,
             f"{before} → {len(agent.queue)}")
        case("补充消息进了防抖缓冲（会单独回一次）",
             any("晚上再说" in p.content for p in agent.deb.pending),
             f"{[p.content for p in agent.deb.pending]}")

        # ---------------- 3. 有积压时 → 合并 ----------------
        print("\n[3] 前面有积压（wait > 5s）时补充消息 → merged，不额外回")
        t0 = time.time()
        # 手工在队首塞两个「别的会话」的积压项（ready 很晚，不会真的被服务）
        for k in range(2):
            agent.queue.items.insert(0, RQ.QueuedReply(
                scope=f"private:block{k}", display_name=f"占位{k}", uin=f"block{k}",
                texts=["x"], seqs=[0], first_at=t0 - 100,
                ready_at=t0 + 3600, hard_deadline=t0 + 3600))
        agent.deb.clear()                      # 先清掉上一步残留，避免干扰
        wait = agent.queue.remaining_wait(agent.scope)
        case(f"此时剩余等待 {wait:.1f}s > 阈值 5s", wait > agent.queue.merge_threshold,
             f"got {wait:.1f}")
        merged_before = len(agent.queue)
        n = agent._ingest([mkmsg(name, "对了，顺便问下明天呢", "t:3")])
        it = agent.queue.get(agent.scope)
        case("判定为 merged：队列项数不变", len(agent.queue) == merged_before,
             f"{merged_before} → {len(agent.queue)}")
        case("补充消息已并入待发上下文",
             bool(it) and any("明天呢" in x for x in it.texts), f"{it.texts if it else None}")
        case("merged 计数 +1", bool(it) and it.merged >= 1, f"{it.merged if it else None}")
        case("没有新建防抖缓冲（不会多回一次）", len(agent.deb.pending) == 0,
             f"{[p.content for p in agent.deb.pending]}")
        # 清掉占位项
        agent.queue.items = [x for x in agent.queue.items if not x.scope.startswith("private:block")]

        # ---------------- 4. 出队 → 生成 → 三重复核 → 彩排止步 ----------------
        print("\n[4] 出队结算：生成回复 + 切会话 + 三重复核（不发出）")
        it = agent.queue.get(agent.scope)
        print(f"    待发上下文共 {it.count} 条：")
        for x in it.texts:
            print(f"      - {x}")
        # 注意：合并会给这一项顺延 unit_cost（默认 3s），让后到的补充消息还有机会进来。
        # 测试里不想真等 3 秒，直接把到期时刻拨到过去。
        if it.ready_at > time.time():
            print(f"    [i] 合并顺延了 {it.ready_at - time.time():.1f}s，测试里直接视为到点")
            it.ready_at = time.time() - 1.0

        # 结算现在是**两阶段**的：第一次只做「读+生成」，排到风控额度才发送。
        # 拆开的理由：桌面 UI 上每个会话只有「最新一条消息的节选」，正文必须等到
        # 静默窗关闭后切过去总读取；而生成回复不占前台，可以早于风控放行做。
        ok1 = agent.serve_queue()
        case("第 1 次 serve_queue：只做「读 + 生成」，不发送", ok1 is False)
        case("回复已备好（prepared）",
             bool(it and it.prepared and it.reply), f"prepared={it.prepared if it else None}")
        case("队列里还留着这一项（等风控放行）", len(agent.queue) == 1, f"{len(agent.queue)}")

        ok = agent.serve_queue()
        case("第 2 次 serve_queue：走完并判定「已就绪发出」", ok is True)
        case("发出后队列已清空", len(agent.queue) == 0, f"{len(agent.queue)}")

        hist = agent.store.history(agent.scope)
        rows = hist.to_dict() if hasattr(hist, "to_dict") else []
        has_user = any(r.get("role") == "user" and "在吗，今天有空吗" in (r.get("content") or "")
                       for r in rows)
        has_bot = any(r.get("role") == "assistant" for r in rows)
        case("上下文里写入了对方的原话", has_user)
        case("上下文里写入了 AI 的回复（彩排模式也记账）", has_bot)
        head, _, tail = agent.scope.partition(":")
        case("scope 主键是号码而不是昵称", head in ("private", "group") and tail.isdigit(),
             f"scope={agent.scope}（当前打开的是{'群聊' if agent.qq.is_group else '私聊'}）")

        # ---------------- 5. 风控闸在真实 Agent 上也生效 ----------------
        print("\n[5] 风控闸：刚发完必须等满硬间隔")
        cfg2 = A.load_config()
        cfg2.setdefault("persist", {})["file"] = "state/_pipeline_test.json"
        a2 = A.Agent(cfg2, no_send=True)
        a2.qq.attach()
        t = time.time()
        a2.queue.served(t)
        case(f"硬间隔 {a2.queue.budget.min_interval:.1f}s 内 pick 不到东西",
             a2.queue.budget.wait_seconds(t + 0.1) > 0)
        a2.queue.submit("private:1", "X", "1", "hi", now=t, wait_seconds=0.0)
        case("风控未放行 → pick 返回 None", a2.queue.pick(t + 0.1) is None)
        case("硬间隔过后 → pick 成功", a2.queue.pick(t + a2.queue.budget.min_interval + 0.1)
             is not None)

    finally:
        try:
            agent.store.save(force=True)
        except Exception:
            pass
        for p in (TEST_STATE, TEST_STATE + ".tmp", ROT_TEST, ROT_TEST + ".tmp"):
            if os.path.exists(p):
                os.remove(p)
                print(f"\n[i] 已清理测试状态文件 {os.path.basename(p)}")

    print("\n" + "=" * 74)
    print(f"结果：{OK} 通过 / {FAIL} 失败")
    print("=" * 74)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
