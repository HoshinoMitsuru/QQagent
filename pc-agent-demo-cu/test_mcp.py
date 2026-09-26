# -*- coding: utf-8 -*-
"""
test_mcp.py —— qq-mcp.py（MCP 薄壳）离线自检

覆盖四层：
1. 协议层（initialize / ping / tools/list / tools/call / 未知方法 / 通知不应答）
2. 参数映射（MCP 工具参数 → qq-cu 子命令行，8 个工具逐个核）
3. 信封提取（整块 JSON / 末行 JSON / 无信封）
4. 真子进程冒烟（喂 initialize+tools/list+ping 三帧，读三帧应答）

不碰 QQ、不碰 UIA。用法：python test_mcp.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OK = FAIL = 0


def case(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# 连字符文件名不能直接 import，用 importlib 加载
_spec = importlib.util.spec_from_file_location("qq_mcp", os.path.join(HERE, "qq-mcp.py"))
mcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mcp)

# ---------------------------------------------------------------------------
# 1. 协议层
# ---------------------------------------------------------------------------

print("[1] 协议层")

resp = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2024-11-05",
                              "capabilities": {}, "clientInfo": {"name": "t"}}})
case("initialize 回显客户端协议版本",
     resp["result"]["protocolVersion"] == "2024-11-05", json.dumps(resp, ensure_ascii=False))
case("initialize 声明 tools 能力与 serverInfo",
     resp["result"]["capabilities"] == {"tools": {}}
     and resp["result"]["serverInfo"]["name"] == "qq-cu-mcp", "")

case("initialize 无版本时给缺省",
     mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                 "params": {}})["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION, "")

case("initialized 通知不应答（返回 None）",
     mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None, "")

case("ping 返回空 result",
     mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"}) == {
         "jsonrpc": "2.0", "id": 3, "result": {}}, "")

resp = mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
tools = resp["result"]["tools"]
names = [t["name"] for t in tools]
case("tools/list 返回 8 个工具",
     len(tools) == 8, str(names))
case("工具名唯一且以 qq_ 前缀",
     len(set(names)) == 8 and all(n.startswith("qq_") for n in names), str(names))
case("每个工具都有 object 型 inputSchema",
     all(t.get("inputSchema", {}).get("type") == "object" for t in tools), "")

# tools/call：stub 掉 call_tool，只验协议映射
_saved_call = mcp.call_tool
try:
    mcp.call_tool = lambda name, args: {"ok": True, "tool": "health", "result": {"fine": 1}}
    resp = mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                       "params": {"name": "qq_health", "arguments": {}}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    case("tools/call 成功信封 → content JSON 透传 + isError=false",
         payload["ok"] is True and resp["result"]["isError"] is False,
         json.dumps(resp, ensure_ascii=False))

    mcp.call_tool = lambda name, args: {"ok": False, "error": {"code": "E-QQ-004", "detail": "x", "ctx": {}}}
    resp = mcp.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                       "params": {"name": "qq_health", "arguments": {}}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    case("tools/call 失败信封 → isError=true 且错误码原样透传",
         resp["result"]["isError"] is True and payload["error"]["code"] == "E-QQ-004", "")
finally:
    mcp.call_tool = _saved_call

resp = mcp.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                   "params": {"name": "nope", "arguments": {}}})
case("未知工具 → JSON-RPC -32602", resp["error"]["code"] == -32602, str(resp))

resp = mcp.handle({"jsonrpc": "2.0", "id": 8, "method": "resources/list"})
case("未知方法（带 id）→ -32601", resp["error"]["code"] == -32601, str(resp))
case("未知通知（无 id）不应答",
     mcp.handle({"jsonrpc": "2.0", "method": "xxx/yyy"}) is None, "")

# ---------------------------------------------------------------------------
# 2. 参数映射（MCP 参数 → qq-cu 子命令行）
# ---------------------------------------------------------------------------

print("[2] 参数映射")

case("qq_health → health --mode hosted（缺省）",
     mcp._build_args("qq_health", {}) == ["health", "--mode", "hosted"], "")
case("qq_list_sessions 显式 attach",
     mcp._build_args("qq_list_sessions", {"mode": "attach"}) == ["sessions", "--mode", "attach"], "")
case("qq_open_chat 按 name",
     mcp._build_args("qq_open_chat", {"name": "测试目标A"})
     == ["open", "--mode", "hosted", "--name", "测试目标A"], "")
case("qq_open_chat 按 index",
     mcp._build_args("qq_open_chat", {"index": 3})
     == ["open", "--mode", "hosted", "--index", "3"], "")
case("qq_read_recent limit 透传",
     mcp._build_args("qq_read_recent", {"limit": 5})
     == ["read", "--mode", "hosted", "--limit", "5"], "")
case("qq_read_recent 缺省 limit=12",
     mcp._build_args("qq_read_recent", {})
     == ["read", "--mode", "hosted", "--limit", "12"], "")
case("qq_screenshot 带 path",
     mcp._build_args("qq_screenshot", {"path": "D:/x.png"})
     == ["shot", "--mode", "hosted", "--path", "D:/x.png"], "")
case("qq_screenshot 缺省不带 path",
     mcp._build_args("qq_screenshot", {}) == ["shot", "--mode", "hosted"], "")
case("qq_send_text → send --text",
     mcp._build_args("qq_send_text", {"text": "你好"})
     == ["send", "--mode", "hosted", "--text", "你好"], "")
case("qq_run_task 带 max-steps",
     mcp._build_args("qq_run_task", {"task": "t", "max_steps": 9})
     == ["run", "--mode", "hosted", "--task", "t", "--max-steps", "9"], "")
case("qq_ask_server 多条 --text 聚合批 + 可选项",
     mcp._build_args("qq_ask_server", {"text": ["在吗", "睡了"], "qq": "12345",
                                       "scope": "group", "group_id": "666",
                                       "channel": "sealdice"})
     == ["ask", "--text", "在吗", "--text", "睡了", "--qq", "12345",
         "--scope", "group", "--group-id", "666", "--channel", "sealdice"], "")
case("qq_ask_server 只带必需项",
     mcp._build_args("qq_ask_server", {"text": ["hi"]}) == ["ask", "--text", "hi"], "")
try:
    mcp._build_args("nope", {})
    case("未知工具 _build_args 抛 KeyError", False, "没抛")
except KeyError:
    case("未知工具 _build_args 抛 KeyError", True, "")

# ---------------------------------------------------------------------------
# 3. 信封提取
# ---------------------------------------------------------------------------

print("[3] 信封提取")

case("整块 stdout 就是 JSON",
     mcp._extract_envelope('{"ok": true, "tool": "health"}') == {"ok": True, "tool": "health"}, "")
case("多行输出取最后一行信封",
     mcp._extract_envelope('日志行\n{"ok": false, "error": {}}') == {"ok": False, "error": {}}, "")
case("无信封返回 None",
     mcp._extract_envelope("完全不是 JSON\nTraceback ...") is None, "")
_multi = '''[13:10:20] INFO  E-CFG-005 配置缺少某些段，已用默认值补齐
[13:10:20] INFO    详情：没有找到 config.json
{
  "ok": false,
  "tool": "open_chat",
  "error": {"code": "E-CU-004", "detail": "名单外", "ctx": {"闸": "open_chat"}}
}'''
case("日志行 + 多行缩进信封混合（真机形态）",
     (mcp._extract_envelope(_multi) or {}).get("tool") == "open_chat", "")
case("信封后有尾随空白",
     mcp._extract_envelope('{"ok": true}  \n') == {"ok": True}, "")

# ---------------------------------------------------------------------------
# 4. 真子进程冒烟（协议回路，不碰 QQ）
# ---------------------------------------------------------------------------

print("[4] 真子进程冒烟")

py = sys.executable
frames = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-06-18"}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "ping"},
]
inp = "\n".join(json.dumps(f, ensure_ascii=False) for f in frames) + "\n"
proc = subprocess.run([py, os.path.join(HERE, "qq-mcp.py")],
                      input=inp, capture_output=True, encoding="utf-8",
                      timeout=30, cwd=HERE,
                      creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
lines = [l for l in (proc.stdout or "").splitlines() if l.strip()]
case("冒烟：退出码 0", proc.returncode == 0, f"rc={proc.returncode} stderr={proc.stderr[-200:]}")
case("冒烟：3 帧请求 → 3 帧应答（通知不计）", len(lines) == 3, str(len(lines)))
if len(lines) == 3:
    r1, r2, r3 = (json.loads(l) for l in lines)
    case("冒烟：initialize 应答含 serverInfo", r1["result"]["serverInfo"]["name"] == "qq-cu-mcp", "")
    case("冒烟：tools/list 8 个工具", len(r2["result"]["tools"]) == 8, "")
    case("冒烟：ping 空结果", r3["result"] == {}, "")
else:
    case("冒烟：initialize 应答含 serverInfo", False, "")
    case("冒烟：tools/list 8 个工具", False, "")
    case("冒烟：ping 空结果", False, "")

print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
