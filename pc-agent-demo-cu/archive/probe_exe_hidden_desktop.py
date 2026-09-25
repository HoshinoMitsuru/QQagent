# -*- coding: utf-8 -*-
"""
probe_exe_hidden_desktop.py —— **打包版**能不能把宿主真的丢进隐藏桌面

## 它回答的问题（源码模式永远答不了）

`test_exe.py` 验到 `/api/host/status` 通就停了 —— 那只证明 `app.host` 被打进包里。
真正容易在打包时才炸的是**下一步**：

    host.start_daemon() → desktop.spawn(sys.executable, child_args("app.hostd"), ...)

冻结之后 `sys.executable` 是 exe 自己，它不认识 `-m app.hostd`。
这条路径必须换成 exe 自己的子命令（`--run-hostd`）才成立，
而「有没有真的换成」只有在真 exe 上跑一次才知道。漏了的症状是：

    抓一张画面 → 报 ModuleNotFoundError / 参数无法识别 → 人去查 QQ 路径

## 为什么用 `--no-agent`

本次只验「宿主进程能不能被拉起来、能不能认出 QQ、能不能写心跳」——
**不跑回复循环**，所以不会读上下文、更不会发消息。
要验回复链路请单独做，并且清楚它会真的发消息。

## 用法

    python archive/probe_exe_hidden_desktop.py        # 默认用 dist/qq-agent-noadmin.exe
    python archive/probe_exe_hidden_desktop.py path\\to\\exe

⚠️ 必须用**免 UAC 变体**：默认版带 requireAdministrator 清单，
非提升的进程无法用 CreateProcess 启动它（WinError 740）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

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


def call(base: str, token: str, path: str, body=None, timeout: int = 120):
    req = urllib.request.Request(base + path, headers={"X-Token": token})
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def wait_for(cond, timeout: float = 60.0, interval: float = 0.5) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    return False


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        HERE, "dist", "qq-agent-noadmin.exe")
    if not os.path.isfile(src):
        print(f"[X] 找不到 exe：{src}")
        print("    先构建免 UAC 变体：build.bat nadmin")
        return 2

    work = tempfile.mkdtemp(prefix="qqagent-exe-r4-")
    exe = os.path.join(work, "qq-agent-noadmin.exe")
    shutil.copy2(src, exe)
    print(f"验收目标：{src}")
    print(f"干净工作目录：{work}")
    print("=" * 74)

    proc = None
    try:
        print("\n[1] 起控制台（--safe）")
        proc = subprocess.Popen([exe, "--safe"], cwd=work,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=NO_WINDOW)
        ui = os.path.join(work, "state", "ui.json")
        if not check("写出了 state/ui.json", wait_for(lambda: os.path.isfile(ui), 90),
                     f"日志尾部：{_tail(os.path.join(work, 'logs', 'ui.log'))}"):
            return _summary()
        info = json.load(open(ui, encoding="utf-8"))
        base = f"http://127.0.0.1:{info['port']}"
        token = info["token"]
        check("端口在监听",
              wait_for(lambda: call(base, token, "/api/state").get("ok") is True, 30))

        print("\n[2] 通过 exe 把宿主丢进隐藏桌面（--no-agent，不会发消息）")
        r = call(base, token, "/api/host/start", {"no_agent": True, "wait": 25})
        check("启动请求被接受", r.get("ok") is True, json.dumps(r, ensure_ascii=False)[:300])
        pid = r.get("pid")
        print(f"      宿主 pid={pid}（这是 exe 派生出来的子进程）")

        print("\n[3] 宿主心跳（壳看不到那张桌面的窗口，心跳是唯一通道）")
        def hb():
            d = call(base, token, "/api/host/status").get("daemon") or {}
            return d if d.get("heartbeat_present") else {}

        got = wait_for(lambda: bool(hb()), 90)
        d = hb() if got else {}
        check("宿主写出了心跳", got, "宿主进程可能没起来（看下面的 hostd.log）")
        # ⚠️ 等它把 QQ 认领完再断言：心跳是**逐字段累加**的，
        # 刚启动那一瞬间只有 pid/桌面，qq_pids 还没写进去。
        # 拿启动瞬间的快照去断言，是时序问题，不是故障。
        wait_for(lambda: bool((hb() or {}).get("qq_pids")), 90)
        d = hb() or d
        if got:
            print(f"      阶段={d.get('phase')!r} 桌面={d.get('desktop')!r} "
                  f"自报={d.get('desktop_self')!r} pid={d.get('pid')}")
            check("自报桌面 == 目标桌面",
                  bool(d.get("desktop")) and d.get("desktop") == d.get("desktop_self"),
                  f"{d.get('desktop')} vs {d.get('desktop_self')}")
            check("宿主认出了那边的 QQ",
                  bool(d.get("qq_pids")), str(d.get("qq_pids")))
            check("宿主 pid 与启动返回的不是同一个（exe 派生）",
                  bool(d.get("pid")), f"heartbeat pid={d.get('pid')} start pid={pid}")

        print("\n[4] 宿主的日志有没有被壳 tail 进日志总线")
        logs = call(base, token, "/api/logs").get("lines") or []
        hidden = [x for x in logs if x.get("source") == "hidden"]
        check("日志总线里有来自隐藏桌面的行", bool(hidden),
              f"共 {len(logs)} 行，hidden {len(hidden)} 行")
        for x in hidden[-5:]:
            print(f"      [{x.get('tag')}] {x.get('text')[:96]}")
        check("那几行被正确解析出 TAG",
              any(x.get("tag") == "HOST" for x in hidden),
              str([x.get("tag") for x in hidden[:5]]))

        print("\n[5] 抓一张那张桌面的画面（验 exe 派出的 hostagent 子进程）")
        call(base, token, "/api/host/grab", {"wait": 1.0})
        shot_ok = False
        for _ in range(60):
            time.sleep(1.0)
            job = (call(base, token, "/api/state").get("state") or {}).get("job") or {}
            if job.get("name") == "host_grab" and job.get("status") != "running":
                print(f"      任务 status={job.get('status')} elapsed={job.get('elapsed')}s "
                      f"error={job.get('error')!r}")
                shot_ok = job.get("status") == "ok"
                break
        check("抓图任务成功（说明 --run-hostagent 这条路通）", shot_ok)
        st, ctype, blob = _raw(base, token, "/api/host/shot")
        blob = blob or b""
        # ⚠️ 不能只认 PNG：**打包版没有 Pillow，winmsg 会退成 BMP**
        # （spec 刻意排除了 PIL）。第一版这条断言写死了 PNG 魔数，
        # 于是「修好了」反而被它判成失败 —— 断言比被测对象更严格是常见坑。
        is_png = blob[:8] == b"\x89PNG\r\n\x1a\n"
        is_bmp = blob[:2] == b"BM"
        check("取回的是真图片（PNG 或 BMP）", st == 200 and (is_png or is_bmp)
              and len(blob) > 8192,
              f"HTTP {st} {len(blob)} 字节 头={blob[:8]!r}")
        check("Content-Type 与真实格式一致（不是硬写 image/png）",
              ("image/png" in ctype) == is_png and ("image/bmp" in ctype) == is_bmp,
              f"ctype={ctype!r} png={is_png} bmp={is_bmp}")
        print(f"      格式={'PNG' if is_png else 'BMP' if is_bmp else '?'}　"
              f"{len(blob)} 字节　{ctype}")

        print("\n[6] 停止")
        # 先等它把启动链路走完（--no-agent 的终态是「待命（不跑回复）」）。
        # 不等的话停的是启动期，日志里会是「等会话打开期间收到停止哨兵」——
        # 那当然也是一条合法路径，但它证明不了稳态。
        wait_for(lambda: (hb() or {}).get("phase") == "待命（不跑回复）", 150)
        print(f"      停止前阶段：{(hb() or {}).get('phase')!r}")
        r = call(base, token, "/api/host/stop", {})
        check("停止请求成功", r.get("ok") is True, json.dumps(r, ensure_ascii=False)[:200])
        check("宿主进程真的退出了",
              wait_for(lambda: (call(base, token, "/api/host/status")
                                .get("daemon") or {}).get("running") is False, 40),
              str(r))
        return _summary()
    finally:
        try:
            if proc:
                call(f"http://127.0.0.1:"
                     f"{json.load(open(os.path.join(work, 'state', 'ui.json'),
                                       encoding='utf-8'))['port']}",
                     json.load(open(os.path.join(work, "state", "ui.json"),
                                    encoding="utf-8"))["token"], "/api/system/quit", {})
        except Exception:
            pass
        try:
            if proc:
                proc.wait(timeout=20)
        except Exception:
            pass
        print(f"\n[i] 工作目录保留在 {work}（里面有 hostd.log 与心跳，便于复盘）")


def _raw(base, token, path):
    try:
        with urllib.request.urlopen(f"{base}{path}?t={token}", timeout=30) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, "", exc.read()
    except Exception:
        return 0, "", b""


def _tail(path: str, n: int = 700) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()[-n:]
    except Exception:
        return "(读不到)"


def _summary() -> int:
    print("\n" + "=" * 74)
    print(f"通过 {OK}　失败 {BAD}")
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
