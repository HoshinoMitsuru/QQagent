# -*- coding: utf-8 -*-
"""
main.py —— 双模式入口

同一个可执行文件，靠第一个参数决定自己是谁：

    qq-agent.exe                  壳模式：起 WebUI、开托盘、拉起 QQ、按需启动常驻
    qq-agent.exe --run-agent ...  把自己当 agent.py 跑（由壳以子进程方式调用）
    qq-agent.exe --run-qqid  ...  把自己当 qqid.py 跑

**为什么必须共用同一个 exe**：冻结之后只有一个可执行文件。如果壳去调用
「系统里的 python」，那台机器上就必须先装 Python 和一堆依赖，整个「拷过去就能用」
的前提就没了。共用 exe 还有一个附带好处 —— 界面里跑的和命令行里跑的**必然是同一份代码**，
不会出现「界面上能跑、命令行里不行」这种漂移。
"""

from __future__ import annotations

import os
import sys
import threading
import time

BANNER = r"""
  ___  ___    _                    _
 / _ \| _ \  /_\  __ _ ___ _ _ _ _| |_
| (_) |   / / _ \/ _` / -_) ' \ ' \  _|
 \__\_\_|_\/_/ \_\__, \___|_||_|_||_\__|
                 |___/     QQ AI 代理控制台
"""


