# -*- coding: utf-8 -*-
"""
probe_two_accounts.py — 本机同时登录两个 QQ 时，能否把「窗口 / 进程」和「QQ 号」对上？

背景：
    用户在本机本地桌面上同时登录了主号与小号。R4 要把小号托管到隐藏桌面，
    前提是先能**可靠区分**两个实例。本脚本回答三个问题：
      Q1 命令行能不能区分？（已知不能：两个主进程命令行逐字相同）
      Q2 UIA 树上能不能读到「本实例自己」的 QQ 号？
      Q3 读不到的话，有什么**不依赖点击**的替代判据？（会话列表指纹 / 窗口几何）

本脚本只读：
    - 不点击、不输入、不发送、不抢前台
    - 只做窗口枚举 + UIA 属性读取 + 树遍历

用法：
    python probe_two_accounts.py                 # 全量扫描
    python probe_two_accounts.py --limit 6000    # 限制每窗口扫描节点数
    python probe_two_accounts.py --outline       # 额外 dump 每个窗口前 3 层结构

产出：archive/two-accounts.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
# 本脚本在 archive/ 下，但 agent / app 在上一级。
# 直接 `python archive/xxx.py` 时 sys.path[0] 是 archive/，必须手动把仓库根加进来。
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent as A  # noqa: E402
import app.winmsg as W  # noqa: E402

OUT_JSON = os.path.join(HERE, "two-accounts.json")

# 形如 QQ 号：5~12 位纯数字
RE_UIN = re.compile(r"^\d{5,12}$")
# 形如「QQ 123456」「QQ号:123456」
RE_QQ_TEXT = re.compile(r"(?:QQ|QQ号|账号|uin|UIN)\s*[:：]?\s*(\d{5,12})", re.I)
# class / AutomationId 里值得单独拎出来的关键词
KEY_HINTS = (
    "profile", "avatar", "uid", "uin", "account", "self", "mine",
    "nickname", "nick", "user-info", "userinfo", "header",
)


def short(ctrl, depth: int = 0) -> dict:
    return {
        "depth": depth,
        "type": A._ctype(ctrl),
        "class": A._cls(ctrl),
        "aid": A._aid(ctrl),
        "name": A._name(ctrl),
        "rect": list(A._rect(ctrl)),
        "vis": A._visible(ctrl),
    }


def find_qq_main_windows() -> list[dict]:
    """找所有 QQ 主窗口（class=Chrome_WidgetWin_1 且 Name='QQ' 且可见）。"""
    out: list[dict] = []
    root = A.auto.GetRootControl()
    for w in root.GetChildren():
        try:
            if (w.ClassName or "") != "Chrome_WidgetWin_1":
                continue
            if (w.Name or "") != "QQ":
                continue
            if not w.IsEnabled:
                continue
        except Exception:
            continue
        hwnd = getattr(w, "NativeWindowHandle", 0) or 0
        r = list(A._rect(w))
        # 主窗口尺寸一定够大；QQ 会开一堆 32x37 的隐藏壳窗口
        if r[2] - r[0] < 300 or r[3] - r[1] < 200:
            continue
        out.append({"hwnd": hwnd, "rect": r, "ctrl": w})
    return out


def scan_window(win: dict, limit: int) -> dict:
    """只读扫一棵树，收集三类证据。"""
    root = win["ctrl"]
    res: dict = {
        "hwnd": win["hwnd"],
        "rect": win["rect"],
        "pid": W.pid_of(win["hwnd"]) if win["hwnd"] else 0,
        "uin_like": [],       # Q2 主证据：Name 就是纯数字
        "qq_text": [],        # Q2 次证据：Name 里带「QQ 123456」
        "key_nodes": [],      # 关键词节点（自己的头像 / 昵称 / 资料卡挂点）
        "sessions": [],       # Q3 替代判据：会话列表昵称指纹
        "scanned": 0,
    }

    n = 0
    for ctrl, d in A.iter_bfs(root, 30, limit=limit):
        n += 1
        nm = (A._name(ctrl) or "").strip()
        cls = A._cls(ctrl) or ""
        aid = A._aid(ctrl) or ""

        if nm and RE_UIN.match(nm):
            if len(res["uin_like"]) < 40:
                res["uin_like"].append(short(ctrl, d))
        if nm:
            m = RE_QQ_TEXT.search(nm)
            if m and len(res["qq_text"]) < 40:
                s = short(ctrl, d)
                s["parsed"] = m.group(1)
                res["qq_text"].append(s)
        low = (cls + " " + aid).lower()
        if any(k in low for k in KEY_HINTS) and len(res["key_nodes"]) < 80:
            if nm or aid:
                res["key_nodes"].append(short(ctrl, d))
    res["scanned"] = n

    # 会话列表昵称（作为实例指纹）
    try:
        lst = None
        for ctrl, _d in A.iter_bfs(root, 22, limit=limit):
            if "recent-contact-list" in (A._cls(ctrl) or ""):
                lst = ctrl
                break
        if lst is not None:
            for item in A._kids(lst):
                if "recent-contact-item" not in (A._cls(item) or ""):
                    continue
                nick = ""
                for node in A._kids(item):
                    if "item__info" in (A._cls(node) or ""):
                        for ch in A._kids(node):
                            if A._ctype(ch) == "TextControl":
                                t = (A._name(ch) or "").strip()
                                if t:
                                    nick = t
                                    break
                if nick and len(res["sessions"]) < 12:
                    res["sessions"].append(nick)
    except Exception as e:
        res["sessions_error"] = f"{type(e).__name__}: {e}"

    return res


def outline(win: dict, maxdepth: int = 3) -> list[dict]:
    """前几层结构概览，用来找「自己的头像/昵称」挂在哪。"""
    out: list[dict] = []
    for ctrl, d in A.iter_bfs(win["ctrl"], maxdepth, limit=400):
        if d <= maxdepth:
            out.append(short(ctrl, d))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=9000, help="每个窗口最多扫多少节点")
    ap.add_argument("--outline", action="store_true", help="额外 dump 前 3 层结构")
    args = ap.parse_args()

    t0 = time.time()
    wins = find_qq_main_windows()
    print(f"[i] 找到 QQ 主窗口 {len(wins)} 个")
    if not wins:
        print("[X] 没有可见的 QQ 主窗口")
        return 2

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "question": "两个 QQ 实例能否各自读出自己的 QQ 号？",
        "windows": [],
    }

    for i, w in enumerate(wins):
        print(f"\n=== 窗口[{i}] hwnd={w['hwnd']} pid={W.pid_of(w['hwnd'])} rect={w['rect']} ===")
        info = scan_window(w, args.limit)
        if args.outline:
            info["outline"] = outline(w)
        report["windows"].append(info)
        print(f"  扫到 {info['scanned']} 节点")
        print(f"  Name 为纯数字(5~12位)的节点: {len(info['uin_like'])}")
        for s in info["uin_like"][:8]:
            print(f"     num={s['name']!r} class={s['class']!r} aid={s['aid']!r} d={s['depth']}")
        print(f"  Name 含「QQ+数字」的节点: {len(info['qq_text'])}")
        for s in info["qq_text"][:8]:
            print(f"     text={s['name'][:40]!r} -> {s['parsed']} class={s['class']!r} d={s['depth']}")
        print(f"  关键词节点: {len(info['key_nodes'])}")
        for s in info["key_nodes"][:10]:
            print(f"     {s['class'][:34]!r} aid={s['aid'][:20]!r} name={s['name'][:30]!r}")
        print(f"  会话列表前 12 个昵称: {info['sessions']}")

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[OK] 报告写入 {OUT_JSON}（{time.time() - t0:.1f}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
