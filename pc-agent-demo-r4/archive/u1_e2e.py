# -*- coding: utf-8 -*-
"""
u1_e2e.py —— U1 端到端的**子进程**（被 spawn 到隐藏桌面上跑）

## 它要回答什么

U1 = 「登录态 QQ 在隐藏桌面上，能不能真的干活」。拆成三问：
    Q1 能不能**附着**并**读到消息**（UIA）
    Q2 能不能**写进**输入框（V1 用的是剪贴板+Ctrl+V，走输入队列，隐藏桌面必失效）
    Q3 能不能**发出去**（发送按钮走 Invoke，理论上隐藏桌面可用）

本轮只做 **Q1（只读）**：切到指定会话、读消息、看编辑器状态。
`--send TEXT` 才会走到 Q2/Q3，那一步会**真的发消息**，先不做。

## 为什么必须作为子进程跑

`EnumWindows` 只枚举调用者所在桌面。壳在用户桌面上，看不到隐藏桌面的 QQ。
所以这个脚本必须被 `desktop.spawn(..., desktop="QQAgentHidden")` 丢过去。

## 结果怎么出来

写 JSON 文件。父进程读文件。
（`CreateProcessW` 没接管 stdout，print 出来父进程也看不见。）

用法：
    python archive/u1_e2e.py --out <json> [--want 会话名] [--send TEXT] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="结果 JSON 写到这里")
    ap.add_argument("--want", default="", help="要切到的会话名（子串匹配）")
    ap.add_argument("--read-limit", type=int, default=10)
    ap.add_argument("--tree", default="", help="切换后把消息区子树 dump 到这个文件")
    ap.add_argument("--type", dest="type_text", default="",
                    help="写进去验证好不好使，**写完擦掉、不发送**")
    ap.add_argument("--send", default="",
                    help="写进去并真的点发送（会真的发一条消息给对方）")
    a = ap.parse_args()

    res: dict = {"ok": False, "error": "", "traceback": ""}
    try:
        from app import desktop
        res["desktop"] = desktop.current_name()
        res["pid"] = os.getpid()

        import agent as A
        import qqid

        cfg = A.load_config()
        q = A.QQWindow(cfg)
        if not q.attach():
            res["error"] = ("E-QQ-004 附着失败：没找到 QQ 窗口"
                            "（本进程所在桌面上没有可附着的 QQ）")
            return _write(a.out, res)
        res["attached"] = True
        res["dialog_title"] = q.dialog_title
        res["ml_list"] = q.ml_list is not None
        res["editor"] = q.editor is not None
        res["send_btn"] = q.send_btn is not None

        # ---- 会话列表 ----
        sessions = qqid.list_sessions(q.win)
        res["sessions"] = [
            {"index": s.index, "name": s.display_name, "unread": s.unread,
             "selected": s.selected, "preview": "".join(s.preview_texts)[:60]}
            for s in sessions[:20]
        ]

        # ---- 切到目标会话 ----
        target = None
        if a.want:
            for s in sessions:
                if a.want.lower() in (s.display_name or "").lower():
                    target = s
                    break
        if target is None:
            res["error"] = (f"没找到会话 {a.want!r}（现有："
                            f"{[s.display_name for s in sessions[:10]]}）")
            return _write(a.out, res)

        res["target"] = target.to_dict()
        switched = qqid.switch_session(target, q.win, prefer_noop=True)
        res["switched"] = bool(switched)

        # 切完重新附着一次：切会话之后控件树会变，旧引用失效
        q.attach()
        res["after_title"] = q.dialog_title
        res["after_ml_list"] = q.ml_list is not None
        res["after_editor"] = q.editor is not None

        # ---- 读消息 ----
        msgs = q.read_messages(limit=a.read_limit)
        # ⚠️ Message 的字段是 `content` 不是 `text`。写错字段名会得到一堆空字符串，
        # 看起来像「隐藏桌面上读不到消息」，实际是自己的锅 —— 这个坑踩过一次。
        res["messages"] = [
            {"direction": getattr(m, "direction", ""),
             "content": (getattr(m, "content", "") or "")[:120],
             "sender": (getattr(m, "sender", "") or ""),
             "kind": getattr(m, "kind", ""),
             "ts": getattr(m, "ts", "")}
            for m in msgs
        ]
        res["message_count"] = len(msgs)

        # ---- 编辑器状态 ----
        try:
            res["editor_text"] = q.editor_text()
        except Exception as exc:
            res["editor_text_error"] = f"{type(exc).__name__}: {exc}"

        # ---- 诊断：消息区到底长什么样 ----
        if a.tree and q.ml_list is not None:
            res["tree"] = _dump(q.ml_list, a.tree)

        # ---- Q2 写入 / Q3 发送 ----
        text = a.send or a.type_text
        if text:
            from app import winmsg as W
            top = q.win.NativeWindowHandle or 0
            kids = W.find_child(top, "Chrome_RenderWidgetHostHWND",
                                visible_only=False)
            renderer = kids[0] if kids else 0
            res["top_hwnd"] = top
            res["renderer_hwnd"] = renderer

            res["write"] = probe_write(q, top, renderer, text)

            if a.send:
                if not res["write"].get("ok"):
                    res["error"] = "写入没成功，不发送（避免发出空消息或半截文本）"
                else:
                    res["send"] = _do_send(q)
            else:
                # 只验证写入能力：打完就擦掉，绝不留下内容、更不发送
                res["cleanup"] = _clear_editor(q, top)

        res["ok"] = True
        return _write(a.out, res)
    except Exception:
        res["traceback"] = traceback.format_exc()
        res["error"] = "子进程自身异常（见 traceback）"
        return _write(a.out, res)


#: 输入框为空时 QQ 给的占位提示。它**不是**真实内容 —— 判「空」必须把它算进去，
#: 否则会把空输入框当成「有字」，然后以为写入失败了。
PLACEHOLDER_HINT = "按住 Win + Alt"

VK_BACK = 0x08


def _editor_is_empty(text: str) -> bool:
    return (not text) or (PLACEHOLDER_HINT in text)


def _clear_editor(q, top_hwnd: int, max_backspaces: int = 40) -> dict:
    """
    逐字 Backspace 清空输入框，回读确认。

    ## 为什么是逐字退格，不是 Ctrl+A + Delete

    V1 用的是 `auto.SendKeys("{Ctrl}a")` + `"{Delete}"` —— 走 OS 输入队列，
    在隐藏桌面上**根本投不进去**。而且 U2 那次实测还发现：即使用窗口消息发
    `Ctrl+A → Backspace`，QQ 的 ProseMirror 也**只删掉 1 个字**。
    逐字退格是当时唯一清干净的办法，这里沿用。

    ⚠️ 硬保护：只在**确实有内容**时才退。空输入框直接返回，不做任何操作。
    """
    from app import winmsg as W

    out = {"cleared": False, "backspaces": 0, "final": "", "error": ""}
    for i in range(max_backspaces):
        try:
            cur = q.editor_text()
        except Exception as exc:
            out["error"] = f"读不回输入框：{type(exc).__name__}: {exc}"
            return out
        if _editor_is_empty(cur):
            out["cleared"] = True
            out["final"] = cur
            return out
        W.send_vk(top_hwnd, VK_BACK)
        out["backspaces"] = i + 1
    out["final"] = cur if "cur" in dir() else ""
    out["error"] = f"退了 {max_backspaces} 次还没清干净（最后读到 {out['final']!r}）"
    return out


def probe_write(q, top_hwnd: int, renderer: int, text: str) -> dict:
    """
    多档阶梯：找到「在隐藏桌面上能把字打进 QQ 输入框」的最小动作集。

    U2 在**用户桌面**上验证过真实 QQ 的 ProseMirror 接受 WM_CHAR，
    但判据是「编辑器处于 ProseMirror-focused」而不是「窗口是前台」。
    隐藏桌面上没有前台可言，所以到底需要几步，只能实测。

    档位从简到繁（先试零激活，能成就不必加动作）：
        1  零激活     → 直接向顶层窗口投递
        2  focus      → focus_steps 之后向顶层投递
        3  focus+渲染 → focus_steps 之后向 Chrome_RenderWidgetHostHWND 投递
    """
    from app import winmsg as W

    out = {"ok": False, "text": text, "attempts": [], "landed_via": ""}

    cur = q.editor_text()
    if not _editor_is_empty(cur):
        out["error"] = (f"输入框不是空的（{cur!r}）—— 中止，绝不覆盖可能存在的草稿。"
                        f"请手动清空后重跑。")
        return out

    plans = [
        ("1 零激活/顶层", False, 0),
        ("2 focus/顶层", True, 0),
        ("3 focus/渲染层", True, 1),
    ]
    for label, do_focus, which in plans:
        # 每档开始前都要保证输入框是空的，否则回读分不清「新写的」和「残留的」
        cl = _clear_editor(q, top_hwnd)
        if not cl["cleared"]:
            out["attempts"].append({"plan": label, "error": f"清不空：{cl['error']}"})
            continue

        snap = {}
        if do_focus:
            snap = W.focus_steps(top_hwnd, renderer if which else 0)
        target = (renderer if which else top_hwnd) or top_hwnd
        sent = W.send_text(target, text, mode="char")
        try:
            got = q.editor_text()
        except Exception as exc:
            got = f"<读回失败 {type(exc).__name__}: {exc}>"

        landed = (got or "") == text
        out["attempts"].append({
            "plan": label, "focus": do_focus, "target_hwnd": target,
            "sent": {k: v for k, v in sent.items() if k != "buf"},
            "focus_snap": snap, "read_back": got, "landed": landed,
        })
        if landed:
            out["ok"] = True
            out["landed_via"] = label
            break

    return out


def _do_send(q) -> dict:
    """
    点发送按钮。**走 InvokePattern，不走 SendKeys**。

    V1 的兜底是 `auto.SendKeys("{Enter}")` —— 输入队列，隐藏桌面投不进去。
    发送按钮是 UIA 控件，`Invoke` 是控件级调用，不受桌面限制。
    """
    out = {"ok": False, "via": "", "error": ""}
    before = [(getattr(m, "content", ""), getattr(m, "direction", ""))
              for m in q.read_messages(limit=6)]
    out["before_count"] = len(before)

    if q.send_btn is None:
        out["error"] = "没定位到发送按钮"
        return out
    try:
        q.send_btn.GetInvokePattern().Invoke()
        out["via"] = "InvokePattern"
    except Exception as exc:
        out["error"] = f"Invoke 失败：{type(exc).__name__}: {exc}"
        return out

    # 回读确认：真的多了一条「自己发的」消息才算数
    import time
    for _ in range(10):
        time.sleep(0.6)
        q.refresh_layout(force=True)
        after = [(getattr(m, "content", ""), getattr(m, "direction", ""))
                 for m in q.read_messages(limit=6)]
        if len(after) > len(before) and after[-1][1] == "me":
            out["ok"] = True
            out["after_count"] = len(after)
            out["last"] = after[-1]
            return out
    out["after_count"] = len(before)
    out["error"] = "点了发送但回读没看到新消息（可能没发出去，也可能只是慢）"
    return out


def _dump(root, path: str, maxdepth: int = 16, limit: int = 8000) -> dict:
    """
    dump 一棵子树（缩进文本）。

    为什么要有它：`read_messages` 找到了消息项却取不到正文时，
    唯一的判断依据就是「这些节点到底带了什么属性」——先看树，再改解析。
    """
    out = {"ok": False, "nodes": 0, "path": path, "error": ""}
    lines: list[str] = []
    stack = [(root, 0)]
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
            val = ""
            try:
                lp = ctrl.GetPropertyValue(3011)  # LegacyIAccessibleValueProperty
                if lp:
                    val = str(lp)[:60]
            except Exception:
                pass
        except Exception:
            continue
        lines.append(f"{'  ' * d}[{ctype}] name={name!r} class={cls!r} "
                     f"aid={aid!r} val={val!r} rect={rect}")
        try:
            for ch in reversed(ctrl.GetChildren()):
                stack.append((ch, d + 1))
        except Exception:
            pass
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        out.update({"ok": True, "nodes": n})
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


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
