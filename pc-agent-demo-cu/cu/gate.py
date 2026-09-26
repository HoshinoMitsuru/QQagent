# -*- coding: utf-8 -*-
"""
gate.py —— 执行面安全闸（2026-09-26 拍板 A1+C1+B）

## 为什么闸在这里

审计（2026-09-26）发现：目标白名单闸原来只在 Brain 循环里（cu/brain.py
的 _allow_check），工具式路径（CLI 六原语 / MCP 的 qq_open_chat +
qq_send_text）**不经过 Brain**，等于敞开。修法是 A1：把闸下沉到
**执行面**——所有调用路径（CLI / MCP / Brain / 未来宿主进程内形态）
的必经之地，单一事实来源。

Brain 的预检闸**保留**（同一份名单，防御纵深 + 更好看的 tool_blocked
trace），但执行面这层是**硬闸**：上层忘了检查也发不出去。

## 两道闸与两个键（config.json 的 cu 节，唯一来源 = 本地 config.json）

| 键 | 留空（默认） | 非空 |
| --- | --- | --- |
| `open_chat_allow` | attach：放行（确认锁兜底，人盯着）；**hosted：fail-closed**（open/send 一律拒——hosted 全自主发送，名单就是授权书，没有授权书不发） | 两面的 open_chat 与 send_text 仅名单内目标 |
| `read_allow`（B 开关） | **B1 方便优先**：read_recent / screenshot 不限 | **B2 安全优先**：仅名单内会话可读/可截屏；读不到当前会话标题 = fail-closed 拒绝 |

- 名单匹配语义与 Brain 一致：任一名单项与目标**互为子串**即命中
  （`a in t or t in a`），幂等兼容既有 config。
- `list_sessions` **永不拦**：它只暴露会话名，不暴露消息内容；拦掉它
  会让 index→name 解析失效。隐私代价已在 CLI用法.md 明示。
- `attach_allow` 键保留原语义（run/harness 的 attach 面名单，进入 run
  时映射到统一键）；执行层目标闸两面统一读 `open_chat_allow`。

## 错误码

拦截一律 E-CU-004（人工确认/白名单域），ctx 带 `闸` 字段与可执行的
修复 hint——一个错误码一个根因，不新开码。
"""

from __future__ import annotations

from cu.base import Executor, ExecutorError

__all__ = ["GatedExecutor", "gates_from_cfg", "target_allowed"]


def _norm(allow) -> list[str]:
    if not allow:
        return []
    return [s.strip() for s in allow if isinstance(s, str) and s.strip()]


def target_allowed(target: str, allow: list[str]) -> bool:
    """名单匹配：任一项与目标互为子串即放行（与 cu.brain._allow_check 同语义）。"""
    t = (target or "").strip()
    if not t:
        return False
    return any(a in t or t in a for a in allow)


def gates_from_cfg(cfg: dict) -> tuple[list[str], list[str]]:
    """从整份配置里取 (open_allow, read_allow)。cfg 缺 cu 节 = 两闸均空。"""
    cu = (cfg or {}).get("cu") or {}
    return _norm(cu.get("open_chat_allow")), _norm(cu.get("read_allow"))


