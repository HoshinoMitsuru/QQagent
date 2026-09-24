# -*- coding: utf-8 -*-
"""
runtime.py —— 一个极小的进程级共享状态

只有一样东西：**退出信号**。

为什么需要它：退出这件事有三个来源，而它们分处不同的线程和模块 ——

    托盘菜单「退出」    在托盘的消息循环里（主线程）
    WebUI 的「退出程序」  在 HTTP 请求线程里
    Ctrl+C              在主线程

如果三处各写一遍收尾逻辑（停常驻、关 HTTP、摘托盘），迟早会漏掉一处，
表现就是「点了退出但进程还在」或者「进程没了但常驻子进程还活着」。
所以这里放一个事件，谁想退出就 set 它，由**唯一一个看门线程**去执行收尾。
"""

from __future__ import annotations

import threading

QUIT = threading.Event()


def request_quit() -> None:
    QUIT.set()


def wait_quit(timeout: float | None = None) -> bool:
    return QUIT.wait(timeout)
