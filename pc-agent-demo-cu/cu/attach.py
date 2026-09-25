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

    def _ensure_qq_visible(self, hwnd: int = 0) -> None:
        """QQ 最小化时自动 SW_RESTORE 还原（2026-09-25 实测驱动）。

        ## 为什么是「还原」而不是「抢前台」

        实测结论（见 archive/probe_minimized.py / probe_switch_nofg.py）：
        最小化时 Chromium 节流 renderer，**会话列表读不出、Invoke 静默失效**；
        但被遮挡（还原后不置顶）时 Invoke 与读取都可用。所以只还原、
        不调 force_foreground —— 尽量不打扰用户。

        ## 实测现场（2026-09-25）

        主号最小化时 list_sessions 三连 E-QQ-007「会话列表为空」，
        还原窗口后同一函数立刻读出全部会话。"""
        if not hwnd:
            _, hwnd = qqid.find_qq_main_window()
        if not hwnd or not winmsg.is_iconic(hwnd):
            return
        _user32.ShowWindow(hwnd, _SW_RESTORE)
        agent.log("INFO", f"QQ 处于最小化，已还原窗口（hwnd={hwnd}，不抢前台）")
        time.sleep(1.5)   # 给 renderer 一点时间恢复 a11y 树

    def _cards(self) -> tuple:
        """会话卡片（qqid.SessionCard），list_sessions / open_chat 共用。
        最小化导致读空时自动还原窗口重试一次。"""
        self._ensure_qq_visible()
        desc, hwnd = qqid.find_qq_main_window()
        if not hwnd:
            raise ExecutorError("E-QQ-003", "找不到可见的 QQ 主窗口",
                                {"窗口枚举": desc})
        try:
            win = agent.control_from_hwnd(hwnd)
            cards = qqid.list_sessions(win)
        except Exception as exc:
            mapped = EC.wrap(exc, "E-CU-001")
            raise ExecutorError(mapped.code, mapped.detail_text or "读会话列表失败",
                                {"异常": f"{type(exc).__name__}: {exc}"})
        if not cards:
            # 最小化是 E-QQ-007 的常见隐形根因：还原后再试一次
            self._ensure_qq_visible(hwnd)
            try:
                cards = qqid.list_sessions(win)
            except Exception as exc:
                cards = []
                agent.log("INFO", f"还原后重读会话列表仍失败：{type(exc).__name__}: {exc}")
        if not cards:
            raise ExecutorError("E-QQ-007", "会话列表为空",
                                {"建议": "确认 QQ 停在「消息」标签页；若窗口最小化，"
                                         "程序已自动还原过一次，仍读不出请把 QQ 窗口"
                                         "还原到桌面上再试"})
        return win, cards

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
        return SendReceipt(ok=True, route=win.last_write_plan,
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
        win = self._window()
        if win.win is None and not win.attach():
            code, ctx = win.diagnose_attach()
            return Health(ok=False, mode=self.name, chat_open=False,
                          code=code or "E-QQ-003", detail="附着 QQ 主窗口失败",
                          extra=ctx)
        code, ctx = win.diagnose_dom()
        ok = win.dom_exposed()
        # diagnose_dom 的 ctx 用中文键（消息列表/输入框/窗口可见）——
        # 2026-09-25 实测发现：按英文键取永远 False
        chat_open = bool(ctx.get("消息列表") or ctx.get("输入框"))
        return Health(ok=ok, mode=self.name, chat_open=chat_open,
                      code=("" if ok else code), detail=("" if ok else "DOM 未暴露"),
                      extra=ctx)
