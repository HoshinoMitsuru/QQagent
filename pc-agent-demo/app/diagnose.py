# -*- coding: utf-8 -*-
"""
diagnose.py —— 一键体检，产出「可复制」的诊断报告

## 为什么需要它

出问题时最花时间的从来不是修，而是**确认到底是哪一环坏了**。
「不工作了」这句话背后可能是：没填密钥 / 端口被占 / QQ 缩在托盘 /
无障碍参数没生效 / 桌面锁了 / 磁盘满了 / 依赖缺了……

所以这里把能自动查的都查一遍，每一项都给：**结论 + 错误码 + 现场数据 + 下一步动作**。
最后拼成一段纯文本，用户复制出来就能带上全部环境信息 ——
不用再来回问「你的数据目录在哪」「Windows 版本多少」「QQ 是最小化的吗」。

## 设计约束

- **每一项独立兜异常**：体检本身绝不能因为某一项炸了而整体失败。
- **慢操作要能跳过**：模型连通性要发网络请求，单独用一个标志控制。
- **绝不产生副作用**：不点 QQ、不发消息、不改配置。
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time

from . import errors as E
from . import paths, platform_win as pw, qqctl, settings
from .logbus import BUS
from .supervisor import SUP

STATUS_ICON = {"ok": "[✓]", "warn": "[!]", "fail": "[X]", "skip": "[-]"}


class Check:
    def __init__(self, cid: str, label: str, status: str = "skip",
                 detail: str = "", code: str = "", fix: str = "", data: dict | None = None):
        self.cid = cid
        self.label = label
        self.status = status
        self.detail = detail
        self.code = code
        self.fix = fix
        self.data = data or {}

    def to_dict(self) -> dict:
        out = {"id": self.cid, "label": self.label, "status": self.status,
               "detail": self.detail, "data": self.data}
        if self.code:
            out["code"] = self.code
            it = E.EC.get(self.code)
            out["severity"] = it["severity"]
            out["error"] = it["title"]
            out["causes"] = list(it["causes"])
            out["fixes"] = list(it["fixes"])
            out["hint"] = self.fix or (it["fixes"][0] if it["fixes"] else "")
        elif self.fix:
            out["hint"] = self.fix
        return out


def _guard(cid: str, label: str) -> Check:
    """每一项的兜底 Check 工厂：异常时给出一致的结构，而不是让体检整体崩掉。"""
    return Check(cid, label, "fail", "这一项自己抛异常了（体检的 bug，不是环境问题）")


def run(with_llm: bool = False, with_uia: bool = True) -> dict:
    checks: list[Check] = []
    started = time.time()

    def add(fn, cid: str, label: str) -> None:
        try:
            c = fn()
        except Exception as exc:
            c = _guard(cid, label)
            c.detail = f"{type(exc).__name__}: {exc}"
            c.code = "E-WEB-003"
        checks.append(c)

    # ---------------------------------------------------------- 运行环境
    def c_runtime() -> Check:
        bits = platform.architecture()[0]
        py = sys.version.split()[0]
        c = Check("runtime", "运行环境", "ok",
                  f"{'打包 exe' if paths.is_frozen() else '源码模式'} · "
                  f"Python {py} · {bits} · {platform.platform()}")
        c.data = {"python": py, "arch": bits, "frozen": paths.is_frozen(),
                  "executable": sys.executable, "cwd": os.getcwd()}
        if bits != "64bit":
            c.status, c.code = "warn", "E-ENV-008"
            c.detail += "（32 位解释器读不到 64 位 QQ 的无障碍树）"
        elif sys.version_info < (3, 10):
            c.status, c.code = "fail", "E-ENV-007"
        return c

    def c_admin() -> Check:
        if pw.is_admin():
            return Check("admin", "管理员权限", "ok", "已获得（自启注册 / VM 加固 / 结束 QQ 进程都可用）")
        c = Check("admin", "管理员权限", "warn", "当前不是管理员", "E-ENV-003")
        c.data = {"system_user": pw.is_system_user()}
        if pw.is_system_user():
            # 计划任务「不管用户是否登录都要运行」跑在 Session 0，没有前台权限
            c.detail += "；而且是以 SYSTEM 身份运行（Session 0），**没有前台权限**，无法抢前台"
        return c

    def c_datadir() -> Check:
        d = paths.describe()
        writable = paths.is_writable(paths.DATA_DIR)
        c = Check("datadir", "数据目录", "ok" if writable else "fail",
                  f"{d['data_dir']}（{d['data_dir_reason']}）",
                  "" if writable else "E-ENV-001")
        c.data = {k: str(v) for k, v in d.items()}
        return c

    def c_deps() -> Check:
        mods = {}
        for name in ("uiautomation", "comtypes", "requests", "pyperclip", "websockets"):
            try:
                __import__(name)
                mods[name] = "OK"
            except Exception as exc:
                mods[name] = f"缺失（{type(exc).__name__}）"
        bad = [k for k, v in mods.items() if v != "OK"]
        # websockets 只用于 CDP 探测与 cdp_probe.py，缺了不影响主流程
        fatal = [k for k in bad if k != "websockets"]
        detail = " / ".join(f"{k}={v}" for k, v in mods.items())
        if fatal:
            c = Check("deps", "运行依赖", "fail", detail, "E-ENV-002")
            c.data = mods
            return c
        if bad:
            c = Check("deps", "运行依赖", "warn", detail)
            c.fix = (f"缺的只是可选依赖（{', '.join(bad)}）：websockets 用于 CDP 探测，"
                     f"不影响 UIA 主链。需要 CDP 时再装即可。")
            c.data = mods
            return c
        return Check("deps", "运行依赖", "ok", detail, data=mods)

    # ---------------------------------------------------------- 配置与密钥
    def c_config() -> Check:
        path = paths.CONFIG_PATH
        if not os.path.isfile(path):
            return Check("config", "配置文件", "warn",
                         f"{path} 不存在（首次运行会自动生成）", "E-CFG-005")
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except json.JSONDecodeError as exc:
            c = Check("config", "配置文件", "fail",
                      f"JSON 语法错：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}", "E-CFG-001")
            c.data = {"path": path}
            return c
        except Exception as exc:
            return Check("config", "配置文件", "fail", f"{type(exc).__name__}: {exc}", "E-CFG-006")

        merged = settings.load_merged()
        # 哪些段在文件里缺了（靠内置默认值补的）
        missing = [s for s in ("llm", "chat", "aggregate", "continuous", "queue",
                               "discovery", "identity", "uia", "persist")
                   if not isinstance(raw.get(s), dict)]
        c = Check("config", "配置文件", "warn" if missing else "ok",
                  f"{path}（{len(raw)} 个顶层键）" + (f"，缺段：{missing}" if missing else ""),
                  "E-CFG-005" if missing else "")
        c.data = {"model": merged.get("llm", {}).get("model"),
                  "api_base": merged.get("llm", {}).get("api_base"),
                  "missing_sections": missing}
        return c

    def c_key() -> Check:
        info = settings.resolve_api_key_display()
        if info["present"]:
            return Check("key", "API Key", "ok", f"已配置（来源 {info['source']}，{info['masked']}）")
        return Check("key", "API Key", "fail", "没有可用的 Key，模型调不通", "E-LLM-001")

    def c_risk_params() -> Check:
        cfg = settings.load_merged()
        q = cfg.get("queue") or {}
        try:
            rpm = float(q.get("max_replies_per_minute") or 0)
            miv = float(q.get("min_interval_seconds") or 0)
        except (TypeError, ValueError):
            return Check("risk", "风控参数", "warn", "风控参数不是数字，已按默认值处理", "E-CFG-002")
        detail = f"{rpm:g} 条/分 · 硬间隔 {miv:g}s"
        if rpm > 0 and miv > 0 and 60.0 / rpm < miv - 1e-9:
            c = Check("risk", "风控参数", "warn", detail + " —— 两者互相压制", "E-CFG-003")
            c.data = {"每分钟": rpm, "最小间隔": miv,
                      "实际速率约": round(60.0 / miv, 2)}
            return c
        return Check("risk", "风控参数", "ok", detail)

    def c_llm() -> Check:
        cfg = settings.load_merged()
        api_base = (cfg.get("llm", {}).get("api_base") or "").rstrip("/")
        model = cfg.get("llm", {}).get("model") or ""
        key_info = settings.resolve_api_key_display()
        if not key_info["present"]:
            return Check("llm", "模型连通性", "skip", "跳过（没有 Key）", "E-LLM-001")
        if not with_llm:
            return Check("llm", "模型连通性", "skip",
                         f"未测试（需要联网）。目标 {api_base} · {model}")

        # 这里刻意不 import agent：那会把 uiautomation 拖进 UI 进程，
        # 而它的进程级单例必须只在 UIA 工作线程上创建（见 qqctl.UiaWorker）。
        import requests
        key = _raw_key()
        try:
            t0 = time.time()
            r = requests.post(
                f"{api_base}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "user", "content": "只回复两个字：收到"}],
                      "max_tokens": 16, "stream": False},
                timeout=float(cfg.get("llm", {}).get("timeout_seconds") or 30),
            )
            elapsed = round(time.time() - t0, 2)
            if r.status_code == 200:
                body = r.json()
                got = ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "")
                return Check("llm", "模型连通性", "ok",
                             f"{elapsed}s 返回 {got!r}（{model} @ {api_base}）")
            code = {401: "E-LLM-004", 403: "E-LLM-004", 404: "E-LLM-005",
                    429: "E-LLM-006"}.get(r.status_code,
                                          "E-LLM-007" if r.status_code >= 500 else "E-LLM-005")
            c = Check("llm", "模型连通性", "fail",
                      f"HTTP {r.status_code}：{r.text[:160]}", code)
            c.data = {"url": f"{api_base}/chat/completions", "model": model,
                      "http": r.status_code, "elapsed": elapsed}
            return c
        except requests.exceptions.Timeout:
            return Check("llm", "模型连通性", "fail", "请求超时", "E-LLM-003")
        except requests.exceptions.SSLError as exc:
            return Check("llm", "模型连通性", "fail", f"TLS 失败：{exc}", "E-LLM-002")
        except requests.exceptions.ConnectionError as exc:
            return Check("llm", "模型连通性", "fail",
                         f"连不上（DNS 或网络不通）：{str(exc)[:160]}", "E-LLM-002")
        except Exception as exc:
            return Check("llm", "模型连通性", "fail",
                         f"{type(exc).__name__}: {exc}", "E-LLM-008")

    # ---------------------------------------------------------- QQ
    def c_qq_exe() -> Check:
        app = settings.load_app_settings()
        found = qqctl.find_qq_exe(app.get("qq_exe_path", ""))
        if found["path"]:
            return Check("qq_exe", "QQ 程序定位", "ok",
                         f"{found['path']}（来源：{found['source']}）",
                         data={"path": found["path"], "source": found["source"]})
        c = Check("qq_exe", "QQ 程序定位", "fail",
                  "没找到 QQ.exe（手动指定 → 注册表 → 常见目录 → 盘根，都试过了）", "E-QQ-001")
        c.data = {"试过": found["candidates"][:12]}
        return c

    def c_qq_proc() -> Check:
        procs = pw.list_processes(qqctl.QQ_PROCESS_NAMES)
        win = qqctl.qq_window_state()
        if not procs:
            return Check("qq_proc", "QQ 进程与窗口", "fail", "QQ 没有在运行", "E-QQ-002")
        detail = (f"进程 {len(procs)} 个 · 顶层窗口 {win['window_count']} 个 · "
                  f"可见 {win['visible_count']} 个")
        data = {"processes": len(procs), "windows": win}
        if not win["main"]:
            c = Check("qq_proc", "QQ 进程与窗口", "fail",
                      detail + " —— 没有可见的主窗口", "E-QQ-003")
            c.data = data
            return c
        if win.get("minimized"):
            c = Check("qq_proc", "QQ 进程与窗口", "warn",
                      detail + f" —— 主窗口**最小化**（标题 {win['main_title']!r}）")
            c.fix = ("读取不受影响，但切会话/写输入框会失败（Chromium 节流）。"
                     "程序会自动还原窗口，但更稳的是保持窗口可见。")
            c.data = data
            return c
        return Check("qq_proc", "QQ 进程与窗口", "ok",
                     detail + f"（标题 {win['main_title']!r}）", data=data)

    def c_qq_uia() -> Check:
        if not with_uia:
            return Check("qq_uia", "无障碍可读性", "skip", "已跳过（按需关闭）")
        procs = pw.list_processes(qqctl.QQ_PROCESS_NAMES)
        if not procs:
            return Check("qq_uia", "无障碍可读性", "skip", "跳过（QQ 没在运行）")
        probe = qqctl.check_accessibility()
        if probe.get("ok"):
            return Check("qq_uia", "无障碍可读性", "ok",
                         f"可读：会话 {probe.get('session_count')} 个 · "
                         f"消息 {probe.get('message_count')} 条 · "
                         f"当前 {probe.get('dialog_title')!r}"
                         f"（{'群聊' if probe.get('is_group') else '私聊'}）",
                         data={k: v for k, v in probe.items() if k != "windows"})
        # 读不到：区分「窗口不在」与「无障碍没生效」
        code = "E-QQ-003" if not probe.get("windows", {}).get("main") else "E-QQ-004"
        c = Check("qq_uia", "无障碍可读性", "fail", probe.get("error") or "读不到界面", code)
        c.data = {k: v for k, v in probe.items() if k != "windows"}
        return c

    def c_cdp() -> Check:
        app = settings.load_app_settings()
        if not app.get("cdp_enabled"):
            return Check("cdp", "CDP 调试端口", "skip", "未启用（不影响功能）")
        st = qqctl.cdp_status(int(app.get("cdp_port") or 9222))
        if st.get("ok"):
            return Check("cdp", "CDP 调试端口", "ok", f"{st['url']} → {st.get('browser')!r}")
        return Check("cdp", "CDP 调试端口", "warn",
                     f"{st['url']} 不可用：{st.get('error')}", "E-QQ-006")

    # ---------------------------------------------------------- 桌面与前台
    def c_desktop() -> Check:
        state = pw.desktop_state()
        fg = pw.foreground_title()
        if state == "locked":
            c = Check("desktop", "桌面状态", "fail", f"已锁屏（前台：{fg!r}）", "E-ENV-006")
            c.fix = "解锁桌面；或用「虚拟机加固」禁用自动锁屏（这才是 VM 路线要解决的核心问题）"
            return c
        return Check("desktop", "桌面状态", "ok", f"已解锁（当前前台：{fg or '(无标题)'}）",
                     data={"state": state, "foreground": fg})

    # ---------------------------------------------------------- 运行时状态
    def c_process() -> Check:
        st = SUP.state()
        r = st["resident"]
        hb = st.get("heartbeat") or {}
        if r.get("running"):
            extra = ""
            if hb.get("stale"):
                extra = f"　⚠️ 心跳已停更 {hb.get('age')}s，进程可能卡住"
            return Check("proc", "常驻进程", "ok" if not hb.get("stale") else "warn",
                         f"运行中 {r.get('elapsed')}s · 当前会话 {hb.get('title') or '—'} · "
                         f"队列 {hb.get('queue_len', 0)}{extra}",
                         data={"resident": r, "heartbeat": hb})
        return Check("proc", "常驻进程", "skip", "未运行（正常，需要时点「开始常驻」）",
                     data={"resident": r})

    def c_state_files() -> Check:
        cfg = settings.load_merged()
        out = {}
        for key, path_key in (("会话上下文", "file"), ("取号缓存", "store")):
            section = "persist" if key == "会话上下文" else "identity"
            p = (cfg.get(section) or {}).get(path_key) or ""
            full = p if os.path.isabs(p) else os.path.join(paths.DATA_DIR, p)
            if os.path.isfile(full):
                try:
                    n = len(json.load(open(full, encoding="utf-8")))
                except Exception:
                    n = -1
                out[key] = f"{full}（{n} 项）"
            else:
                out[key] = f"{full}（未生成）"
        return Check("state", "状态文件", "ok", " ｜ ".join(f"{k}: {v}" for k, v in out.items()),
                     data=out)

    def c_catalog() -> Check:
        s = E.catalog_summary()
        if s["problems"]:
            return Check("catalog", "错误码目录", "warn",
                         f"{s['total']} 条，但有 {len(s['problems'])} 个问题（报错本身可能失真）",
                         data=s)
        return Check("catalog", "错误码目录", "ok", f"{s['total']} 条，自检通过", data=s)

    def c_bootstrap() -> Check:
        """
        进程启动期写下的关键事实（单实例、端口、令牌来源）。

        这一项存在的意义：上面所有检查都跑在「已经起来」的前提下，
        而最常见的"打不开"恰恰是**根本没起来**（端口被占、数据目录不可写、
        第二个实例被单实例锁挡住）。那时用户看到的只有一句浏览器错误。
        """
        info_path = os.path.join(paths.STATE_DIR, "ui.json")
        if not os.path.isfile(info_path):
            c = Check("bootstrap", "控制台启动信息", "warn",
                      "没有 state/ui.json（首次运行属正常，反复出现说明上次异常退出）")
            c.fix = ("不用处理；若反复出现，说明程序上次是被强杀的 —— "
                     "退出请用界面右上角「退出程序」或托盘右键 → 退出")
            return c
        try:
            info = json.load(open(info_path, encoding="utf-8"))
        except Exception as exc:
            return Check("bootstrap", "控制台启动信息", "warn",
                         f"state/ui.json 读不出来：{exc}", "E-PATH-003")
        age = round(time.time() - float(info.get("started_at") or 0), 1)
        return Check("bootstrap", "控制台启动信息", "ok",
                     f"监听 {info.get('host')}:{info.get('port')} · pid {info.get('pid')} · "
                     f"已运行 {age}s", data=info)

    # ---------------------------------------------------------- 依次执行
    add(c_runtime, "runtime", "运行环境")
    add(c_admin, "admin", "管理员权限")
    add(c_datadir, "datadir", "数据目录")
    add(c_deps, "deps", "运行依赖")
    add(c_bootstrap, "bootstrap", "控制台启动信息")
    add(c_config, "config", "配置文件")
    add(c_key, "key", "API Key")
    add(c_risk_params, "risk", "风控参数")
    add(c_llm, "llm", "模型连通性")
    add(c_qq_exe, "qq_exe", "QQ 程序定位")
    add(c_qq_proc, "qq_proc", "QQ 进程与窗口")
    add(c_qq_uia, "qq_uia", "无障碍可读性")
    add(c_cdp, "cdp", "CDP 调试端口")
    add(c_desktop, "desktop", "桌面状态")
    add(c_process, "proc", "常驻进程")
    add(c_state_files, "state", "状态文件")
    add(c_catalog, "catalog", "错误码目录")

    counts = {"ok": 0, "warn": 0, "fail": 0, "skip": 0}
    for c in checks:
        counts[c.status] = counts.get(c.status, 0) + 1

    result = {
        # `ok` 表示「这次体检调用本身成功了」，`healthy` 才是「环境是否健康」。
        # 两者必须分开：否则界面会把「体检跑完且发现了 3 个问题」当成「接口调用失败」，
        # 然后弹一句「读取失败」——恰好把最有价值的信息丢掉。
        "ok": True,
        "healthy": counts["fail"] == 0,
        "elapsed": round(time.time() - started, 2),
        "counts": counts,
        "checks": [c.to_dict() for c in checks],
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    result["report_text"] = render_text(result)
    BUS.emit(f"诊断完成：{counts['ok']} 正常 / {counts['warn']} 警告 / "
             f"{counts['fail']} 失败 / {counts['skip']} 跳过（{result['elapsed']}s）",
             tag="DIAG", source="ui",
             level="warn" if counts["fail"] else "info")
    return result


def _raw_key() -> str:
    """
    取明文 Key（只在本机、只用于发一次请求）。

    刻意不经过 agent：那条路会 import uiautomation，把 COM 单例拖进错误的线程。
    """
    cfg = settings.load_merged()
    key = (cfg.get("llm", {}).get("api_key") or "").strip()
    if key:
        return key
    try:
        sec = json.load(open(paths.SECRETS_PATH, encoding="utf-8"))
        return ((sec.get("llm") or {}).get("api_key") or "").strip()
    except Exception:
        return ""


def render_text(result: dict) -> str:
    """
    拼成一段可以直接复制出去的纯文本。

    格式刻意保持朴素（没有表格边框、没有颜色）：它要能贴进任何聊天窗口 / issue
    而不散版，也要能被人一眼扫过并定位到 [X] 那几行。
    """
    lines = []
    lines.append("=" * 68)
    lines.append("QQAgent 诊断报告")
    lines.append("=" * 68)
    lines.append(f"生成时间：{result['generated_at']}　耗时 {result['elapsed']}s")
    c = result["counts"]
    lines.append(f"结论：{c['ok']} 项正常 / {c['warn']} 项警告 / {c['fail']} 项失败 / "
                 f"{c['skip']} 项跳过")
    lines.append("")

    # 先列问题，再列全部 —— 让读的人第一眼就看到需要动手的地方
    problems = [x for x in result["checks"] if x["status"] in ("fail", "warn")]
    if problems:
        lines.append("-" * 68)
        lines.append("需要处理的项目")
        lines.append("-" * 68)
        for x in problems:
            mark = STATUS_ICON[x["status"]]
            code = f"  {x['code']}" if x.get("code") else ""
            lines.append(f"{mark} {x['label']}{code}")
            if x["detail"]:
                lines.append(f"     现状：{x['detail']}")
            if x.get("hint"):
                lines.append(f"     建议：{x['hint']}")
        lines.append("")

    lines.append("-" * 68)
    lines.append("全部检查项")
    lines.append("-" * 68)
    for x in result["checks"]:
        mark = STATUS_ICON[x["status"]]
        code = f"  [{x['code']}]" if x.get("code") else ""
        lines.append(f"{mark} {x['label']}{code}")
        if x["detail"]:
            lines.append(f"     {x['detail']}")
    lines.append("")
    lines.append("-" * 68)
    lines.append("环境明细")
    lines.append("-" * 68)
    for x in result["checks"]:
        if not x.get("data"):
            continue
        for k, v in x["data"].items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)[:300]
            lines.append(f"  {x['label']}.{k} = {v}")
    lines.append("")
    lines.append("提示：带 [X] / [!] 的行就是要处理的地方；上面的错误码可以单独搜索，")
    lines.append("      每个码都有一份「判据 + 动作」的说明（error_codes.py）。")
    return "\n".join(lines)