# -*- coding: utf-8 -*-
"""
test_live_chain.py — 「发现 → 占位 → 5s → 总读取 → 生成 → (可选)发送」的**真机**联调

## 为什么需要一个真机脚本

`test_discovery.py` 把 QQ 和模型全 fake 掉了，它能证明**判定逻辑**对，
但证明不了下面这三件事 —— 而它们恰恰最容易在真机上翻车：

  1. `_ensure_session` 的坐标点击**真的能切到那个会话**（锁屏/最小化就切不过去）；
  2. `read_messages` 在**切过去之后**真的能读到那个会话的正文
     （锚点 `ml_list` 切会话后可能还指着旧容器 → 必须 `force=True` 重扫）；
  3. 发送前那三道闸在真实控件结构上**复核通过**
     （标题 / QQ 号 / 会话签名，签名依赖 `ml-item` 的 AutomationId，fake 里是没有的）。

## 它怎么在不惊动真人的前提下跑完整条链路

「首次红点」这个输入没法凭空造 —— 总不能让别人给你发消息。所以本脚本的做法是
**伪造会话列表的读数**：拿到真实的 `list_sessions()` 结果，只把目标会话那一条的
`unread` 改成 3、预览改成标记串，其余原样。这样：

    discover 看到「首见 + 有未读」→ 真实占位入队
      → 真实的 _ensure_session（真点击、真切前台）
      → 真实的 read_messages / split_new / _admit
      → 真实的模型调用
      → 真实的 chat_signature 三道闸复核

唯一被替换的只有「谁发了新消息」这一个输入。**默认 `no_send=True`，不会真的发出去。**

## 它会动到什么 / 不会动到什么

| | |
| --- | --- |
| 会切换你的 QQ 会话（抢一次前台） | 结束时**自动切回原来那个** |
| 会写上下文 | 写到**临时目录**，不碰 `state/conversations.json` |
| 会动指纹快照 | 同上，临时 `rotation.json` |
| 会读真实聊天记录并调模型 | 是。这是它验证「总读取」的唯一办法 |
| 会发消息 | **默认不会**。加 `--send` 才会，且必须同时给 `--target` |

## 用法

    # 彩排：跑完整条链路，但不发送（推荐先跑这个）
    python -X utf8 test_live_chain.py --live

    # 指定目标会话（强烈建议指定，别让它自己挑）
    python -X utf8 test_live_chain.py --live --target "小清澈教研室"

    # 真发一条（会出现在你的聊天记录里，请自己确认目标选对了）
    python -X utf8 test_live_chain.py --live --target "小清澈教研室" --send

前置条件：QQ 已用 `--force-renderer-accessibility` 启动、窗口未最小化、**桌面未锁屏**。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
from dataclasses import replace

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


def hr(title: str = "") -> None:
    print("\n" + "=" * 78)
    if title:
        print(title)
        print("=" * 78)


def make_cfg(tmpdir: str) -> dict:
    """
    只隔离「会写东西」的那几个文件；**identity 用真的**。

    为什么 identity 不隔离：发送前第二道闸要按 QQ 号复核，而 QQ 号来自
    `state/uid-map.json` 缓存。隔离掉它就没号可核，那道闸等于没测。
    真库只读不写（除非目标会话本来没取过号），风险可控。
    """
    return A._deep_merge(A.DEFAULTS, {
        "persist": {"file": os.path.join(tmpdir, "conversations.json")},
        "discovery": {"state_file": os.path.join(tmpdir, "rotation.json")},
        "queue": {"jitter_seconds": 0.0},
    })


def push_focus_away(qq_hwnd: int) -> None:
    """把前台让给一个别的可见窗口，好让「切会话没有抢前台」这条断言真的有意义。"""
    if not A._is_foreground(qq_hwnd):
        return
    for w in A._enum_top_windows():
        r = w.get("rect") or [0, 0, 0, 0]
        if not w["visible"] or w["hwnd"] == qq_hwnd:
            continue
        if w["class"] in ("IME", "MSCTFIME UI", "Base_PowerMessageWindow",
                          "Electron_NotifyIconHostWindow"):
            continue
        if r[2] - r[0] < 400 or r[3] - r[1] < 300:
            continue
        if A.force_foreground(w["hwnd"], retries=2) and not A._is_foreground(qq_hwnd):
            time.sleep(0.3)
            return


def pick_target(sessions: list, cur: str, forced: str = ""):
    """
    挑一个目标会话。规则：

      1) `--target` 指定了就用它（找不到就直接报错退出，不猜）；
      2) 否则优先名字带「小清澈」的（通常是自建测试群）；
      3) 再否则取第一个不是「当前已打开」的会话。

    为什么要排掉当前打开的：`discover()` 会跳过它（交给 step 的实时路径），
    拿它当目标会看到「入队 0 条」然后误以为链路坏了。
    """
    if forced:
        for s in sessions:
            if s.display_name == forced:
                return s
        return None
    for s in sessions:
        if s.display_name != cur and s.looks_group and "小清澈" in s.display_name:
            return s
    for s in sessions:
        if s.display_name != cur:
            return s
    return None


def run(target_name: str, do_send: bool, unread: int) -> int:
    hr("真机联调：发现 → 总读取 → 生成" + ("（会真的发送）" if do_send else "（彩排，不发送）"))

    tmpdir = tempfile.mkdtemp(prefix="live-chain-")
    cfg = make_cfg(tmpdir)
    ag = A.Agent(cfg, no_send=not do_send)
    if not ag.qq.attach():
        code, ctx = A.diag_attach()
        print(f"  [SKIP] 没找到可用的 QQ 窗口：{A.EC.one_line(code)}")
        print(f"         {A.EC.get(code)['fixes'][0]}")
        print(f"         现场：{ctx}")

        return 2

    original = ag.qq.title_now()
    print(f"  当前打开 = {original!r}")
    if A._desktop_locked():
        print("  [SKIP] 桌面处于锁屏状态：发现扫描能跑，但切换会话是坐标点击，必然失败。")
        return 3

    # 把前台让给别的窗口。否则"切会话没有抢前台"这条断言没有意义
    # （QQ 本来就是前台的话，抢不抢都看不出来）。
    push_focus_away(ag.qq.hwnd)
    print(f"  前台窗口 = {A._foreground_title()!r}（QQ 在前台={A._is_foreground(ag.qq.hwnd)}）")

    # 真实会话列表（只读，不碰前台）
    real_list = Q.list_sessions           # 先留一份真身：下面替换后模块属性就指向 fake 了
    real = real_list(ag.qq.win)
    if not real:
        print("  [SKIP] 会话列表读不到任何会话")
        return 2
    print("  会话列表：")
    for s in real:
        flag = "（当前打开，会被跳过）" if s.display_name == original else ""
        print(f"    - {s.display_name!r} 未读={s.unread} 预览={s.summary[:24]!r} {flag}")

    target = pick_target(real, original, target_name)
    if target is None:
        print(f"  [SKIP] 找不到目标会话 {target_name!r}，或列表里除当前会话外没有别的可选项")
        return 2
    marker = "[联调标记]"
    print(f"\n  目标 = {target.display_name!r}（伪造未读={unread}，预览={marker!r}）"
          f"  群聊={target.looks_group}")

    # ---- 伪造这一条会话的读数：unread + 预览变化 ----
    # 注意：这里必须调上面存下来的 real_list，不能写 Q.list_sessions ——
    # 替换之后模块属性已经指向 fake_list 本身，写它就是一个无限递归。
    def fake_list(win):
        return [replace(s, preview_texts=(marker,), unread=unread)
                if s.display_name == target.display_name else s
                for s in real_list(win)]

    Q.list_sessions = fake_list

    # ---- 记录真实的会话切换（真切换照走，只是顺手记一笔） ----
    real_click = Q.switch_session
    clicks: list = []

    def logging_click(session, win, prefer_noop=True, timeout=6.0):
        clicks.append(session.display_name)
        return real_click(session, win, prefer_noop=prefer_noop, timeout=timeout)

    Q.switch_session = logging_click

    # ---- 建立「当前会话」的基线，避免实时路径干扰（与 run_forever 一致） ----
    ag.scope = ag.current_scope()
    ag.qq.baseline(scope=ag.scope)
    ag._rot().mark_baselined(ag.scope)
    print(f"  当前会话 scope = {ag.scope!r}")

    try:
        # ============================================ 1. 发现
        print("\n[1] 发现（只扫列表，不许碰前台）")
        n = ag.discover()
        case("发现到目标会话并占位入队", n >= 1, f"n={n}")
        case("发现阶段没有切会话", clicks == [], f"{clicks}")

        # 真机上跑的时候，队列里可能**同时**有别的会话 —— 这恰恰说明发现链路
        # 对真实到达的消息也生效。但会让后面的断言变得不确定（serve_queue 一次
        # 只处理一项，取到谁不一定），所以这里把非目标的项清掉，让本测试确定化。
        others = [sc for sc in ag.queue.scopes()
                  if (ag.queue.get(sc) or None) is not None
                  and ag.queue.get(sc).display_name != target.display_name]
        if others:
            print(f"  （真机期间另有 {len(others)} 个会话也进了队列：{others}"
                  f" → 本测试清掉它们以保持确定性）")
            for sc in others:
                ag.queue.drop(sc, aborted=True)

        item = None
        for sc in ag.queue.scopes():
            it = ag.queue.get(sc)
            if it is not None and it.display_name == target.display_name:
                item = it
                break
        case("队列里找到占位项", item is not None, f"{ag.queue.scopes()}")
        if item is None:
            print("  [ABORT] 没排上队，后面的步骤没法继续")
            return 1
        case("占位项不带正文（摘要不当上下文）", item.count == 0, f"count={item.count}")
        case("标记为待总读", item.pending_read is True)
        case("未读条数已记下", item.unread_hint == unread, f"{item.unread_hint}")

        # ============================================ 2. 总读取 + 生成
        print("\n[2] 静默窗关闭 → 总读取 → 生成（不发送）")
        wait = max(0.0, item.ready_at - time.time())
        if wait > 0:
            print(f"  等静默窗关闭（{wait:.1f}s）…")
            time.sleep(wait + 0.2)

        fg_before = A._fg_hwnd()
        t0 = time.time()
        ok1 = ag.serve_queue()
        dt = time.time() - t0
        case("阶段一返回 False（只读+生成，不发）", ok1 is False)
        case("真的切了一次会话", clicks == [target.display_name], f"{clicks}")
        case("切换到目标会话", ag.qq.title_now() == target.display_name,
             f"{ag.qq.title_now()!r}")
        # 这是 2026-09-11 改动的核心回归点：
        # 切会话走 InvokePattern（不需要前台），但它会**激活** QQ（Chromium 内部
        # SetFocus）→ 所以切换后必须把前台还给你原来的窗口（方案 C）。
        case("切会话后前台已归还给原来的窗口",
             A._fg_hwnd() == fg_before,
             f"before={fg_before!r} after={A._fg_hwnd()!r}"
             f"（QQ在前台={A._is_foreground(ag.qq.hwnd)}）")
        case("回复已备好", bool(item.prepared and item.reply),
             f"prepared={item.prepared} reply={item.reply!r}")

        rows = ag.store.history(item.scope).to_dict()
        users = [r for r in rows if r.get("role") == "user"]
        case("总读取拿到了对方的正文并写进上下文", len(users) >= 1,
             f"{[r.get('content') for r in rows][:6]}")
        if item.reply:
            print(f"\n  ── 模型回复（{len(item.reply)} 字，耗时 {dt:.1f}s）──")
            print(f"  {item.reply}")
            print("  ────────────────────────────────\n")
        case("模型回复非空", bool(item.reply), f"{item.reply!r}")

        # ============================================ 3. 发送（彩排 or 真发）
        if do_send:
            print("\n[3] 排到风控额度 → 真的发送")
            while ag.queue.budget.wait_seconds() > 0:
                time.sleep(min(1.0, ag.queue.budget.wait_seconds()))
            ok2 = ag.serve_queue()
            case("发送成功（三道闸复核通过）", ok2 is True)
        else:
            print("\n[3] 排到风控额度 → 彩排发送（三道闸复核，但点发送前收手）")
            ok2 = ag.serve_queue()
            case("彩排返回 True（已过①②闸并捕获签名）", ok2 is True)
            case("发送后指纹被重采（防自我叫醒）",
                 ag._rot().fp_of(target.display_name) is not None)

        case("队列已清空", len(ag.queue) == 0, f"{ag.queue.scopes()}")

    finally:
        # 无论如何都切回原来那个会话，别把用户的 QQ 留在别人聊天窗口上
        Q.list_sessions = real_list
        Q.switch_session = real_click
        try:
            cur = ag.qq.title_now()
            if cur != original:
                print(f"\n  回位：{cur!r} → {original!r}")
                for s in real:
                    if s.display_name == original:
                        real_click(s, ag.qq.win)
                        break
        except Exception as exc:
            print(f"  回位失败（请手动切回 {original!r}）：{exc}")
        shutil.rmtree(tmpdir, ignore_errors=True)

    hr()
    print(f"结果：{OK} 通过 / {FAIL} 失败")
    hr()
    return 0 if FAIL == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="真机联调：发现 → 总读取 → 生成 →（可选）发送")
    ap.add_argument("--live", action="store_true", help="真的跑（不加只打印说明）")
    ap.add_argument("--target", default="", help="目标会话显示名（强烈建议指定）")
    ap.add_argument("--send", action="store_true", help="真的把回复发出去（默认只为彩排）")
    ap.add_argument("--unread", type=int, default=3, help="伪造的未读条数（决定总读取保留最后几条）")
    args = ap.parse_args()

    if not args.live:
        print(__doc__)
        print("这是真机脚本，必须显式加 --live 才会执行。请先读一遍上面的『它会动到什么』。")
        return 0

    if args.send and not args.target:
        print("拒绝执行：--send 必须同时给 --target，不接受「自动挑一个然后发出去」。")
        return 2

    return run(args.target, args.send, args.unread)


if __name__ == "__main__":
    raise SystemExit(main())
