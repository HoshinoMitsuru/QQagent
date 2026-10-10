# -*- coding: utf-8 -*-
"""
test_stop_cleanup.py —— stop_daemon 收尾清理（杀 QQ + 销毁桌面）离线自检

2026-09-26 拍板：R4 进程关闭 = 同时关闭虚拟桌面上的 QQ 和虚拟桌面。
本文件用 stub 验证四条链路（不碰 QQ、不碰真实桌面）：
1. _cleanup_after_stop 的正常路径（QQ 杀干净 → 桌面销毁）
2. 容错路径（stop 炸 / close 炸 / 销毁超时，各记各的错，不阻塞主结论）
3. stop_daemon 两个退出分支（宿主在跑/不在跑）都要做收尾
4. keep_qq（切换模式专用）真的不碰 QQ；restart_daemon 只换模式、其余参数原样回放
5. 启动期失败（rc=2）由宿主自己收尾（2026-10-10）：判据只有一处、正常停止不动 QQ、
   收尾炸了不改退出码

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
#: 后面几节还会替换这些入口（含 [4] 的切换模式）—— 一并存下来，收尾时还回去。
#: 不还原的话，将来往本文件末尾再加用例，就会在一个「假装」的环境里跑。
_saved_more = {n: getattr(host_mod, n) for n in
               ("daemon_status", "start_daemon", "stop_daemon", "last_launch",
                "_cleanup_after_stop")}
_saved_time = host_mod.time
_saved_pid_alive = pw.pid_alive

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

    # ------------------------------------------------------------------
    print("[4] 切换模式：keep_qq 不动 QQ；restart_daemon 原样回放启动参数")
    #
    # 2026-10-09：界面加了「运行中切模式」。模式是宿主的**启动参数**，所以切换
    # 必须重启宿主进程；但**不必**连 QQ 一起重启 —— 而原来唯一那条
    # 「先停再启动」的路会把那张桌面上的 QQ 一起收走（_cleanup_after_stop），
    # 于是切一次模式要重新冷启 QQ、重新等窗口，一两分钟。
    # 这里钉住两件事：keep_qq 真的不碰 QQ；restart 真的只换模式、其余照旧。

    calls = []
    host_mod.daemon_status = lambda: {"pid": 4321, "alive": True}
    pw.pid_alive = lambda pid: False
    host_mod._cleanup_after_stop = lambda *a, **k: (
        calls.append("cleanup"), {"qq": None, "desktop": None})[1]

    out = host_mod.stop_daemon(keep_qq=True)
    case("keep_qq=True：完全不碰 QQ 与桌面（cleanup 没被调用）",
         calls == [] and "keep_qq" in str(out["cleanup"]), f"calls={calls} {out}")

    calls.clear()
    out = host_mod.stop_daemon()
    case("默认 keep_qq=False：仍然照老语义收尾（杀 QQ + 销毁桌面）",
         calls == ["cleanup"], f"calls={calls} {out}")

    # ---- restart_daemon 的替身：把 stop/start 换成记录本 ----
    seen = {"stop": [], "start": [], "sleep": 0}
    RUN_ST = {"pid": 4321, "alive": True, "running": True, "dry_run": True,
              "no_send": False, "agent_enabled": True,
              "desktop": "QQAgentHidden", "profile": "C:\\x\\qq-profile"}
    REC = {"desktop_name": "QQAgentHidden", "profile": "C:\\x\\qq-profile",
           "qq_exe": "D:\\QQ\\QQ.exe", "with_agent": True, "stop_qq_on_exit": False,
           "chat": "测试会话"}
    host_mod.daemon_status = lambda: dict(RUN_ST)
    host_mod.last_launch = lambda: dict(REC)

    def fake_stop(timeout=20.0, keep_qq=False):
        seen["stop"].append({"timeout": timeout, "keep_qq": keep_qq})
        return {"ok": True, "pid": 4321, "waited": 1.2, "killed": False,
                "error": "", "cleanup": None}

    def fake_start(**kw):
        seen["start"].append(kw)
        return {"ok": True, "pid": 999, "desktop": kw.get("desktop_name"),
                "profile": kw.get("profile"), "args": [], "error": ""}

    host_mod.stop_daemon = fake_stop
    host_mod.start_daemon = fake_start
    _saved_time = host_mod.time
    host_mod.time = types.SimpleNamespace(sleep=lambda s: seen.__setitem__("sleep", s))

    r = host_mod.restart_daemon(dry_run=False)
    kw = seen["start"][0] if seen["start"] else {}
    case("切模式：先停（且是 keep_qq=True，QQ 留着）",
         seen["stop"] == [{"timeout": 20.0, "keep_qq": True}], str(seen["stop"]))
    case("切模式：只换 dry_run，其余参数从启动记录原样回放",
         kw.get("dry_run") is False and kw.get("no_send") is False
         and kw.get("desktop_name") == "QQAgentHidden"
         and kw.get("profile") == "C:\\x\\qq-profile"
         and kw.get("qq_exe") == "D:\\QQ\\QQ.exe"
         and kw.get("chat") == "测试会话"
         and kw.get("with_agent") is True and kw.get("stop_qq_on_exit") is False,
         str(kw))
    case("切模式：返回值说清「以前是什么、现在是什么」",
         r.get("ok") is True and r.get("mode") == {"dry_run": False, "no_send": False}
         and r.get("prev_mode") == {"dry_run": True, "no_send": False}
         and r.get("degraded") == [], str(r))
    case("切模式：起新宿主前让旧进程退干净（那个 0.6s）",
         seen["sleep"] == 0.6, str(seen["sleep"]))

    # 模式没变 → 不该白重启一次（重启是真实的中断），更不能假装切过了
    seen["stop"].clear(); seen["start"].clear()
    r = host_mod.restart_daemon(dry_run=True)          # 当前就是 dry_run=True
    case("模式没变 → 什么都不做，并如实说「没变」（不假装切过）",
         not seen["stop"] and not seen["start"]
         and r.get("ok") is True and r.get("unchanged") is True, str(r))

    # 没有启动记录（老版本起的宿主 / 文件被删）→ 用心跳兜底，并把没还原到的项报出来
    seen["stop"].clear(); seen["start"].clear()
    host_mod.last_launch = lambda: {}
    r = host_mod.restart_daemon(dry_run=False)
    kw = seen["start"][0] if seen["start"] else {}
    case("启动记录缺失 → 用心跳兜底（桌面/profile 不丢），并报出没还原的项",
         kw.get("desktop_name") == "QQAgentHidden"
         and kw.get("profile") == "C:\\x\\qq-profile"
         and set(r.get("degraded") or []) == {"qq_exe", "chat", "stop_qq_on_exit"},
         f"{kw} degraded={r.get('degraded')}")

    # 没跑宿主 / 只是「摆着」的宿主，都不该装作切成功
    host_mod.daemon_status = lambda: {"pid": 0, "alive": False, "running": False}
    seen["stop"].clear(); seen["start"].clear()
    r = host_mod.restart_daemon(dry_run=False)
    case("宿主没在跑 → 明说「没有模式可切」，不动任何东西",
         r.get("ok") is False and "没在运行" in str(r.get("error"))
         and not seen["stop"] and not seen["start"], str(r))

    host_mod.daemon_status = lambda: dict(RUN_ST, agent_enabled=False)
    r = host_mod.restart_daemon(dry_run=False)
    case("--no-agent 起的（没跑回复循环）→ 没有模式可切，明说原因",
         r.get("ok") is False and "回复循环" in str(r.get("error")), str(r))

    # ------------------------------------------------------------------
    print("[5] 启动期失败（rc=2）由宿主自己收尾 —— 判据 + 收尾入口")
    #
    # 2026-10-10 拍板。真实事故：宿主启动失败（rc=2）退出后，隐藏桌面上留下一台
    # 渲染进程已死的 QQ；而壳**不知道**宿主失败了（start_daemon 丢完进程就返回），
    # 于是没人收 —— 现场还删不掉（壳提权 → QQ 也提权 → 非提权 shell 的 taskkill
    # 是「拒绝访问」），旧 exe 被占用，下一次构建直接失败。
    # 这里钉住三件事：判据只有一处定义；正常停止**不许**顺手带走 QQ；
    # 收尾自己炸了也不许改退出码（退出码是壳判断「正常停止 / 启动失败」的唯一依据）。

    import app.hostd as hostd_mod

    case("RC_STARTUP_FAIL 就是 2（与 fail() / _run_agent 的约定一致）",
         hostd_mod.RC_STARTUP_FAIL == 2)
    case("rc=2 → full（启动期失败自动收尾）",
         hostd_mod._exit_cleanup_plan(2, False) == "full")
    case("rc=0 → none（正常停止交给壳的 stop_daemon，不在这里动 QQ）",
         hostd_mod._exit_cleanup_plan(0, False) == "none")
    case("--stop-qq-on-exit → qq-only（用户显式指定，不叠一次全量收尾）",
         hostd_mod._exit_cleanup_plan(0, True) == "qq-only")
    case("--stop-qq-on-exit 优先于 rc=2（不重复收一遍）",
         hostd_mod._exit_cleanup_plan(2, True) == "qq-only")

    seen_cleanup = []
    host_mod._cleanup_after_stop = lambda profile="", **k: (
        seen_cleanup.append(profile),
        {"qq": {"ok": True, "killed": 3},
         "desktop": {"name": "QQAgentHidden", "closed": True, "destroyed": True}})[1]
    out = hostd_mod._cleanup_on_exit("", "启动期失败 rc=2（本用例）")
    case("_cleanup_on_exit 真的调 host._cleanup_after_stop 并把结果原样返回",
         seen_cleanup == [""] and out["qq"]["killed"] == 3, str(out))

    def _boom(*a, **k):
        raise RuntimeError("收尾炸了")

    host_mod._cleanup_after_stop = _boom
    out = hostd_mod._cleanup_on_exit("", "启动期失败 rc=2（本用例）")
    case("收尾自己炸了 → 吞掉并报 ok=False（绝不改退出码）",
         out.get("ok") is False and "RuntimeError" in str(out.get("error")), str(out))

    # 源码守门：main() 的 finally 里必须走这条判据，而不是留着旧的裸 if。
    # （dir() 看不见「写了但没接上」这种情况，只能扫源码。）
    _hd_src = open(os.path.join(HERE, "app", "hostd.py"), encoding="utf-8").read()
    case("hostd 的 finally 通过 _exit_cleanup_plan 决定收尾（不是裸 if）",
         "_exit_cleanup_plan(rc, a.stop_qq_on_exit)" in _hd_src, "")
    case("hostd 里 full 分支真的调了 _cleanup_on_exit",
         'elif plan == "full":' in _hd_src and "_cleanup_on_exit(a.profile" in _hd_src, "")

finally:
    for n, v in _saved.items():
        setattr(host_mod, n, v)
    desktop.close, desktop.exists = _saved_close, _saved_exists
    # [3]/[4] 里被替换掉的模块级符号也要还回去 —— 它们是真模块的入口，
    # 不还原的话，将来往本文件后面再加用例就会在一个「假装」的环境里跑。
    for n, v in _saved_more.items():
        setattr(host_mod, n, v)
    host_mod.time = _saved_time
    pw.pid_alive = _saved_pid_alive

print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
