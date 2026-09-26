# -*- coding: utf-8 -*-
"""
probe_qqx_claim.py —— 探针：QQX 真主进程「命令行读不到」的真相（只读）

## 当时要回答什么问题

host.own_processes 按「命令行带 --user-data-dir」认领我们起的 QQ，
但 QQNT 是启动器模式：启动器（带参数）拉起真主进程后，**真主进程
经 PowerShell CIM 读到的 CommandLine 为空** —— 认领永远看不到它，
stop 杀不干净、状态面板失明（2026-09-25 实测）。

要回答：
1. CIM 读到空 cmdline 的进程，换 **psutil（NtQuery PEB）** 能不能读到？
2. 真主进程的命令行里**到底有没有** --user-data-dir（即「启动器不透传」
   还是「CIM 读不到」）？
3. profile 目录里的**锁文件**由哪个进程持有（备选认领路径）？

## 复现

    python archive/probe_qqx_claim.py

前置：QQ 在跑（主号 / hosted 小号均可，越多样本越好）。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psutil

NAMES = {"qq.exe", "qqx.exe", "qqex.exe"}
OUR_PROFILE = os.path.normpath(os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "QQAgent", "qq-profile")).lower()


def main() -> int:
    print(f"我们的 profile 标记：{OUR_PROFILE}\n")
    for p in psutil.process_iter(["pid", "name", "exe", "cmdline",
                                  "ppid", "create_time"]):
        try:
            name = (p.info["name"] or "").lower()
            if name not in NAMES:
                continue
            cmd = " ".join(p.info["cmdline"] or [])
            mark = "OURS?" if OUR_PROFILE in cmd.lower() else ""
            print(f"pid={p.info['pid']:<7} name={p.info['name']:<10} ppid={p.info['ppid']:<7} {mark}")
            print(f"   exe : {p.info['exe']}")
            print(f"   cmdline({len(p.info['cmdline'] or [])} 段): {cmd[:180]}")
            if p.info["ppid"]:
                try:
                    par = psutil.Process(p.info["ppid"])
                    pname = par.name()
                    print(f"   父进程: {pname} (pid={p.info['ppid']})")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    print("   父进程: <已退出/不可读>")
            print()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    print("=== profile 目录里的锁痕迹 ===")
    for fn in ("SingletonLock", "SingletonCookie", "SingletonSocket",
               "lockfile", "Singleton"):
        path = os.path.join(OUR_PROFILE, fn)
        if os.path.exists(path):
            print(f"  存在: {path}")
    if not any(os.path.exists(os.path.join(OUR_PROFILE, fn)) for fn in
               ("SingletonLock", "SingletonCookie", "lockfile", "Singleton")):
        print("  （未发现常见 Chromium 锁文件 —— QQNT 可能用命名互斥量）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
