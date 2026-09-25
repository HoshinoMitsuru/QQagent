# -*- coding: utf-8 -*-
"""
u1_prod.py —— 把 **agent.py 自带的命令行入口**跑在隐藏桌面上（生产路径验证）

## 为什么要有这一步

`archive/u1_e2e.py` 是手搓的验证脚本，它能通只说明「原理可行」。
真正要证明的是：**produce 代码路径（`agent.send_once` → `QQWindow.send_text`
→ `type_text`）在隐藏桌面上也能跑通** —— 那条路里带着三重复核、草稿保护、
错误码上报等一大堆东西，跟手搓版不是一回事。

## 为什么 stdout 要重定向

`desktop.spawn` 用 `CreateProcessW`，**没有接管 stdout 管道**
（`subprocess.Popen` 不暴露 `lpDesktop`，只能自己调 Win32 API）。
`agent.py` 那套入口是往 stdout 打诊断报告的，直接 spawn 等于把报告扔了。
所以这里 `redirect_stdout` 到内存，再连同结果一起写进 JSON。

用法（由父进程 spawn 到指定桌面）：
    python archive/u1_prod.py --out <json> [--send TEXT] [--peek N] [--sessions]
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _mark(path: str, stage: str) -> None:
    """
    随时把「走到哪一步了」写进结果文件。

    为什么需要它：`--once`（真发）那次子进程退出码是 2 却**连结果文件都没写出来**，
    而 `finally` 是一定会跑的 —— 这说明进程死在比 `finally` 更早、且不走
    Python 异常机制的地方。没有中间标记就只能靠猜；有了标记，死在哪一步一目了然。
    """
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"stage": stage, "pid": os.getpid()}, f, ensure_ascii=False)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--send", default="", help="走 agent.send_once 真发一条（有副作用！）")
    ap.add_argument("--peek", type=int, default=0, help="走 agent.py 的读消息入口")
    ap.add_argument("--sessions", action="store_true", help="列会话列表")
    ap.add_argument("--args", default="",
                    help="原样透传给 agent.main() 的参数串，如 \"--once --no-send\"。"
                         "这是本脚本最有用的开关：agent.py 的任意命令行模式都能被搬到隐藏桌面上跑。")
    a = ap.parse_args()

    res: dict = {"ok": False, "error": "", "traceback": ""}
    buf = io.StringIO()
    _mark(a.out, "start")
    try:
        from app import desktop
        res["desktop"] = desktop.current_name()
        res["pid"] = os.getpid()
        _mark(a.out, "imported-app")

        import agent as A
        _mark(a.out, "imported-agent")

        code = 0
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            cfg = A.load_config()
            if a.args:
                code = _run_agent_main(a.args)
            elif a.send:
                code = A.send_once(cfg, a.send, force=True)
            elif a.peek:
                code = A.peek(cfg, a.peek) if hasattr(A, "peek") else _peek(cfg, a.peek)
            elif a.sessions:
                code = _sessions(cfg)
        res["exit_code"] = code
        res["ok"] = code == 0
        if not res["ok"]:
            res["error"] = f"agent 入口返回 {code}"
    except Exception:
        res["traceback"] = traceback.format_exc()
        res["error"] = "包装脚本自身异常（见 traceback）"
    finally:
        res["stdout"] = buf.getvalue()[-8000:]
        try:
            os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
            with open(a.out, "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return 0 if res.get("ok") else 1


def _run_agent_main(argstr: str) -> int:
    """
    把参数串原样喂给 `agent.main()`。

    为什么要绕这一下：`--once` 这类逻辑是**内联在 agent.main() 里的**，
    外面没有可直接调用的函数。与其在这里复刻一遍（复刻就意味着会漂移），
    不如把 sys.argv 改成 agent.py 看到的样子，让它自己走完整流程。

    `main()` 用 `raise SystemExit(code)` 结束，所以必须接住它取码。
    """
    import shlex
    import agent as A       # A 是 main() 的局部变量，这里要自己引一次
    sys.argv = ["agent.py"] + shlex.split(argstr)
    try:
        A.main()
        return 0
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else (0 if e.code is None else 1)


def _sessions(cfg) -> int:
    """列会话列表（只读）。"""
    import agent as A
    import qqid
    qq = A.QQWindow(cfg)
    if not qq.attach():
        print("[X] 附着失败")
        return 2
    ss = qqid.list_sessions(qq.win)
    print(f"会话 {len(ss)} 个：")
    for s in ss:
        print(f"  [{s.index:>2}] {s.display_name!r} 未读={s.unread} {s.summary[:40]!r}")
    return 0


def _peek(cfg, n: int) -> int:
    """读当前会话最近 n 条（只读）。"""
    import agent as A
    qq = A.QQWindow(cfg)
    if not qq.attach():
        print("[X] 附着失败")
        return 2
    print(f"标题={qq.dialog_title!r} 群聊={qq.is_group}")
    for m in qq.read_messages(limit=n):
        print(f"  【{m.direction}】{m.sender}: {m.content}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
