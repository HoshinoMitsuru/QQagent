# -*- coding: utf-8 -*-
"""
tools.py —— 把六个执行面原语包装成 LLM 可调用的工具（OpenAI tool calling 格式）

## 本模块的职责边界

    Brain（F4）           ── 模型吐出 {"name": ..., "arguments": ...}
        ↓ dispatch()
    cu.tools（本模块）     ── 校验 / 矫正 / 调原语 / 统一信封
        ↓
    cu.base.Executor      ── 六原语（F2）

Brain 不直接摸 Executor，dispatch 是唯一入口 —— 参数校验、确认锁、
错误信封才能有唯一的收口处。

## 安全设计：armed 绝不进 schema

attach 面的发送必须人工确认（E-CU-004 的设计）。**给模型看的 schema 里
没有 armed 这个参数** —— 模型永远不能给自己解锁。armed 只由上层的人工
确认路径传入：`dispatch(ex, "send_text", args, armed=True)`。
测试（test_tools.py §3）专门断言 schema 里不含 "armed"。

## 信封约定（Brain 要按它决定下一步）

    成功     {"ok": True,  "tool": 名称, ...动作各自的字段}
    确认锁   {"ok": False, "needs_confirmation": True, "text": ...,
              "error": {"code": "E-CU-004", ...}}   （2026-09-25 起与错误信封同形）
              "text": 拟发送内容}              ← 只出现在 send_text
    失败     {"ok": False, "error": {"code", "detail", "ctx"}}
             （ExecutorError 原样转信封；意外异常兜底 E-CU-001）

dispatch **不抛异常** —— Brain 的主循环不该被一次工具调用炸掉，
所有失败都以信封返回。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from cu.base import Executor, ExecutorError

__all__ = ["TOOLS", "tool_schemas", "dispatch", "ToolDef"]


# ============================================================ schema 片段
#
# 描述写给模型看：每个参数说清「是什么、怎么填、填错会怎样」。
# 模型拿到的描述质量直接决定 F4 的工具调用成功率，这里不省字。

_LIMIT = {
    "type": "integer",
    "description": "要读的消息条数。默认 12；读长上下文可到 100。"
                   "只会读到 QQ 界面里实际渲染出来的消息。",
}

_TOOLSPEC = {
    "list_sessions": {
        "description": "列出 QQ 当前会话列表（只读，不点击不切换）。"
                       "想知道「现在有哪些人在聊、谁有未读」时用它。"
                       "返回 index（序号）、name（会话名）、unread（未读数）、is_group。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "open_chat": {
        "description": "切换到指定会话（QQ 里点开一个聊天）。"
                       "name 是会话名的子串匹配（优先用）；index 是 list_sessions "
                       "返回的序号（name 给不出时才用）。切换成功后 read_recent / "
                       "send_text 都作用于这个会话。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "目标会话名的子串，如「霖韵」。"
                                        "不确定全名时给子串即可。"},
                "index": {"type": "integer",
                          "description": "会话序号（list_sessions 给的 index）。"
                                         "name 与 index 都给时以 name 为准。"},
            },
            "required": [],
        },
    },
    "read_recent": {
        "description": "读**当前打开的会话**最近的消息（含自己发的，direction 字段区分 "
                       "me/other）。调用前通常要先 open_chat。",
        "parameters": {"type": "object",
                       "properties": {"limit": _LIMIT},
                       "required": []},
    },
    "send_text": {
        "description": "往**当前打开的会话**发送一条文本消息。发送后程序会读回验证。"
                       "⚠️ 附着主号的执行面上，此调用会先返回待确认状态（"
                       "needs_confirmation=true），需人工批准后才会真正发出。",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string",
                                               "description": "要发送的完整文本，"
                                                             "不允许为空。"}},
                       "required": ["text"]},
    },
    "screenshot": {
        "description": "抓取当前 QQ 窗口的画面并保存为图片（供视觉校验或人工查看）。"
                       "返回文件路径；无 Pillow 环境会落成 .bmp，不要假定后缀。",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string",
                                               "description": "图片保存路径，"
                                                              "留空自动命名。"}},
                       "required": []},
    },
    "health": {
        "description": "执行面体检：QQ 在不在、无障碍树通不通、是否停在可用的聊天页。"
                       "其它工具接连失败时先用它定位环境问题。只读，无副作用。",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


@dataclass(frozen=True)
class ToolDef:
    name: str
    schema: dict                                   # OpenAI tool calling 完整定义
    run: Callable[..., dict]                       # 执行函数
    #: 允许出现的参数名（schema 之外的键一律拒绝 —— 防模型夹带私货，比如 armed）
    allowed: tuple[str, ...]


def _tool(name: str, run: Callable[..., dict]) -> ToolDef:
    spec = _TOOLSPEC[name]
    schema = {"type": "function",
              "function": {"name": name,
                           "description": spec["description"],
                           "parameters": spec["parameters"]}}
    return ToolDef(name=name, schema=schema, run=run,
                   allowed=tuple(spec["parameters"]["properties"].keys()))


# ============================================================ 各工具执行函数
#
# 只做「原语调用 + 结果拍平」，失败交给 dispatch 的统一兜底。
# send_text 特殊：确认锁在**这里**拦（返回信封而不是抛异常），
# 因为它不是故障，是 Brain 必须理解的正常状态。

def _run_list_sessions(ex: Executor, args: dict, armed: bool) -> dict:
    items = ex.list_sessions()
    return {"ok": True, "sessions": [s.to_dict() for s in items],
            "count": len(items)}


def _run_open_chat(ex: Executor, args: dict, armed: bool) -> dict:
    # index 缺省是 -1（未指定），**不是 0** —— 0 是合法序号（列表第一条）。
    # 2026-09-25 真机实测踩坑：把 0 当未指定会挡掉常测的群。
    name = args.get("name") or ""
    index = -1
    if "index" in args:
        try:
            index = int(args["index"])
        except (TypeError, ValueError):
            index = -1
    return {"ok": True, **ex.open_chat(name=name, index=index)}


def _run_read_recent(ex: Executor, args: dict, armed: bool) -> dict:
    msgs = ex.read_recent(int(args.get("limit") or 12))
    return {"ok": True, "messages": [m.to_dict() for m in msgs],
            "count": len(msgs)}


def _run_send_text(ex: Executor, args: dict, armed: bool) -> dict:
    if ex.require_confirmation and not armed:
        # 2026-09-25：形状统一为标准错误信封（此前是扁平 {"code": ...}，
        # 与 ExecutorError 的 {"error": {...}} 两套形状，CLI/MCP 的 JSON
        # 契约没法写）。needs_confirmation 顶层保留 —— Brain 靠它触发确认。
        return {"ok": False, "needs_confirmation": True,
                "text": args.get("text", ""),
                "error": {"code": "E-CU-004",
                          "detail": "主号执行面的发送必须人工确认后由上层"
                                    "以 armed=True 重发",
                          "ctx": {"策略": "attach=人工确认；hosted=全自主"}}}
    receipt = ex.send_text(args.get("text", ""), armed=armed)
    return {"ok": True, **receipt.to_dict()}


def _run_screenshot(ex: Executor, args: dict, armed: bool) -> dict:
    shot = ex.screenshot(path=args.get("path") or "")
    return {"ok": shot.ok, **shot.to_dict()}


def _run_health(ex: Executor, args: dict, armed: bool) -> dict:
    h = ex.health()
    return {"ok": h.ok, **h.to_dict()}


#: 注册表。dispatch / tool_schemas 都只认这一份 —— 新增工具只改这里。
TOOLS: dict[str, ToolDef] = {
    t.name: t for t in (
        _tool("list_sessions", _run_list_sessions),
        _tool("open_chat", _run_open_chat),
        _tool("read_recent", _run_read_recent),
        _tool("send_text", _run_send_text),
        _tool("screenshot", _run_screenshot),
        _tool("health", _run_health),
    )
}


# ============================================================ 对外接口

def tool_schemas() -> list[dict]:
    """给模型的 tools 定义（OpenAI chat completions 的 tools 参数原样用）。
    返回深拷贝，调用方改坏了不影响注册表。"""
    return json.loads(json.dumps([t.schema for t in TOOLS.values()],
                                 ensure_ascii=False))


def _coerce(value: Any, want: str) -> Any:
    """宽松矫正：模型偶尔把整数写成 "12"、布尔写成 "true"。
    矫正不了就原样返回，交给类型检查报 E-CU-007。"""
    if want == "integer" and isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return value
    if want == "boolean" and isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "false"):
            return low == "true"
    return value


def _validate(t: ToolDef, args: dict) -> str:
    """轻量校验（各工具参数很少，不值得引入 jsonschema 依赖）。
    返回错误描述；空串 = 通过。"""
    params = t.schema["function"]["parameters"]
    for key in params.get("required", []):
        if key not in args:
            return f"缺少必填参数 {key}"
    props = params.get("properties", {})
    for key, value in args.items():
        if key not in props:
            return f"未知参数 {key}（允许的参数：{list(props) or '无'}）"
        want = props[key].get("type")
        value = _coerce(value, want or "")
        if want == "integer" and not isinstance(value, int):
            return f"参数 {key} 应为整数，收到 {value!r}"
        if want == "string" and not isinstance(value, str):
            return f"参数 {key} 应为字符串，收到 {value!r}"
        if want == "boolean" and not isinstance(value, bool):
            return f"参数 {key} 应为布尔，收到 {value!r}"
    return ""


def dispatch(ex: Executor, name: str, arguments: Any,
             *, armed: bool = False) -> dict:
    """工具调用的唯一入口。**永不抛异常**，全部以信封返回。

    Args:
        ex: 执行面（F2）。
        name: 工具名（模型给的）。
        arguments: 参数 dict，或 JSON 字符串（模型给的原始串也能吃）。
        armed: 人工确认标记。只由上层确认路径传入，**绝不来自模型参数**。
    """
    t = TOOLS.get(name)
    if t is None:
        return {"ok": False, "tool": name,
                "error": {"code": "E-CU-006",
                          "detail": f"工具 {name!r} 未注册",
                          "ctx": {"可用工具": list(TOOLS)}}}

    # arguments 可能是 JSON 串（模型 raw 输出）——解析失败给 E-CU-007
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return {"ok": False, "tool": name,
                    "error": {"code": "E-CU-007",
                              "detail": f"arguments 不是合法 JSON：{exc}",
                              "ctx": {"工具": name}}}
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {"ok": False, "tool": name,
                "error": {"code": "E-CU-007",
                          "detail": f"arguments 应为对象，收到 {type(arguments).__name__}",
                          "ctx": {"工具": name}}}

    problem = _validate(t, arguments)
    if problem:
        return {"ok": False, "tool": name,
                "error": {"code": "E-CU-007", "detail": problem,
                          "ctx": {"工具": name,
                                  "schema": t.schema["function"]["parameters"]}}}

    try:
        result = t.run(ex, arguments, armed)
        result.setdefault("tool", name)     # 信封里永远能看出是哪个工具
        return result
    except ExecutorError as exc:
        return {"ok": False, "tool": name, **exc.envelope()}
    except Exception as exc:  # noqa: BLE001 —— Brain 主循环不允许被工具炸掉
        return {"ok": False, "tool": name,
                "error": {"code": "E-CU-001",
                          "detail": f"工具执行出现未分类异常：{type(exc).__name__}: {exc}",
                          "ctx": {"工具": name}}}
