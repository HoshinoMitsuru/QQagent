# -*- coding: utf-8 -*-
"""
attach.py —— 附着主号的执行面（用户桌面，进程内 UIA）

## 它是什么

把 agent.py 里**已实跑**的附着路径（`QQWindow` + `qqid` + `winmsg`）包成
六个语义原语。本模块不发明任何新的 QQ 交互机制 —— 凡是需要新机制的动作
一律不在加进这里，先在 agent/qqid 层做实跑验证。

## 安全策略（收在最底层，上层忘不掉）

`require_confirmation = True`：主号上每一条要发出去的消息都必须先给用户看过。
`send_text(armed=False/缺省)` 直接抛 E-CU-004，**根本不会碰到 QQ**。
这是 2026-09-25「误发群消息」事故的直接对策：安全约束不能指望上层记得。

## 线程约定

本模块所有方法内部会碰 UIA —— **必须在全进程唯一的 UIA 线程上调用**
（见 cu/base.py 的线程约定）。HostedExecutor 无此约束。

## 前台注意

`open_chat` 走 `qqid.switch_session`（InvokePattern 优先）。实测 Invoke
免前台但**不免焦点**：QQ 会自己把自己顶到最上层且不还。V1 常驻在 VM/无
人值守环境里这是可接受的；若将来挂进用户日常会话，上层要负责调
`agent.QQWindow` / `Agent._return_focus_after_switch` 的归还逻辑。
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

import agent
import error_codes as EC
import qqid
from app import paths, winmsg

from cu.base import (ChatMessage, Executor, ExecutorError, Health,
                     SendReceipt, SessionInfo, Shot)

# ShowWindow：本模块自用的最小声明（遵守「纯 ctypes 显式 argtypes」约定）
_user32 = ctypes.windll.user32
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.ShowWindow.restype = wintypes.BOOL
_SW_RESTORE = 9
_SW_SHOW = 5


def _dataclass_to_chat(m) -> ChatMessage:
    return ChatMessage(sender=m.sender, content=m.content, direction=m.direction,
                       kind=m.kind, ts=m.ts, dir_src=m.dir_src)


class AttachExecutor(Executor):
    name = "attach"
    require_confirmation = True

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._win = None          # 惰性构造的 agent.QQWindow
        self._attached = False

    # ---------------------------------------------------- 内部
    def _window(self) -> "agent.QQWindow":
        if self._win is None:
            self._win = agent.QQWindow(self.cfg)
        return self._win

    def _ensure_attached(self) -> "agent.QQWindow":
        """惰性附着。附着失败**原样上抛** diagnose_attach 的精确码，
        不允许在这里换成 E-CU-001（那会把「QQ 没开」和「真 bug」搅在一起）。"""
        win = self._window()
        if not self._attached:
            if not win.attach():
                code, ctx = win.diagnose_attach()
                raise ExecutorError(code or "E-QQ-003", "附着 QQ 主窗口失败", ctx)
            self._attached = True
        return win

    def _ensure_qq_visible(self, hwnd: int) -> None:
        """QQ 不在可用状态时自动救回（2026-09-25 真机两轮实测驱动）：

        - 最小化（IsIconic）→ SW_RESTORE：最小化时 Chromium 节流 renderer，
          会话列表读不出、Invoke 静默失效；
        - 隐藏（IsWindowVisible=False，如被收进托盘）→ SW_SHOW；
        - 被遮挡**不需要管**：还原后窗口可以躺在别的窗口下面，
          遮挡态下 Invoke 与读取都可用（probe_minimized 实测结论）。

        全程不调 force_foreground —— 尽量不打扰用户。"""
        if not hwnd:
            return
        if winmsg.is_iconic(hwnd):
            _user32.ShowWindow(hwnd, _SW_RESTORE)
            agent.log("INFO", f"QQ 处于最小化，已还原窗口（hwnd={hwnd}，不抢前台）")
            time.sleep(1.5)   # 给 renderer 一点时间恢复 a11y 树
        elif not winmsg.is_visible(hwnd):
            if _user32.ShowWindow(hwnd, _SW_SHOW):
                agent.log("INFO", f"QQ 主窗口处于隐藏状态，已显示（hwnd={hwnd}，不抢前台）")
                time.sleep(1.0)

    def _locate(self) -> tuple:
        """定位 QQ 主窗口控件 —— 只有一个事实来源：QQWindow.attach()。

        2026-09-25 第三轮真机教训：此前未附着时走 qqid.find_qq_main_window()
        （判据 title=="QQ" 且宽≥600），与 QQWindow.attach（挑最大的可见
        Chrome_WidgetWin_1，不管标题）**本来就不一致**，两条路会定位到
        不同的窗口：

        - 主窗口开着会话时标题是会话名（如「测试群B」）而非 "QQ"，
          find_qq_main_window 匹配不到它，却可能匹配到别的宽≥600 窗口；
        - 那个窗口的 UIA 树里没有会话列表 → list_sessions 返回空 →
          E-QQ-007 假象；「现场」键报的窗口状态也是它的，与附着窗口
          互相矛盾（真机日志：step1-4 报「非最小化」，step5 却检测到
          最小化并还原成功，还原后一切正常）。

        所以这里无条件走附着路径：health/read/screenshot 共用同一个
        附着状态与窗口句柄，不再有第二套定位判据。"""
        win = self._ensure_attached()
        return win.win, win.hwnd

    def _read_cards(self, win_ctrl) -> tuple:
        try:
            return qqid.list_sessions(win_ctrl), None
        except Exception as exc:
            agent.log("INFO", f"读会话列表异常：{type(exc).__name__}: {exc}")
            return [], exc

    def _cards(self) -> tuple:
        """会话卡片（qqid.SessionCard），list_sessions / open_chat 共用。
        窗口不可用时自动救回并重试一次。"""
        win_ctrl, hwnd = self._locate()
        self._ensure_qq_visible(hwnd)
        cards, first_exc = self._read_cards(win_ctrl)
        if not cards:
            self._ensure_qq_visible(hwnd)
            cards, first_exc = self._read_cards(win_ctrl)
        if not cards:
            if first_exc is not None:
                mapped = EC.wrap(first_exc, "E-CU-001")
                raise ExecutorError(mapped.code, mapped.detail_text or "读会话列表失败",
                                    {"异常": f"{type(first_exc).__name__}: {first_exc}"})
            raise ExecutorError("E-QQ-007", "会话列表为空",
                                {"建议": "确认 QQ 停在「消息」标签页；程序已自动尝试"
                                         "还原/显示窗口，仍读不出请手动把 QQ 窗口还原"
                                         "到桌面上再试",
                                 "现场": {"最小化": winmsg.is_iconic(hwnd),
                                          "可见": winmsg.is_visible(hwnd)}})
        return win_ctrl, cards

    # ---------------------------------------------------- 六原语
    def list_sessions(self) -> list[SessionInfo]:
        _, cards = self._cards()
        return [SessionInfo(index=c.index, name=c.display_name,
                            unread=c.unread, is_group=c.looks_group)
                for c in cards]

    def open_chat(self, name: str = "", index: int = -1) -> dict:
        if not name and index < 0:
            raise ExecutorError("E-CU-005",
                                "open_chat 需要 name（子串匹配）或 index（>=0，"
                                "list_sessions 给出的序号）之一",
                                {"name": name, "index": index})
        win, cards = self._cards()
        target = None
        if name:
            for c in cards:
                if name in c.display_name:
                    target = c
                    break
        elif 0 <= index < len(cards):
            target = cards[index]
        if target is None:
            raise ExecutorError("E-CU-005", "会话列表里没有匹配目标",
                                {"want": name, "index": index,
                                 "列表": [c.display_name for c in cards[:12]]})
        # switch_session 的成功判据就是「标题 == 目标名」，False 即 E-FG-004 的根因
        if not qqid.switch_session(target, win):
            raise ExecutorError("E-FG-004", "切换会话后标题没变",
                                {"目标": target.display_name,
                                 "注意": "窗口最小化时 Invoke 会静默失效，先还原窗口"})
        return {"ok": True, "opened": target.display_name}

    def read_recent(self, limit: int = 12) -> list[ChatMessage]:
        win = self._ensure_attached()
        try:
            msgs = win.read_messages(limit)
        except Exception as exc:
            mapped = EC.wrap(exc, "E-CU-001")
            raise ExecutorError(mapped.code, "读当前会话消息失败",
                                {"异常": f"{type(exc).__name__}: {exc}"})
        return [_dataclass_to_chat(m) for m in msgs]

    def send_text(self, text: str, *, armed: bool | None = None) -> SendReceipt:
        # 确认锁先于一切 QQ 交互 —— 这是本执行面存在的意义之一
        if self.require_confirmation and armed is not True:
            raise ExecutorError("E-CU-004",
                                "主号执行面的发送必须人工确认后以 armed=True 调用",
                                {"策略": "attach=人工确认；hosted=全自主"})
        win = self._ensure_attached()
        try:
            ok = win.send_text(text)
        except Exception as exc:
            mapped = EC.wrap(exc, "E-CU-001")
            raise ExecutorError(mapped.code, "发送链路抛出异常",
                                {"异常": f"{type(exc).__name__}: {exc}"})
        if not ok:
            # 不在这里断言根因：win.send_text 内部已经用精确码 report 过
            raise ExecutorError("E-CU-001", "发送未成功（根因见前一条错误码日志）",
                                {"route": win.last_write_plan})
        return SendReceipt(ok=True,
                           # last_write_plan 只在 wmchar 路径赋值（2026-09-25
                           # 真机实测：剪贴板路径回执 route=""）——
                           # 空时回退 write_mode_resolved（clipboard/wmchar）
                           route=(win.last_write_plan
                                  or getattr(win, "write_mode_resolved", "")),
                           chat_title=win.title_now())

    def screenshot(self, path: str = "") -> Shot:
        win = self._ensure_attached()
        # ⚠️ win.hwnd 是 @property —— 加括号会把 int 当函数调（'int' object is
        # not callable，2026-09-25 真机实测踩过）。另外最小化时 GetWindowRect
        # 给 0x0，抓图必然失败，先还原。
        hwnd = win.hwnd
        if hwnd:
            self._ensure_qq_visible(hwnd)
        p = path or os.path.join(
            paths.STATE_DIR, f"cu-shot-{time.strftime('%Y%m%d-%H%M%S')}.png")
        try:
            res = winmsg.grab_any(hwnd, p)
        except Exception as exc:
            return Shot(ok=False, error=f"{type(exc).__name__}: {exc}")
        if not res.get("ok"):
            return Shot(ok=False, error=res.get("error") or "抓图失败")
        return Shot(ok=True, path=res.get("file") or res.get("path") or p)

    def health(self) -> Health:
        # 走 _ensure_attached 而不是自己 win.attach()：
        # 2026-09-25 第三轮真机教训 —— 原写法附着成功但**不置 self._attached**，
        # 预检附着了主窗口，step1 的 _locate 却又走 find_qq_main_window
        # 重新定位到另一个窗口（根因见 _locate 的注释）。
        # 附着失败不抛（health 是探活原语，返回不 ok 的 Health 即可）。
        try:
            win = self._ensure_attached()
        except ExecutorError as exc:
            return Health(ok=False, mode=self.name, chat_open=False,
                          code=exc.code, detail=exc.detail,
                          extra=dict(exc.ctx))
        code, ctx = win.diagnose_dom()
        ok = win.dom_exposed()
        # diagnose_dom 的 ctx 用中文键（消息列表/输入框/窗口可见）——
        # 2026-09-25 实测发现：按英文键取永远 False
        chat_open = bool(ctx.get("消息列表") or ctx.get("输入框"))
        return Health(ok=ok, mode=self.name, chat_open=chat_open,
                      code=("" if ok else code), detail=("" if ok else "DOM 未暴露"),
                      extra=ctx)
