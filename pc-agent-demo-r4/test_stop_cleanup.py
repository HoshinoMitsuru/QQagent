# -*- coding: utf-8 -*-
"""
test_stop_cleanup.py —— stop_daemon 收尾清理（杀 QQ + 销毁桌面）离线自检

2026-09-26 拍板：R4 进程关闭 = 同时关闭虚拟桌面上的 QQ 和虚拟桌面。
本文件用 stub 验证三条链路（不碰 QQ、不碰真实桌面）：
1. _cleanup_after_stop 的正常路径（QQ 杀干净 → 桌面销毁）
2. 容错路径（stop 炸 / close 炸 / 销毁超时，各记各的错，不阻塞主结论）
3. stop_daemon 两个退出分支（宿主在跑/不在跑）都要做收尾

用法：python test_stop_cleanup.py
"""

from __future__ import annotations

import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from app import desktop, platform_win as pw          # noqa: E402
import app.host as host_mod                          # noqa: E402

OK = FAIL = 0


def case(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


class Stopper:
    """替身工具箱：替换 host_mod 的模块级符号，逐用例重置。"""

    def __init__(self):
        self.calls = []

    def reset(self, *, stop_result=None, stop_exc=None, close_result=True,
               close_exc=None, exists_result=False, record=None):
        self.calls = []

        def stop(profile=""):
            self.calls.append(f"stop({profile!r})")
            if stop_exc:
                raise stop_exc
            return stop_result

        def last_record():
            self.calls.append("last_record()")
            return record if record is not None else {"desktop": "QQAgentHidden"}

        def close(name=desktop.DEFAULT_NAME):
            self.calls.append(f"close({name!r})")
            if close_exc:
                raise close_exc
            return close_result

        def exists(name=desktop.DEFAULT_NAME, probe=None):
            self.calls.append(f"exists({name!r})")
            return exists_result

        host_mod.stop = stop
        host_mod.last_record = last_record
        desktop.close = close
        desktop.exists = exists


S = Stopper()
_saved = {n: getattr(host_mod, n) for n in ("stop", "last_record")}
_saved_close, _saved_exists = desktop.close, desktop.exists

try:
    # ------------------------------------------------------------------
    print("[1] 正常路径：QQ 杀干净 → 放句柄 → 桌面销毁")
    S.reset(stop_result={"ok": True, "killed": 8},
            close_result=True, exists_result=False)
    out = host_mod._cleanup_after_stop(destroy_wait=0.2)
    case("qq 字段来自 host.stop",
         out["qq"] == {"ok": True, "killed": 8}, str(out))
    case("close 收到 record 里的桌面名",
         "close('QQAgentHidden')" in S.calls, str(S.calls))
    case("destroyed=True（exists 探测失败 = 引用归零）",
         out["desktop"]["destroyed"] is True, str(out))

    # ------------------------------------------------------------------
    print("[2] 容错：stop 炸 → 照样收桌面；close 炸 → 记错不阻塞")
    S.reset(stop_exc=RuntimeError("认领列表炸了"))
    out = host_mod._cleanup_after_stop(destroy_wait=0.2)
    case("stop 炸 → qq.ok=False 且继续走桌面收尾",
         out["qq"]["ok"] is False and "close" in str(S.calls), str(out))

    S.reset(stop_result={"ok": True}, close_exc=OSError("句柄无效"))
    out = host_mod._cleanup_after_stop(destroy_wait=0.2)
    case("close 炸 → desktop.error 记录且不再探测",
         out["desktop"]["closed"] is False and "destroyed" not in out["desktop"],
         str(out))

    S.reset(stop_result={"ok": True}, close_result=False, exists_result=True)
    out = host_mod._cleanup_after_stop(destroy_wait=0.3)
    case("销毁超时 → destroyed=False + 指认 QQ 残留的 error",
         out["desktop"]["destroyed"] is False and "QQ 没死透" in out["desktop"]["error"],
         str(out))

    # ------------------------------------------------------------------
    print("[3] stop_daemon：两个退出分支都要收尾")

    # 分支 A：宿主本来就没在跑（崩溃/被手动杀后的僵尸场景）
    calls = []
    host_mod.daemon_status = lambda: {"pid": 0, "alive": False}
    host_mod._cleanup_after_stop = lambda *a, **k: (calls.append("cleanup"), {"qq": None, "desktop": None})[1]
    try:
        os.remove(host_mod.HOSTD_STOP)
    except OSError:
        pass
    out = host_mod.stop_daemon()
    case("无宿主分支也执行收尾（僵尸 QQ/桌面靠它收）",
         calls == ["cleanup"] and out["ok"] is True and out["cleanup"] is not None,
         str(out))

    # 分支 B：宿主在跑 → 优雅退出 → 收尾
    calls.clear()
    host_mod.daemon_status = lambda: {"pid": 4321, "alive": True}
    pw.pid_alive = lambda pid: False          # 写哨兵后立即「退出」
    host_mod.paths.STATE_DIR = os.path.join(HERE, "state")
    os.makedirs(host_mod.paths.STATE_DIR, exist_ok=True)
    out = host_mod.stop_daemon()
    case("正常停止分支执行收尾",
         calls == ["cleanup"] and out["ok"] is True and out["cleanup"] is not None,
         str(out))

finally:
    for n, v in _saved.items():
        setattr(host_mod, n, v)
    desktop.close, desktop.exists = _saved_close, _saved_exists

print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
