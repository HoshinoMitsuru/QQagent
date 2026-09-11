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

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from app.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
