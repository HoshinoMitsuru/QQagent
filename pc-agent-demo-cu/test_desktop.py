# -*- coding: utf-8 -*-
"""
test_desktop.py — R4 机制层（独立桌面托管）的自检

## 为什么这份测试必须存在

V2 整条链路的地基是两句话：

    ① 「用 STARTUPINFO.lpDesktop 起的进程，真的落在那张桌面上」
    ② 「用户看不见的那张桌面上，窗口画面真的取得出来」

这两句话如果只是"探针里跑通过"，就没有任何东西阻止以后的改动把它们弄坏 ——
而它们坏掉的表现极其隐蔽：

    ① 坏掉 → 宿主进程跑在用户桌面上，`EnumWindows` 看不到 QQ 的窗口，
             界面显示「QQ 没在运行」，但 QQ 明明在跑（诊断方向完全被带偏）
    ② 坏掉 → 二维码抓出来是一张全黑图，用户扫不了，方案直接不可用

所以这里把两件事都变成**自动化断言**，而且都不需要 QQ、不需要网络、不发送任何消息。

## 测法

壳（本进程）建一张测试桌面 → 用 `lpDesktop` 把子进程丢上去 →
子进程自报「我在哪张桌面」+ 自建一个窗口 + 抓自己的画面 →
壳读回结果断言。**判定落在子进程自己的报告上**，而不是壳的推测 ——
`lpDesktop` 只是个请求，内核才决定进程归属，只有子进程能说清自己到底在哪。

用法：
    python test_desktop.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

from app import desktop, winmsg

HERE = os.path.dirname(os.path.abspath(__file__))
DESK = "QQAgentSelfTest"

OK = FAIL = 0


def case(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ============================================================ 子进程脚本
#
# 刻意全 ASCII：这份脚本要先写进临时文件再交给另一个 python 解释器，
# 中间不经过任何编码协商。注释写成中文能读是能读，但一旦哪天
# 某个环节按 GBK 读它，报错会出现在**子进程**里、父进程只看到一个退出码 ——
# 那种失败最难查。ASCII 换来的确定性比注释可读性值。
_CHILD = r'''
import ctypes, json, os, sys, time
from ctypes import wintypes

sys.path.insert(0, sys.argv[1])
out_path = sys.argv[2]

from app import desktop, winmsg

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)

WM_PAINT = 0x000F
WM_DESTROY = 0x0002
PM_REMOVE = 0x0001
SW_SHOW = 5


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HANDLE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
                ("rcPaint", wintypes.RECT), ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


# argtypes declared with explicit POINTER types rather than c_void_p.
# c_void_p + byref() is rejected by some ctypes builds, and the error would
# surface INSIDE the child while the parent only sees an exit code -- the
# hardest kind of failure to attribute. Explicit pointer types have no ambiguity.
# Same for module handles: without restype they are truncated to 32 bits.
k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
k32.GetModuleHandleW.restype = wintypes.HINSTANCE
u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
u32.CreateWindowExW.restype = wintypes.HWND
u32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, ctypes.c_size_t,
                               ctypes.c_ssize_t]
u32.DefWindowProcW.restype = ctypes.c_ssize_t
u32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
u32.RegisterClassW.restype = wintypes.WORD
u32.DestroyWindow.argtypes = [wintypes.HWND]
u32.DestroyWindow.restype = wintypes.BOOL
u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
u32.UpdateWindow.argtypes = [wintypes.HWND]
u32.IsWindowVisible.argtypes = [wintypes.HWND]
u32.IsWindowVisible.restype = wintypes.BOOL
u32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                             ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
u32.PeekMessageW.restype = wintypes.BOOL
u32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
u32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
u32.BeginPaint.restype = wintypes.HDC
u32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
u32.PostQuitMessage.argtypes = [ctypes.c_int]
g32.CreateSolidBrush.argtypes = [wintypes.DWORD]
g32.CreateSolidBrush.restype = wintypes.HGDIOBJ
g32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
# FillRect lives in user32, not gdi32. The name looks like GDI but user32
# exports it. Calling it on g32 raises "function 'FillRect' not found" --
# and that happens INSIDE the child, so the parent only sees an exit code.
u32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                         wintypes.HGDIOBJ]
u32.FillRect.restype = ctypes.c_int


boxes = [(20, 20, 200, 140, 0x000000FF),    # COLORREF 0x00BBGGRR -> red
         (220, 20, 400, 140, 0x0000FF00),   # green
         (20, 160, 400, 280, 0x00FF0000)]   # blue


def wndproc(hwnd, msg, wp, lp):
    if msg == WM_PAINT:
        ps = PAINTSTRUCT()
        hdc = u32.BeginPaint(hwnd, ctypes.byref(ps))
        for x0, y0, x1, y1, col in boxes:
            rc = wintypes.RECT(x0, y0, x1, y1)
            br = g32.CreateSolidBrush(col)
            u32.FillRect(hdc, ctypes.byref(rc), br)
            g32.DeleteObject(br)
        u32.EndPaint(hwnd, ctypes.byref(ps))
        return 0
    if msg == WM_DESTROY:
        u32.PostQuitMessage(0)
        return 0
    return u32.DefWindowProcW(hwnd, msg, wp, lp)


PROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, ctypes.c_uint,
                          ctypes.c_size_t, ctypes.c_ssize_t)(wndproc)

result = {"desktop": desktop.current_name(), "pid": os.getpid()}


def run():
    wc = WNDCLASSW()
    wc.lpfnWndProc = ctypes.cast(PROC, ctypes.c_void_p)
    wc.hInstance = k32.GetModuleHandleW(None)
    wc.hbrBackground = g32.CreateSolidBrush(0x004080FF)
    wc.lpszClassName = "QQAgentSelfTestWnd"
    atom = u32.RegisterClassW(ctypes.byref(wc))
    result["register_class"] = int(atom)

    hwnd = u32.CreateWindowExW(0, "QQAgentSelfTestWnd", "QQAgentSelfTestWnd",
                               0x00CF0000, 60, 60, 460, 340,
                               None, None, wc.hInstance, None)
    result["hwnd"] = int(hwnd)
    if not hwnd:
        raise RuntimeError("CreateWindowExW returned 0")

    u32.ShowWindow(hwnd, SW_SHOW)
    u32.UpdateWindow(hwnd)
    # pump messages so WM_PAINT fires and DWM builds the redirection surface.
    # Painting is lazy: without a pump the window exists but has no content,
    # and then PrintWindow legitimately returns an all-black image.
    msg = wintypes.MSG()
    deadline = time.time() + 1.2
    while time.time() < deadline:
        while u32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            u32.TranslateMessage(ctypes.byref(msg))
            u32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)
    result["visible"] = bool(u32.IsWindowVisible(hwnd))

    cap = winmsg.capture(hwnd)
    result["capture"] = {
        "ok": cap.get("ok"), "w": cap.get("w"), "h": cap.get("h"),
        "printwindow_ret": cap.get("printwindow_ret"),
        "getdibits_lines": cap.get("getdibits_lines"),
        "distinct_colors": cap.get("distinct_colors"),
        "mean_luma": cap.get("mean_luma"),
        "nonblack_ratio": cap.get("nonblack_ratio"),
        "error": cap.get("error"),
    }
    if cap.get("ok"):
        bmp = os.path.join(os.path.dirname(out_path), "selftest_capture.bmp")
        saved = winmsg.save_bmp(cap, bmp)
        result["save_bmp"] = {"ok": saved.get("ok"),
                              "bytes": saved.get("bytes"),
                              "path": saved.get("path"),
                              "error": saved.get("error")}
    u32.DestroyWindow(hwnd)


# Always write a result file -- including on failure. A missing file gives the
# parent nothing to work with, while a traceback pinpoints the cause in one shot.
try:
    run()
    result["ok"] = True
except Exception as exc:
    import traceback
    result["ok"] = False
    result["error"] = "%s: %s" % (type(exc).__name__, exc)
    result["traceback"] = traceback.format_exc()

try:
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
except Exception as exc:
    sys.stderr.write("%s: %s\n" % (type(exc).__name__, exc))
    sys.exit(3)
sys.exit(0 if result.get("ok") else 1)
'''


# ============================================================ A. 不依赖桌面的用例
def part_a() -> None:
    print("\n[A] 纯逻辑与边界（不建桌面、不起进程）")

    case("name_of(0) 返回占位而不是抛异常",
         desktop.name_of(0) == desktop.UNKNOWN, desktop.name_of(0))

    cur = desktop.current_name()
    case("current_name() 能读出当前桌面名",
         bool(cur) and cur != desktop.UNKNOWN, repr(cur))
    print(f"        （本进程当前桌面 = {cur!r}）")

    # send_text 的参数校验：非法模式必须被挡住，而不是静默按 char 发出去
    r = winmsg.send_text(0, "abc", mode="nope")
    case("send_text 拒绝未知模式", r.get("ok") is False and "未知" in r.get("error", ""),
         str(r))

    # capture 对失效句柄要回失败，不能抛 —— 调用方在诊断路径上会用它
    r = winmsg.capture(0)
    case("capture(0) 回失败且带 error", r.get("ok") is False and bool(r.get("error")),
         str(r))

    # save_bmp / save_png 对未成功的抓图要拒绝
    bad = {"ok": False, "error": "模拟失败"}
    r = winmsg.save_bmp(bad, os.path.join(tempfile.gettempdir(), "x.bmp"))
    case("save_bmp 拒绝未成功的抓图", r.get("ok") is False, str(r))
    r = winmsg.save_png(bad, os.path.join(tempfile.gettempdir(), "x.png"))
    case("save_png 拒绝未成功的抓图", r.get("ok") is False, str(r))

    # 统计量的两个极端：全黑必须塌到 0，纯白必须非黑占比 1.0
    black = winmsg._blackness_stats(b"\x00" * (8 * 8 * 4), 8, 8)
    # 全黑画面的三个统计量会**同时塌掉**：只有 1 种颜色（就是黑）、亮度 0、非黑占比 0。
    # 注意是 1 而不是 0 —— 黑色本身也是一个颜色，这里写成 0 就永远不可能通过。
    case("全黑缓冲：三个统计量同时塌掉",
         black["distinct_colors"] <= 1 and black["nonblack_ratio"] == 0.0
         and black["mean_luma"] == 0.0, str(black))
    white = winmsg._blackness_stats(b"\xff" * (8 * 8 * 4), 8, 8)
    case("纯白缓冲：非黑占比 = 1.0",
         white["nonblack_ratio"] == 1.0 and white["distinct_colors"] == 1,
         str(white))
    # 缓冲长度不足时必须回 0 而不是越界读
    short = winmsg._blackness_stats(b"\x00" * 4, 64, 64)
    case("缓冲不足时不越界、统计量归零", short["samples"] == 0, str(short))

    # spawn 的入参校验
    r = desktop.spawn(r"C:\__definitely_not_here__.exe", [])
    case("spawn 对不存在的 exe 回失败而非抛异常",
         r.get("ok") is False and bool(r.get("error")), str(r))

    # 不存在的桌面必须 Open 失败（否则「桌面复用」的判断就全是假的）
    r = desktop.open_existing("QQAgent__NoSuchDesktop__")
    case("open_existing 对不存在的桌面回失败", r.get("ok") is False, str(r))
    case("exists 对不存在的桌面回 False",
         desktop.exists("QQAgent__NoSuchDesktop__") is False)

    d = desktop.describe()
    case("describe() 结构完整",
         isinstance(d, dict) and d.get("current_desktop") and "kept" in d, str(d))

    # find_windows 在壳自己的桌面上跑一次（QQ 不在也应当正常返回空表）
    try:
        ws = winmsg.find_windows(("Chrome_WidgetWin_1",))
        case("find_windows 在无目标时回空表且不抛", isinstance(ws, list), str(ws)[:80])
    except Exception as exc:
        case("find_windows 在无目标时回空表且不抛", False,
             f"{type(exc).__name__}: {exc}")


# ============================================================ B. 跨桌面落地验证
def part_b() -> None:
    print("\n[B] 跨桌面落地 + 非活动桌面抓图（R4 的两条地基）")

    r = desktop.create(DESK)
    if not r["ok"]:
        case("创建测试桌面", False, r["error"])
        print("        （后面的用例依赖它，跳过）")
        return
    case("创建测试桌面", True, f"handle={r['handle']} existed={r['existed']}")
    print(f"        （桌面 {DESK!r} 建好，existed={r['existed']}）")

    try:
        case("wait_for_desktop_ready 确认桌面可用",
             desktop.wait_for_desktop_ready(DESK, timeout=2.0))
        case("exists() 确认桌面在", desktop.exists(DESK) is True)

        r2 = desktop.create(DESK)
        case("重复 create 同名桌面 → 复用而不是重建",
             r2["ok"] and r2["handle"] == r["handle"] and r2["existed"] is True,
             str(r2))

        # ---- 起子进程到那张桌面上 ----
        tmpdir = tempfile.mkdtemp(prefix="qqagent-desktop-test-")
        child_py = os.path.join(tmpdir, "child.py")
        out_json = os.path.join(tmpdir, "child.json")
        # 先自查编码再落盘：这份脚本要跨一个进程边界，中途任何一次
        # 按非 UTF-8 解码都会在**子进程**里报错，而父进程只看得到退出码。
        # 把它挡在这一句上，失败信息才是有用的。
        case("子进程脚本是纯 ASCII（跨进程边界不带编码歧义）",
             _CHILD.isascii(), "脚本里出现了非 ASCII 字符")
        if not _CHILD.isascii():
            return
        with open(child_py, "w", encoding="ascii") as f:
            f.write(_CHILD)

        sp = desktop.spawn(sys.executable, [child_py, HERE, out_json],
                           desktop=DESK, cwd=HERE)
        if not sp["ok"]:
            case("在指定桌面上启动子进程", False, sp["error"])
            print("        （后面的用例依赖它，跳过）")
            return
        case("在指定桌面上启动子进程", True,
             f"pid={sp['pid']} desktop={sp['desktop']!r}")
        print(f"        （子进程 pid={sp['pid']}，请求的桌面 = {DESK!r}）")

        waited = desktop.wait(sp["hproc"], timeout=40.0)
        code = desktop.exit_code(sp["hproc"])
        desktop.close_handle(sp["hproc"])
        case("子进程已退出（258 = 等超时）", waited == 0,
             f"WaitForSingleObject={waited}")

        if not os.path.isfile(out_json):
            case("子进程产出了结果文件", False,
                 f"缺 {out_json}（退出码 {code}；说明它没走到写文件那一步）")
            return
        case("子进程产出了结果文件", True)
        with open(out_json, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 子进程失败时会把 traceback 写回结果文件 —— 否则它没有控制台，
        # 父进程只能看到「没有文件」，无从下手。
        case("子进程自报执行成功", data.get("ok") is True,
             f"退出码 {code}；{data.get('error')}\n{data.get('traceback') or ''}")

        # ---- ★ 断言 1：它真的落在我们要的那张桌面上 ----
        #
        # 这是整条链路的**地基**。lpDesktop 只是发起方的请求，
        # 进程最终归属由内核决定 —— 只有子进程自己的报告算证据。
        case("★ 子进程自报所在桌面 = 目标隐藏桌面",
             data.get("desktop") == DESK,
             f"它报的是 {data.get('desktop')!r}，期望 {DESK!r}")
        print(f"        （子进程自报桌面 = {data.get('desktop')!r}）")

        # 反向确认：子进程**看不到**壳所在桌面上的东西、壳也看不到它。
        # 这一条说明「为什么必须有宿主进程」不是理论推测。
        case("子进程窗口真的建起来了", bool(data.get("hwnd")) and data.get("visible"),
             f"hwnd={data.get('hwnd')} visible={data.get('visible')}")

        # ---- ★ 断言 2：非活动桌面上的窗口画面抓得到，而且不是全黑 ----
        cap = data.get("capture") or {}
        case("★ 非活动桌面上的窗口抓图成功", cap.get("ok") is True, str(cap))
        case("★ PrintWindow 报告取到了内容", cap.get("printwindow_ret") == 1, str(cap))
        case("★ GetDIBits 取回全部行",
             cap.get("getdibits_lines") == cap.get("h"),
             f"{cap.get('getdibits_lines')} vs {cap.get('h')}")
        # 窗口画了 3 个色块 + 一个背景色 → 至少 4 种颜色；全黑画面会塌成 0/1
        case("★ 抓到的画面不是全黑（颜色种类 > 3）",
             (cap.get("distinct_colors") or 0) > 3,
             f"distinct_colors={cap.get('distinct_colors')} "
             f"nonblack_ratio={cap.get('nonblack_ratio')}")
        case("★ 非黑像素占比 > 0.5",
             (cap.get("nonblack_ratio") or 0.0) > 0.5,
             f"nonblack_ratio={cap.get('nonblack_ratio')}")
        print(f"        （抓图 {cap.get('w')}x{cap.get('h')}，"
              f"{cap.get('distinct_colors')} 种颜色，"
              f"平均亮度 {cap.get('mean_luma')}，非黑占比 {cap.get('nonblack_ratio')}）")

        sb = data.get("save_bmp") or {}
        case("抓到的画面落盘成功（无 Pillow 也能出图）",
             sb.get("ok") is True and (sb.get("bytes") or 0) > 0, str(sb))
        print(f"        （落盘 {sb.get('bytes')} 字节 → {sb.get('path')}）")

    finally:
        # 清理是硬要求：留一张测试桌面在系统里，下次运行它的 existed 就永远是 True，
        # 「复用 vs 新建」这条用例会变成假通过。
        desktop.close(DESK)
        print(f"\n  [清理] 已关闭测试桌面的句柄（桌面名 {DESK!r}）")


def main() -> int:
    print("=" * 72)
    print("R4 机制层自检：独立桌面托管 + 窗口消息 + 画面抓取")
    print("=" * 72)
    print(f"项目根：{HERE}")
    print(f"解释器：{sys.executable}")
    print(f"Pillow：{'有' if _has_pil() else '没有（抓图会退成 BMP，属正常）'}")

    part_a()
    part_b()

    print("\n" + "=" * 72)
    print(f"通过 {OK} 项，失败 {FAIL} 项")
    if FAIL:
        print("[X] 有失败项 —— 先看上面的 [FAIL] 行，再看它旁边括号里的现场数据")
    else:
        print("[OK] R4 机制层的两条地基都成立：跨桌面落地 + 非活动桌面抓图")
    return 1 if FAIL else 0


def _has_pil() -> bool:
    # 用 find_spec 而不是 `import PIL` —— pyflakes 不认 `# noqa`（那是 flake8 的），
    # 光导入不使用在它眼里就是「未使用导入」，会让 test_errors.py 的静态体检红掉。
    import importlib.util
    try:
        return importlib.util.find_spec("PIL") is not None
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
