# -*- coding: utf-8 -*-
"""
host_setup.py —— hosted 执行面（独立桌面小号）真机实测前的准备工具

## 它解决什么

hosted 路线的小号 QQ 跑在**用户看不见的桌面**上（R4：CreateDesktopW +
lpDesktop），独立 profile 首次登录必须扫码，而二维码画在看不见的桌面上
—— 靠 PrintWindow 把窗口画面抓出来给人看（R4 实测有效）。

本工具把「起 QQ → 看二维码 → 确认登录态 → 停止」收成四条子命令，
全部走 host.py / HostedExecutor 的既有路径，**本进程不碰 UIA**
（读/抓图都经一次性 hostagent 子进程，守「宿主不碰 UIA」铁律）。

## 用法（首次登录四步）

    python host_setup.py start    # 建隐藏桌面并拉起小号 QQ（独立 profile + 四件套）
    python host_setup.py qr       # 抓 QQ 窗口画面（登录二维码）并自动打开图片
    python host_setup.py status   # 确认登录态（E-QQ-004 消失、has_ml_list=true）
    python host_setup.py stop     # 结束小号 QQ（只杀我们起的那个，不碰主号）

之后日常复跑只需要 `stop` / `start`（profile 已有登录态，免扫码）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent                                        # noqa: E402


def _executor():
    from cu.hosted import HostedExecutor
    return HostedExecutor(agent.load_config())


def cmd_start(args) -> int:
    from app import host
    r = host.start(desktop_name=(host.desktop.DEFAULT_NAME if not args.visible
                                 else ""),
                   qq_exe=args.qq_exe or "")
    print(json.dumps(r, ensure_ascii=False, indent=2)[:800])
    if not r.get("ok"):
        return 1
    print("\n[i] QQ 已拉起，等 8 秒让窗口稳定……")
    time.sleep(8)
    print("下一步：python host_setup.py qr   （抓二维码扫码登录）")
    return 0


def cmd_qr(_args) -> int:
    ex = _executor()
    shot = ex.screenshot()
    if not shot.ok:
        print(f"[X] 抓图失败：{shot.error}")
        print("    常见原因：QQ 还没起好（等几秒重试）/ 桌面上没有 QQ 主窗口")
        return 1
    print(f"[ok] 已抓取 QQ 窗口画面：{shot.path}")
    try:
        os.startfile(shot.path)          # Windows 关联程序打开（只开图，无副作用）
    except Exception as exc:
        print(f"    自动打开失败（{exc}），请手动打开上面的文件扫码")
    return 0


def cmd_status(_args) -> int:
    ex = _executor()
    try:
        h = ex.health()
    except Exception as exc:
        print(f"[X] health 异常：{type(exc).__name__}: {exc}")
        return 1
    d = h.to_dict()
    print(json.dumps(d, ensure_ascii=False, indent=2)[:900])
    if h.ok:
        print("\n[ok] 已登录且停在会话页，可以跑 test_live_brain.py --mode hosted")
        return 0
    if h.code == "E-QQ-004":
        print("\n[i] 还在登录页 —— 用 python host_setup.py qr 抓码扫码")
    elif h.code == "E-QQ-008":
        print("\n[i] 已登录但没打开会话 —— 直接跑测试即可，Brain 会自己开会话")
    return 1


def cmd_stop(_args) -> int:
    from app import host
    r = host.stop()
    print(json.dumps(r, ensure_ascii=False)[:400])
    return 0 if r.get("ok") else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="hosted 独立桌面小号的准备工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start", help="建隐藏桌面并拉起小号 QQ")
    p.add_argument("--visible", action="store_true",
                   help="拉到用户可见桌面（首登备用；独立 profile，不碰主号）")
    p.add_argument("--qq-exe", dest="qq_exe", default="", help="QQ.exe 路径")

    sub.add_parser("qr", help="抓 QQ 窗口画面（登录二维码）并打开")
    sub.add_parser("status", help="看登录态与执行面体检")
    sub.add_parser("stop", help="结束小号 QQ（按 pid，不碰主号）")

    a = ap.parse_args()
    return {"start": cmd_start, "qr": cmd_qr,
            "status": cmd_status, "stop": cmd_stop}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
