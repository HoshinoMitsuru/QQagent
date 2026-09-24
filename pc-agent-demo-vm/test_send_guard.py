# -*- coding: utf-8 -*-
"""
test_send_guard.py — 实测「绝不发错人」的三道闸

**全程不发送任何消息**（不碰发送按钮、不碰回车），只验证判定逻辑对不对。
会抢前台（切换会话要点会话项），跑完归还。

三道闸（对应 `并发能力评估与优化方向.md` 的 D1）：
    ① 会话标题复核   —— 切过去之后，当前打开的是不是目标
    ② QQ 号复核      —— 标题只是备注名，必须拿 QQ 号再对一次
    ③ 会话签名复核   —— 写入前后签名必须一致，挡住「生成/写入期间被人切走」

其中 ③ 是最关键也最难测的：它要在「外部把会话切走」之后仍然检出不一致。
本脚本用真实 QQ 做这件事。

用法：
    python test_send_guard.py                 # 用默认两个会话
    python test_send_guard.py --a 0 --b 2     # 指定会话列表序号
"""

from __future__ import annotations

import argparse
import sys
import time

import agent as A
import qqid as Q

import reply_queue as RQ

OK = FAIL = 0


def case(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", type=int, default=None, help="会话 A 的序号（默认自动挑第一个私聊）")
    ap.add_argument("--b", type=int, default=None, help="会话 B 的序号（默认 A 的下一个）")
    args = ap.parse_args()

    win_desc, hwnd = Q.find_qq_main_window()
    if not hwnd:
        code, ctx = A.diag_attach()
        A.diag(code, "这道闸的实测需要 QQ 主窗口可见", ctx)
        return 2
    win = A.auto.ControlFromHandle(hwnd)
    qq = A.QQWindow(A.load_config())
    if not qq.attach():
        code, ctx = qq.diagnose_attach()
        A.diag(code, "附着失败", ctx)
        return 2

    print("=" * 72)
    print("发送前三道闸 · 实测")
    print("=" * 72)

    sessions = Q.list_sessions(win)
    if len(sessions) < 2:
        print(f"[X] 会话太少（{len(sessions)} 个），至少需要 2 个才能测「切走」")
        return 2
    ia = args.a if args.a is not None else 0
    ib = args.b if args.b is not None else (1 if ia != 1 else 2)
    sa, sb = sessions[ia], sessions[ib]
    print(f"[i] 会话 A = [{ia}] {sa.display_name!r}")
    print(f"[i] 会话 B = [{ib}] {sb.display_name!r}")

    prev_fg = A._fg_hwnd()

    try:
        # ---------------- 闸③ 基础：签名在同一个会话内稳定 ----------------
        print("\n[1] 会话签名在同一会话内是否稳定")
        Q.switch_session(sa, win)
        qq.refresh_layout(force=True)
        sig1 = qq.chat_signature()
        time.sleep(0.3)
        sig2 = qq.chat_signature()
        print(f"    sig = {sig1[0]!r} / 群聊={sig1[1]} / {len(sig1[2])} 条消息 ID")
        case("两次读取完全一致（没有假抖动）", sig1 == sig2, f"\n      {sig1}\n      {sig2}")
        case("标题 = 目标会话名", sig1[0] == sa.display_name,
             f"got {sig1[0]!r} want {sa.display_name!r}")
        case("消息 ID 非空且形如 18~19 位数字",
             bool(sig1[2]) and all(x.isdigit() and 15 <= len(x) <= 20 for x in sig1[2]),
             f"{sig1[2]}")
        sig_a = sig1

        # ---------------- 闸① 标题复核：切走之后必须能检出 ----------------
        print("\n[2] 切换会话后，标题复核必须失败")
        Q.switch_session(sb, win)
        time.sleep(0.6)
        qq.refresh_layout(force=True)
        title_now = qq.title_now()
        print(f"    切走后的标题 = {title_now!r}")
        case("标题复核检出不一致", title_now != sa.display_name,
             f"仍读到 {title_now!r}")
        case("标题复核与新会话一致", title_now == sb.display_name,
             f"got {title_now!r} want {sb.display_name!r}")

        # ---------------- 闸③ 核心：切走之后签名必须变化 ----------------
        print("\n[3] 切换会话后，会话签名必须变化（这是防错发的核心）")
        sig_b = qq.chat_signature()
        print(f"    A 的签名 = {sig_a[0]!r} … {sig_a[2][-2:]}")
        print(f"    B 的签名 = {sig_b[0]!r} … {sig_b[2][-2:]}")
        case("签名 A ≠ 签名 B", sig_a != sig_b,
             "两个会话签名相同 → 假通过风险！")

        # 模拟 guard：拿 A 的签名去复核当前（已是 B）的状态
        guard_pass = (qq.chat_signature() == sig_a)
        case("用 A 的签名复核 B → guard 应当判否", guard_pass is False)

        # ---------------- 签名可复现：切回去必须与原来一致 ----------------
        print("\n[4] 切回 A 之后，签名能不能复现（避免假阳性）")
        Q.switch_session(sa, win)
        time.sleep(0.6)
        qq.refresh_layout(force=True)
        sig_a2 = qq.chat_signature()
        print(f"    切回后 = {sig_a2[0]!r} … {sig_a2[2][-2:]}")
        case("切回后标题复原", sig_a2[0] == sa.display_name)
        if sa.selected or True:
            # 消息 ID 集合可能因为「我们自己刚看过」而完全一致；
            # 若中间有新消息进来导致变化，只要标题+群标记对得上也算合理
            case("切回后签名与首次一致（或至少有交集）",
                 sig_a2 == sig_a or bool(set(sig_a[2]) & set(sig_a2[2])),
                 f"\n      first={sig_a[2]}\n      again={sig_a2[2]}")

        # ---------------- 闸② QQ 号复核 ----------------
        print("\n[5] QQ 号复核")
        store = Q.UidStore()
        uin_a = store.uin_of(sa.display_name)
        uin_b = store.uin_of(sb.display_name)
        print(f"    {sa.display_name!r} → QQ {uin_a or '（未取号）'}")
        print(f"    {sb.display_name!r} → QQ {uin_b or '（未取号）'}")
        if uin_a and uin_b:
            case("两个会话的 QQ 号不同", uin_a != uin_b)
            item_a = RQ.QueuedReply(scope=f"private:{uin_a}", display_name=sa.display_name,
                                    uin=uin_a, texts=["x"], seqs=[0])
            item_b = RQ.QueuedReply(scope=f"private:{uin_b}", display_name=sb.display_name,
                                    uin=uin_b, texts=["x"], seqs=[0])
            # 手工复刻 _verify_target 的判定
            def verify(item):
                title = qq.title_now()
                if title != item.display_name:
                    return False
                uin = store.uin_of(title)
                if item.uin and uin and uin != item.uin:
                    return False
                return True

            case("当前是 A → 目标是 A 的项，通过", verify(item_a) is True)
            case("当前是 A → 目标是 B 的项，拒绝", verify(item_b) is False)
        else:
            print("    [i] 有会话没取号，跳过（先跑 python qqid.py --enroll-all）")

    finally:
        time.sleep(0.2)
        if prev_fg:
            A.restore_foreground(prev_fg)

    print("\n" + "=" * 72)
    print(f"结果：{OK} 通过 / {FAIL} 失败")
    print("=" * 72)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
