# -*- coding: utf-8 -*-
"""
probe_minimized.py — 实测：QQ 窗口最小化之后，UIA 读取还能不能用？

## 为什么需要这个实验

「把 QQ 放进虚拟机、虚拟机窗口最小化到托盘」这个方案能不能成立，取决于一个前提：

    宿主把虚拟机窗口最小化 ≠ 客户机里的窗口被最小化

客户机有自己的虚拟桌面，它根本不知道宿主那个窗口显示成了什么样。所以理论上
宿主侧最小化**不应该**影响客户机内的 UIA 读取。

但这里还有一个**独立的问题**必须先答清楚：

    **客户机内**的 QQ 窗口如果被最小化（托盘/最小化），UIA 读取还成立吗？

README 里一直写着"窗口不能最小化，最小化会让无障碍树失效"，但这条是从
Chromium 的常规行为推断的，没有实测过。它直接决定虚拟机方案的操作纪律：
到底能不能让 QQ 在客户机里也缩着。

## 它测什么

| 步骤 | 观察 |
| --- | --- |
| 1 | 正常状态下：控件树节点数 / 会话列表条数 / 消息条数 / 输入框是否找得到 |
| 2 | `ShowWindow(SW_MINIMIZE)` 之后：同上四项 + `IsIconic` |
| 3 | 最小化状态下 `force_foreground()` 能否把它救回来（这是 agent 的自愈路径） |
| 4 | 复原后：四项是否恢复原值（确认实验本身没有把东西弄坏） |

只读 + 一次最小化/复原，**不发送任何消息、不切会话、不关窗口**。

用法：
    python -X utf8 probe_minimized.py
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

import agent as A
import qqid as Q

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.IsIconic.argtypes = [wintypes.HWND]
_u32.IsIconic.restype = wintypes.BOOL

SW_MINIMIZE = 6
SW_RESTORE = 9


def count_nodes(root, cap: int = 6000) -> int:
    """数控件树节点数（带上限，免得异常时刷爆）。"""
    n = 0
    stack = [root]
    while stack and n < cap:
        c = stack.pop()
        n += 1
        try:
            stack.extend(c.GetChildren())
        except Exception:
            pass
    return n


def snapshot(qq, label: str) -> dict:
    """采一次样本：控件树规模 + 会话列表 + 消息 + 输入框。"""
    qq.refresh_layout(force=True)
    row = {"label": label}

    try:
        row["nodes"] = count_nodes(qq.win) if qq.win is not None else -1
    except Exception as exc:
        row["nodes"] = f"ERR {type(exc).__name__}"

    t0 = time.perf_counter()
    try:
        row["sessions"] = len(Q.list_sessions(qq.win))
    except Exception as exc:
        row["sessions"] = f"ERR {type(exc).__name__}"
    row["scan_ms"] = round((time.perf_counter() - t0) * 1000)

    try:
        row["msgs"] = len(qq.read_messages(limit=30))
    except Exception as exc:
        row["msgs"] = f"ERR {type(exc).__name__}"

    row["title"] = qq.title_now() or "-"
    row["editor"] = "有" if qq.editor is not None else "无"
    row["send_btn"] = "有" if qq.send_btn is not None else "无"
    row["iconic"] = bool(_u32.IsIconic(qq.hwnd)) if qq.hwnd else None
    row["fg"] = A._is_foreground(qq.hwnd) if qq.hwnd else None
    return row


def show(rows: list) -> None:
    cols = ["label", "nodes", "sessions", "scan_ms", "msgs", "title", "editor",
            "send_btn", "iconic", "fg"]
    heads = ["状态", "节点数", "会话数", "扫描ms", "消息数", "标题", "输入框",
             "发送钮", "已最小化", "在前台"]
    print()
    print("  " + " | ".join(f"{h:<{max(6, len(str(heads[i])) + 2)}}"
                            for i, h in enumerate(heads)))
    print("  " + "-" * 100)
    for r in rows:
        print("  " + " | ".join(f"{str(r.get(c, '-')):<{max(6, len(str(heads[i])) + 2)}}"
                                for i, c in enumerate(cols)))


def main() -> int:
    print("=" * 100)
    print("实验：QQ 窗口最小化后，UIA 读取还能不能用？")
    print("=" * 100)

    cfg = A.load_config()
    qq = A.QQWindow(cfg)
    if not qq.attach():
        print("  没找到 QQ 窗口（需带 --force-renderer-accessibility 且未缩托盘）")
        return 2
    if not qq.hwnd:
        print("  没拿到 hwnd")
        return 2

    print(f"  hwnd = {qq.hwnd}  title = {qq.dialog_title!r}")
    if A._desktop_locked():
        print("  ⚠ 桌面处于锁屏状态，结论会被污染（锁屏本身就会让点击失效）。建议先解锁。")

    rows = []
    rows.append(snapshot(qq, "① 正常"))
    normal = dict(rows[-1])

    # ---------------- 最小化 ----------------
    print("\n  → ShowWindow(SW_MINIMIZE)")
    _u32.ShowWindow(qq.hwnd, SW_MINIMIZE)
    time.sleep(1.5)
    rows.append(snapshot(qq, "② 已最小化"))

    # ---------------- 最小化时尝试自愈 ----------------
    print("  → 最小化状态下尝试 force_foreground()（agent 的自愈路径）")
    healed = A.force_foreground(qq.hwnd, retries=3)
    time.sleep(1.0)
    rows.append(snapshot(qq, "③ force 后"))
    print(f"     force_foreground() 返回 {healed}")

    # ---------------- 复原 ----------------
    if _u32.IsIconic(qq.hwnd):
        print("  → 仍然是图标态，显式 SW_RESTORE")
        _u32.ShowWindow(qq.hwnd, SW_RESTORE)
        time.sleep(1.2)
    rows.append(snapshot(qq, "④ 已复原"))

    show(rows)

    # ---------------- 判定 ----------------
    print()
    mini = rows[1]
    back = rows[3]

    def ok(r, key):
        return isinstance(r.get(key), int) and r[key] > 0

    print("=" * 100)
    print("判定")
    print("=" * 100)
    print(f"  正常态可读            : 会话数={normal['sessions']} 消息数={normal['msgs']} "
          f"节点数={normal['nodes']}")
    print(f"  最小化后可读          : 会话数={mini['sessions']} 消息数={mini['msgs']} "
          f"节点数={mini['nodes']}")
    print(f"  最小化后输入框/发送钮 : 输入框={mini['editor']} 发送钮={mini['send_btn']}")
    print(f"  自愈（force_foreground）: {'成功' if not rows[2]['iconic'] else '失败（仍是图标态）'}")
    print(f"  复原后可读            : 会话数={back['sessions']} 消息数={back['msgs']} "
          f"节点数={back['nodes']}")
    print(f"  复原校验              : 已最小化={back['iconic']}（应为 False）")

    verdict = []
    if ok(mini, "sessions") and mini.get("sessions") == normal.get("sessions"):
        verdict.append("✅ 最小化后**会话列表读取不受影响**")
    else:
        verdict.append("❌ 最小化后会话列表读取**受影响**")
    if ok(mini, "msgs") and mini.get("msgs") == normal.get("msgs"):
        verdict.append("✅ 最小化后**消息读取不受影响**")
    else:
        verdict.append("❌ 最小化后消息读取**受影响**")
    if mini.get("editor") == "有" and mini.get("send_btn") == "有":
        verdict.append("✅ 最小化后输入框/发送钮**仍可定位**（→ 免前台的 Invoke 发送仍可用）")
    else:
        verdict.append("❌ 最小化后输入框/发送钮**定位失败**")
    for v in verdict:
        print("  " + v)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
