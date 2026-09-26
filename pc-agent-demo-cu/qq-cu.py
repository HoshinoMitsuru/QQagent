# -*- coding: utf-8 -*-
"""
qq-cu —— pc-agent-demo-cu 的正式 CLI 入口（F6：Agent 可调用接口）

## 定位（2026-09-25 本人拍板：形态 3 + 接口 A）

调用方是 **Agent 应用**（Codex / Claude Code / WorkBuddy），不是人。
所以本工具没有操作面板，只有两条调用路径：

- **委托式**：`run` 子命令 —— CU 内置 Brain（deepseek-flash）自己走
  工具循环，调用方只下发一句任务，拿回最终答复 + 结构化 trace。
- **工具式**：六原语子命令 —— 调用方 agent 自己编排每一步，
  CU 只提供带安全闸的原子动作。

## 输出契约（CLI-first，A）

- stdout 输出 **JSON 信封**（`--json` 或缺省；人类可读用 `--human`）：
      成功：{"ok": true,  "tool": "...", "result": {...}}
      失败：{"ok": false, "tool": "...", "error": {"code","detail","ctx"}}
  `run` 的成功信封多一层：{"ok", "answer", "usage_steps", "steps", "error"}。
- 退出码：**0** = 动作成功；**1** = 动作失败（信封 ok=false，细节看 JSON）；
  **2** = 配置/参数错（Key 缺失、argparse 拒绝）。agent 按退出码分流。

## 安全（不随调用方变化）

- **hosted**（独立桌面小号）：全自主发送 —— 用户已授权。
- **attach**（附着主号）：send_text 永远 fail-closed。非交互终端直接拒绝；
  交互终端逐条 y/n。不提供预授权通道（agent 场景主推 hosted）。

## 用法示例

    qq-cu run --mode hosted --task "给「测试目标A」发一句『在吗』" --json
    qq-cu health  --mode hosted --json
    qq-cu sessions --mode hosted --json
    qq-cu open    --mode hosted --name "测试目标A" --json
    qq-cu read    --mode hosted --limit 10 --json
    qq-cu shot    --mode hosted --json
    qq-cu send    --mode hosted --text "你好" --json
"""

from __future__ import annotations

import argparse
import json
import sys

HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))
sys.path.insert(0, HERE)

#: 退出码契约
EXIT_OK, EXIT_ACTION_FAIL, EXIT_CONFIG = 0, 1, 2


