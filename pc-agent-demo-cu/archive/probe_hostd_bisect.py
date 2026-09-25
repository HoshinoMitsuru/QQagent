# -*- coding: utf-8 -*-
"""
probe_hostd_bisect.py —— 二分定位：宿主进程里 UIA 读不到树，是哪一步造成的

## 背景（2026-09-25 实测）

同一个隐藏桌面、同一个 QQ：

    host.grab 派出去的**一次性子进程**     → 无障碍树读得到（昵称、会话列表、143 节点）
    hostd 常驻宿主进程里直接 import agent  → attach 成功但 dom_exposed() = False
                                             （E-QQ-004 无障碍树是空壳）

100% 可复现，所以是 hostd 进程的某个**确定性差异**。本脚本把 hostd 做过的事
按顺序一件件加回来，每次跑一个"档位"，找出是哪一件让树变空的。

## 为什么这件事值得单独查

「子进程读得到、宿主读不到」意味着**只有常驻形态是坏的**，而常驻恰恰是这个
版本要交付的东西。不查清楚就只能靠猜，会浪费很多轮。

## 用法

    python archive/probe_hostd_bisect.py            # 跑全部档位（每个档位一个新进程）
    python archive/probe_hostd_bisect.py --only E
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

os.environ.setdefault("QQ_AGENT_HOME", _HERE)

from app import desktop, host, paths  # noqa: E402

OUT_DIR = os.path.join(paths.STATE_DIR, "bisect")

#: 档位定义：名字 → 在做完「基准动作」之后、import agent 之前，额外做哪些事。
#:
#: 基准动作（所有档位都做，因为 hostd 一定会做）：自报桌面名。
LEVELS = {
    "A": "最简：只自报桌面名，然后 import agent",
    "B": "A + 接管 Tee（把 stdout/stderr 换成写 hostd.log 的那个对象）",
    "C": "B + 接管桌面句柄（desktop.create(keep=True)）",
    "D": "C + 认领进程（host.own_processes，走 PowerShell）",
    "E": "D + 在本进程里枚举一次窗口（hostagent.find_qq_window）",
    "F": "E + 派一个子进程抓一次图（host.grab，含 UIA）",
    "G": "F + 走一遍完整的免扫码登录探测（host.grab(login=True,state=True)）",
}
ORDER = list(LEVELS)


def _tee_like_hostd():
    """复刻 hostd 的 _install_stdio：把 stdout 换成同时写文件的 tee。"""
    from app import hostd
    return hostd._install_stdio()


def run_level(name: str) -> dict:
    """在**当前进程**里跑一个档位。调用方负责每个档位起一个新进程。"""
    res: dict = {"level": name, "desc": LEVELS[name], "steps": [], "ok": False,
                 "error": ""}

    def mark(text):
        res["steps"].append(text)

    try:
        mark(f"桌面自报={desktop.current_name()!r}")
        if name >= "B":
            _tee_like_hostd()
            mark("已接管 stdout/stderr（Tee）")
        if name >= "C":
            r = desktop.create(host_path_desktop(), keep=True)
            mark(f"desktop.create({host_path_desktop()!r}) -> ok={r['ok']} existed={r['existed']}")
        if name >= "D":
            procs = host.own_processes()
            mark(f"own_processes -> {len(procs)} 个进程")
        if name >= "E":
            from app import hostagent
            found = hostagent.find_qq_window()
            mark(f"find_qq_window -> {found['count']} 个候选，"
                 f"main={found['main']['hwnd'] if found['main'] else None}")

        # ---- 到这里才开始碰 UIA（和 hostd 一样：agent 是第一个碰它的）
        mark("import agent ...")
        import agent as A
        cfg = A.load_config()
        ag = A.Agent(cfg, dry_run=True, no_send=True)
        t0 = time.time()
        a = ag.qq.attach()      # attach 在 QQWindow 上，不在 Agent 上
        mark(f"attach() -> {a}（{time.time() - t0:.2f}s）")
        if a:
            mark(f"选中的窗口：title={ag.qq.dialog_title!r} "
                 f"class={ag.qq.win.ClassName if ag.qq.win else None!r} "
                 f"hwnd={ag.qq.hwnd}")
        d = ag.qq.dom_exposed()
        mark(f"dom_exposed() -> {d}（消息列表={ag.qq.ml_list is not None} "
             f"输入框={ag.qq.editor is not None}）")
        res["ok"] = bool(a and d)

        # ---- 决定性对比：**同一个进程**里换一种走法再读一次。
        # agent 用的是 uiautomation 的搜索（Control(searchDepth=..., ClassName=...)），
        # hostagent 用的是手工 GetChildren 递归。如果后者在同一进程里能读到，
        # 那就说明「读不到」不是权限/桌面/COM 的问题，而是**搜索这一步**的问题。
        if a:
            # agent 自己的 BFS 走一遍：看它到底能走到第几层
            try:
                n = 0
                for ctrl, dep in A.iter_bfs(ag.qq.win, A.SCAN_MAX_DEPTH):
                    n += 1
                    if n <= 6:
                        mark(f"  agent-BFS#{n} 深度={dep} cls={A._cls(ctrl)!r} "
                             f"type={A._ctype(ctrl)!r} name={A._name(ctrl)!r}")
                mark(f"agent-BFS 共走到 {n} 个节点（上限 {A.SCAN_MAX_NODES}，"
                     f"深度上限 {A.SCAN_MAX_DEPTH}）")
                anchors = ag.qq._scan_anchors()
                mark(f"_scan_anchors 命中键：{sorted(anchors)}")
            except Exception as exc:
                mark(f"agent-BFS 异常 {type(exc).__name__}: {exc}")
            try:
                from app import hostagent
                u = hostagent.probe_uia(ag.qq.hwnd)
                mark(f"同进程·手工遍历 hostagent.probe_uia -> ok={u.get('ok')} "
                     f"昵称={u.get('nickname')!r} 节点={u.get('nodes_scanned')} "
                     f"会话列表={u.get('has_recent_list')} err={u.get('error')!r}")
            except Exception as exc:
                mark(f"同进程·手工遍历异常 {type(exc).__name__}: {exc}")
            try:
                import uiautomation as auto
                w = auto.ControlFromHandle(ag.qq.hwnd)
                kids = w.GetChildren()
                mark(f"同进程·ControlFromHandle -> Name={w.Name!r} "
                     f"ClassName={w.ClassName!r} 子节点数={len(kids)}")
                if kids:
                    mark("  前几个子节点：" + "; ".join(
                        f"{k.ControlTypeName}/{k.ClassName}" for k in kids[:5]))
            except Exception as exc:
                mark(f"同进程·ControlFromHandle 异常 {type(exc).__name__}: {exc}")
            # uiautomation 的**搜索**这一步单独试一次，把范围放到最大
            try:
                import uiautomation as auto
                win = auto.ControlFromHandle(ag.qq.hwnd)
                hits = win.FindAll(auto.ControlType(50033),  # 50033 = UIA_TextControlTypeId
                                   maxSearchSeconds=3.0)
                mark(f"同进程·FindAll(控件类型=Text) 命中 {len(hits)} 个")
            except Exception as exc:
                mark(f"同进程·FindAll 异常 {type(exc).__name__}: {exc}")

        if name >= "F":
            r = host.grab("", desktop_name=host_path_desktop(), wait=0.5, uia=True)
            u = r.get("uia") or {}
            mark(f"子进程抓图 -> ok={r.get('ok')} uia.ok={u.get('ok')} "
                 f"昵称={u.get('nickname')!r} 节点={u.get('nodes_scanned')}")
        if name >= "G":
            r = host.grab("", desktop_name=host_path_desktop(), wait=1.0,
                          uia=True, login=True, state=True)
            st = r.get("login_state") or {}
            mark(f"子进程登录态探测 -> ok={r.get('ok')} logged_in={st.get('logged_in')} "
                 f"nickname={st.get('nickname')!r}")
        return res
    except Exception:
        res["error"] = traceback.format_exc()
        mark("异常（见 error）")
        return res


def host_path_desktop() -> str:
    return os.environ.get("QQ_AGENT_DESKTOP") or desktop.DEFAULT_NAME


def _child(level: str) -> int:
    """子进程入口：跑一个档位，把结果写文件。"""
    r = run_level(level)
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, f"{level}.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=2)
    return 0 if r["ok"] else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--child", default="", help="内部使用：在子进程里跑这个档位")
    ap.add_argument("--only", default="", help="只跑某个档位，逗号分隔")
    a = ap.parse_args()

    if a.child:
        return _child(a.child)

    if getattr(sys, "frozen", False):
        print("[X] 打包版不支持（sys.executable 是 exe 自身）")
        return 2

    name = host_path_desktop()
    os.makedirs(OUT_DIR, exist_ok=True)
    if not desktop.exists(name):
        print(f"[X] 桌面 {name} 不存在 —— 先在控制台面板里启动一次隐藏桌面")
        return 2

    wanted = [x.strip().upper() for x in a.only.split(",") if x.strip()] or ORDER
    print(f"隐藏桌面：{name}　档位：{','.join(wanted)}")
    print("=" * 78)

    results = []
    for lv in wanted:
        if lv not in LEVELS:
            print(f"[X] 未知档位 {lv}")
            continue
        path = os.path.join(OUT_DIR, f"{lv}.json")
        try:
            os.remove(path)
        except OSError:
            pass
        # 每个档位**一个新进程**：UIA / COM 的状态在进程内是有粘性的，
        # 在同一个进程里连着跑几个档位，前一个档位的副作用会污染后一个。
        r = desktop.spawn(sys.executable,
                          ["-m", "archive.probe_hostd_bisect", "--child", lv],
                          desktop=name, cwd=_HERE)
        if not r["ok"]:
            print(f"[{lv}] 启动失败：{r['error']}")
            continue
        desktop.wait(r["hproc"], 120.0)
        desktop.close_handle(r["hproc"])
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            print(f"[{lv}] 读不到结果：{exc}")
            continue
        results.append(data)
        print(f"\n[{lv}] {'★通过' if data['ok'] else '✗失败'}　{data['desc']}")
        for s in data["steps"]:
            print(f"      {s}")
        if data.get("error"):
            print(f"      !! {data['error'].strip().splitlines()[-1]}")

    print("\n" + "=" * 78)
    print("结论：")
    for d in results:
        print(f"  {d['level']}　{'通过' if d['ok'] else '失败'}　{d['desc']}")
    first_bad = next((d["level"] for d in results if not d["ok"]), "")
    if not first_bad:
        print("  所有档位都通过 —— 说明问题不在这些步骤里，往别处找")
    else:
        idx = ORDER.index(first_bad)
        print(f"  第一个失败的档位：{first_bad}"
              f"（{LEVELS[first_bad]}）")
        if idx > 0:
            print(f"  → 也就是说，问题出在这一档**新加的那件事**上："
                  f"对比 {ORDER[idx - 1]} 与 {first_bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
