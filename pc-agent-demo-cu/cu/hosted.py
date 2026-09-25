# -*- coding: utf-8 -*-
"""
hosted.py —— 独立桌面小号的执行面（一次性宿主子进程派发）

## 为什么这边全靠「派子进程」

两条铁律的交点：

1. `EnumWindows` 只枚举调用者所在桌面的窗口 → 壳对隐藏桌面是瞎的，
   一切动作必须由**那张桌面上**的进程执行（`app/hostagent.py`）；
2. 宿主进程里**不要提前碰 UIA** —— 一次失败的 Invoke 会把本进程的
   uiautomation 弄成永久坏状态（-2147220991，实测不可自愈）。

所以本执行面**自己从不碰 UIA**：每个原语都通过 `host.grab()` 派一个
一次性的 hostagent 子进程进隐藏桌面干完活、写 JSON、退出。进程即用即弃，
UIA 坏状态跟着进程一起消失。

## 与 attach 面的安全差异

`require_confirmation = False`：这张桌面上的 QQ 是独立 profile 的小号，
误操作的爆炸半径不伤主号 —— 这正是双执行面存在的意义（2026-09-25
误发群消息事故的另一半对策）。

## 代价（诚实清单）

每 个原语一次 spawn + 等待 + 读文件，单次开销秒级。F4 的 Brain 若在
hosted 面上高频多步执行，应把整个 Brain 循环搬进宿主进程（hostd 形态），
届时执行面方法退化为进程内调用 —— 接口不变，这是抽象层买到的自由。
"""

from __future__ import annotations

from cu.base import (ChatMessage, Executor, ExecutorError, Health,
                     SendReceipt, SessionInfo, Shot)

#: hostagent 错误串前缀 → 精确错误码。只做**能确定的**映射（承 EC.wrap 纪律），
#: 前缀本身是 host.grab / hostagent 写进 error 字段的固定文案。
_ERROR_PREFIX_CODES = ("E-DESK-001", "E-DESK-002", "E-QQ-008", "E-UIA-003")
_ERROR_SUBSTR_CODES = (
    ("读不到宿主结果", "E-CU-003"),      # JSON 缺失/损坏
    ("超时未结束", "E-CU-003"),          # hostagent 子进程没在时限内退出
    ("本桌面里没有 QQ 主窗口", "E-DESK-003"),
)


def _map_host_error(err: str) -> str:
    for prefix in _ERROR_PREFIX_CODES:
        if err.startswith(prefix):
            return prefix
    for sub, code in _ERROR_SUBSTR_CODES:
        if sub in err:
            return code
    return "E-CU-001"


