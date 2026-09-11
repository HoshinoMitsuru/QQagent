# -*- coding: utf-8 -*-
"""
app —— QQ AI 代理的「一体化壳」

把原本要手动开终端敲的一串 python 命令，收进一个双击即可运行的窗口程序：

    qq-agent.exe                  → 起 WebUI（默认浏览器打开），所有操作在界面里点
    qq-agent.exe --run-agent ...  → 内部使用：把自己当成 agent.py 跑（子进程复用同一份代码）
    qq-agent.exe --run-qqid  ...  → 内部使用：把自己当成 qqid.py 跑

这样做的关键理由：**UI 和 agent 必须跑在两个进程里**。UIA 的 COM 对象绑定在创建它的
公寓线程上，和 Web 服务共用一个进程既会被信号处理干扰，也没法「停了再起」。
子进程复用同一个 exe 则保证界面里跑的和命令行跑的是同一份代码，不会两边行为漂移。
"""

__version__ = "1.0.0"
