# -*- coding: utf-8 -*-
"""
bench.py — 只读性能基准

给「并发能力估算」提供真实基数：测量 UIA 侧的窗口附着、锚点扫描、消息读取耗时。

**本脚本绝不抢前台、绝不发任何按键、绝不发送消息** —— 全部是 UIA 只读取值。
所以可以随时跑，不会打断你正在做的事。

用法：
    python -X utf8 bench.py                 # 默认每项 5 轮
    python -X utf8 bench.py --rounds 20
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import agent  # noqa: E402  （agent.py 有 __main__ 守卫，import 不会跑主流程）


def stats(samples: list[float]) -> str:
    if not samples:
        return "无数据"
    lo, hi = min(samples), max(samples)
    avg = statistics.fmean(samples)
    p95 = sorted(samples)[min(len(samples) - 1, int(len(samples) * 0.95))]
    return f"min={lo*1000:6.1f}ms  avg={avg*1000:6.1f}ms  p95={p95*1000:6.1f}ms  max={hi*1000:6.1f}ms"


def main() -> int:
    ap = argparse.ArgumentParser(description="只读性能基准（不抢前台、不发送）")
    ap.add_argument("--rounds", type=int, default=5)
    args = ap.parse_args()

    cfg = agent.load_config()
    qq = agent.QQWindow(cfg)

    print("=" * 78)
    print("只读性能基准（全程不抢前台、不发按键、不发送消息）")
    print("=" * 78)

    t0 = time.perf_counter()
    if not qq.attach():
        print("[X] 找不到可见的 QQ 主窗口 —— 请把 QQ 主窗口显示出来（不要缩托盘/最小化）。")
        return 2
    attach_ms = (time.perf_counter() - t0) * 1000

    print(f"\n[会话] title={qq.dialog_title!r}  群聊={qq.is_group}  群人数={qq.member_count}")
    print(f"[锚点] 消息列表={'OK' if qq.ml_list is not None else '缺失'}  "
          f"输入框={'OK' if qq.editor is not None else '缺失'}  "
          f"发送按钮={'OK' if qq.send_btn is not None else '缺失'}")
    print(f"[附着] 首次 attach（含首次全量扫描）= {attach_ms:.1f}ms")

    # 1) 强制全量重扫锚点（最贵路径）
    full = []
    for _ in range(args.rounds):
        t = time.perf_counter()
        qq.refresh_layout(force=True)
        full.append(time.perf_counter() - t)
    print(f"\n[1] refresh_layout(force=True)  全量重扫   n={len(full)}")
    print(f"    {stats(full)}")

    # 2) 走缓存的刷新（主循环每轮都会调）
    cached = []
    for _ in range(args.rounds):
        t = time.perf_counter()
        qq.refresh_layout()
        cached.append(time.perf_counter() - t)
    print(f"\n[2] refresh_layout()  走缓存                n={len(cached)}")
    print(f"    {stats(cached)}")

    # 3) 读消息（主循环核心动作）
    reads, counts = [], []
    for _ in range(args.rounds):
        t = time.perf_counter()
        msgs = qq.read_messages()
        reads.append(time.perf_counter() - t)
        counts.append(len(msgs))
    print(f"\n[3] read_messages()  解析全部可视消息        n={len(reads)}")
    print(f"    {stats(reads)}")
    print(f"    解析条数 = {counts}")

    # 4) 输入框回读（发送前的校验动作）
    if qq.editor is not None:
        et = []
        for _ in range(args.rounds):
            t = time.perf_counter()
            qq.editor_text()
            et.append(time.perf_counter() - t)
        print(f"\n[4] editor_text()  输入框回读              n={len(et)}")
        print(f"    {stats(et)}")

    # 5) 发送按钮禁用态检查
    sd = []
    for _ in range(args.rounds):
        t = time.perf_counter()
        qq._send_disabled()
        sd.append(time.perf_counter() - t)
    print(f"\n[5] _send_disabled()  发送按钮状态检查      n={len(sd)}")
    print(f"    {stats(sd)}")

    total_cached = statistics.fmean(cached) + statistics.fmean(reads)
    print("\n" + "=" * 78)
    print(f"合计：走缓存的一轮「刷新 + 读消息」 ≈ {total_cached*1000:.1f}ms")
    print("（以上全部为 UIA 读取，未触碰前台窗口）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
