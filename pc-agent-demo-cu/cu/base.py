# -*- coding: utf-8 -*-
"""
cu —— LLM Computer Use 执行面包（pc-agent-demo-cu 的新增层）

## 它在架构里的位置

    Brain（F4，tool calling 循环）
        ↓ 只认识下面的六个原语
    cu.base.Executor（本包定义的抽象接口）
        ├── cu.attach.AttachExecutor   附着主号（用户桌面，进程内 UIA）
        └── cu.hosted.HostedExecutor   独立桌面小号（一次性宿主子进程派发）

Brain **不允许**直接 import agent / qqid / app.host 去摸 QQ ——
所有动作必须走执行面。这样两条执行路径的安全策略（主号发送必须人工确认）
与错误信封才有唯一的收口处。

## 为什么动作空间是「语义原语」而不是截图+点击

QQNT 是 Electron，`--force-renderer-accessibility` 之后无障碍树的信息密度
（控件名 / 文本 / Pattern）远高于截图，而且免前台。像素级 computer use
是为「拿不到无障碍信息的任意软件」设计的兜底，在 QQ 上是绕远路。
本包只做语义原语；视觉（PrintWindow 截图 + 模型图像理解）在 F5 里只做
**校验**（verifier），不做动作来源。

## 线程约定（不可违反）

全进程仍然只有一条线程碰 UIA（`CUIAutomation` 是进程级单例）。
AttachExecutor 的方法**必须在 UIA 线程上调用**（与 agent 的 UiaWorker
同一约束）；HostedExecutor 不碰 UIA，可以随便调。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

__all__ = [
    "ExecutorError", "Executor", "SessionInfo", "ChatMessage",
    "SendReceipt", "Health", "Shot", "create_executor", "MODES",
]

#: 合法的执行面模式。工厂按它分发；配置里写了别的值就报 E-CU-002。
MODES = ("attach", "hosted")


class ExecutorError(Exception):
    """执行面动作失败。

    ## 为什么不用 AppError

    AppError 走的是 agent 的 report()/抑制器链路（带日志节流），
    而执行面错误是**返回给 Brain 的结构化结果**，不该被抑制、也不该
    静默进日志了事 —— Brain 要拿 code 决定下一步。

    ## 兜底码的纪律（承自 E-LLM-012 的教训）

    只在**确实无法判定根因**时才用 E-CU-001，且 detail 必须指明
    「真根因看前一条日志」。宁可码少，不可码错。
    """

    def __init__(self, code: str, detail: str = "", ctx: dict | None = None):
        super().__init__(f"{code} {detail}")
        self.code = code
        self.detail = detail
        self.ctx = dict(ctx or {})

    def envelope(self) -> dict:
        """转成与 app.errors 同构的 JSON 信封（ok=False 分支）。"""
        return {"ok": False, "error": {"code": self.code,
                                       "detail": self.detail, "ctx": self.ctx}}


@dataclass
class SessionInfo:
    """会话列表里的一条。字段与 qqid.SessionCard 对齐，但只留 Brain 需要的。"""
    index: int          # 在会话列表里的序号（0 起，open_chat 可用它定位）
    name: str           # 显示名（群名 / 昵称）
    unread: int         # 未读数
    is_group: bool

    def to_dict(self) -> dict:
        return {"index": self.index, "name": self.name,
                "unread": self.unread, "is_group": self.is_group}


@dataclass
class ChatMessage:
    """当前会话里的一条消息。字段是 agent.Message 的子集 + JSON 化的 rect。"""
    sender: str
    content: str
    direction: str          # "me" | "other" | "unknown"
    kind: str = "text"      # "text" | "nontext"（图片/语音/文件等）
    ts: str = ""            # QQ 只在间隔大时显示时间，可能为空
    dir_src: str = ""       # 方向判据来源（class/nick/position/...）

    def to_dict(self) -> dict:
        return {"sender": self.sender, "content": self.content,
                "direction": self.direction, "kind": self.kind,
                "ts": self.ts, "dir_src": self.dir_src}


@dataclass
class SendReceipt:
    """send_text 的回执。verifier（F5）要靠它决定要不要再做视觉复核。"""
    ok: bool
    route: str = ""         # 写入走了哪一档（wmchar / clipboard / focus 计划）
    chat_title: str = ""    # 发送时的会话标题（读回验证的锚点之一）
    detail: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "route": self.route,
                "chat_title": self.chat_title, "detail": self.detail}


@dataclass
class Health:
    """执行面体检结果。ok 的判据与 agent.dom_exposed() 保持一致。"""
    ok: bool
    mode: str = ""
    chat_open: bool = False     # 是否已停在可用的聊天页（会话列表页 = False）
    code: str = ""              # 失败/降级时的精确错误码（成功为空）
    detail: str = ""
    extra: dict = field(default_factory=dict)   # 下层诊断原文，供界面展开

    def to_dict(self) -> dict:
        return {"ok": self.ok, "mode": self.mode, "chat_open": self.chat_open,
                "code": self.code, "detail": self.detail, "extra": self.extra}


@dataclass
class Shot:
    """截图回执。后缀不能假定 —— 没有 Pillow 时会落 .bmp（打包版刻意排除 Pillow）。"""
    ok: bool
    path: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "path": self.path, "error": self.error}


class Executor(ABC):
    """六个语义原语。两条执行面都必须完整实现（不允许"先跑通一半"）。"""

    #: 执行面名（= MODES 之一），Health / 日志里要带上
    name: str = "?"
    #: True 表示 send_text 必须显式 armed=True 才会真正发送（attach 面）。
    #: 安全策略收在**最底层**：上层忘了检查也发不出去。
    require_confirmation: bool = False

    @abstractmethod
    def list_sessions(self) -> list[SessionInfo]:
        """列出当前会话列表（只读，不点击不切换）。"""

    @abstractmethod
    def open_chat(self, name: str = "", index: int = -1) -> dict:
        """切到一个会话。name 优先（子串匹配），否则用 index。

        index 的语义（2026-09-25 修正）：**0 起的合法序号**，与 list_sessions
        返回的 index 一致；-1 = 未指定（name 与 index 都缺 → E-CU-005）。
        实测踩坑：把 0 当「未指定」会挡掉列表第一个会话（恰是常测的群）。"""

    @abstractmethod
    def read_recent(self, limit: int = 12) -> list[ChatMessage]:
        """读**当前打开的会话**最近 limit 条消息（含自己发的，direction 区分）。"""

    @abstractmethod
    def send_text(self, text: str, *, armed: bool | None = None) -> SendReceipt:
        """往当前打开的会话发一条文本（内含读回验证）。

        armed 语义：None = 按 require_confirmation 的默认走；
        显式 False 在 require_confirmation 面上同样拒绝。
        """

    @abstractmethod
    def screenshot(self, path: str = "") -> Shot:
        """抓当前 QQ 窗口画面落盘。path 留空则落到数据目录下自动命名。"""

    @abstractmethod
    def health(self) -> Health:
        """体检：QQ 在不在、树通不通、是否停在可用的聊天页。只读。"""

    def current_chat_title(self) -> str:
        """当前会话标题（安全闸 send/read 校验用）。

        缺省返回空串（= 闸侧 fail-closed）；两面各自给出真实现：
        hosted 走宿主回执的顶层 title，attach 扫聊天页标题控件。
        探测动作失败应上抛 ExecutorError（真根因优先于闸拦截）。"""
        return ""


def create_executor(mode: str, cfg: dict) -> Executor:
    """按模式构造执行面并**包上安全闸**（cu/gate.py，A1 拍板：闸收在
    所有调用路径的必经之地）。闸的名单来自 cfg 的 cu 节（唯一来源 =
    本地 config.json）。**延迟导入**：tests / 只用某一面的调用方
    不应为另一面付出 agent / uiautomation 的导入开销。

    要绕过闸的受信本地工具（host_setup 等）直接构造具体执行面类，
    不走本工厂——这是有意的：闸管「agent 可达的路径」，不管人手敲的
    本地体检工具。

    Raises:
        ExecutorError: mode 不合法（E-CU-002）。
    """
    if mode not in MODES:
        raise ExecutorError("E-CU-002", f"执行面模式 {mode!r} 不在 {list(MODES)} 里",
                            {"合法值": list(MODES)})
    if mode == "attach":
        from cu.attach import AttachExecutor
        inner: Executor = AttachExecutor(cfg)
    else:
        from cu.hosted import HostedExecutor
        inner = HostedExecutor(cfg)
    from cu.gate import GatedExecutor, gates_from_cfg
    open_allow, read_allow = gates_from_cfg(cfg)
    return GatedExecutor(inner, open_allow=open_allow, read_allow=read_allow)
