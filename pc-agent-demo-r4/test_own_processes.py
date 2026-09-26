# -*- coding: utf-8 -*-
"""
test_own_processes.py —— host.own_processes（QQX 认领根治）的离线自检

## 背景（2026-09-26 根治）

旧实现走 PowerShell CIM 读命令行，对 QQ 主进程返回空 —— 认领永远看不到
主进程，stop 杀不干净。新实现走 psutil（PEB 读取），且「名字过滤 +
命中后 oneshot 精读」防批量读空。本文件用 stub psutil 验证逻辑。

用法：python test_own_processes.py
"""

from __future__ import annotations

import os
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


class FakeProc:
    """psutil.Process 替身。cmdline_segs=None 模拟「批量读空」→ oneshot 精读成功。"""

    def __init__(self, pid, name, cmdline_segs, ppid=100):
        self._pid = pid
        self._name = name
        self._cmdline_segs = cmdline_segs
        self._ppid = ppid
        self.oneshot_reads = 0
        # 真 psutil 的 process_iter(["pid", "name"]) 会把这两个字段填进
        # info dict（批量缓存属性），own_processes 用它做名字预过滤并取 pid。
        self.info = {"pid": pid, "name": name}

    @property
    def pid(self):
        return self._pid

    def name(self):
        return self._name

    def oneshot(self):
        self.oneshot_reads += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cmdline(self):
        if self._cmdline_segs is None:
            raise psutil.AccessDenied()
        return self._cmdline_segs

    def ppid(self):
        return self._ppid

    def create_time(self):
        return 1000.0 + self._pid


class FakeProcessAttr:
    """模拟 process_iter 批量 info 的旧风格对象（用于验证新实现不依赖它）。"""


PROFILE = "c:\\users\\test\\appdata\\local\\qqagent\\qq-profile"
MARKER = PROFILE
FLAG = "D:\\QQ.exe --force-renderer-accessibility --user-data-dir=" + PROFILE

# ---- stub psutil 进 app.host 的命名空间 ----
import psutil                                    # noqa: E402
import app.host as host_mod                      # noqa: E402

_saved_iter = psutil.process_iter
_saved_profile_dir = host_mod.profile_dir

procs = [
    FakeProc(20120, "QQ.exe", ["D:\\QQ.exe", "--force-renderer-accessibility",
                               "--user-data-dir=" + PROFILE,
                               "--disable-backgrounding-occluded-windows"]),
    FakeProc(2584, "QQ.exe", ["D:\\QQ.exe", "--type=renderer",
                              "--user-data-dir=" + PROFILE]),
    FakeProc(11988, "QQ.exe", ["D:\\QQ.exe", "--relaunch",
                               "--force-renderer-accessibility"]),  # 主号：无标记
    FakeProc(5555, "chrome.exe", ["chrome.exe", "--user-data-dir=" + PROFILE]),  # 非 QQ
    FakeProc(6666, "QQ.exe", None),  # cmdline 批量不可读（oneshot 也失败 → 跳过）
]

psutil.process_iter = lambda attrs=None: iter(procs)
host_mod.profile_dir = lambda profile="": PROFILE

rows = host_mod.own_processes()
pids = sorted(r["pid"] for r in rows)

case("主进程（带 user-data-dir）被认领", 20120 in pids, str(pids))
case("子进程（同 profile）被认领", 2584 in pids, str(pids))
case("主号主进程（无标记）不误认领", 11988 not in pids, str(pids))
case("非 QQ 进程（名字过滤）不认领", 5555 not in pids, str(pids))
case("cmdline 完全读不到的进程被跳过且不炸", 6666 not in pids, str(pids))
case("行结构带 ppid/create_time 扩展字段",
     all(("ppid" in r and "create_time" in r and "cmdline" in r) for r in rows),
     str(rows[:1]))

# oneshot 被使用（防退化回批量读空）
used_oneshot = all(p.oneshot_reads >= 1 for p in procs[:2])
case("命中进程走 oneshot 精读（防批量读空退化）", used_oneshot, "")

# profile_dir 还原
host_mod.profile_dir = _saved_profile_dir
psutil.process_iter = _saved_iter

print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
