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


def make_executor(mode: str):
    cfg = __import__("agent").load_config()
    if mode == "hosted":
        from cu.hosted import HostedExecutor
        return HostedExecutor(cfg)
    from cu.attach import AttachExecutor
    return AttachExecutor(cfg)


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
    """委托式：Brain 循环。授权表与白名单承自实测拍板（test_live_brain）。"""
    from test_live_brain import ATTACH_ALLOW, HOSTED_ALLOW
    from cu.brain import Brain, load_cu_config
    from cu.tools import dispatch
    import agent

    cfg = agent.load_config()
    cu = load_cu_config(cfg)
    cu["max_steps"] = a.max_steps or int(cu.get("max_steps", 8))
    if a.mode == "hosted":
        cu["open_chat_allow"] = HOSTED_ALLOW
    else:
        cu["open_chat_allow"] = ATTACH_ALLOW

    brain = Brain(cu, cfg)
    if not brain.api_key:
        return _emit({"ok": False, "tool": "run",
                      "error": {"code": "E-LLM-001", "detail": "没有可用 API Key",
                                "ctx": {"cu.api_key": bool(cu.get("api_key"))}}},
                     None, a.as_json)

    ex = make_executor(a.mode)

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

    a = ap.parse_args()
    a.as_json = (not a.human) or a.json   # JSON 是缺省；--json 显式自文档
    try:
        if a.cmd == "run":
            return cmd_run(a)
        if a.cmd == "ask":
            return cmd_ask(a)
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
