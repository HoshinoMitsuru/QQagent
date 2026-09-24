# -*- coding: utf-8 -*-
"""
probe_uid.py — 探查 QQ 界面里能否拿到「稳定用户标识」（QQ号 / UIN）

为什么需要它：
    现在的 scope 主键是**昵称**（agent.py:1722 current_scope），昵称可以重复、可以随时改，
    一旦重名就会把 A 的上下文喂给 B。要根治必须换成「QQ号」这类稳定标识。
    但 UIA 树上有没有 QQ 号，只能实测。

本脚本只读：
    - 不抢前台、不点击、不输入、不发送
    - 只做 UIA 属性读取与树遍历

探查三层：
    L1  会话列表项 recent-contact-item —— 每个 UIA 属性的实际取值
    L2  会话标题 / 资料卡 / 头像节点 —— 是否出现 QQ 号
    L3  全树扫描 —— AutomationId 非空的节点、Name 形如 QQ 号（5~12 位数字）的节点

用法：
    python probe_uid.py                 # 全量探查，报告写到 uid-probe.json + uid-tree.txt
    python probe_uid.py --limit 8       # 只看前 8 个会话项
    python probe_uid.py --tree          # 额外 dump 完整 UIA 树（很慢，默认只 dump 会话列表区）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import agent as A

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_JSON = os.path.join(HERE, "uid-probe.json")
OUT_TREE = os.path.join(HERE, "uid-tree.txt")

# 会话列表相关 class（从 probe-tree.txt 实测得到）
CLS_RECENT_LIST = "recent-contact-list"
CLS_RECENT_ITEM = "recent-contact-item"
CLS_ITEM_INFO = "item__info"
CLS_ITEM_AVATAR = "item__avatar"
CLS_HEADER_NAME = "chat-header__contact-name"
CLS_PROFILE_NICK = "user-profile-card__nickname"

# 形如 QQ 号：5~12 位纯数字（QQ 号最短 5 位，最长 11 位；留一点余量）
RE_UIN = re.compile(r"^\d{5,12}$")
# 形如 QQ 邮箱 / 号码前缀的文本
RE_UIN_IN_TEXT = re.compile(r"(?:^|[^\d])(\d{5,12})(?:[^\d]|$)")

# 要逐个读的 UIA 属性（名字 → PropertyId）
PROP_NAMES = [
    "NameProperty",
    "AutomationIdProperty",
    "ClassNameProperty",
    "ControlTypeProperty",
    "HelpTextProperty",
    "ItemStatusProperty",
    "FullDescriptionProperty",
    "AcceleratorKeyProperty",
    "AccessKeyProperty",
    "PositionInSetProperty",
    "SizeOfSetProperty",
    "LevelProperty",
    "AriaRoleProperty",
    "AriaPropertiesProperty",
    "LocalizedControlTypeProperty",
    "IsEnabledProperty",
    "IsOffscreenProperty",
    "FrameworkIdProperty",
    "LegacyIAccessibleNameProperty",
    "LegacyIAccessibleValueProperty",
]


def read_props(ctrl) -> dict:
    """逐个读 UIA 属性，失败置 None —— 绝不因为某个属性不支持就整体崩掉。"""
    out: dict = {}
    for nm in PROP_NAMES:
        pid = getattr(A.auto.PropertyId, nm, None)
        if pid is None:
            continue
        key = nm.replace("Property", "")
        try:
            v = ctrl.GetPropertyValue(pid)
        except Exception as e:
            v = f"<err {type(e).__name__}>"
        if v in ("", None):
            continue  # 空值不记录，报告才干净
        out[key] = v
    return out


def short(ctrl) -> dict:
    """一个节点的精简描述。"""
    return {
        "type": A._ctype(ctrl),
        "class": A._cls(ctrl),
        "aid": A._aid(ctrl),
        "name": A._name(ctrl),
        "rect": list(A._rect(ctrl)),
    }


def find_window() -> object:
    root = A.auto.GetRootControl()
    for w in root.GetChildren():
        try:
            if (w.ClassName or "") == "Chrome_WidgetWin_1" and (w.Name or "") == "QQ":
                return w
        except Exception:
            continue
    return None


def find_recent_list(win):
    for ctrl, _d in A.iter_bfs(win, 20, limit=6000):
        if CLS_RECENT_LIST in A._cls(ctrl):
            return ctrl
    return None


def probe_sessions(win, limit: int) -> dict:
    """L1：会话列表逐项深挖。"""
    lst = find_recent_list(win)
    if lst is None:
        return {"error": "找不到 recent-contact-list（QQ 是否用 --force-renderer-accessibility 启动？）"}

    items = [c for c in A._kids(lst) if CLS_RECENT_ITEM in A._cls(c)]
    report = {
        "list_rect": list(A._rect(lst)),
        "item_count": len(items),
        "items": [],
    }
    for idx, item in enumerate(items[:limit]):
        info = {
            "index": idx,
            "selected": "recent-contact-item--selected" in A._cls(item),
            "top": "recent-contact-item--top" in A._cls(item),
            "self": short(item),
            "self_props": read_props(item),
            "descendants": [],
        }
        # 会话项本身是 GroupControl，昵称在子 TextControl 里；把 3 层内所有节点都记下来
        for node, d in A.iter_bfs(item, 4, limit=120):
            s = short(node)
            s["depth"] = d
            p = read_props(node)
            if p:
                s["props"] = p
            info["descendants"].append(s)

        # 语义化提取：昵称 = item__info 下第一个 TextControl
        nick = ""
        for node in A._kids(item):
            if CLS_ITEM_INFO in A._cls(node):
                for ch in A._kids(node):
                    if A._ctype(ch) == "TextControl" and A._name(ch).strip():
                        nick = A._name(ch).strip()
                        break
        info["nickname"] = nick
        info["uin_candidates"] = sorted({
            m for s in info["descendants"]
            for t in (str(s.get("name", "")), str(s.get("aid", "")))
            for m in RE_UIN.findall(t)
        })
        report["items"].append(info)
    return report


def probe_header(win) -> dict:
    """L2：当前会话标题 + 顶栏个人卡片 + 头像节点。"""
    out: dict = {"titles": [], "avatars": []}
    for ctrl, _d in A.iter_bfs(win, 22, limit=6000):
        cls = A._cls(ctrl)
        if CLS_HEADER_NAME in cls:
            out["titles"].append(short(ctrl))
        if CLS_PROFILE_NICK in cls:
            out["titles"].append(short(ctrl))
        if "avatar" in cls and A._visible(ctrl):
            s = short(ctrl)
            s["props"] = read_props(ctrl)
            if len(out["avatars"]) < 25:
                out["avatars"].append(s)
    return out


def probe_tree_wide(win) -> dict:
    """L3：全树扫描 AutomationId 非空节点 + 疑似 QQ 号的文本。"""
    aids: dict = {}
    uins: dict = {}
    n = 0
    for ctrl, _d in A.iter_bfs(win, 26, limit=12000):
        n += 1
        aid = A._aid(ctrl)
        if aid:
            aids.setdefault(aid, []).append(short(ctrl)["class"] or short(ctrl)["type"])
        nm = A._name(ctrl)
        if nm:
            for m in RE_UIN.findall(nm):
                uins.setdefault(m, []).append({
                    "class": A._cls(ctrl), "type": A._ctype(ctrl), "name": nm[:60],
                })
    return {
        "nodes_scanned": n,
        "automation_ids": {k: v[:6] for k, v in list(aids.items())[:60]},
        "aid_count": len(aids),
        "uin_like_texts": {k: v[:4] for k, v in list(uins.items())[:60]},
        "uin_like_count": len(uins),
    }


def dump_tree(win, path: str, maxdepth: int = 26) -> int:
    """把 UIA 树写成人类可读的缩进文本，方便离线 grep。"""
    lines: list[str] = []
    stack = [(win, 0)]
    cnt = 0
    while stack and cnt < 15000:
        ctrl, d = stack.pop()
        if d > maxdepth:
            continue
        cnt += 1
        r = A._rect(ctrl)
        tag = "!" if A._visible(ctrl) else " "
        lines.append(
            f"{'  ' * d}{tag}[{A._ctype(ctrl)}] '{A._name(ctrl)}' "
            f"class='{A._cls(ctrl)}' aid='{A._aid(ctrl)}' rect={list(r)}"
        )
        for ch in reversed(A._kids(ctrl)):
            stack.append((ch, d + 1))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return cnt


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=12, help="探查前 N 个会话项")
    ap.add_argument("--tree", action="store_true", help="额外 dump 完整 UIA 树")
    args = ap.parse_args()

    t0 = time.time()
    win = find_window()
    if win is None:
        print("[X] 没找到 QQ 窗口（class=Chrome_WidgetWin_1 / Name='QQ'）")
        return 2
    print(f"[i] 已附着 QQ 窗口 rect={list(A._rect(win))}")

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "window": short(win),
        "L1_sessions": probe_sessions(win, args.limit),
        "L2_header": probe_header(win),
    }
    print(f"[i] L1 会话项 {report['L1_sessions'].get('item_count', '?')} 个")

    if args.tree:
        n = dump_tree(win, OUT_TREE)
        report["tree_nodes"] = n
        print(f"[i] UIA 树已写入 {OUT_TREE}（{n} 节点）")

    report["L3_wide"] = probe_tree_wide(win)
    print(f"[i] L3 扫到 {report['L3_wide']['nodes_scanned']} 节点，"
          f"AutomationId 非空 {report['L3_wide']['aid_count']} 个，"
          f"疑似 QQ 号 {report['L3_wide']['uin_like_count']} 个")

    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[OK] 报告写入 {OUT_JSON}（{time.time() - t0:.1f}s）")

    # 控制台先给一段最关键的结论
    print("\n--- 会话列表项速览 ---")
    for it in report["L1_sessions"].get("items", []):
        av = next((s for s in it["descendants"] if CLS_ITEM_AVATAR in s.get("class", "")), {})
        print(f"  [{it['index']:>2}] 昵称={it['nickname']!r:<16} "
              f"aid={it['self']['aid']!r} 头像aid={av.get('aid')!r} "
              f"候选QQ号={it['uin_candidates']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
