# -*- coding: utf-8 -*-
"""
probe.py — QQNT UIA 结构探针（路线 A 的第 0 步）

为什么需要它：
    agent.py 的业务逻辑（读消息 / 分辨我方与对方 / 定位输入框）完全依赖
    「QQNT 到底把 DOM 暴露成了哪些 UIA 控件」。这一步不做任何猜测，
    先把真实的控件树打印出来，再据此定稿读取逻辑。

用法：
    python probe.py                 # 探针 + 控制台摘要（最常用）
    python probe.py --dump          # 额外把完整控件树写到 probe-tree.txt
    python probe.py --depth 16      # 加深遍历（默认 12）
    python probe.py --title 小清澈   # 手动指定窗口标题关键字

产出：
    probe-report.json  机器可读的完整结果（供 agent.py 定稿参考）
    probe-tree.txt     仅 --dump 时生成

前置：QQ 必须带 --force-renderer-accessibility 参数启动，否则 UIA 树是空的。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes
from datetime import datetime

# ---------------------------------------------------------------- 控制台编码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import uiautomation as auto
except ImportError:
    print("[X] 缺少 uiautomation。请先安装依赖：")
    print("    pip install -r requirements.txt")
    raise SystemExit(2)

auto.SetGlobalSearchTimeout(1.5)
if hasattr(auto, "SetGlobalSearchInterval"):
    auto.SetGlobalSearchInterval(0.3)

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 进程名查询
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def process_path(pid: int) -> str:
    """拿到进程完整路径，比 uiautomation 自带的进程名接口更稳。"""
    if not pid:
        return ""
    handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _k32.CloseHandle(handle)


# ---------------------------------------------------------------- 窗口枚举
# QQNT 基于 Electron/Chromium，顶层窗口类名基本固定
QQ_WINDOW_CLASSES = {
    "Chrome_WidgetWin_1",   # QQNT 主窗口（Electron）
    "Chrome_WidgetWin_0",
    "TXGuiFoundation",      # 旧版 QQ 兜底
}

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u32.IsWindowVisible.argtypes = [wintypes.HWND]
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]


def enum_top_windows() -> list[dict]:
    """
    用 EnumWindows 枚举所有顶层窗口。
    比 uiautomation 的 GetRootControl().GetChildren() 强的地方：
    能看到「缩在托盘里 / 最小化」的隐藏窗口 —— 而 QQ 恰恰经常处于这个状态。
    """
    rows: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        n = _u32.GetWindowTextLengthW(hwnd)
        tbuf = ctypes.create_unicode_buffer(n + 1)
        _u32.GetWindowTextW(hwnd, tbuf, n + 1)
        cbuf = ctypes.create_unicode_buffer(256)
        _u32.GetClassNameW(hwnd, cbuf, 256)
        r = _RECT()
        _u32.GetWindowRect(hwnd, ctypes.byref(r))
        rows.append(
            {
                "hwnd": hwnd,
                "pid": pid.value,
                "visible": bool(_u32.IsWindowVisible(hwnd)),
                "class": cbuf.value,
                "title": tbuf.value,
                "rect": [r.left, r.top, r.right, r.bottom],
            }
        )
        return True

    _u32.EnumWindows(_cb, 0)
    return rows


def control_from_hwnd(hwnd):
    if hasattr(auto, "ControlFromHandle"):
        try:
            return auto.ControlFromHandle(hwnd)
        except Exception:
            pass
    try:
        return auto.Control(Handle=hwnd)
    except Exception:
        return None


def pick_qq_windows(title_hint: str = ""):
    """返回 [(uia_control, info_dict), ...]，可见窗口优先。"""
    hits = []
    for row in enum_top_windows():
        p = process_path(row["pid"])
        base = os.path.basename(p).lower()
        is_qq = base == "qq.exe" or "qqnt" in p.lower().replace("/", "\\")
        is_legacy = row["class"] == "TXGuiFoundation"
        if not (is_qq or is_legacy):
            continue
        if row["class"] not in QQ_WINDOW_CLASSES:
            continue
        r = row["rect"]
        w, h = r[2] - r[0], r[3] - r[1]
        # 排掉 IME / 通知图标之类没有实际尺寸的小窗
        if w < 200 or h < 150:
            continue
        if title_hint and title_hint.lower() not in row["title"].lower():
            continue

        info = dict(row)
        info["exe"] = p
        info["reason"] = "进程名匹配" if is_qq else "旧版类名匹配"
        ctrl = control_from_hwnd(row["hwnd"])
        if ctrl is not None:
            hits.append((ctrl, info))

    def sort_key(pair):
        info = pair[1]
        r = info["rect"]
        a = (r[2] - r[0]) * (r[3] - r[1])
        return (not info["visible"], info["title"] != "QQ", -a)

    hits.sort(key=sort_key)
    return hits


# ---------------------------------------------------------------- 树遍历
SKIP_TYPES = {"ScrollBarControl", "ThumbControl", "TitleBarControl"}


def runtime_id(ctrl):
    try:
        rid = ctrl.GetRuntimeId()
        if rid:
            return list(rid)
    except Exception:
        pass
    return None


def walk(ctrl, depth, maxdepth, acc, path="/", limit=40000):
    if len(acc) >= limit:
        return
    try:
        ctype = ctrl.ControlTypeName
    except Exception:
        return
    if ctype in SKIP_TYPES:
        return

    try:
        r = ctrl.BoundingRectangle
        rect = [r.left, r.top, r.right, r.bottom]
    except Exception:
        rect = [0, 0, 0, 0]

    try:
        name = (ctrl.Name or "")[:300]
    except Exception:
        name = ""
    try:
        cls = ctrl.ClassName or ""
    except Exception:
        cls = ""
    try:
        auto_id = getattr(ctrl, "AutomationId", "") or ""
    except Exception:
        auto_id = ""

    acc.append(
        {
            "depth": depth,
            "path": path,
            "type": ctype,
            "name": name,
            "class": cls,
            "auto_id": auto_id,
            "rect": rect,
            "has_runtime_id": runtime_id(ctrl) is not None,
        }
    )

    if depth >= maxdepth:
        return
    try:
        children = ctrl.GetChildren()
    except Exception:
        return
    for i, ch in enumerate(children):
        walk(ch, depth + 1, maxdepth, acc, f"{path}{i}/", limit)
        if len(acc) >= limit:
            break


# ---------------------------------------------------------------- 结果分析
def area(rect):
    w = max(0, rect[2] - rect[0])
    h = max(0, rect[3] - rect[1])
    return w * h


def analyze(nodes, win_rect):
    wl, wt, wr, wb = win_rect
    ww = max(1, wr - wl)
    wh = max(1, wb - wt)

    report = {
        "window": {"rect": win_rect, "width": ww, "height": wh},
        "counts": {},
        "lists": [],
        "list_items": [],
        "editors": [],
        "buttons": [],
        "top_texts": [],
        "notes": [],
    }

    for n in nodes:
        report["counts"][n["type"]] = report["counts"].get(n["type"], 0) + 1

    # --- List 控件（消息列表 / 会话列表 都在这）
    for n in nodes:
        if n["type"] != "ListControl":
            continue
        r = n["rect"]
        rel_left = (r[0] - wl) / ww
        report["lists"].append(
            {
                "path": n["path"],
                "name": n["name"],
                "class": n["class"],
                "rect": r,
                "area": area(r),
                "rel_left": round(rel_left, 3),
                "guess": "会话列表(左)" if rel_left < 0.35 else "消息列表(右)候选",
            }
        )
    report["lists"].sort(key=lambda x: -x["area"])

    # --- ListItem 控件（消息气泡候选）
    for n in nodes:
        if n["type"] != "ListItemControl":
            continue
        r = n["rect"]
        report["list_items"].append(
            {
                "path": n["path"],
                "parent": "/".join(n["path"].split("/")[:-2]) + "/",
                "name": n["name"],
                "rect": r,
                "rel_left_in_win": round((r[0] - wl) / ww, 3),
                "rel_right_in_win": round((r[2] - wl) / ww, 3),
                "has_runtime_id": n["has_runtime_id"],
            }
        )

    # --- 输入框候选
    for n in nodes:
        if n["type"] in ("DocumentControl", "EditControl"):
            r = n["rect"]
            report["editors"].append(
                {
                    "path": n["path"],
                    "type": n["type"],
                    "name": n["name"],
                    "class": n["class"],
                    "rect": r,
                    "area": area(r),
                    "in_bottom_half": r[1] > wt + wh * 0.5,
                }
            )
    report["editors"].sort(key=lambda x: -x["area"])

    # --- 按钮（发送按钮候选，一般在右下角）
    for n in nodes:
        if n["type"] != "ButtonControl":
            continue
        r = n["rect"]
        report["buttons"].append(
            {
                "path": n["path"],
                "name": n["name"],
                "rect": r,
                "in_bottom_right": r[1] > wt + wh * 0.6 and r[0] > wl + ww * 0.5,
            }
        )

    # --- 顶部区域文本（用于判断私聊 / 群聊）
    for n in nodes:
        if n["type"] != "TextControl" or not n["name"]:
            continue
        r = n["rect"]
        if r[1] < wt + 90 and r[0] > wl + ww * 0.30:
            report["top_texts"].append({"name": n["name"], "rect": r, "path": n["path"]})

    # --- 结论与提醒
    notes = report["notes"]
    if report["counts"].get("ListItemControl", 0) == 0:
        notes.append(
            "没有发现 ListItemControl —— 极可能 QQ 未使用 "
            "--force-renderer-accessibility 启动，UIA 树是残缺的。"
        )
    if report["counts"].get("TextControl", 0) == 0:
        notes.append("TextControl 为 0，UIA 完全不可用，需要走 OCR 兜底路线。")
    if not report["editors"]:
        notes.append("未找到 DocumentControl/EditControl，输入需要走『坐标点击 + 键盘输入』兜底。")
    if report["list_items"]:
        sample = report["list_items"][-3:]
        if all((not x["name"]) for x in sample):
            notes.append(
                "最近 3 个 ListItem 的 Name 为空 —— 消息文本挂在其子 TextControl 上，"
                "读取逻辑应改为『递归收集子节点 Name』而不是直接取 ListItem.Name。"
            )
        if len({x["rel_left_in_win"] for x in sample}) == 1:
            notes.append(
                "ListItem 左边界高度一致 —— 无法用水平位置区分我方/对方气泡，"
                "建议降级为『昵称匹配』或『只把最新一条当成对方消息』。"
            )
    return report


# ---------------------------------------------------------------- 打印
def p(title):
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def print_report(report, hits):
    win = report["window"]
    p("① 窗口")
    print(f"  尺寸: {win['width']} x {win['height']}   左上角: ({win['rect'][0]}, {win['rect'][1]})")
    for _ctrl, h in hits:
        vis = "可见" if h.get("visible") else "不可见(托盘)"
        print(f"  标题: {h['title']!r}  类名: {h['class']}  PID={h['pid']}  hwnd={h.get('hwnd')}")
        print(f"  进程: {h['exe']}  ({h['reason']})  窗口状态: {vis}")

    p("② 控件类型统计（前 15）")
    items = sorted(report["counts"].items(), key=lambda kv: -kv[1])[:15]
    for k, v in items:
        print(f"  {k:<28} {v}")

    p("③ List 控件（判断哪个是消息列表）")
    if not report["lists"]:
        print("  （无）")
    for i, l in enumerate(report["lists"], 1):
        print(f"  [{i}] {l['guess']}")
        print(f"      path={l['path']}  area={l['area']}  rel_left={l['rel_left']}")
        print(f"      rect={l['rect']}  name={l['name']!r}")

    p("④ 消息条目候选（最后 8 条）")
    if not report["list_items"]:
        print("  （无）")
    for it in report["list_items"][-8:]:
        print(f"  name={it['name']!r}")
        print(
            f"      parent={it['parent']}  left={it['rel_left_in_win']}  "
            f"right={it['rel_right_in_win']}  runtimeId={it['has_runtime_id']}"
        )

    p("⑤ 输入框候选（前 5）")
    if not report["editors"]:
        print("  （无）→ 发送需要走坐标兜底")
    for e in report["editors"][:5]:
        print(
            f"  {e['type']:<18} area={e['area']:<9} bottom={e['in_bottom_half']}  "
            f"rect={e['rect']}  name={e['name']!r}"
        )

    p("⑥ 底部右下角按钮（发送按钮候选）")
    cands = [b for b in report["buttons"] if b["in_bottom_right"]]
    if not cands:
        print("  （无）")
    for b in cands[:8]:
        print(f"  name={b['name']!r}  rect={b['rect']}")

    p("⑦ 顶部文本（判断私聊 / 群聊）")
    if not report["top_texts"]:
        print("  （无）")
    for t in report["top_texts"][:10]:
        print(f"  {t['name']!r}  rect={t['rect']}")

    p("⑧ 关键提示")
    if not report["notes"]:
        print("  UIA 结构看起来是健康的，可以进入 agent.py 定稿。")
    for n in report["notes"]:
        print(f"  - {n}")


def print_launch_hint(exe_path: str) -> None:
    """给出可直接复制的『带无障碍开关启动 QQ』命令。"""
    if not exe_path:
        return
    print("\n[i] 检测到 QQ 可执行文件，用下面这条命令重启 QQ（关键是无障碍开关）：")
    print(f'    "{exe_path}" --force-renderer-accessibility')
    print("    做法：把桌面/任务栏上 QQ 快捷方式的『目标』改成上面这一行（路径要带引号）")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="QQNT UIA 结构探针")
    ap.add_argument("--depth", type=int, default=12, help="UIA 树遍历深度，默认 12")
    ap.add_argument("--dump", action="store_true", help="把完整控件树写入 probe-tree.txt")
    ap.add_argument("--title", default="", help="按窗口标题关键字过滤")
    args = ap.parse_args()

    p("QQNT UIA 探针")
    hits = pick_qq_windows(args.title)
    if not hits:
        print("[X] 没有找到 QQ 窗口。请确认：")
        print("    1) QQ（NT 版）已经启动并登录")
        print("    2) 已用 --force-renderer-accessibility 参数启动")
        print("       例如快捷方式目标改为：")
        print('       "C:\\Program Files\\Tencent\\QQNT\\QQ.exe" --force-renderer-accessibility')
        print("    3) 若 UIA 权限受限，请以管理员身份运行本脚本")
        all_wins = enum_top_windows()
        qq_like = [w for w in all_wins if "qq" in os.path.basename(process_path(w["pid"])).lower()]
        print(f"\n[i] 全系统顶层窗口 {len(all_wins)} 个，其中属于 QQ.exe 的 {len(qq_like)} 个：")
        for w in qq_like[:20]:
            print(f"    hwnd={w['hwnd']} visible={w['visible']} class={w['class']!r} title={w['title']!r} rect={w['rect']}")
        if qq_like and not any(w["class"] in QQ_WINDOW_CLASSES for w in qq_like):
            print("    ↑ 都是子窗口/辅助窗口，没有找到主窗口")
        exes = []
        for w in qq_like:
            e = process_path(w["pid"])
            if e and e not in exes:
                exes.append(e)
        if exes:
            print_launch_hint(exes[0])
        return 2

    win, info = hits[0]
    if len(hits) > 1:
        print(f"[i] 匹配到 {len(hits)} 个 QQ 窗口，使用第一个：{info['title']!r}")

    if not info["visible"]:
        print()
        print("!" * 72)
        print("[!] 选中的 QQ 主窗口当前是【不可见】状态（缩在系统托盘里）。")
        print("    Chromium/Electron 对隐藏窗口不构建无障碍树，所以这次读不到任何内容。")
        print("    按下面顺序处理：")
        print("      ① 双击任务栏右下角的 QQ 托盘图标，把主窗口打开，停在目标私聊上")
        print("      ② 再跑一次本脚本。若『④ 消息条目候选』还是空，说明无障碍树没被激活")
        print("      ③ 那就重启 QQ 并加上无障碍开关（命令见下方）")
        print_launch_hint(info.get("exe", ""))
        print("!" * 72)
        print()

    r = info["rect"]
    win_rect = list(r)

    print(f"[i] 已附着窗口 {info['title']!r}，开始遍历 UIA 树（depth={args.depth}）…")
    t0 = time.time()
    nodes: list[dict] = []
    walk(win, 0, args.depth, nodes)
    print(f"[i] 遍历完成：{len(nodes)} 个节点，耗时 {time.time() - t0:.2f}s")

    report = analyze(nodes, win_rect)
    report["probed_at"] = datetime.now().isoformat(timespec="seconds")
    report["window"]["info"] = info

    print_report(report, hits)

    out_json = os.path.join(HERE, "probe-report.json")
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[i] 完整报告已写入：{out_json}")

    if args.dump:
        out_tree = os.path.join(HERE, "probe-tree.txt")
        with open(out_tree, "w", encoding="utf-8") as f:
            for n in nodes:
                f.write(
                    "{indent}[{t}] {name!r} class={cls!r} id={aid!r} rect={rect}\n".format(
                        indent="  " * n["depth"],
                        t=n["type"],
                        name=n["name"],
                        cls=n["class"],
                        aid=n["auto_id"],
                        rect=n["rect"],
                    )
                )
        print(f"[i] 控件树已写入：{out_tree}")

    print("\n[✓] 请把上面【③④⑤】三节的输出发回来，我据此定稿 agent.py 的读取逻辑。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
