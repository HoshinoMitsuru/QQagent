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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--send", default="", help="走 agent.send_once 真发一条（有副作用！）")
    ap.add_argument("--peek", type=int, default=0, help="走 agent.py 的读消息入口")
    ap.add_argument("--sessions", action="store_true", help="列会话列表")
    a = ap.parse_args()

    res: dict = {"ok": False, "error": "", "traceback": ""}
    buf = io.StringIO()
    try:
        from app import desktop
        res["desktop"] = desktop.current_name()
        res["pid"] = os.getpid()

        import agent as A

        code = 0
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            cfg = A.load_config()
            if a.send:
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
