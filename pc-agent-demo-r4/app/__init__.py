# -*- coding: utf-8 -*-
"""
app —— QQ AI 代理的「一体化壳」

把原本要手动开终端敲的一串 python 命令，收进一个双击即可运行的窗口程序：

    qq-agent.exe                  → 起 WebUI（默认浏览器打开），所有操作在界面里点
    qq-agent.exe --run-agent ...  → 内部使用：把自己当成 agent.py 跑（子进程复用同一份代码）
    qq-agent.exe --run-qqid  ...  → 内部使用：把自己当成 qqid.py 跑
    qq-agent.exe --run-hostagent ... → 内部使用：R4 宿主，进隐藏桌面抓图/读界面/点登录
    qq-agent.exe --run-hostd ...     → 内部使用：R4 宿主，隐藏桌面上的常驻进程

这样做的关键理由：**UI 和 agent 必须跑在两个进程里**。UIA 的 COM 对象绑定在创建它的
公寓线程上，和 Web 服务共用一个进程既会被信号处理干扰，也没法「停了再起」。
子进程复用同一个 exe 则保证界面里跑的和命令行跑的是同一份代码，不会两边行为漂移。

R4（V2）多一层：宿主进程要被 `lpDesktop` 丢进**另一张桌面**，
所以那两个角色同样得能由 exe 自己扮演 —— 冻结之后 `sys.executable` 是 exe，
不接受 `-m`（见 `app/host.py` 的 `child_args()`）。
"""

# 版本只对「控制台壳」这一层有意义（agent.py 是同一份代码，不分版本）。
# V2 与 V1 是两棵独立目录树、两个 exe，都叫 1.0.0 的话，
# 用户手里同时有两个 exe 时分不清点开的是哪个 —— 界面上那行版本号就是唯一的区分点。
__version__ = "2.0.0"
