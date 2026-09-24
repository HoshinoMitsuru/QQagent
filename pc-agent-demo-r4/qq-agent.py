# -*- coding: utf-8 -*-
"""
qq-agent.py —— 源码模式下的启动入口（打包后的等价物是 qq-agent.exe）

直接跑：
    python qq-agent.py                 启动控制台
    python qq-agent.py --run-agent --selftest   以 agent 身份执行

日常开发建议用 run_ui.bat（它会把隔离环境的 Scripts 加进 PATH，并带上 -X utf8）。
"""

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from app.main import main  # noqa: E402

if __name__ == "__main__":
    # 兜住启动期的任何异常：windowed exe 没有控制台，不兜的话
    # 用户只会看到「一闪然后没了」，连一份可发的报告都没有。
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException as exc:            # noqa: BLE001
        try:
            from app.main import _crash_report, _msgbox
            path = _crash_report(exc, sys.argv[1:])
            traceback.print_exc()
            _msgbox("QQAgent 启动失败", (
                f"程序在启动阶段抛了异常，无法继续。\n\n"
                f"{type(exc).__name__}: {exc}\n\n"
                + (f"完整报告已写入：\n{path}\n\n（把这个文件发出来即可定位）"
                   if path else "（报告写入也失败了，请把本窗口的输出截下来）")),
                "error")
        except Exception:
            traceback.print_exc()
        raise SystemExit(3)
