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

import os
import time

import agent
import error_codes as EC
import qqid
from app import paths, winmsg

from cu.base import (ChatMessage, Executor, ExecutorError, Health,
                     SendReceipt, SessionInfo, Shot)


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

    def _cards(self) -> list:
        """会话卡片（qqid.SessionCard），list_sessions / open_chat 共用。"""
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
            raise ExecutorError("E-QQ-007", "会话列表为空",
                                {"建议": "确认 QQ 停在「消息」标签页，且刚启动的话稍等几秒"})
        return win, cards

    # ---------------------------------------------------- 六原语
    def list_sessions(self) -> list[SessionInfo]:
        _, cards = self._cards()
        return [SessionInfo(index=c.index, name=c.display_name,
                            unread=c.unread, is_group=c.looks_group)
                for c in cards]

    def open_chat(self, name: str = "", index: int = 0) -> dict:
        if not name and index <= 0:
            raise ExecutorError("E-CU-005",
                                "open_chat 需要 name（子串匹配）或 index（>0）之一",
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
        p = path or os.path.join(
            paths.STATE_DIR, f"cu-shot-{time.strftime('%Y%m%d-%H%M%S')}.png")
        try:
            res = winmsg.grab_any(win.hwnd(), p)
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
        chat_open = bool(ctx.get("chat_open")
                         or ctx.get("ml_list_found") or ctx.get("editor_found"))
        return Health(ok=ok, mode=self.name, chat_open=chat_open,
                      code=("" if ok else code), detail=("" if ok else "DOM 未暴露"),
                      extra=ctx)