def _ensure_stdio() -> None:
    """
    windowed 版 exe 启动时 `sys.stdout` / `sys.stderr` 是 None，任何一次 print 都会抛
    AttributeError。这里给它们塞一个黑洞文件，让 `print` 永远安全 ——
    真正的日志走日志总线和日志文件，不依赖标准输出。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass
        else:
            try:
                stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:
                pass


def _ensure_root_on_path() -> None:
    from . import paths
    root = paths.exe_dir()
    if root not in sys.path:
        sys.path.insert(0, root)


# ============================================================ 子模式
def run_script(module_name: str, args: list[str]) -> int:
    """
    把自己当成 `agent.py` / `qqid.py` 来跑。

    `sys.argv` 要重写成脚本的形态 —— 它们用的是 argparse，读的就是 sys.argv[1:]，
    而且 prog 名会出现在 `--help` 里，写成 exe 名会让帮助信息对不上文档。
    """
    _ensure_root_on_path()
    sys.argv = [f"{module_name}.py", *args]
    try:
        import importlib
        mod = importlib.import_module(module_name)
    except Exception as exc:
        print(f"[X] 加载 {module_name} 失败：{type(exc).__name__}: {exc}")
        return 3
    fn = getattr(mod, "main", None)
    if not callable(fn):
        print(f"[X] {module_name} 里没有 main()")
        return 3
    try:
        return int(fn() or 0)
    except KeyboardInterrupt:
        print("\n[i] 已退出")
        return 0


# ============================================================ 壳模式
def _open_browser(url: str) -> None:
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        try:
            os.startfile(url)
        except Exception:
            pass


def _read_running_instance() -> dict:
    """第二次双击时，从上一实例留下的文件里拿到端口和令牌，直接把浏览器指过去。"""
    import json
    from . import paths
    p = os.path.join(paths.STATE_DIR, "ui.json")
    try:
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def run_ui(safe: bool = False) -> int:
    """
    safe=True 时进入「安全启动」：不自动拉起 QQ、不自动开始常驻、不开托盘、不开浏览器，
    退出只认 QUIT 信号。用于自动化测试和「只想先看看界面」的场景 ——
    否则光是把程序启动起来，就会去动你的 QQ。
    """
    from . import paths, platform_win as pw, runtime, server, settings, tray
    from .logbus import BUS
    from .supervisor import SUP

    paths.ensure_dirs()
    fresh_config = settings.ensure_config_file()
    settings.ensure_secrets_file()

    BUS.emit(BANNER.strip("\n"), tag="BOOT", source="ui", level="info")
    BUS.emit(f"版本 {server._version()}　Python {sys.version.split()[0]}　"
             f"{'打包模式' if paths.is_frozen() else '源码模式'}"
             + ("　安全启动（不会自动拉起 QQ）" if safe else ""), tag="BOOT", source="ui")
    BUS.emit(f"数据目录：{paths.DATA_DIR}（{paths.DATA_DIR_REASON}）", tag="BOOT", source="ui")

    # ---- 单实例：第二个实例不要去抢端口，直接把浏览器指向已经在跑的那个
    if not pw.acquire_single_instance():
        prev = _read_running_instance()
        port = prev.get("port")
        token = prev.get("token")
        if port and token:
            url = f"http://127.0.0.1:{port}/?t={token}"
            BUS.emit(f"检测到已有一个控制台在运行，直接把浏览器指向它：{url}",
                     tag="BOOT", source="ui")
            _open_browser(url)
            time.sleep(1.0)
            return 0
        BUS.emit("检测到已有一个控制台在运行，但它没有留下端口记录。"
                 "请检查托盘图标，或先在任务管理器里结束重复的进程。", tag="WARN", source="ui")
        return 1

    if not pw.is_admin():
        BUS.emit("当前不是管理员权限：自启注册、VM 加固、结束 QQ 进程会失败。"
                 "界面右上角可以一键提权重启。", tag="WARN", source="ui")
    if pw.desktop_state() == "locked":
        BUS.emit("桌面处于锁屏状态 —— 读取可用，但切会话与发送会失败。"
                 "（虚拟机路线的意义就是把这条限制去掉）", tag="WARN", source="ui")

    app = settings.load_app_settings()
    host = str(app.get("web_host") or "127.0.0.1")
    want_port = int(app.get("web_port") or 8765)
    port = pw.pick_port(want_port, host)

    httpd = None
    try:
        httpd = server.serve(host, port)
    except OSError as exc:
        BUS.emit(f"监听 {host}:{port} 失败：{exc}。请在「运行环境」里换个端口。",
                 tag="ERR", source="ui")
        return 2

    url = f"http://127.0.0.1:{port}/?t={server.TOKEN}"
    lan = pw.lan_ip()
    BUS.emit(f"控制台地址：{url}", tag="BOOT", source="ui")
    if host == "0.0.0.0" and lan:
        BUS.emit(f"已监听所有网卡，可从其它设备访问：http://{lan}:{port}/?t={server.TOKEN}",
                 tag="BOOT", source="ui")
    BUS.emit(f"访问令牌（本次运行有效）：{server.TOKEN}", tag="BOOT", source="ui")

    try:
        import json
        with open(os.path.join(paths.STATE_DIR, "ui.json"), "w", encoding="utf-8") as f:
            json.dump({"port": port, "host": host, "token": server.TOKEN,
                       "pid": os.getpid(), "started_at": time.time()},
                      f, ensure_ascii=False)
    except Exception:
        pass

    if fresh_config:
        BUS.emit("这是首次运行，已生成 config.json。请先到「模型接入」填好 API Key。",
                 tag="BOOT", source="ui")

    if not safe:
        _open_browser(url)

    # ---- QQ：只在「完全没在跑」时自动拉起。已经登录的绝不去动它
    if not safe and app.get("auto_launch_qq") and not pw.list_processes(("QQ.exe", "QQEX.exe")):
        found = None
        try:
            from . import qqctl
            found = qqctl.find_qq_exe(app.get("qq_exe_path", ""))
        except Exception:
            found = None
        if found and found.get("path"):
            from . import qqctl
            res = qqctl.launch_qq(found["path"], bool(app.get("cdp_enabled")),
                                  int(app.get("cdp_port") or 9222),
                                  str(app.get("qq_extra_args") or ""))
            BUS.emit(f"QQ 未在运行，已自动拉起：{found['path']}（{found['source']}）"
                     if res.get("ok") else f"自动拉起 QQ 失败：{res.get('error')}",
                     tag="QQ" if res.get("ok") else "ERR", source="ui")
        else:
            BUS.emit("QQ 未在运行，而且没找到 QQ.exe。请在「运行环境」里手动填写路径。",
                     tag="WARN", source="ui")

    if not safe and app.get("auto_start_agent"):
        res = SUP.start_resident(dry_run=False)
        BUS.emit("已按设置自动开始常驻" if res.get("ok") else f"自动开始常驻失败：{res.get('error')}",
                 tag="RUN" if res.get("ok") else "WARN", source="ui")

    # ---- 收尾：三个退出入口（托盘菜单 / 界面按钮 / Ctrl+C）全部汇到这里
    #
    # ⚠️ 这里有个必须处理的竞态：如果让一个后台线程去收尾、主线程同时往下走，
    # 主线程会**先于收尾完成**就返回、进程直接退出 —— 结果就是常驻子进程被丢下没人管、
    # 最后几行日志也没落盘。所以：谁在等退出信号，谁就负责收尾；
    # 只有托盘模式（主线程被消息循环占住）才交给看门线程，并由主线程等它做完。
    lock = threading.Lock()
    stopped = threading.Event()
    shutdown_done = threading.Event()

    def do_shutdown(reason: str = "退出请求") -> None:
        """幂等：第一个进来的执行收尾，后来的直接返回。"""
        with lock:
            if stopped.is_set():
                return
            stopped.set()
        BUS.emit(f"{reason}，正在收尾…", tag="BOOT", source="ui")
        try:
            if SUP.state()["resident"]["running"]:
                SUP.stop_resident()
        except Exception as exc:
            BUS.emit(f"停止常驻时出错：{exc}", tag="WARN", source="ui")
        try:
            if httpd:
                httpd.shutdown()
        except Exception:
            pass
        BUS.emit("已退出。", tag="BOOT", source="ui")
        shutdown_done.set()

    def on_open():
        _open_browser(url)

    def on_data():
        pw.open_in_explorer(paths.DATA_DIR)

    def on_quit():
        runtime.request_quit()

    t = tray.Tray(f"QQAgent 控制台 · 端口 {port}", on_open=on_open,
                  on_quit=on_quit, on_data=on_data)

    if safe:
        # 安全启动不建托盘：自动化测试里托盘只会占住主线程，
        # 而且那种进程也没有可点的图标。主线程自己等、自己收尾。
        BUS.emit("安全启动：不创建托盘图标，退出只认 QUIT 信号。", tag="BOOT", source="ui")
        runtime.wait_quit()
        do_shutdown("程序退出")
        return 0

    if t.setup():
        BUS.emit("托盘图标已就绪：双击打开界面，右键可退出。", tag="BOOT", source="ui")
        t.notify("QQAgent 已启动", f"控制台运行在端口 {port}，双击托盘图标打开。")

        def watch_quit() -> None:
            """主线程在托盘消息循环里出不来，所以由它来收尾并唤醒消息循环。"""
            runtime.wait_quit()
            do_shutdown("程序退出")
            t.quit()

        threading.Thread(target=watch_quit, name="quit-watch", daemon=True).start()
        t.run()                  # 阻塞直到用户点「退出」或 runtime.QUIT 被置位
        # 消息循环结束了：可能是用户点的退出（那收尾已经做完），也可能是别的原因。
        do_shutdown("程序退出")
        shutdown_done.wait(20)   # 等看门线程把收尾做完，别抢在它前面退出进程
        return 0

    BUS.emit(f"托盘图标创建失败（{t.error}）。程序继续在后台运行 —— "
             f"关闭它的方式是界面右上角的「退出程序」。"
             f"（想看到实时输出，请用 build.bat console 构建带控制台的版本）",
             tag="WARN", source="ui")
    runtime.wait_quit()          # 没有托盘就在主线程上等退出信号
    do_shutdown("程序退出")
    return 0


# ============================================================ 入口
def main() -> int:
    _ensure_stdio()
    argv = sys.argv[1:]

    if argv:
        head = argv[0]
        if head == "--run-agent":
            return run_script("agent", argv[1:])
        if head == "--run-qqid":
            return run_script("qqid", argv[1:])
        if head == "--safe":
            return run_ui(safe=True)
        if head in ("--version", "-V"):
            from . import __version__
            print(f"QQAgent {__version__}")
            return 0
        if head in ("--help", "-h"):
            print("QQAgent —— QQ AI 代理控制台\n")
            print("  qq-agent.exe                启动控制台（默认）")
            print("  qq-agent.exe --safe         安全启动：不自动拉起 QQ / 不开托盘 / 不开浏览器")
            print("  qq-agent.exe --run-agent X  以 agent.py 的身份运行 X（内部使用）")
            print("  qq-agent.exe --run-qqid X   以 qqid.py 的身份运行 X（内部使用）")
            print("  qq-agent.exe --version      显示版本")
            print("\n所有 agent.py 的命令行参数都可以直接透传，例如：")
            print("  qq-agent.exe --run-agent --selftest")
            return 0
        print(f"[X] 无法识别的参数：{head}（用 --help 看用法）")
        return 2

    return run_ui()


if __name__ == "__main__":
    raise SystemExit(main())