class GatedExecutor(Executor):
    """执行面包装器：三道收口（open / send / read）。

    - 未配置的闸**零开销**：连标题探测都不做，原语直通内层。
    - 探测标题失败**原样上抛**内层的 ExecutorError（真根因优先于闸）；
      探测成功但拿不到标题 → fail-closed（E-CU-004）。
    - `__getattr__` 把其余属性（require_confirmation / name / health…）
      原样委托给内层，上层无感。
    """

    def __init__(self, inner: Executor, open_allow=None, read_allow=None):
        self._inner = inner
        self._open_allow = _norm(open_allow)
        self._read_allow = _norm(read_allow)

    # ---------------------------------------------------- 委托
    # name / require_confirmation 在基类上有**类属性**，普通查找会命中
    # 基类缺省值而绕过 __getattr__ —— 必须显式 property 委托
    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def require_confirmation(self) -> bool:
        return self._inner.require_confirmation

    def __getattr__(self, item: str):
        # 只在普通属性找不到时才会进来。下划线属性也委托（包装器全透明，
        # 测试/内部要摸 inner 的 _win 等）；唯独 _inner 自身要拦——
        # __init__ 完成前访问它会无限递归。
        if item == "_inner":
            raise AttributeError(item)
        return getattr(self._inner, item)

    # ---------------------------------------------------- 闸原语
    def _deny(self, action: str, target: str, allow: list[str],
              hint: str) -> ExecutorError:
        return ExecutorError(
            "E-CU-004",
            f"{action} 目标「{target or '（未知）'}」不在允许名单"
            f"（{'、'.join(allow)}）内，已拦截",
            {"闸": action, "修复": hint})

    def _target_of_open(self, name: str, index: int) -> str:
        """open 的目标名。index-only 时经 list_sessions 解析名字；
        解析不到 = fail-closed（不猜）。"""
        if (name or "").strip():
            return name.strip()
        if index >= 0:
            for s in self._inner.list_sessions():
                if s.index == index:
                    return s.name
            raise self._deny("open_chat", f"index={index}（解析不到会话名）",
                             self._open_allow, "先用 list_sessions 再按 name 指定目标")
        raise self._deny("open_chat", "", self._open_allow,
                         "open_chat 需要 name 或 index")

    def _current_title(self) -> str:
        """当前会话标题。内层探测失败原样上抛（真根因），拿不到内容返回空。"""
        return (self._inner.current_chat_title() or "").strip()

    def _check_open(self, name: str, index: int) -> None:
        face = getattr(self._inner, "name", "?")
        if face == "hosted" and not self._open_allow:
            # hosted 空名单 = fail-closed：open 是通向发送的第一步，
            # 名单就是授权书，没有授权书连会话都不开
            raise self._deny("open_chat", (name or "").strip()
                             or (f"index={index}" if index >= 0 else ""),
                             [],
                             "hosted 面 open/send 必须先配置 cu.open_chat_allow"
                             "（qq-cu allow add --name 目标名）")
        if not self._open_allow:
            return                          # attach 空名单：确认锁兜底，放行
        target = self._target_of_open(name, index)
        if not target_allowed(target, self._open_allow):
            raise self._deny("open_chat", target, self._open_allow,
                             "把目标加入 config.json 的 cu.open_chat_allow"
                             "（或用 qq-cu allow add --name 目标名）")

    def _check_send(self) -> None:
        face = getattr(self._inner, "name", "?")
        if face == "hosted" and not self._open_allow:
            # hosted 空名单 = fail-closed（名单就是授权书）
            raise self._deny("send_text", "", [],
                             "hosted 面发送前必须配置 cu.open_chat_allow"
                             "（或用 qq-cu allow add --name 目标名）")
        if not self._open_allow:
            return                          # attach 空名单：确认锁兜底，放行
        title = self._current_title()
        if not target_allowed(title, self._open_allow):
            raise self._deny("send_text", title, self._open_allow,
                             "先 open 名单内会话，或把目标加入"
                             " cu.open_chat_allow")

    def _check_read(self, action: str) -> None:
        if not self._read_allow:            # B1：方便优先，不拦
            return
        title = self._current_title()
        if not target_allowed(title, self._read_allow):
            raise self._deny(action, title, self._read_allow,
                             "read_allow 非空 = 安全优先：把会话加入"
                             " cu.read_allow，或清空该键回到方便优先")

    # ---------------------------------------------------- 六原语（带闸）
    def list_sessions(self):
        # 不拦：只暴露会话名，不暴露消息内容（隐私代价已在文档明示）
        return self._inner.list_sessions()

    def open_chat(self, name: str = "", index: int = -1) -> dict:
        self._check_open(name, index)
        return self._inner.open_chat(name=name, index=index)

    def read_recent(self, limit: int = 12):
        self._check_read("read_recent")
        return self._inner.read_recent(limit)

    def send_text(self, text: str, *, armed: bool | None = None):
        self._check_send()
        return self._inner.send_text(text, armed=armed)

    def screenshot(self, path: str = ""):
        self._check_read("screenshot")
        return self._inner.screenshot(path)

    def health(self):
        return self._inner.health()

    def current_chat_title(self) -> str:
        return self._current_title()
