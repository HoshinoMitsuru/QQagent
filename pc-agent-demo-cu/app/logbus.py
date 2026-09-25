# -*- coding: utf-8 -*-
"""
logbus.py —— 日志总线

界面上的日志面板要和 agent 子进程的 stdout 实时对齐，而 stdout 是纯文本、
一次可能吐半行。所以这里的职责有三件：

    1. 把字节流切成完整的行（处理 \r\n、半行、无换行结尾的情况）
    2. 解析出 `[HH:MM:SS] TAG  正文` 三段，让前端能按级别着色、按标签过滤
    3. 分发给所有订阅者（SSE 连接），同时留一份环形缓冲给「刚打开页面的人」

环形缓冲很关键：用户打开页面时，之前几秒的日志应该立刻可见，
而不是从「打开之后发生的事」开始 —— 否则一进界面看到空白，会以为程序死了。
"""

from __future__ import annotations

import collections
import json
import os
import sys
import queue
import re
import threading
import time

from . import paths

MAX_BUFFER = 3000
MAX_UI_LOG_BYTES = 4 * 1024 * 1024

_LINE_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*(\S+)?\s*(.*)$")

# 标签 → 级别。级别只用于着色，不影响行为。
_LEVEL_BY_TAG = {
    "ERR": "error", "ERROR": "error", "X": "error", "FATAL": "error",
    "WARN": "warn", "WARNING": "warn",
    "SEND": "action", "QUE": "action", "DELEV": "action", "DISC": "action",
    "DISCO": "action", "SCAN": "action", "BUF": "action", "CONT": "action",
    "TEACH": "action", "ID": "action", "DRY": "action", "BEAT": "debug",
    "DBG": "debug", "TRACE": "debug",
}


def _classify(tag: str) -> str:
    return _LEVEL_BY_TAG.get((tag or "").upper(), "info")


class LogBus:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._buf: collections.deque = collections.deque(maxlen=MAX_BUFFER)
        self._subs: list[queue.Queue] = []
        self._seq = 0

    # ---------------------------------------------------------- 写入
    def emit(self, text: str, *, tag: str = "UI", source: str = "ui",
             level: str | None = None) -> dict:
        """
        记一条**已经成行**的日志。

        除了写文件与推给订阅者，还会**回显到 stdout**（前提是 stdout 是个真流）。

        ## 为什么必须回显

        `build.bat console` 产出的诊断版，存在的唯一意义就是「让用户看见发生了什么」。
        如果日志只进文件，用户双击后看到的是一个**全黑的空窗口** ——
        和 windowed 版的「一闪就没了」一样没有信息量，诊断版就白做了。

        windowed 版里 stdout 被换成 devnull，回显自然不会发生（也不会报错）。
        """
        with self._lock:
            self._seq += 1
            row = {
                "seq": self._seq,
                "ts": time.time(),
                "clock": time.strftime("%H:%M:%S"),
                "tag": tag or "",
                "level": level or _classify(tag),
                "source": source,
                "text": text.rstrip("\r\n"),
            }
            self._buf.append(row)
            subs = list(self._subs)
        self._echo(row)
        payload = json.dumps(row, ensure_ascii=False)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # 订阅者卡住了（比如浏览器标签页被挂起）。丢最旧的，保住最新的，
                # 千万别在这里阻塞写入方 —— 那会把 agent 的子进程一起拖死。
                try:
                    q.get_nowait()
                    q.put_nowait(payload)
                except Exception:
                    pass
        self._write_file(row)
        return row

    def _echo(self, row: dict) -> None:
        """
        把日志回显到 stdout —— 只在本进程真的有控制台时。

        判据用「stdout 不是 None 且不是 devnull」：windowed 打包时
        `app/main._ensure_stdio()` 会把它指向 devnull，这时全程静默（也不报错）。
        """
        try:
            out = sys.stdout
            if out is None or getattr(out, "name", "") == os.devnull:
                return
            out.write(f"[{row['clock']}] {row['tag']:<5} {row['text']}\n")
            out.flush()
        except Exception:
            pass        # 回显是纯观测，绝不能因为写 stdout 失败影响主流程

    def feed(self, raw: str, *, source: str = "agent") -> None:
        """喂一段可能不完整的文本，按行切分后 emit（供子进程 stdout 泵使用）。"""
        for line in str(raw).splitlines():
            if not line.strip():
                continue
            m = _LINE_RE.match(line)
            if m:
                # group(1) 是行首时间戳，这里不用它 ——
                # emit() 会盖自己的时间戳（以实际写入时刻为准）。
                tag, text = (m.group(2) or ""), m.group(3)
                level = _classify(tag)
                self.emit(text, tag=tag, source=source, level=level)
            else:
                self.emit(line, tag="", source=source)

    def _write_file(self, row: dict) -> None:
        try:
            os.makedirs(paths.LOG_DIR, exist_ok=True)
            if os.path.isfile(paths.UI_LOG_PATH) and \
                    os.path.getsize(paths.UI_LOG_PATH) > MAX_UI_LOG_BYTES:
                rotated = paths.UI_LOG_PATH + ".1"
                try:
                    if os.path.isfile(rotated):
                        os.remove(rotated)
                    os.replace(paths.UI_LOG_PATH, rotated)
                except Exception:
                    pass
            with open(paths.UI_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{row['clock']}] {row['tag']:<5} {row['text']}\n")
        except Exception:
            pass        # 日志写不进去也不能影响主流程

    # ---------------------------------------------------------- 读取
    def since(self, seq: int = 0, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = [r for r in self._buf if r["seq"] > seq]
        return rows[-limit:]

    def tail(self, limit: int = 200) -> list[dict]:
        with self._lock:
            return list(self._buf)[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def counts(self) -> dict:
        with self._lock:
            rows = list(self._buf)
        out = {"error": 0, "warn": 0, "action": 0, "info": 0, "debug": 0}
        for r in rows:
            out[r["level"]] = out.get(r["level"], 0) + 1
        return out

    # ---------------------------------------------------------- 订阅
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


BUS = LogBus()

# 让别处 `from .logbus import BUS` 就能用；同时提供一个模块级函数减少样板
log = BUS.emit