class HostedExecutor(Executor):
    name = "hosted"
    require_confirmation = False

    def __init__(self, cfg: dict, desktop_name: str = ""):
        self.cfg = cfg
        self.desktop_name = desktop_name   # 留空 = host.grab 用默认桌面名

    # ---------------------------------------------------- 内部
    def _run(self, **kw) -> dict:
        """派一次性宿主子进程执行动作并取回结果。

        host.grab 的 ok 指的是「子进程跑完了」，**不代表动作成功** ——
        动作结果在各自的嵌套字段里，由各原语自己检查。
        """
        from app import host
        out = host.grab(desktop_name=self.desktop_name, **kw)
        err = out.get("error") or ""
        if err and not out.get("ok"):
            raise ExecutorError(_map_host_error(err), err,
                                {"desktop": out.get("desktop")})
        return out

    @staticmethod
    def _cu(out: dict) -> dict:
        cu = out.get("cu") or {}
        if not cu.get("attached", False):
            raise ExecutorError(cu.get("code") or "E-QQ-003",
                                cu.get("error") or "宿主侧未能附着 QQ 主窗口",
                                cu.get("ctx") or {})
        return cu

    # ---------------------------------------------------- 六原语
    def list_sessions(self) -> list[SessionInfo]:
        out = self._run(list_sessions=True)
        cu = self._cu(out)
        return [SessionInfo(index=s.get("index", i), name=s.get("name", ""),
                            unread=int(s.get("unread", 0)),
                            is_group=bool(s.get("is_group")))
                for i, s in enumerate(cu.get("sessions") or [])]

    def open_chat(self, name: str = "", index: int = -1) -> dict:
        if not name and index < 0:
            raise ExecutorError("E-CU-005",
                                "open_chat 需要 name（子串匹配）或 index（>=0）之一",
                                {"name": name, "index": index})
        # hostagent 语义：index 0 = 第一条；-1（未指定）归到 0
        out = self._run(open_chat=True, chat=name, chat_index=max(index, 0))
        oc = out.get("open_chat") or {}
        if not oc.get("ok"):
            raise ExecutorError(_map_host_error(oc.get("error") or "开图失败"),
                                oc.get("error") or "宿主侧 open_chat 未成功",
                                {"target": oc.get("target", ""),
                                 "candidates": oc.get("candidates")})
        return {"ok": True,
                "opened": oc.get("target") or name or f"index={index}",
                "already_open": bool(oc.get("already_open")),
                "clicked": oc.get("clicked", "")}

    def read_recent(self, limit: int = 12) -> list[ChatMessage]:
        out = self._run(read_messages=int(limit))
        cu = self._cu(out)
        msgs = []
        for m in cu.get("messages") or []:
            msgs.append(ChatMessage(
                sender=m.get("sender", ""), content=m.get("content", ""),
                direction=m.get("direction", "unknown"),
                kind=m.get("kind", "text"), ts=m.get("ts", ""),
                dir_src=m.get("dir_src", "")))
        return msgs

    def send_text(self, text: str, *, armed: bool | None = None) -> SendReceipt:
        if not text:
            raise ExecutorError("E-CU-001", "send_text 拒绝空文本",
                                {"原因": "空文本会让「发送成功」的读回验证失去意义"})
        out = self._run(send_text=text)
        cu = self._cu(out)
        send = cu.get("send") or {}
        if not send.get("ok"):
            raise ExecutorError("E-CU-001", "宿主侧发送未成功（根因看 hostagent 日志）",
                                {"route": send.get("route", "")})
        return SendReceipt(ok=True, route=send.get("route", ""),
                           chat_title=send.get("title", ""))

    def screenshot(self, path: str = "") -> Shot:
        out = self._run(png_path=path or None)
        saved = out.get("saved") or {}
        if not saved.get("ok"):
            return Shot(ok=False, error=saved.get("error") or out.get("error")
                        or "抓图失败")
        return Shot(ok=True, path=saved.get("file") or saved.get("path")
                    or out.get("png", ""))

    def health(self) -> Health:
        out = self._run(state=True, uia=True)
        ls = out.get("login_state") or {}
        uia = ls.get("uia") or out.get("uia") or {}
        window_ok = bool(out.get("hwnd"))
        logged = bool(ls.get("logged_in"))
        chat_open = bool(uia.get("has_ml_list"))
        if not window_ok:
            code, detail = "E-DESK-003", "隐藏桌面上没有 QQ 主窗口"
        elif not ls.get("ok"):
            code, detail = "E-CU-001", ls.get("error") or "登录态读取失败"
        elif not logged:
            code, detail = "E-QQ-004", "QQ 未登录或停在登录页（先走登录动作）"
        else:
            code, detail = ("", "" if chat_open else "QQ 已登录但停在会话列表页，"
                            "没有打开任何会话")
            if not chat_open:
                code = "E-QQ-008"
        return Health(ok=window_ok and logged and chat_open, mode=self.name,
                      chat_open=chat_open, code=code, detail=detail,
                      extra={"login_state": {k: ls.get(k) for k in
                                             ("is_login_page", "logged_in",
                                              "nickname")},
                             "desktop": out.get("desktop", ""),
                             "window": out.get("window", {})})
