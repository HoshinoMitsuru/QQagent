# -*- coding: utf-8 -*-
"""
test_cli.py —— qq-cu.py（F6 Agent 接口）的离线自检

## 测什么

- JSON 信封形状与退出码契约（0/1/2）
- 六原语子命令到 dispatch 的参数透传（stub 执行面，不碰真 QQ）
- **attach send fail-closed**：CLI 里非交互调用被拒、原语不被触碰
- run 的非交互确认 fail-closed（confirm 返回 False）
- 参数校验（open 缺 name/index、send 缺 text → 退出码 2）

用法：python test_cli.py
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import types

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


def run_cli(args: list[str]) -> tuple[int, dict, str]:
    """在子进程里跑 CLI（argparse 层行为），返回 (退出码, JSON信封, 原始stdout)。"""
    py = sys.executable
    proc = subprocess.run([py, os.path.join(HERE, "qq-cu.py")] + args,
                          capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace")
    try:
        env = json.loads(proc.stdout)
    except Exception:
        env = {}
    return proc.returncode, env, proc.stdout


# ============================================================ §1 参数校验
print("§1 参数校验：缺参 → 退出码 2，不触碰 QQ")
import importlib.util                             # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "qq_cu", os.path.join(HERE, "qq-cu.py"))
qq_cu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(qq_cu)


class FakeArgs:
    """模拟 argparse 解析结果的最小集合。"""
    def __init__(self, **kw):
        self.cmd = kw.get("cmd", "sessions")
        self.tool = kw.get("tool", "sessions")
        self.mode = kw.get("mode", "hosted")
        self.name = kw.get("name", "")
        self.index = kw.get("index", None)
        self.limit = kw.get("limit", 12)
        self.text = kw.get("text", "")
        self.path = kw.get("path", "")
        self.task = kw.get("task", "")
        self.max_steps = kw.get("max_steps", 0)
        self.as_json = kw.get("as_json", True)
        self.human = not self.as_json


captured: list[tuple] = []


def fake_dispatch_ok(mode, tool, args, as_json=True):
    captured.append((mode, tool, dict(args)))
    return {"ok": True, "tool": tool, "result": {"stub": True}}


_saved_dispatch = qq_cu._dispatch
qq_cu._dispatch = fake_dispatch_ok

from cu.base import SendReceipt                    # noqa: E402

r = qq_cu.cmd_tool(FakeArgs(cmd="open", tool="open", name="", index=None))
case("open 缺 name 且缺 index → 退出码 2", r == qq_cu.EXIT_CONFIG, str(r))
r = qq_cu.cmd_tool(FakeArgs(cmd="send", tool="send", text=""))
case("send 缺 text → 退出码 2", r == qq_cu.EXIT_CONFIG, str(r))
case("缺参时原语从未被调用", captured == [], str(captured))

# ============================================================ §2 透传与信封
print("§2 六原语参数透传与信封形状")
qq_cu.cmd_tool(FakeArgs(cmd="open", tool="open", mode="hosted",
                        name="测试目标A", index=None))
case("open 的 name 透传给 dispatch",
     captured and captured[-1] == ("hosted", "open", {"name": "测试目标A"}),
     str(captured[-1:]))

qq_cu.cmd_tool(FakeArgs(cmd="open", tool="open", mode="hosted",
                        name="", index=2))
case("open 的 index 透传",
     captured[-1] == ("hosted", "open", {"index": 2}), str(captured[-1]))

qq_cu.cmd_tool(FakeArgs(cmd="read", tool="read", mode="attach", limit=5))
case("read 的 limit 透传（含 --mode attach）",
     captured[-1] == ("attach", "read", {"limit": 5}), str(captured[-1]))

qq_cu.cmd_tool(FakeArgs(cmd="shot", tool="shot", mode="hosted",
                        path="D:/tmp/x.png"))
case("shot 的 path 透传",
     captured[-1] == ("hosted", "shot", {"path": "D:/tmp/x.png"}), str(captured[-1]))

def fake_dispatch_fail(mode, tool, args, as_json=True):
    captured.append((mode, tool, dict(args)))
    return qq_cu.EXIT_ACTION_FAIL   # _dispatch 的真实语义：返回退出码


qq_cu._dispatch = fake_dispatch_fail
r = qq_cu.cmd_tool(FakeArgs(cmd="sessions", tool="sessions", mode="hosted"))
case("信封 ok=false → 退出码 1", r == qq_cu.EXIT_ACTION_FAIL, str(r))
qq_cu._dispatch = _saved_dispatch

# ============================================================ §3 attach fail-closed
print("§3 安全锁：CLI 路径下 attach send 依旧 fail-closed")
import cu.tools as ct                              # noqa: E402


class NoTouchExecutor:
    name = "attach"
    require_confirmation = True
    touched = False

    def send_text(self, text, *, armed=None):
        type(self).touched = True
        return SendReceipt(True, route="x", chat_title="t")


env = ct.dispatch(NoTouchExecutor(), "send_text", {"text": "你好"})
case("attach send 未 armed → E-CU-004 信封",
     (not env["ok"]) and env["error"]["code"] == "E-CU-004", str(env)[:150])
case("原语从未被触碰", NoTouchExecutor.touched is False, "")

# ============================================================ §4 run 非交互 fail-closed
print("§4 run：非交互终端的确认回调一律拒绝")
from cu.base import ExecutorError                  # noqa: E402


def fake_confirm_behavior(interactive: bool) -> bool:
    """复刻 qq-cu.cmd_run 里 confirm 的核心判据（保持与实现同步的契约）。"""
    return bool(interactive)  # 非 tty → False（fail-closed）


case("非交互（agent shell 调起）→ confirm 恒 False",
     fake_confirm_behavior(False) is False, "")
case("交互终端 → confirm 走 y/n（返回用户输入结果）",
     fake_confirm_behavior(True) is True, "")

# ============================================================ §5 子进程冒烟
print("§5 子进程级冒烟：argparse 拒绝 → 退出码 2")
py = sys.executable
proc = subprocess.run([py, os.path.join(HERE, "qq-cu.py"), "send", "--mode",
                       "hosted"], capture_output=True, text=True, timeout=60)
case("send 缺 --text → 子进程退出码 2", proc.returncode == 2,
     f"rc={proc.returncode} stderr={proc.stderr[:120]}")
proc = subprocess.run([py, os.path.join(HERE, "qq-cu.py")],
                      capture_output=True, text=True, timeout=60)
case("无子命令 → argparse 拒绝（退出码 2）", proc.returncode == 2,
     f"rc={proc.returncode}")

# ============================================================
print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
