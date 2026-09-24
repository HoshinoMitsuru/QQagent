# -*- coding: utf-8 -*-
"""
logtail.py —— 把一个**正在被别的进程追加**的日志文件接进日志总线

## 为什么需要它

V1 的常驻是壳 `subprocess.Popen` 起的子进程，stdout 接管道，直接泵进日志总线。
V2（隐藏桌面）做不到：宿主进程是被 `CreateProcessW` + `lpDesktop` 丢进另一张桌面的，
**没有 stdout 管道可接**（见 `desktop.spawn` 的说明）。

于是宿主把 stdout 落到 `logs/hostd.log`，壳再把这个文件 tail 进来。
少了这一环，隐藏桌面上发生的一切对界面都是黑的 ——
而「常驻到底有没有在读消息」恰恰是这个程序最需要被看见的东西。

## 三个必须处理的细节

    半行    对方可能只写了一半就还没换行，直接 emit 会把一行日志劈成两条
    截断    宿主每次启动是**重写**日志（"w"），文件会变小 —— 不回到开头就会读到垃圾
    旋转    文件被换掉时按截断处理，代价是重读一遍，可接受

## 首次从哪里读（这里改过一次，原因是第一版的选择在真实场景下是错的）

第一版是「从文件末尾开始」。理由是「上一轮宿主留下的日志没有时间意义」。
但真实使用顺序是**先有宿主、后有控制台**（宿主要先在隐藏桌面上把 QQ 拉起来、
把号登进去，用户才打开界面看）—— 于是从末尾读等于把宿主**启动那一整段**
（拉起 QQ / 等窗口 / 免扫码登录 / 出错）全部丢掉，而那段恰恰是最需要看的。

所以改成：首次读**文件最后 64KB**，并把开头那半行丢掉。
这样「宿主已经在跑」时能看到它的启动过程；「宿主已经退出」时能看到它
**为什么退出**（宿主的日志本来就每次启动重写，不会混进上上次的内容）。
"""

from __future__ import annotations

import os
import threading
import time

from .logbus import BUS

#: 单轮最多喂多少字节。宿主在极端情况下（栈回溯刷屏）可能瞬间写很多，
#: 一次全喂会把 SSE 订阅队列撑爆，进而拖慢界面。
MAX_CHUNK = 256 * 1024

#: 首次最多回看多少字节。取 64KB 不是拍脑袋：宿主一次完整启动大约 1~3KB，
#: 64KB 足够覆盖「拉起 + 登录 + 好几轮循环」，又不至于在文件很大时卡一下。
FIRST_READ_BYTES = 64 * 1024


def _tail_once(path: str, state: dict, source: str = "hidden") -> None:
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    pos = state.get("pos", -1)
    if pos < 0:
        # 首次：回看最后 64KB（理由见模块说明）。
        # 裁到 64KB 之后起点多半落在某一行中间，直接喂进去会得到一条残缺的日志 ——
        # 标记 `skip_partial` 让下面把第一行整行丢掉。
        state["pos"] = max(0, size - FIRST_READ_BYTES)
        state["skip_partial"] = state["pos"] > 0
        pos = state["pos"]
    if size < pos:
        pos = state["pos"] = 0        # 被截断/重写了
    if size == pos:
        return
    try:
        with open(path, "rb") as f:
            f.seek(pos)
            raw = f.read(min(size - pos, MAX_CHUNK))
    except OSError:
        return
    state["pos"] = pos + len(raw)
    text = raw.decode("utf-8", "replace")
    if state.pop("skip_partial", False):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    # 最后一段若没有换行，说明是半行：留到下一轮，等对方把这一行写完
    buf = state.get("buf", "") + text
    if not buf.endswith("\n"):
        head, _, state["buf"] = buf.rpartition("\n")
        buf = head
    else:
        state["buf"] = ""
    if buf.strip():
        # `source` 会一路走到日志行的 source 字段，界面用它区分「这行来自哪」。
        # ⚠️ 第一版把这里写死成 "hidden"、`start(source=...)` 那个参数根本没被用 ——
        # 参数收了不用，是比不收更容易骗人的一种「支持」。
        BUS.feed(buf, source=source)


def start(path: str, *, interval: float = 0.4, source: str = "hidden") -> threading.Thread:
    """
    起一条守护线程盯着 `path`，把新写进去的内容喂进日志总线。

    文件一开始不存在也没关系（宿主还没启动），线程会一直等到它出现 ——
    这个线程从壳启动时就在跑，比宿主活着的时间更长。
    """
    state: dict = {"pos": -1, "buf": ""}

    def loop() -> None:
        seen = False
        while True:
            try:
                if os.path.isfile(path):
                    _tail_once(path, state, source)
                    if not seen:
                        seen = True
                else:
                    # 文件被换掉/删掉了：把游标失效，下次出现时重新回看一段。
                    # 代价是可能重复几行日志 —— 重复比漏掉好，而漏掉这一段的
                    # 后果是「宿主明明报了错，界面上却没有」。
                    state["pos"] = -1
                    state["buf"] = ""
            except Exception:
                pass
            time.sleep(interval)

    t = threading.Thread(target=loop, name="logtail", daemon=True)
    t.start()
    return t
