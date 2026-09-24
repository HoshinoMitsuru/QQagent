# -*- coding: utf-8 -*-
"""
bench_llm.py — 模型侧基准：不同「合并批次大小」下的生成耗时与回复长度

为什么需要它
------------
防抖等待窗口从 2~5s 拉长到 10~30s 之后，一次回复要覆盖更多条消息，
回复也会变得更长 —— 而模型耗时是并发量估算里最大的单项，必须实测而不是猜。

做法：直接构造 `flush()` 里同款的合并文本（`【发送者】：内容` 换行拼接），
      用同一个 LLMClient 调用，测「纯生成耗时」与「回复字数」。
      已绕过 `min_llm_interval_seconds` 的串行限流，拿到的是干净的单次延迟。

**不碰 QQ、不抢前台、不发送任何消息。**

用法：
    python -X utf8 bench_llm.py                # 每档 3 轮
    python -X utf8 bench_llm.py --rounds 5
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

import agent  # noqa: E402

SENDER = "光みつる"

# 一条私聊用户「一口气说完」的典型内容池，按批次大小取前 N 条
POOL = [
    "哥，在吗",
    "我今天有点累",
    "你说我该不该继续熬夜改代码",
    "算了，还是先睡吧",
    "对了，明天你有空吗",
    "想跟你聊会儿天",
]


def make_batch(n: int) -> str:
    """复刻 flush() 的合并格式。"""
    return "\n".join(f"【{SENDER}】：{c}" for c in POOL[:n])


def run_one(llm, cfg, n: int) -> tuple[float, int, str]:
    hist = agent.History(
        max_entries=int(cfg["chat"].get("max_history_entries") or 40),
        system_prompt=cfg["llm"].get("system_prompt") or "",
    )
    hist.push("user", make_batch(n), source="incoming")
    llm._last_call = 0.0          # 绕过 min_llm_interval 的串行限流，测纯延迟
    t = time.perf_counter()
    reply = llm.chat(hist.build_messages())
    return time.perf_counter() - t, len(reply or ""), (reply or "")


def main() -> int:
    ap = argparse.ArgumentParser(description="模型侧基准（不碰 QQ、不发送）")
    ap.add_argument("--rounds", type=int, default=3)
    args = ap.parse_args()

    cfg = agent.load_config()
    llm = agent.LLMClient(cfg)
    if not llm.ready:
        print("[X] 没有可用密钥，无法测模型连通性。")
        return 2

    print("=" * 78)
    print(f"模型侧基准　model={cfg['llm'].get('model')}　"
          f"max_tokens={cfg['llm'].get('max_tokens')}　每档 {args.rounds} 轮")
    print("（已绕过 min_llm_interval 串行限流，测的是纯生成延迟）")
    print("=" * 78)

    rows = []
    for n in (1, 2, 4, 6):
        lats, chars = [], []
        sample = ""
        for i in range(args.rounds):
            try:
                dt, ln, txt = run_one(llm, cfg, n)
            except Exception as exc:                       # noqa: BLE001
                print(f"[批次 {n} 条] 第 {i+1} 轮失败：{exc}")
                continue
            lats.append(dt)
            chars.append(ln)
            sample = txt
        if not lats:
            print(f"\n[批次 {n} 条] 全部失败")
            continue
        avg_lat = statistics.fmean(lats)
        avg_chr = statistics.fmean(chars)
        rows.append((n, avg_lat, avg_chr, min(lats), max(lats)))
        print(f"\n[批次 {n} 条]  输入 {len(make_batch(n))} 字")
        print(f"    耗时  min={min(lats):.2f}s  avg={avg_lat:.2f}s  max={max(lats):.2f}s")
        print(f"    回复  avg={avg_chr:.0f} 字   （≈{avg_chr / avg_lat:.1f} 字/秒）")
        print(f"    样例：{sample[:90]}")

    if not rows:
        return 1

    print("\n" + "=" * 78)
    print("汇总（用于并发量估算）")
    print("=" * 78)
    print(f"{'批次':<8}{'单次耗时':<14}{'回复字数':<12}{'相对单条':<10}")
    base = rows[0][1]
    for n, lat, ch, _lo, _hi in rows:
        print(f"{n:<8}{lat:<14.2f}{ch:<12.0f}{lat / base:<10.2f}x")
    print("\n提示：长等待窗口（10~30s）会让批次变大、回复变长，")
    print("      单次模型耗时随之上升 —— 但「回复次数」会同步下降，净效果要看两者乘积。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
