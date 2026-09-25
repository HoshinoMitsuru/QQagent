# -*- coding: utf-8 -*-
"""
probe_host_panel.py —— 控制台「隐藏桌面」面板的接口验收

## 它要回答的问题

面板做完了，但「面板能显示」和「接口真的通」是两件事。这个脚本按界面上的
点击顺序把接口走一遍，并**核对返回内容**，而不是只看 HTTP 200：

    1. GET  /api/state            → 有没有 host / grab_last 两段
    2. GET  /api/host/status      → 宿主的实时状态（阶段、pid、登录态）
    3. GET  /api/logs             → 日志总线里有没有**宿主写的那几行**
                                    （这一条是在验 logtail 那条通道真的接通了：
                                     宿主没有 stdout 管道，界面能看见它全靠这个）
    4. POST /api/host/grab        → 抓图任务能不能起来
    5. GET  /api/host/shot.png    → 抓出来的图能不能取回（并核对它是不是真图）
    6. GET  /api/host/status      → 图的信息有没有回填到状态里

## 用法（需要控制台已经在跑）

    python archive/probe_host_panel.py                 # 只读，不启动/停止任何东西
    python archive/probe_host_panel.py --grab          # 额外做一次抓图（会派子进程进隐藏桌面）

端口与令牌从 `state/ui.json` 里读（那是控制台自己写的，手抄容易抄错）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

os.environ.setdefault("QQ_AGENT_HOME", HERE)

from app import paths  # noqa: E402

UI_JSON = os.path.join(paths.STATE_DIR, "ui.json")

OK, BAD = 0, 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global OK, BAD
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        BAD += 1
        print(f"  [FAIL] {name} {detail}")
    return bool(cond)


def call(url: str, token: str, body: dict | None = None, raw: bool = False):
    req = urllib.request.Request(url, headers={"X-Token": token})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.data = data
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            payload = r.read()
            if raw:
                return r.status, r.headers.get("Content-Type", ""), payload
            return r.status, json.loads(payload.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, ({} if raw else {"ok": False, "error": exc.read()[:200].decode("utf-8", "replace")})
    except Exception as exc:
        return 0, ({} if raw else {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grab", action="store_true", help="额外做一次抓图（会派子进程进隐藏桌面）")
    a = ap.parse_args()

    if not os.path.isfile(UI_JSON):
        print(f"[X] 读不到 {UI_JSON} —— 控制台还没启动过")
        return 2
    with open(UI_JSON, encoding="utf-8") as f:
        ui = json.load(f)
    base = f"http://127.0.0.1:{ui['port']}"
    token = ui["token"]
    print(f"控制台：{base}（令牌 {token[:6]}…）")
    print("=" * 72)

    print("\n[1] GET /api/state")
    st, res = call(f"{base}/api/state", token)
    check("接口通", st == 200 and res.get("ok"), str(res)[:200])
    state = res.get("state") or {}
    h = state.get("host") or {}
    check("状态里带 host 段", "host" in state, str(list(state))[:200])
    check("状态里带 grab_last 段", "grab_last" in state, str(list(state))[:200])
    print(f"      宿主 running={h.get('running')} phase={h.get('phase')!r} "
          f"pid={h.get('pid')} 桌面={h.get('desktop')!r} 登录={h.get('logged_in')} "
          f"昵称={h.get('nickname')!r}")
    # ⚠️ 面板要能被两种状态各自说清楚，所以这里的断言必须**分状态**。
    # 第一版只写了「运行中」那一支，宿主停着时就误判成失败 ——
    # 那是探针的毛病，不是产品的毛病（会让你去查一个不存在的问题）。
    host_up = bool(h.get("running"))
    if host_up:
        check("宿主心跳是新鲜的",
              (h.get("age") or 999) < (h.get("stale_after_seconds") or 20),
              f"age={h.get('age')}")
        check("已登录", bool(h.get("logged_in")), str(h.get("nickname")))
    else:
        check("已停止时给出的是「已停止」而不是报错",
              h.get("phase") == "已停止" and not h.get("error"),
              f"phase={h.get('phase')!r} error={h.get('error')!r}")
        check("已停止时能说清上次是怎么退出的",
              h.get("rc") is not None or bool(h.get("reason")),
              f"rc={h.get('rc')} reason={h.get('reason')!r}")
    check("宿主自报桌面 == 目标桌面",
          bool(h.get("desktop")) and h.get("desktop") == h.get("desktop_self"),
          f"{h.get('desktop')} vs {h.get('desktop_self')}")
    check("心跳里记着登录态（供「上次跑到哪一步」复盘）",
          h.get("logged_in") is not None, str(h.get("logged_in")))

    print("\n[2] GET /api/host/status")
    st, res = call(f"{base}/api/host/status", token)
    check("接口通", st == 200 and res.get("ok"), str(res)[:200])
    d = res.get("daemon") or {}
    check("带 daemon 段", bool(d), str(list(res))[:160])
    check("带 last_grab 段", "last_grab" in res, str(list(res))[:160])
    check("桌面存在", bool(res.get("desktop_exists")), str(res.get("desktop_exists")))
    print(f"      日志 {d.get('log_bytes')} 字节　心跳 {d.get('age')}s 前")

    print("\n[3] GET /api/logs（验 logtail 通道）")
    st, res = call(f"{base}/api/logs", token)
    check("接口通", st == 200 and res.get("ok"), str(res)[:200])
    lines = res.get("lines") or []
    host_lines = [r for r in lines if r.get("source") == "hidden"]
    check("日志总线里有来自隐藏桌面的行", bool(host_lines),
          f"共 {len(lines)} 行，其中 hidden 来源 {len(host_lines)} 行")
    for r in host_lines[-6:]:
        print(f"      [{r.get('tag')}] {r.get('text')[:96]}")
    check("宿主那几行被正确解析出 TAG（不是整行当正文）",
          any(r.get("tag") == "HOST" for r in host_lines),
          str([r.get("tag") for r in host_lines[:5]]))

    if a.grab:
        print("\n[4] POST /api/host/grab")
        st, res = call(f"{base}/api/host/grab", token, {"wait": 1.5})
        check("任务已接受", st == 200 and res.get("ok"), str(res)[:200])
        shot_ok = False
        for _ in range(40):
            time.sleep(1.0)
            _, s2 = call(f"{base}/api/state", token)
            job = (s2.get("state") or {}).get("job") or {}
            if job.get("name") == "host_grab" and job.get("status") != "running":
                print(f"      任务结束：status={job.get('status')} "
                      f"elapsed={job.get('elapsed')}s error={job.get('error')!r}")
                shot_ok = job.get("status") == "ok"
                break
        check("抓图任务成功结束", shot_ok)

        print("\n[5] GET /api/host/shot")
        st, ctype, blob = call(f"{base}/api/host/shot", token, raw=True)
        check("接口通", st == 200, f"HTTP {st}")
        # ⚠️ 不能写死 PNG：抓图优先 PNG，**没有 Pillow 时退成 BMP**。
        # 打包版就是 BMP（spec 刻意排除 PIL）。断言比被测对象更严格，
        # 会把「本来是对的」判成失败。
        blob = blob or b""
        is_png = blob[:8] == b"\x89PNG\r\n\x1a\n"
        is_bmp = blob[:2] == b"BM"
        check("是真图片（PNG 或 BMP）", is_png or is_bmp, str(blob[:8]))
        check("Content-Type 与真实格式一致（不是硬写 image/png）",
              ("image/png" in (ctype or "")) == is_png
              and ("image/bmp" in (ctype or "")) == is_bmp,
              f"ctype={ctype!r} png={is_png} bmp={is_bmp}")
        check("体积像张真图（> 8KB）", len(blob) > 8192, f"{len(blob)} 字节")
        print(f"      {'PNG' if is_png else 'BMP' if is_bmp else '?'}　"
              f"{len(blob)} 字节　{ctype}")

        print("\n[6] 状态里回填了抓图结果")
        st, res = call(f"{base}/api/host/status", token)
        g = res.get("last_grab") or {}
        cap = g.get("capture") or {}
        check("有 at 时间戳", bool(g.get("at")), str(g.get("at")))
        check("有抓图统计", bool(cap), str(cap)[:160])
        check("画面不是全黑", (cap.get("distinct_colors") or 0) > 3,
              f"颜色种类={cap.get('distinct_colors')}")
        check("读到了界面", bool((g.get("uia") or {}).get("ok")),
              str((g.get("uia") or {}).get("error")))
        print(f"      {cap.get('w')}x{cap.get('h')} 颜色={cap.get('distinct_colors')} "
              f"亮度={cap.get('mean_luma')} 非黑={cap.get('nonblack_ratio')}")

    print("\n" + "=" * 72)
    print(f"通过 {OK}　失败 {BAD}")
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
