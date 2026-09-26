# -*- coding: utf-8 -*-
"""
qq-mcp.py —— MCP 薄壳：把 qq-cu 六原语 + run/ask 包成 MCP server（stdio）

## 定位（2026-09-26 拍板：A+A）

- 手写 stdio JSON-RPC 子集（initialize / ping / tools/list / tools/call），
  **零新依赖**；不引入官方 mcp SDK（pydantic 依赖链不值当）。
- 本进程只做协议翻译，**绝不碰 UIA**：每次 tools/call 派一次性
  `<python> qq-cu.py <子命令>` 子进程（承「宿主进程里不要提前碰 UIA」铁律，
  UIA 坏状态随子进程退出消失），解析 JSON 信封后包成 MCP tool result。
- 交付形态：**源码直跑**。宿主（WorkBuddy / Claude Code）mcp.json 里配：

      "qq-cu": {
        "command": "<python.exe 全路径>",
        "args": ["D:\\...\\pc-agent-demo-cu\\qq-mcp.py"],
        "env": { "QQ_AGENT_HOME": "D:\\...\\pc-agent-demo-cu" }
      }

  注意：command 指向的 python 必须装了本项目依赖（psutil / uiautomation…）。
  想让壳转调 qq-cu.exe 而不是源码，设环境变量 QQ_CU_EXE 指向 exe 即可。

## 协议注意

- stdout 只准出协议帧（单行 JSON，无内嵌换行）；日志/诊断全走 stderr。
- 请求/响应按 id 配对；通知（无 id）不应答。
- qq-cu 的输出契约（{"ok", tool, result/answer/error} JSON 信封）原样透传，
  失败信封映射为 MCP 的 isError=true，错误码（E-QQ-*/E-CU-*）不翻译。

## 壳自身的错误码（仅在壳层失败时出现，正常失败来自子进程信封）

- E-MCP-001 子进程超时（可用环境变量 QQ_MCP_TIMEOUT_<工具大写名> 覆盖秒数）
- E-MCP-002 子进程拉起失败（python 路径不对 / 依赖缺失等）
- E-MCP-003 子进程 stdout 里找不到 JSON 信封（参数错/崩溃，stderr 附在 ctx）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "qq-cu-mcp", "version": "1.0.0"}

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# 每个工具的子进程超时（秒）；run 是完整任务循环，给最宽。
_DEFAULT_TIMEOUTS = {
    "qq_health": 90,
    "qq_list_sessions": 90,
    "qq_open_chat": 120,
    "qq_read_recent": 120,
    "qq_screenshot": 120,
    "qq_send_text": 120,
    "qq_run_task": 900,
    "qq_ask_server": 180,
}

# ---------------------------------------------------------------------------
# 工具定义（name 映射 qq-cu 子命令；schema 供宿主 agent 自文档）
# ---------------------------------------------------------------------------

_MODE_PROP = {
    "mode": {
        "type": "string",
        "enum": ["hosted", "attach"],
        "default": "hosted",
        "description": "执行面：hosted=隐藏桌面小号（全自主发送）；"
                       "attach=附着主号（fail-closed，非交互终端拒绝发送）",
    }
}


def _tooldefs() -> list[dict]:
    return [
        {
            "name": "qq_health",
            "description": "执行面体检：QQ 是否可操作、当前打开的会话等（只读）",
            "inputSchema": {"type": "object", "properties": dict(_MODE_PROP)},
        },
        {
            "name": "qq_list_sessions",
            "description": "读 QQ 会话列表（只读）",
            "inputSchema": {"type": "object", "properties": dict(_MODE_PROP)},
        },
        {
            "name": "qq_open_chat",
            "description": "打开指定会话（按名称或列表序号）。发送前必须先 open。"
                           "受目标闸 cu.open_chat_allow 约束：名单外/无授权书时拒绝（E-CU-004）",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "会话名（昵称/群名）"},
                    "index": {"type": "integer", "description": "会话列表序号（name 为空时用）"},
                    **_MODE_PROP,
                },
            },
        },
        {
            "name": "qq_read_recent",
            "description": "读当前会话最近 N 条消息（先 open 再 read）。"
                           "受读取闸 cu.read_allow 约束：非空=仅名单内会话可读（安全优先）；"
                           "留空=全可读（方便优先）",
            "inputSchema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 12}, **_MODE_PROP},
            },
        },
        {
            "name": "qq_screenshot",
            "description": "抓当前 QQ 窗口截图，返回落盘路径。"
                           "受读取闸 cu.read_allow 约束（同 qq_read_recent）",
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "截图落盘路径（缺省进 state/）"},
                               **_MODE_PROP},
            },
        },
        {
            "name": "qq_send_text",
            "description": "向当前会话发送文本（先 open）。attach 面 fail-closed："
                           "非交互终端直接拒绝；hosted 面受目标闸 open_chat_allow 约束"
                           "（名单外拒绝，名单为空=fail-closed）。名单管理：qq-cu allow",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}, **_MODE_PROP},
                "required": ["text"],
            },
        },
        {
            "name": "qq_run_task",
            "description": "委托式：把整个任务（如『给XX发一句在吗』）交给 CU 内置 "
                           "Brain 走工具循环，返回完整 trace。需要 DeepSeek Key",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "max_steps": {"type": "integer", "description": "覆盖 cu.max_steps"},
                    **_MODE_PROP,
                },
                "required": ["task"],
            },
        },
        {
            "name": "qq_ask_server",
            "description": "服务端大脑（纯 HTTP，不碰 QQ 执行面）：把用户消息交给 "
                           "ai-web-page /api/qq/agent 生成回复文本",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {"type": "array", "items": {"type": "string"},
                             "description": "用户消息，按时间序；多条构成聚合批"},
                    "qq": {"type": "string", "description": "小号 QQ 号"},
                    "scope": {"type": "string", "enum": ["private", "group"], "default": "private"},
                    "group_id": {"type": "string"},
                    "channel": {"type": "string", "enum": ["r4", "sealdice"], "default": "r4"},
                },
                "required": ["text"],
            },
        },
    ]


# ---------------------------------------------------------------------------
# 子进程调用（一次性，UIA 全在子进程里）
# ---------------------------------------------------------------------------

def _cli_base() -> list[str]:
    exe = os.environ.get("QQ_CU_EXE", "").strip()
    if exe:
        return [exe]
    return [sys.executable, os.path.join(HERE, "qq-cu.py")]


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # 防 cp936 把中文信封弄花
    return env


def _timeout(tool: str) -> float:
    raw = os.environ.get(f"QQ_MCP_TIMEOUT_{tool.upper()}", "")
    try:
        return float(raw) if raw else float(_DEFAULT_TIMEOUTS[tool])
    except ValueError:
        return float(_DEFAULT_TIMEOUTS[tool])


def _build_args(tool: str, a: dict) -> list[str]:
    """MCP 工具名+参数 → qq-cu 子命令行。"""
    mode = a.get("mode") or "hosted"
    if tool == "qq_health":
        return ["health", "--mode", mode]
    if tool == "qq_list_sessions":
        return ["sessions", "--mode", mode]
    if tool == "qq_open_chat":
        args = ["open", "--mode", mode]
        if a.get("name"):
            args += ["--name", str(a["name"])]
        if a.get("index") is not None:
            args += ["--index", str(a["index"])]
        return args
    if tool == "qq_read_recent":
        return ["read", "--mode", mode, "--limit", str(int(a.get("limit") or 12))]
    if tool == "qq_screenshot":
        args = ["shot", "--mode", mode]
        if a.get("path"):
            args += ["--path", str(a["path"])]
        return args
    if tool == "qq_send_text":
        return ["send", "--mode", mode, "--text", str(a.get("text", ""))]
    if tool == "qq_run_task":
        args = ["run", "--mode", mode, "--task", str(a.get("task", ""))]
        if a.get("max_steps"):
            args += ["--max-steps", str(int(a["max_steps"]))]
        return args
    if tool == "qq_ask_server":
        args = ["ask"]
        for t in a.get("text") or []:
            args += ["--text", str(t)]
        if a.get("qq"):
            args += ["--qq", str(a["qq"])]
        if a.get("scope"):
            args += ["--scope", str(a["scope"])]
        if a.get("group_id"):
            args += ["--group-id", str(a["group_id"])]
        if a.get("channel"):
            args += ["--channel", str(a["channel"])]
        return args
    raise KeyError(tool)


def _extract_envelope(stdout: str) -> dict | None:
    """从子进程 stdout 里找 JSON 信封。

    真实 stdout 可能是「agent 日志行 + 多行缩进 JSON 信封」混合
    （2026-09-26 实测：E-CFG-005 日志块在信封前面）。所以不能只试
    整块或单行——从每个 '{' 起点（从后往前）尝试解析到串尾，
    第一個能解析且含 ok 键的 dict 即信封。"""
    s = stdout or ""
    starts = [i for i, ch in enumerate(s) if ch == "{"]
    for i in reversed(starts):
        try:
            obj = json.loads(s[i:])
        except Exception:
            continue
        if isinstance(obj, dict) and "ok" in obj:
            return obj
    return None


def call_tool(tool: str, arguments: dict) -> dict:
    """派一次性子进程执行 qq-cu 子命令，返回透传的 JSON 信封。"""
    try:
        args = _build_args(tool, arguments or {})
    except KeyError:
        return {"ok": False, "error": {"code": "E-MCP-003",
                                       "detail": f"未知工具：{tool}", "ctx": {}}}
    try:
        proc = subprocess.run(
            _cli_base() + args, cwd=HERE, capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=_timeout(tool), creationflags=_NO_WINDOW,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {
            "code": "E-MCP-001",
            "detail": f"子进程超时（>{_timeout(tool):.0f}s）：{tool}",
            "ctx": {"tool": tool,
                    "hint": f"可设环境变量 QQ_MCP_TIMEOUT_{tool.upper()} 调大"}}}
    except OSError as exc:
        return {"ok": False, "error": {
            "code": "E-MCP-002",
            "detail": f"子进程拉起失败：{exc}",
            "ctx": {"cmd": _cli_base(),
                    "hint": "mcp.json 的 command 必须指向装了项目依赖的 python"
                            "（或设 QQ_CU_EXE 指向 qq-cu.exe）"}}}
    env = _extract_envelope(proc.stdout or "")
    if env is None:
        return {"ok": False, "error": {
            "code": "E-MCP-003",
            "detail": "子进程 stdout 里没有 JSON 信封",
            "ctx": {"rc": proc.returncode, "tool": tool,
                    "stderr_tail": (proc.stderr or "")[-500:]}}}
    return env


# ---------------------------------------------------------------------------
# MCP stdio 协议帧处理
# ---------------------------------------------------------------------------

def _result(mid, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": code, "message": message}}


def handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        client_v = params.get("protocolVersion")
        ver = client_v if isinstance(client_v, str) and client_v else PROTOCOL_VERSION
        return _result(mid, {"protocolVersion": ver,
                             "capabilities": {"tools": {}},
                             "serverInfo": SERVER_INFO})
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": _tooldefs()})
    if method == "tools/call":
        name = params.get("name") or ""
        known = {t["name"] for t in _tooldefs()}
        if name not in known:
            return _error(mid, -32602, f"unknown tool: {name}")
        res = call_tool(name, params.get("arguments") or {})
        return _result(mid, {
            "content": [{"type": "text",
                         "text": json.dumps(res, ensure_ascii=False)}],
            "isError": not res.get("ok", False),
        })
    if mid is None:          # 未知通知：不应答
        return None
    return _error(mid, -32601, f"method not found: {method}")


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    # stdout 是协议通道：强制 UTF-8 + 不做换行翻译
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            _send(_error(None, -32700, "parse error"))
            continue
        if isinstance(msg, list):
            _send(_error(None, -32600, "batch requests not supported"))
            continue
        resp = handle(msg)
        if resp is not None:
            _send(resp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
