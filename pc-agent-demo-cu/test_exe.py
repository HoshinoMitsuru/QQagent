# -*- coding: utf-8 -*-
"""
test_exe.py —— 打包产物的验收测试

测的是**真的 exe**，不是源码：把它拷到一个干净的临时目录里，像用户那样双击运行，
然后用 HTTP 打它的接口。这一步不能省 —— 冻结之后最容易出的两类问题
（`__file__` 指向临时解压目录导致状态写到重启就丢的地方、隐式导入漏掉导致
运行到某个功能才 ImportError）都**只有跑真 exe 才能发现**，源码模式一律看不出来。

运行：
    python test_exe.py                 # 用 dist\\qq-agent.exe
    python test_exe.py path\\to\\exe    # 指定别的产物
    python test_exe.py -v              # 打印接口返回

覆盖：
    1  exe 存在、体积合理
    1b R4 的两个宿主子模式可用（--run-hostagent / --run-hostd）——
       它们是「进隐藏桌面」的唯一入口，且**只能**由 exe 自己扮演
    2  --version / --help 正常
    3  --run-agent 子模式可用，且**数据目录落在 exe 旁边**（冻结适配的关键）
    4  --safe 起服务、写出 state/ui.json（端口 + 令牌）
    5  /api/state、/api/settings、/api/tasks、/api/host/* 都通
    6  真的能起子进程任务并跑完（验证 exe 自调用自己这条链路）
    7  改配置能落盘到 exe 旁边的数据目录
    8  SSE 能收到日志
    9  首屏 HTML 里注入了令牌
    10 /api/system/quit 能让 exe 真的退出
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

HERE = os.path.dirname(os.path.abspath(__file__))
VERBOSE = "-v" in sys.argv
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Result:
    def __init__(self):
        self.ok = 0
        self.fail = 0
        self.errors = []

    def check(self, name, cond, detail=""):
        if cond:
            self.ok += 1
            print(f"  [PASS] {name}", flush=True)
        else:
            self.fail += 1
            self.errors.append(f"{name} {detail}")
            print(f"  [FAIL] {name} {detail}", flush=True)
        return bool(cond)

    def summary(self):
        print("")
        print("=" * 70)
        print(f"通过 {self.ok}　失败 {self.fail}")
        for e in self.errors:
            print(f"  - {e}")
        print("=" * 70)
        return 0 if not self.fail else 1


R = Result()


def http(base, path, body=None, token=None, raw=False, timeout=25):
    url = base + path
    if token:
        url += ("&" if "?" in url else "?") + "t=" + token
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read()
            return r.status, (b if raw else json.loads(b.decode("utf-8")))
    except urllib.error.HTTPError as e:
        b = e.read()
        if raw:
            return e.code, b
        try:
            return e.code, json.loads(b.decode("utf-8"))
        except Exception:
            return e.code, {"ok": False, "error": b.decode("utf-8", "replace")}


class NeedElevation(RuntimeError):
    """exe 带 requireAdministrator 清单，而当前进程没有提升权限。"""


def _run(args, **kw):
    """
    启动 exe，并把「权限不够」翻译成一条能照做的说明。

    ## 为什么必须单独处理 WinError 740

    默认构建出来的 `qq-agent.exe` 带 `requireAdministrator` 清单。
    `CreateProcess`（`subprocess` 用的就是它）**不会弹 UAC** ——
    权限不够时它直接返回 740「请求的操作需要提升」。结果就是本测试以一段
    `OSError: [WinError 740]` 回溯结束，看起来像程序坏了，
    而真相只是「从非提升的终端里跑，启动不了那个变体」。

    正确做法不是让用户去猜，而是明说：要么用管理员终端跑，
    要么测 `dist\\qq-agent-noadmin.exe`（`build.bat nadmin` 产出）——
    两者代码完全一样，差的只有那份清单。
    """
    kw.setdefault("creationflags", NO_WINDOW)
    try:
        return subprocess.run(args, **kw)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 740:
            raise NeedElevation(
                "这个 exe 申请了管理员权限，而非提升的进程**无法**用 CreateProcess 启动它"
                "（UAC 不会被触发）。两种做法：\n"
                "  ① 用「以管理员身份运行」打开的终端再跑本测试；\n"
                "  ② 构建免 UAC 变体并测它（代码完全一样，只差清单）：\n"
                "       build.bat nadmin\n"
                "       python test_exe.py dist\\qq-agent-noadmin.exe"
            ) from exc
        raise


def wait_for(cond, timeout=60.0, interval=0.4):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    return False


def main():
    print("=" * 70)
    exe_src = os.path.join(HERE, "dist", "qq-agent.exe")
    for a in sys.argv[1:]:
        if not a.startswith("-"):
            exe_src = a
            break
    print(f"验收目标：{exe_src}")
    print("=" * 70)

    if not R.check("exe 存在", os.path.isfile(exe_src), exe_src):
        return R.summary()
    size_mb = os.path.getsize(exe_src) / 1024 / 1024
    R.check("体积合理（5~80 MB）", 5 <= size_mb <= 80, f"{size_mb:.1f} MB")
    print(f"      体积 {size_mb:.1f} MB")

    work = tempfile.mkdtemp(prefix="qqagent-exe-")
    exe = os.path.join(work, "qq-agent.exe")
    shutil.copy2(exe_src, exe)
    print(f"[i] 干净工作目录：{work}")

    proc = None
    try:
        # ---------------- 1. 命令行模式 ----------------
        print("\n[1] 命令行模式")
        r = _run([exe, "--version"], capture_output=True, timeout=90, cwd=work)
        out = r.stdout.decode("utf-8", "replace").strip()
        R.check("--version 正常", r.returncode == 0 and "QQAgent" in out, out[:100])
        print(f"      {out}")

        r = _run([exe, "--help"], capture_output=True, timeout=90, cwd=work)
        out = r.stdout.decode("utf-8", "replace")
        R.check("--help 正常", r.returncode == 0 and "--run-agent" in out)
        R.check("--help 里列出了 R4 的两个宿主子模式",
                "--run-hostagent" in out and "--run-hostd" in out,
                out[-400:])

        # ---------------- 1b. R4 的两个宿主角色 ----------------
        # 这两个**只由 exe 自己扮演**（app/host.py 用 lpDesktop 把它们丢进隐藏桌面）。
        # 源码模式永远正常，所以「有没有真的打进 exe、子命令有没有接上」
        # 只有在真 exe 上才验得出来 —— 而这正是 R4 的全部功能入口。
        print("\n[1b] R4 宿主子模式（--run-hostagent / --run-hostd）")
        for flag, must in (("--run-hostagent", "--open-chat"),
                           ("--run-hostd", "--desktop")):
            r = _run([exe, flag, "--help"], capture_output=True,
                     timeout=120, cwd=work)
            out = (r.stdout + r.stderr).decode("utf-8", "replace")
            R.check(f"{flag} 可执行（不是 ModuleNotFoundError）",
                    r.returncode == 0 and must in out,
                    out[-400:])

        # ---------------- 2. agent 子模式 + 数据目录 ----------------
        print("\n[2] agent 子模式与数据目录（冻结适配的关键）")
        r = _run([exe, "--run-agent", "--state"], capture_output=True,
                 timeout=120, cwd=work)
        out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
        R.check("--run-agent 可执行", r.returncode == 0, out[-300:])
        R.check("agent 输出正常（不是 ImportError）",
                "会话上下文" in out and "ModuleNotFoundError" not in out, out[-300:])
        R.check("数据目录落在 exe 旁边（不是临时解压目录）",
                work.lower() in out.lower().replace("/", "\\"), out[-400:])
        if VERBOSE:
            print(out[:800])

        # ---------------- 3. 起控制台服务 ----------------
        print("\n[3] 控制台服务（--safe）")
        proc = subprocess.Popen([exe, "--safe"], cwd=work,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=NO_WINDOW)
        ui_json = os.path.join(work, "state", "ui.json")
        got = wait_for(lambda: os.path.isfile(ui_json), timeout=90)
        if not R.check("写出了 state/ui.json", got,
                       "日志：" + _tail(os.path.join(work, "logs", "ui.log"))):
            return R.summary()

        info = json.load(open(ui_json, encoding="utf-8"))
        port, token = info["port"], info["token"]
        base = f"http://127.0.0.1:{port}"
        R.check("端口是监听状态", wait_for(lambda: http(base, "/api/state", token=token)[0] == 200,
                                        timeout=30), base)
        print(f"      服务地址 {base}")

        # ---------------- 4. 接口 ----------------
        print("\n[4] 接口")
        st, d = http(base, "/api/state", token=token)
        R.check("/api/state 通", st == 200 and d.get("ok"), str(d)[:200])
        s = d["state"]
        R.check("报告为打包模式", s["paths"]["frozen"] is True, str(s["paths"]))
        R.check("数据目录 == exe 所在目录",
                os.path.normcase(s["paths"]["data_dir"]) == os.path.normcase(work),
                s["paths"]["data_dir"])
        R.check("warnings 是列表", isinstance(s.get("warnings"), list))

        st, d = http(base, "/api/settings", token=token)
        R.check("/api/settings 通", st == 200 and d.get("ok"))
        R.check("字段齐备", len(d["settings"]["fields"]) > 40, str(len(d["settings"]["fields"])))

        st, d = http(base, "/api/tasks", token=token)
        R.check("/api/tasks 通", st == 200 and len(d.get("tasks", [])) >= 10)

        # R4 的隐藏桌面接口也要通：这几条同时验两件事 ——
        # app.host 被打进 exe 了（否则 500/断连），以及没跑宿主时它给的是
        # 「明确的未运行状态」而不是报错（面板的两态都要能说清楚）。
        st, d = http(base, "/api/host/status", token=token)
        R.check("/api/host/status 通", st == 200 and d.get("ok"), str(d)[:200])
        R.check("没跑宿主机时状态是「未运行」而不是报错",
                (d.get("daemon") or {}).get("running") is False,
                str(d.get("daemon"))[:200])
        R.check("host 状态里有 E-DESK 相关的路径字段",
                "log_path" in (d.get("daemon") or {})
                and "heartbeat_path" in (d.get("daemon") or {}),
                str(list(d.get("daemon") or {}))[:200])
        st, d = http(base, "/api/host/shot.png", token=token)
        R.check("没抓过图时 shot.png 给的是带码的错误（不是断连）",
                st in (404, 500) and isinstance(d, dict) and d.get("code"),
                f"HTTP {st} {str(d)[:160]}")

        # 错误码目录必须被打进 exe —— 漏了的话「带码报错」会静默退化成裸异常，
        # 而且只有在真 exe 上才会暴露（源码模式永远正常）。
        st, d = http(base, "/api/errors", token=token)
        R.check("/api/errors 通（错误码目录已打包）",
                st == 200 and len(d.get("catalog", {})) >= 50, str(d)[:160])
        R.check("错误码目录自检通过（exe 内）",
                not (d.get("summary", {}).get("problems") or []),
                str(d.get("summary", {}).get("problems"))[:200])

        # 诊断报告：会跑网络请求（模型连通性）和 UIA，是打包后最容易出问题的一条链路
        st, d = http(base, "/api/diagnose", token=token, timeout=180)
        R.check("/api/diagnose 通", st == 200 and d.get("ok") is True, str(d)[:200])
        R.check("ok 与 healthy 分离", "healthy" in d, str(sorted(d.keys())))
        R.check("检查项齐备", len(d.get("checks", [])) >= 15, str(len(d.get("checks", []))))
        R.check("可复制报告非空", len(d.get("report_text") or "") > 400,
                str(len(d.get("report_text") or "")))
        R.check("失败项都带 hint",
                all(c.get("hint") for c in d.get("checks", [])
                    if c["status"] in ("fail", "warn")),
                str([c["id"] for c in d.get("checks", [])
                     if c["status"] in ("fail", "warn") and not c.get("hint")]))
        print(f"      体检结论：{d.get('counts')}　健康={d.get('healthy')}")

        st, b = http(base, "/", token=token, raw=True)
        body = b.decode("utf-8", "replace")
        R.check("首屏 HTML 可打开且注入了令牌",
                st == 200 and token in body and "__TOKEN__" not in body, f"HTTP {st}")

        st, b = http(base, "/favicon.ico", token=token, raw=True)
        R.check("图标已打包进 exe", st == 200 and b"<svg" in b[:200], f"HTTP {st}")

        st, b = http(base, "/", raw=True)
        R.check("无令牌访问被拒", st == 401, f"HTTP {st}")

        # ---------------- 5. 真的起子进程（exe 调用自己）----------------
        print("\n[5] exe 自调用子进程")
        st, d = http(base, "/api/task", {"id": "state", "params": {}}, token=token)
        R.check("任务被接受", d.get("ok"), str(d)[:250])
        R.check("拿到了子进程 pid", isinstance(d.get("pid"), int) and d["pid"] > 0, str(d.get("pid")))
        done = wait_for(lambda: any(r.get("rc") is not None for r in
                                    http(base, "/api/state", token=token)[1]["state"]
                                    ["supervisor"]["task_history"]), timeout=120)
        R.check("子进程跑完了", done, "超时未完成")
        if done:
            hist = http(base, "/api/state", token=token)[1]["state"]["supervisor"]["task_history"]
            rc = [r for r in hist if r["id"] == "state"][0]["rc"]
            R.check("子进程返回码为 0", rc == 0, f"rc={rc}")

        st, d = http(base, "/api/logs", token=token)
        joined = "\n".join(x["text"] for x in d.get("lines", []))
        R.check("子进程输出进了日志", "启动子进程" in joined, joined[-300:])

        # ---------------- 6. 配置落盘 ----------------
        print("\n[6] 配置落盘")
        st, d = http(base, "/api/settings", token=token)
        vals = dict(d["settings"]["values"])
        vals["llm.model"] = "exe-acceptance-test-model"
        vals["llm.api_key"] = "sk-exe-acceptance-123456"
        st, d = http(base, "/api/settings", {"values": vals}, token=token)
        R.check("保存成功", d.get("ok"), str(d)[:250])
        cfg_path = os.path.join(work, "config.json")
        R.check("config.json 写在 exe 旁边", os.path.isfile(cfg_path), cfg_path)
        cfg = json.load(open(cfg_path, encoding="utf-8"))
        R.check("值真的落盘了", cfg["llm"]["model"] == "exe-acceptance-test-model")
        sec = json.load(open(os.path.join(work, "secrets.local.json"), encoding="utf-8"))
        R.check("Key 进了 secrets.local.json",
                sec.get("llm", {}).get("api_key") == "sk-exe-acceptance-123456")
        R.check("Key 没进 config.json", "sk-exe-acceptance" not in json.dumps(cfg))

        # ---------------- 7. SSE ----------------
        print("\n[7] SSE")
        got_any = {"n": 0}

        def read_sse():
            try:
                req = urllib.request.Request(f"{base}/api/events?t={token}")
                with urllib.request.urlopen(req, timeout=8) as r:
                    start = time.time()
                    while time.time() - start < 4:
                        chunk = r.read(1)
                        if not chunk:
                            break
                        if chunk == b"\n":
                            got_any["n"] += 1
            except Exception:
                pass

        import threading
        th = threading.Thread(target=read_sse, daemon=True)
        th.start()
        th.join(timeout=10)
        R.check("SSE 有数据流出", got_any["n"] > 0, str(got_any["n"]))

        # ---------------- 8. 退出 ----------------
        print("\n[8] 退出")
        st, d = http(base, "/api/system/quit", {}, token=token)
        R.check("退出请求被接受", d.get("ok"), str(d)[:200])
        exited = wait_for(lambda: proc.poll() is not None, timeout=40)
        R.check("进程真的退出了", exited, f"poll={proc.poll()}")
        R.check("退出码为 0", proc.poll() == 0, f"rc={proc.poll()}")
        R.check("日志里记录了收尾",
                "已退出" in _tail(os.path.join(work, "logs", "ui.log")), "")

        # ---------------- 9. 清理 ----------------
        print("\n[9] 数据目录结构")
        for name in ("config.json", "secrets.local.json", "state", "logs"):
            R.check(f"生成了 {name}", os.path.exists(os.path.join(work, name)))
        print(f"      目录内容：{sorted(os.listdir(work))}")

    finally:
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        rc = R.summary()
        shutil.rmtree(work, ignore_errors=True)
    return rc


def _tail(path, n=800):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()[-n:]
    except Exception:
        return "(无日志)"


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except NeedElevation as exc:
        print()
        print("=" * 70)
        print("[i] 这个 exe 启动不了，原因不是它坏了：")
        print(exc)
        print("=" * 70)
        raise SystemExit(3)
