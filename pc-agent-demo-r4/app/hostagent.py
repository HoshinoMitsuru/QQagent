# -*- coding: utf-8 -*-
"""
hostagent.py —— **宿主进程**（被启动到隐藏桌面上的那个）

## 为什么非得有这么一个进程

`EnumWindows` 只枚举**调用者所在桌面**的窗口。壳活在用户桌面上，
于是它对隐藏桌面上那个 QQ 是彻底瞎的：找不到窗口、抓不了图、读不了界面。

唯一能碰它的办法，是派一个进程**同样被启动到那张桌面上**——
`desktop.spawn(..., desktop=NAME)` 干的就是这件事。本模块是那个进程跑的代码。

## 它做什么

    1. 自报「我到底落在哪张桌面」        —— 验证 lpDesktop 真的生效
    2. 在这张桌面上枚举窗口，找 QQ 主窗口 —— 只有在这里才找得到
    3. PrintWindow 抓画面并落盘           —— 非活动桌面唯一的抓图路径
    4. 可选：读 UIA 树，回答「登录了没有」
    5. 把全部结果写进 JSON 文件

## 为什么结果走文件而不是 stdout

`desktop.spawn` 用的是 `CreateProcessW`，**没有接管 stdout 管道**
（`subprocess.Popen` 不暴露 `lpDesktop`，必须自己调 Win32 API）。
所以子进程没法把结果 print 回来 —— 它写文件，父进程读文件。
这条约束反过来也是好事：父子生命周期彻底解耦，父进程崩了不影响这边跑完。

同理：**任何异常都必须写进 JSON 的 traceback 字段**。否则父进程只能看到
「文件没生成」，然后去查根本不存在的问题。

用法（由 host.grab 调用，一般不手跑）：
    python -m app.hostagent --out <json路径> --png <图片路径> [--uia] [--wait 秒]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

# 作为被 spawn 的子进程，sys.path[0] 未必是仓库根 —— 显式补上，
# 否则 `from . import winmsg` 这类相对导入根本不成立（本模块是包内模块）。
_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from app import desktop, winmsg  # noqa: E402

QQ_CLASSES = ("Chrome_WidgetWin_1", "TXGuiFoundation")


def find_qq_window() -> dict:
    """在本桌面里找 QQ 主窗口：类名匹配 + 面积最大。"""
    cands = []
    for hwnd in winmsg.enum_windows():
        cls = winmsg.win_class(hwnd)
        if cls not in QQ_CLASSES:
            continue
        r = winmsg.win_rect(hwnd)
        area = max(0, r[2] - r[0]) * max(0, r[3] - r[1])
        if area < 300 * 200:
            continue  # QQ 会开一堆 32x37 的隐藏壳窗口，按面积滤掉
        cands.append({
            "hwnd": hwnd, "class": cls, "title": winmsg.win_text(hwnd),
            "pid": winmsg.pid_of(hwnd), "rect": r, "area": area,
            "visible": winmsg.is_visible(hwnd), "iconic": winmsg.is_iconic(hwnd),
        })
    cands.sort(key=lambda c: c["area"], reverse=True)
    return {"main": cands[0] if cands else None, "candidates": cands[:12],
            "count": len(cands)}


def probe_uia(hwnd: int) -> dict:
    """
    读一下界面，回答「这个 QQ 是登录了还是在等扫码」。

    失败不影响主流程 —— 抓图才是硬需求，这是附赠的信息。
    """
    out = {"ok": False, "error": ""}
    try:
        import uiautomation as auto
    except Exception as exc:
        out["error"] = f"uiautomation 不可用：{type(exc).__name__}: {exc}"
        return out
    try:
        win = auto.ControlFromHandle(hwnd)
        out["window_name"] = win.Name or ""
        marks = {"nickname": "", "has_recent_list": False, "has_ml_list": False,
                 "text_count": 0}
        samples: list[str] = []
        stack = [(win, 0)]
        n = 0
        while stack and n < 6000:
            ctrl, d = stack.pop()
            if d > 22:
                continue
            n += 1
            try:
                cls = ctrl.ClassName or ""
                ctype = ctrl.ControlTypeName or ""
                name = (ctrl.Name or "").strip()
            except Exception:
                continue
            if "profile-nickname__text" in cls and not marks["nickname"]:
                try:
                    for ch in ctrl.GetChildren():
                        t = (ch.Name or "").strip()
                        if t:
                            marks["nickname"] = t
                            break
                except Exception:
                    pass
            if "recent-contact-list" in cls:
                marks["has_recent_list"] = True
            if "ml-list" in cls:
                marks["has_ml_list"] = True
            if ctype == "TextControl" and name and len(samples) < 30:
                marks["text_count"] += 1
                samples.append(name[:40])
            try:
                for ch in ctrl.GetChildren():
                    stack.append((ch, d + 1))
            except Exception:
                pass
        out.update(marks)
        out["text_samples"] = samples[:20]
        out["nodes_scanned"] = n
        out["ok"] = True
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-400:]}"
    return out


def dump_tree(hwnd: int, path: str, maxdepth: int = 30,
              limit: int = 20000) -> dict:
    """
    把整棵 UIA 树 dump 成人可读的缩进文本。

    为什么要有它：登录页这种界面元素很少（实测 38 个节点），
    但它决定了「能不能免手动登录」—— 值得完整看一遍而不是只报统计。
    """
    out = {"ok": False, "path": path, "nodes": 0, "error": ""}
    try:
        import uiautomation as auto
        win = auto.ControlFromHandle(hwnd)
        lines: list[str] = []
        stack = [(win, 0)]
        n = 0
        while stack and n < limit:
            ctrl, d = stack.pop()
            if d > maxdepth:
                continue
            n += 1
            try:
                cls = ctrl.ClassName or ""
                aid = getattr(ctrl, "AutomationId", "") or ""
                name = (ctrl.Name or "").replace("\n", "\\n")
                ctype = ctrl.ControlTypeName or ""
                r = ctrl.BoundingRectangle
                rect = [r.left, r.top, r.right, r.bottom]
                en = ctrl.IsEnabled
            except Exception:
                continue
            lines.append(f"{'  ' * d}[{ctype}] name={name!r} class={cls!r} "
                         f"aid={aid!r} en={int(en)} rect={rect}")
            try:
                for ch in reversed(ctrl.GetChildren()):
                    stack.append((ch, d + 1))
            except Exception:
                pass
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        out.update({"ok": True, "nodes": n})
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-400:]}"
    return out


#: 登录按钮的类名（实测 2026-09-24：`q-button q-button--primary q-button--middle login-btn`）
LOGIN_BTN_CLASS = "login-btn"
#: 「自动登录」复选框的类名（`q-checkbox`，Name 就是 '自动登录'）
AUTO_LOGIN_CLASS = "q-checkbox"


def _patterns_available(ctrl) -> dict:
    """
    读 Pattern 可用性。

    ⚠️ 这里为什么不用 `GetCurrentPattern`：Chromium 对它的回答**会谎报**
    —— 实测 QQ 输入框上 `GetCurrentPattern(ValuePattern)` 看起来「能用」，
    真调下去才发现 `IsValuePatternAvailable=False`。
    唯一可信的是 `IsXxxPatternAvailable` **属性投影**，所以逐个读属性。
    """
    ids = {
        "invoke": "IsInvokePatternAvailableProperty",
        "legacy": "IsLegacyIAccessiblePatternAvailableProperty",
        "toggle": "IsTogglePatternAvailableProperty",
    }
    out = {}
    for key, prop in ids.items():
        pid = getattr(_auto().PropertyId, prop, None)
        if pid is None:
            out[key] = None
            continue
        try:
            out[key] = bool(ctrl.GetPropertyValue(pid))
        except Exception:
            out[key] = None
    return out


_AUTO_MOD = None


def _auto():
    """惰性导入 uiautomation —— 只有真的要读界面时才把它拉进来。"""
    global _AUTO_MOD
    if _AUTO_MOD is None:
        import uiautomation as _m
        _AUTO_MOD = _m
    return _AUTO_MOD


def _find_by(hwnd: int, *, cls_has: str = "", name_eq: str = "") -> object:
    """在窗口树里找第一个匹配的节点（类名含 / 名字等）。"""
    auto = _auto()
    win = auto.ControlFromHandle(hwnd)
    stack = [(win, 0)]
    n = 0
    while stack and n < 8000:
        ctrl, d = stack.pop()
        if d > 26:
            continue
        n += 1
        try:
            cls = ctrl.ClassName or ""
            nm = (ctrl.Name or "").strip()
            hit = (cls_has and cls_has in cls) or (name_eq and nm == name_eq)
        except Exception:
            hit = False
        if hit:
            return ctrl
        try:
            for ch in ctrl.GetChildren():
                stack.append((ch, d + 1))
        except Exception:
            pass
    return None


def _describe(ctrl) -> dict:
    try:
        r = ctrl.BoundingRectangle
        return {
            "name": ctrl.Name or "",
            "class": ctrl.ClassName or "",
            "type": ctrl.ControlTypeName or "",
            "enabled": bool(ctrl.IsEnabled),
            "rect": [r.left, r.top, r.right, r.bottom],
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def do_login(hwnd: int, *, dry_run: bool = False,
             check_auto: bool = True) -> dict:
    """
    在登录页上把「登录」按掉，目标是**免扫码自动登录**。

    ## 为什么这一步值得单独实现

    隐藏桌面上的 QQ 是看不见的，二维码没人扫得到。但实测发现：只要这台机器
    之前登录过这个号，QQ 的登录页会直接列出账号 + 一个「登录」按钮
    （`archive` 抓的图里就是头像 + `Susurrus-苏霖韵` + 自动登录勾选项）。
    把这个按钮按掉，就完成了「免手动登录」——全程不需要用户、不需要前台。

    ## 顺序

      1. 抓 `IsInvokePatternAvailable` / `IsLegacyIAccessiblePatternAvailable`（只读）
      2. `dry_run=True` 就在这里返回 —— 先说清楚能不能点，再决定点不点
      3. 先勾「自动登录」（勾上之后下次冷启动能自己登录，不用再点）
      4. `Invoke()` 登录按钮；`Invoke` 不可用则退 `LegacyIAccessible.DoDefaultAction()`

    ⚠️ 调用方要清楚一件事：**同一个 QQ 号在两处登录会互相挤下线**。
    所以点这个按钮之前，桌面上那个同号实例应该先退出。
    """
    out: dict = {"ok": False, "dry_run": bool(dry_run), "error": ""}
    try:
        btn = _find_by(hwnd, cls_has=LOGIN_BTN_CLASS)
        if btn is None:
            btn = _find_by(hwnd, name_eq="登录")
        if btn is None:
            out["error"] = ("E-QQ-004 没找到「登录」按钮。可能这个 QQ 不是登录页"
                            "（已经登录了？），或界面结构变了。")
            return out
        out["button"] = _describe(btn)
        out["patterns"] = _patterns_available(btn)

        chk = _find_by(hwnd, cls_has=AUTO_LOGIN_CLASS) if check_auto else None
        if chk is not None:
            out["auto_login_checkbox"] = _describe(chk)
            out["auto_login_patterns"] = _patterns_available(chk)

        if dry_run:
            out["ok"] = True
            out["note"] = "dry-run：只报可行性，没有点击任何东西"
            return out

        # --- 勾「自动登录」---
        if chk is not None and out.get("auto_login_patterns", {}).get("toggle"):
            try:
                chk.GetPattern(_auto().PatternId.TogglePattern).Toggle()
                out["auto_login_toggled"] = True
            except Exception as exc:
                out["auto_login_toggled"] = False
                out["auto_login_error"] = f"{type(exc).__name__}: {exc}"

        # --- 按「登录」---
        pats = out["patterns"]
        if pats.get("invoke"):
            try:
                btn.GetPattern(_auto().PatternId.InvokePattern).Invoke()
                out["clicked_via"] = "InvokePattern"
            except Exception as exc:
                out["error"] = f"Invoke 失败：{type(exc).__name__}: {exc}"
        elif pats.get("legacy"):
            try:
                btn.GetLegacyIAccessiblePattern().DoDefaultAction()
                out["clicked_via"] = "LegacyIAccessible.DoDefaultAction"
            except Exception as exc:
                out["error"] = f"DoDefaultAction 失败：{type(exc).__name__}: {exc}"
        else:
            out["error"] = ("E-QQ-004 这个按钮既不支持 Invoke 也不支持 "
                            "LegacyIAccessible —— 两条点击路径都断了")
        out["ok"] = not out["error"]
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = traceback.format_exc()[-600:]
        return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="结果 JSON 写到这里")
    ap.add_argument("--png", default="", help="画面落盘路径")
    ap.add_argument("--uia", action="store_true", help="顺便读一下界面")
    ap.add_argument("--tree", default="", help="把整棵 UIA 树 dump 到这个文件")
    ap.add_argument("--login", action="store_true",
                    help="在登录页上按「登录」（免扫码自动登录）")
    ap.add_argument("--dry-run", action="store_true",
                    help="配合 --login：只报可行性，不点击任何东西")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="启动后先等几秒，给 QQ 把窗口画出来")
    a = ap.parse_args()

    res: dict = {"ok": False, "error": "", "traceback": ""}
    try:
        # 1. 自报桌面 —— 这是「lpDesktop 真的生效」的唯一可信证据
        res["desktop"] = desktop.current_name()
        res["pid"] = os.getpid()

        if a.wait > 0:
            time.sleep(a.wait)

        # 2. 在本桌面里找 QQ 窗口
        found = find_qq_window()
        res["windows"] = found
        main = found["main"]
        if not main:
            res["error"] = ("本桌面里没有 QQ 主窗口"
                            "（QQ 可能还没起来，或窗口还没画出来）")
            return _write(a.out, res)

        res["hwnd"] = main["hwnd"]
        res["window"] = main

        # 3. 抓图
        if a.png:
            cap = winmsg.capture(main["hwnd"])
            res["capture"] = {k: v for k, v in cap.items() if k != "buf"}
            if cap.get("ok"):
                saved = winmsg.grab_any(main["hwnd"], a.png)
                res["saved"] = saved
            else:
                res["saved"] = {"ok": False, "error": cap.get("error") or "抓图失败"}

        # 4. 可选：读界面
        if a.uia:
            res["uia"] = probe_uia(main["hwnd"])
        if a.tree:
            res["tree"] = dump_tree(main["hwnd"], a.tree)
        if a.login:
            res["login"] = do_login(main["hwnd"], dry_run=a.dry_run)

        res["ok"] = True
        return _write(a.out, res)
    except Exception:
        res["traceback"] = traceback.format_exc()
        res["error"] = "宿主进程自身异常（见 traceback）"
        return _write(a.out, res)


def _write(path: str, res: dict) -> int:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