def _emit(envelope: dict, human: list[str] | None, as_json: bool) -> int:
    """统一出口：--json 走信封，--human 走几行人话。返回退出码。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # 防 GBK 终端 UnicodeEncodeError
    except Exception:
        pass
    if as_json or human is None:
        print(json.dumps(envelope, ensure_ascii=False, indent=2))
    else:
        for line in human:
            print(line)
    return EXIT_OK if envelope.get("ok") else EXIT_ACTION_FAIL


def make_executor(mode: str, gates: dict | None = None):
    """构造执行面（统一走 cu.base.create_executor —— A1 拍板：安全闸
    收在执行面，CLI/MCP/Brain 三条路同一收口）。gates 可覆盖闸名单
    （run 按模式挑选 open_chat_allow / attach_allow 时用）。"""
    import agent
    cfg = agent.load_config()
    if gates:
        cfg = dict(cfg)
        cu = dict(cfg.get("cu") or {})
        cu.update(gates)
        cfg["cu"] = cu
    from cu.base import create_executor
    return create_executor(mode, cfg)


def _dispatch(mode: str, tool: str, args: dict, as_json: bool) -> int:
    """工具式入口：六原语统一走 cu.tools.dispatch（安全闸都在那里）。"""
    from cu.tools import dispatch
    # CLI 子命令名 → dispatch 工具名（工具名保持与模型 schema 一致）
    _TOOL_ALIAS = {"sessions": "list_sessions", "read": "read_recent",
                   "shot": "screenshot", "open": "open_chat",
                   "send": "send_text", "health": "health"}
    tool = _TOOL_ALIAS.get(tool, tool)
    try:
        ex = make_executor(mode)
    except Exception as exc:                       # noqa: BLE001
        return _emit({"ok": False, "tool": tool,
                      "error": {"code": "E-CU-001",
                                "detail": f"执行面构造失败：{type(exc).__name__}: {exc}",
                                "ctx": {}}}, None, as_json)
    env = dispatch(ex, tool, args)
    human = None
    if not as_json:
        if env.get("ok"):
            human = [f"[ok] {tool} → {json.dumps(env.get('result') or {}, ensure_ascii=False)[:400]}"]
        else:
            err = env.get("error") or {}
            human = [f"[X] {tool} → {err.get('code')} {err.get('detail')}",
                     f"    ctx: {json.dumps(err.get('ctx'), ensure_ascii=False)[:300]}"]
        if env.get("needs_confirmation"):
            human.append("    （attach 面发送必须人工确认：交互终端逐条 y/n，"
                         "或改用 --mode hosted；本工具不提供非交互预授权）")
    return _emit(env, human, as_json)


def cmd_run(a) -> int:
    """委托式：Brain 循环。名单唯一来源 = 本地 config.json（C1 拍板：
    不再从 test_live_brain 导入覆盖——那个 import 在 deb6d0b 之后本来就是
    坏的，一跑就 ImportError）。名单为空 = fail-closed 拒绝执行。"""
    import agent
    from cu.brain import Brain, load_cu_config

    cfg = agent.load_config()
    cu = load_cu_config(cfg)
    cu["max_steps"] = a.max_steps or int(cu.get("max_steps", 8))

    # run 按模式挑选名单：hosted 用目标闸 open_chat_allow，attach 用
    # attach_allow（历史语义），统一映射到 Brain 与执行面闸的同一个键
    key = "open_chat_allow" if a.mode == "hosted" else "attach_allow"
    allow = [s for s in (cu.get(key) or []) if isinstance(s, str) and s.strip()]
    if not allow:
        return _emit({"ok": False, "tool": "run",
                      "error": {"code": "E-CU-004",
                                "detail": f"{a.mode} 面名单为空，拒绝执行"
                                          "（名单就是授权书，留空 = fail-closed）",
                                "ctx": {"名单键": f"cu.{key}",
                                        "修复": f"编辑 config.json 填 cu.{key}，"
                                                f"或 qq-cu allow add --name 目标名 "
                                                f"--list "
                                                f"{'open' if a.mode == 'hosted' else 'attach'}"}}},
                     None, a.as_json)
    cu["open_chat_allow"] = allow

    brain = Brain(cu, cfg)
    if not brain.api_key:
        return _emit({"ok": False, "tool": "run",
                      "error": {"code": "E-LLM-001", "detail": "没有可用 API Key",
                                "ctx": {"cu.api_key": bool(cu.get("api_key"))}}},
                     None, a.as_json)

    ex = make_executor(a.mode, gates={"open_chat_allow": allow,
                                      "read_allow": cu.get("read_allow")})

    interactive = sys.stdin.isatty()

    def confirm(info: dict) -> bool:
        # 非交互终端（被 agent 的 shell 调起）：fail-closed，绝不猜
        if not interactive:
            return False
        print("\n" + "!" * 70)
        print(f"!! 主号发送确认  目标：{info.get('chat_title') or '（未知）'}")
        print(f"!! 内容：{info.get('text')}")
        return input("!! 发送？(y=发送 / 其他=拒绝) > ").strip().lower() == "y"

    r = brain.run(ex, a.task, confirm=confirm)

    envelope = {"ok": r["ok"], "tool": "run",
                "answer": r.get("answer", ""),
                "usage_steps": r.get("usage_steps", 0),
                "steps": r.get("steps", []),
                "error": r.get("error")}
    human = None
    if not a.as_json:
        if r["ok"]:
            human = [f"[完成，{r['usage_steps']} 步] {r['answer']}"]
        else:
            err = r.get("error") or {}
            human = [f"[失败] {err.get('code')} {err.get('detail')}",
                     f"    ctx: {json.dumps(err.get('ctx'), ensure_ascii=False)[:300]}"]
        if not interactive:
            human.append("    （非交互终端：attach 面的发送确认一律拒绝 = fail-closed）")
    return _emit(envelope, human, a.as_json)


def cmd_tool(a) -> int:
    """六原语的公共壳：构造参数并走 dispatch。"""
    tool = a.tool
    args: dict = {}
    if tool == "open":
        if not a.name and a.index is None:
            print("open 需要 --name 或 --index", file=sys.stderr)
            return EXIT_CONFIG
        if a.name:
            args["name"] = a.name
        if a.index is not None:
            args["index"] = a.index
    elif tool == "read":
        args["limit"] = a.limit
    elif tool == "send":
        if not a.text:
            print("send 需要 --text", file=sys.stderr)
            return EXIT_CONFIG
        args["text"] = a.text
    elif tool == "shot":
        if a.path:
            args["path"] = a.path
    return _dispatch(a.mode, tool, args, a.as_json)


def cmd_allow(a) -> int:
    """安全闸白名单管理（C1 拍板：config.json 是唯一事实来源，这里是清晰入口）。

    直接对 agent.CONFIG_PATH 做 JSON 读改写（保留其余键不动）；
    真名只落本机，该文件不入库（gitignore）。
    """
    import agent
    path = agent.CONFIG_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    except Exception as exc:                       # noqa: BLE001
        return _emit({"ok": False, "tool": "allow",
                      "error": {"code": "E-CFG-001",
                                "detail": f"config.json 解析失败：{exc}",
                                "ctx": {"文件": path}}}, None, a.as_json)

    key = {"open": "open_chat_allow", "read": "read_allow",
           "attach": "attach_allow"}[a.which]
    cu = dict(cfg.get("cu") or {})
    items = [s for s in (cu.get(key) or []) if isinstance(s, str) and s.strip()]

    if a.action == "list":
        envelope = {"ok": True, "tool": "allow",
                    "result": {"list": a.which, "key": f"cu.{key}",
                               "items": items,
                               "语义": _ALLOW_SEMANTICS[a.which],
                               "文件": path}}
        human = [f"[cu.{key}] {'、'.join(items) if items else '（空）'}",
                 f"    语义：{_ALLOW_SEMANTICS[a.which]}"]
        return _emit(envelope, human, a.as_json)

    if not a.name.strip():
        print(f"add/remove 需要 --name（当前 cu.{key} = {items}）", file=sys.stderr)
        return EXIT_CONFIG
    name = a.name.strip()
    if a.action == "add":
        if not any(s == name for s in items):
            items.append(name)
    else:                                          # remove
        if name not in items:
            return _emit({"ok": False, "tool": "allow",
                          "error": {"code": "E-CU-002",
                                    "detail": f"「{name}」不在 cu.{key} 里",
                                    "ctx": {"现有": items}}}, None, a.as_json)
        items = [s for s in items if s != name]

    cu[key] = items
    cfg["cu"] = cu
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    envelope = {"ok": True, "tool": "allow",
                "result": {"action": a.action, "list": a.which,
                           "key": f"cu.{key}", "items": items,
                           "语义": _ALLOW_SEMANTICS[a.which], "文件": path}}
    human = [f"[{a.action}] cu.{key} ← {name}",
             f"    现为：{'、'.join(items) if items else '（空）'}"]
    return _emit(envelope, human, a.as_json)


#: 各名单的语义速查（list/add 的回显都带上，修改入口要能自解释）
_ALLOW_SEMANTICS = {
    "open": "目标闸：两面 open_chat/send_text 仅名单内可做；"
            "hosted 面留空 = fail-closed（不发不开），attach 面留空 = 放行（确认锁兜底）",
    "read": "读取闸：read/shot 仅名单内会话可读；留空 = 方便优先（B1，全可读），"
            "非空 = 安全优先（B2，读不到标题也拒）。list_sessions 永不拦",
    "attach": "run --mode attach 的 Brain 名单（历史键）：留空 = run attach 拒绝执行",
}


def cmd_ask(a) -> int:
    """服务端大脑（P1）：文本 → ECS /api/qq/agent → 回复信封。不碰 QQ 执行面。"""
    from cu.server_brain import get_reply
    cfg = __import__("agent").load_config()
    env = get_reply(cfg, list(a.text or []), qq_number=a.qq, scope=a.scope,
                    group_id=a.group_id, channel=a.channel)
    if env.get("ok"):
        envelope = {"ok": True, "tool": "ask", **env}
        human = [f"[服务端大脑] {env['reply']}",
                 f"    会话 {env.get('conversation_id')}｜通道 {env.get('channel')}"
                 f"｜耗时 {env.get('elapsed')}s"]
    else:
        envelope = {"ok": False, "tool": "ask", "error": env["error"]}
        human = None
    return _emit(envelope, human, a.as_json)


def _frozen_dispatch() -> int | None:
    """冻结形态的宿主角色分派（承 app/main.py 同款机制，见 host.child_args）。

    hosted 面每次动作都会 spawn **exe 自己** + `--run-hostagent` 前缀
    （冻结后 sys.executable 是 exe 自己，不接受 -m）。源码模式跳过。"""
    if not getattr(sys, "frozen", False):
        return None
    argv = sys.argv[1:]
    if argv and argv[0] == "--run-hostagent":
        sys.argv = [sys.argv[0]] + argv[1:]
        from app.hostagent import main as _ha_main
        return _ha_main()
    return None


def main() -> int:
    ap = argparse.ArgumentParser(prog="qq-cu",
                                 description="QQ 的 LLM Computer Use 工具"
                                             "（供 Agent 应用调用）")
    # 公共参数挂到**每个子命令**上：agent 习惯 `health --json`（参数在
    # 子命令后面），顶层参数写法 `--json health` argparse 会拒 —— 两种都要能跑。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--human", action="store_true",
                        help="人类可读输出（缺省输出 JSON 信封）")
    common.add_argument("--json", action="store_true",
                        help="JSON 信封输出（缺省即是；显式写出便于调用方自文档）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", parents=[common],
                       help="委托式：CU 内置 Brain 完成整个任务")
    p.add_argument("--mode", choices=("attach", "hosted"), default="hosted")
    p.add_argument("--task", required=True)
    p.add_argument("--max-steps", type=int, default=0, help="覆盖 cu.max_steps")

    for name, help_ in (("health", "执行面体检"),
                        ("sessions", "读会话列表"),
                        ("shot", "抓当前窗口截图")):
        p = sub.add_parser(name, parents=[common], help=help_)
        p.add_argument("--mode", choices=("attach", "hosted"), default="hosted")
        if name == "shot":
            p.add_argument("--path", default="", help="截图落盘路径（缺省进 state/）")
        p.set_defaults(tool=name)

    p = sub.add_parser("open", parents=[common], help="打开会话")
    p.add_argument("--mode", choices=("attach", "hosted"), default="hosted")
    p.add_argument("--name", default="")
    p.add_argument("--index", type=int, default=None)
    p.set_defaults(tool="open")

    p = sub.add_parser("read", parents=[common], help="读当前会话最近消息")
    p.add_argument("--mode", choices=("attach", "hosted"), default="hosted")
    p.add_argument("--limit", type=int, default=12)
    p.set_defaults(tool="read")

    p = sub.add_parser("send", parents=[common],
                       help="发送文本（attach 面 fail-closed；hosted 全自主）")
    p.add_argument("--mode", choices=("attach", "hosted"), default="hosted")
    p.add_argument("--text", required=True)
    p.set_defaults(tool="send")

    p = sub.add_parser("ask", parents=[common],
                       help="服务端大脑：文本交给 ai-web-page /api/qq/agent 生成回复"
                            "（纯 HTTP，不碰 QQ 执行面）")
    p.add_argument("--text", action="append", required=True,
                   help="用户消息；可重复传多条构成聚合批（按时间序）")
    p.add_argument("--qq", default="",
                   help="发送者 QQ 号（缺省取 config.json 的 qq_agent.qq_number）")
    p.add_argument("--scope", choices=("private", "group"), default="private")
    p.add_argument("--group-id", default="")
    p.add_argument("--channel", choices=("r4", "sealdice"), default="r4")

    p = sub.add_parser("allow", parents=[common],
                       help="管理安全闸白名单（读改写本地 config.json 的 cu 节）")
    p.add_argument("action", choices=("list", "add", "remove"))
    p.add_argument("--list", dest="which", choices=("open", "read", "attach"),
                   default="open",
                   help="open=cu.open_chat_allow（目标闸：hosted open/send 必需）"
                        "；read=cu.read_allow（读取闸：留空=方便优先，非空=安全优先）；"
                        "attach=cu.attach_allow（run --mode attach 的名单）")
    p.add_argument("--name", default="", help="要加/删的会话名（list 时忽略）")

    a = ap.parse_args()
    a.as_json = (not a.human) or a.json   # JSON 是缺省；--json 显式自文档
    try:
        if a.cmd == "run":
            return cmd_run(a)
        if a.cmd == "ask":
            return cmd_ask(a)
        if a.cmd == "allow":
            return cmd_allow(a)
        return cmd_tool(a)
    except SystemExit:
        raise
    except Exception as exc:                       # noqa: BLE001 —— CLI 绝不裸栈
        return _emit({"ok": False, "tool": getattr(a, "tool", a.cmd),
                      "error": {"code": "E-CU-001",
                                "detail": f"{type(exc).__name__}: {exc}",
                                "ctx": {"提示": "真根因看前一条错误码日志"}}},
                     None, not a.as_json)


if __name__ == "__main__":
    _rc = _frozen_dispatch()
    if _rc is None:
        _rc = main()
    raise SystemExit(_rc)
