# -*- coding: utf-8 -*-
"""
probe_profile.py — 测试「打开资料卡能否拿到 QQ 号」

背景：
    UIA 侧的会话列表项（recent-contact-item）及全部后代 **AutomationId 全为空**，
    各种 UIA 属性里也没有 QQ 号（见 uid-probe.json）。剩下能试的非 CDP 通路只有一条：
    点开会话 → 打开资料卡 → 看新出现的节点里有没有 QQ 号。

本脚本会**短暂抢前台**（模拟点击会话项与资料卡按钮），因此：
    - 运行前会记录当前前台窗口，结束后归还
    - **绝不发送任何消息**（不碰输入框、不碰发送按钮）

用法：
    python probe_profile.py --dry         # 只报告将要做什么，不动手
    python probe_profile.py               # 实际执行（抢占前台 ~3 秒）
    python probe_profile.py --index 1     # 指定会话列表第几项（默认 0）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import agent as A
import probe_uid as P

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "profile-probe.json")

RE_UIN = re.compile(r"^\d{5,12}$")


def snapshot(win) -> dict:
    """把当前树的 (class, aid, name) 打平成集合，方便做前后 diff。"""
    seen: dict = {}
    for ctrl, d in A.iter_bfs(win, 26, limit=15000):
        key = f"{A._ctype(ctrl)}|{A._cls(ctrl)}|{A._aid(ctrl)}|{A._name(ctrl)}"
        seen[key] = {"depth": d, "rect": list(A._rect(ctrl)),
                     "class": A._cls(ctrl), "name": A._name(ctrl),
                     "type": A._ctype(ctrl), "aid": A._aid(ctrl)}
    return seen


def find_by_class(win, token: str, visible_only: bool = True):
    for ctrl, _d in A.iter_bfs(win, 26, limit=15000):
        if token in A._cls(ctrl):
            if not visible_only or A._visible(ctrl):
                return ctrl
    return None


def items_of_list(lst) -> list:
    return [c for c in A._kids(lst) if P.CLS_RECENT_ITEM in A._cls(c)]


def item_label(item) -> dict:
    """
    会话项在当前 UIA 树里**全部可用**的标识信息。

    实测结构（probe-tree.txt:96-105）：
        recent-contact-item
          └ item__content
              ├ item__avatar  (GroupControl，Name 空、aid 空 —— 拿不到头像 URL)
              └ item__info
                  ├ TextControl           '光みつる'      ← 昵称
                  ├ secondary-info        '13:40'        ← 时间
                  └ summary-main          '测试'          ← 最后一条摘要

    除此之外没有任何 id 可用，所以「会话项指纹」只能由这三项拼成。
    """
    nick = summary = ts = ""
    info = None
    for node, _d in A.iter_bfs(item, 4, limit=80):
        if P.CLS_ITEM_INFO in A._cls(node):
            info = node
            break
    if info is not None:
        for ch in A._kids(info):
            cls = A._cls(ch)
            if "summary-main" in cls:
                t = "".join(A.collect_texts(ch, 3)).strip()
                if t and not summary:
                    summary = t
            elif A._ctype(ch) == "TextControl":
                t = A._name(ch).strip()
                if t and not nick:
                    nick = t
            elif "secondary-info" in cls:
                t = "".join(A.collect_texts(ch, 3)).strip()
                if t:
                    ts = t
    return {"nickname": nick, "summary": summary, "ts": ts, "rect": list(A._rect(item))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, default=0, help="点第几个会话项")
    ap.add_argument("--dry", action="store_true", help="只报告计划，不动手")
    ap.add_argument("--click", action="store_true",
                    help="打开资料卡时强制用真实鼠标点击（InvokePattern 实测唤不醒资料卡）")
    args = ap.parse_args()

    win = P.find_window()
    if win is None:
        print("[X] 没找到 QQ 窗口")
        return 2

    lst = P.find_recent_list(win)
    if lst is None:
        print("[X] 找不到会话列表")
        return 2
    items = items_of_list(lst)
    print(f"[i] 会话列表 {len(items)} 项")
    for i, it in enumerate(items):
        lb = item_label(it)
        sel = "★" if "recent-contact-item--selected" in A._cls(it) else " "
        print(f"   {sel}[{i}] {lb['nickname']!r:<18} 摘要={lb['summary'][:24]!r:<28} 时间={lb['ts']!r}")
    if not items:
        return 2
    if args.index >= len(items):
        print(f"[X] index {args.index} 越界")
        return 2

    target = items[args.index]
    tlabel = item_label(target)
    print(f"\n[i] 目标会话项 [{args.index}] = {tlabel['nickname']!r}")

    if args.dry:
        print("[i] --dry：不执行点击。将要做的是：")
        print(f"    1) 点击会话项 → 等渲染 → 读 chat-header__contact-name")
        print(f"    2) InvokePattern 点开资料卡 → 扫描新增节点里的 QQ 号")
        print(f"    3) 归还前台")
        return 0

    prev_fg = A._fg_hwnd()
    print(f"[i] 当前前台 hwnd={prev_fg}，操作后会归还")
    before = snapshot(win)
    print(f"[i] 操作前树节点 {len(before)}")

    report: dict = {"index": args.index, "target_label": tlabel,
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}

    try:
        # ---------- 步骤 1：点击会话项 ----------
        # ⚠️ 实测坑：QQ 里点击「当前已选中的会话项」会**关闭**会话面板（toggle）。
        #    所以这里点一次后如果读不到标题，说明刚才是关掉的，再点一次。
        print("[1/3] 点击会话项 …")
        title = None
        for attempt in (1, 2):
            try:
                target.Click(simulateMove=False)
            except Exception as e:
                print(f"    Click 异常（忽略）：{e}")
            time.sleep(1.2)
            title = find_by_class(win, P.CLS_HEADER_NAME)
            if title is not None:
                break
            print(f"    第 {attempt} 次点击后会话未展开（QQ 的 toggle 行为），再点一次")
        report["header_title"] = A._name(title) if title is not None else ""
        print(f"    会话标题 = {report['header_title']!r}")

        # ---------- 步骤 2：打开资料卡 ----------
        print("[2/3] 打开资料卡 …")
        opened = False
        name_btn = title
        if name_btn is not None:
            if not args.click:
                ip = None
                try:
                    ip = name_btn.GetInvokePattern()
                except Exception:
                    ip = None
                if ip is not None:
                    try:
                        ip.Invoke()
                        opened = True
                        print("    已用 InvokePattern 打开（免前台）")
                    except Exception as e:
                        print(f"    InvokePattern 失败：{e}")
            if not opened:
                try:
                    name_btn.Click(simulateMove=False)
                    opened = True
                    print("    已用鼠标点击打开（会抢前台）")
                except Exception as e:
                    print(f"    Click 失败：{e}")
        time.sleep(1.5)

        # ---------- 步骤 3：扫描新增节点 ----------
        print("[3/3] 扫描新增节点 …")
        after = snapshot(win)
        new_keys = [k for k in after if k not in before]
        report["nodes_before"] = len(before)
        report["nodes_after"] = len(after)
        report["new_nodes"] = [after[k] for k in new_keys]

        # 找 QQ 号
        cands: dict = {}
        for k in new_keys:
            n = after[k]
            for t in (str(n.get("name", "")), str(n.get("aid", ""))):
                for m in RE_UIN.findall(t):
                    cands.setdefault(m, []).append({"class": n["class"], "type": n["type"], "name": n["name"][:50]})
        # 也扫一遍全树（资料卡可能复用已有节点）
        allc: dict = {}
        for ctrl, _d in A.iter_bfs(win, 26, limit=15000):
            nm = A._name(ctrl)
            if nm:
                for m in RE_UIN.findall(nm):
                    allc.setdefault(m, []).append({"class": A._cls(ctrl), "name": nm[:50]})
        report["new_node_uin"] = cands
        report["tree_uin"] = allc

        print(f"\n--- 结果 ---")
        print(f"新增节点 {len(new_keys)} 个；其中疑似 QQ 号：{list(cands)[:10] or '无'}")
        print(f"全树疑似 QQ 号：{list(allc)[:10] or '无'}")
        if new_keys:
            print("\n新增节点样本（前 25 个）：")
            for k in new_keys[:25]:
                n = after[k]
                print(f"   [{n['type']}] class={n['class'][:44]!r} name={n['name'][:40]!r}")

        # 关掉资料卡（再 invoke 一次 toggle）
        if opened and name_btn is not None:
            try:
                name_btn.GetInvokePattern().Invoke()
                print("[i] 已尝试关闭资料卡")
            except Exception:
                pass

    finally:
        time.sleep(0.3)
        ok = A.restore_foreground(prev_fg) if prev_fg else False
        print(f"[i] 前台归还 {'成功' if ok else '失败/无需'}")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[OK] 报告写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
