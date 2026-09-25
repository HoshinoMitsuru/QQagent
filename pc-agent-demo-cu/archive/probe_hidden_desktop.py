# -*- coding: utf-8 -*-
"""
探针：把目标应用启动到**独立 Windows 桌面**上，验证它还能不能被 UIA 读到、
能不能当自己桌面的前台，以及用户桌面的前台是否**完全不受影响**。

这是 R4（隐藏桌面）方案的成立性判据，四个观察项：

  ① 窗口可见性      —— 在那个桌面上 EnumWindows 能不能返回它的窗口
  ② 无障碍树        —— 四个锚点/树规模是否正常（renderer 有没有被冻结）
  ③ 抢前台          —— 在那个桌面上 SetForegroundWindow 能不能成功
  ④ 用户前台不受影响 —— ★ 这一项才是 R4 区别于「虚拟显示器」的唯一价值

## 两种角色

    --role launcher   跑在**用户桌面**：建隐藏桌面 → 在其中启动目标应用与 host → 汇总
    --role host       跑在**隐藏桌面**：做 ①②③ 的观察，结果写 JSON 文件

host 由 launcher 用 `CreateProcessW` + `STARTUPINFO.lpDesktop` 启动，
**从进程创建的那一刻起就在隐藏桌面上** —— 这一点很重要：
UIA 的 `GetRootElement()` 返回的是**当前线程所在桌面**的根，
所以驱动进程必须和被测应用同桌面，否则看不到对方的窗口。

只读为主：除了 ③ 会尝试一次 SetForegroundWindow（且只发生在那张隐藏桌面上），
不点击、不切会话、不发消息。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwm = ctypes.WinDLL("dwmapi", use_last_error=True)

HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL
HWND = wintypes.HWND
LPCWSTR = wintypes.LPCWSTR

GENERIC_ALL = 0x10000000
CREATE_NO_WINDOW = 0x08000000
STARTF_USESHOWWINDOW = 0x00000001
SW_SHOWNORMAL = 1
UOI_NAME = 2
DWMWA_CLOAKED = 14

u32.CreateDesktopW.argtypes = [LPCWSTR, LPCWSTR, ctypes.c_void_p, DWORD, DWORD, ctypes.c_void_p]
u32.CreateDesktopW.restype = HANDLE
u32.CloseDesktop.argtypes = [HANDLE]
u32.CloseDesktop.restype = BOOL
u32.GetThreadDesktop.argtypes = [DWORD]
u32.GetThreadDesktop.restype = HANDLE
u32.GetUserObjectInformationW.argtypes = [HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD,
                                          ctypes.POINTER(DWORD)]
u32.GetUserObjectInformationW.restype = BOOL
u32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
u32.EnumChildWindows.argtypes = [HWND, ctypes.c_void_p, wintypes.LPARAM]
u32.IsWindowVisible.argtypes = [HWND]
u32.IsWindowVisible.restype = BOOL
u32.IsIconic.argtypes = [HWND]
u32.IsIconic.restype = BOOL
u32.GetWindowTextLengthW.argtypes = [HWND]
u32.GetWindowTextLengthW.restype = ctypes.c_int
u32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
u32.GetForegroundWindow.restype = HWND
u32.SetForegroundWindow.argtypes = [HWND]
u32.SetForegroundWindow.restype = BOOL
u32.BringWindowToTop.argtypes = [HWND]
u32.ShowWindow.argtypes = [HWND, ctypes.c_int]
u32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
u32.AttachThreadInput.argtypes = [DWORD, DWORD, BOOL]
u32.SetActiveWindow.argtypes = [HWND]
u32.SetActiveWindow.restype = HWND
u32.SetFocus.argtypes = [HWND]
u32.SetFocus.restype = HWND
u32.GetActiveWindow.restype = HWND
u32.GetFocus.restype = HWND
u32.SendMessageW.argtypes = [HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
u32.SendMessageW.restype = ctypes.c_ssize_t
# 一律用**带超时**的版本发消息：对端若不泵消息队列，裸 SendMessage 会把本进程挂死。
# 返回 0 = 超时或失败；非 0 = 对端处理了。
u32.SendMessageTimeoutW.argtypes = [HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
                                    ctypes.c_uint, ctypes.c_uint,
                                    ctypes.POINTER(ctypes.c_size_t)]
u32.SendMessageTimeoutW.restype = ctypes.c_ssize_t

WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
SMTO_ABORTIFHUNG = 0x0002
SEND_TIMEOUT_MS = 3000
MAPVK_VK_TO_VSC = 0
u32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
u32.MapVirtualKeyW.restype = ctypes.c_uint
u32.VkKeyScanW.argtypes = [ctypes.c_wchar]
u32.VkKeyScanW.restype = ctypes.c_short

# ---------------------------------------------------------------------------
# 激活方案的消融矩阵
#
# 为什么要做消融而不是"能跑就行"：
#   这几步里有两步是**有副作用的** ——
#     · `AttachThreadInput` 把我们的输入队列接到目标线程上，接错会影响真实输入
#     · `SetFocus` 会改 OS 焦点（在用户桌面上跑就等于偷用户的焦点）
#   所以"最小必需集合"直接决定工程里要不要用它们。
#   多留一步 = 多一个副作用点，必须逐个证伪。
#
# 注意：消融**不能在一个进程里连跑**。Chromium 侧的焦点状态是有粘性的 ——
# 一旦 renderer 被聚焦过，后面所有方案都会"成功"，消融就废了。
# 所以每个方案一次独立运行、一个全新窗口。
FOCUS_PLANS: dict[str, dict] = {
    "none":              dict(attach=False, active=False, focus_top=False, focus_renderer=False),
    "renderer_only":     dict(attach=False, active=False, focus_top=False, focus_renderer=True),
    "no_attach":         dict(attach=False, active=True,  focus_top=True,  focus_renderer=True),
    "no_renderer_focus": dict(attach=True,  active=True,  focus_top=True,  focus_renderer=False),
    "full":              dict(attach=True,  active=True,  focus_top=True,  focus_renderer=True),
}

dwm.DwmGetWindowAttribute.argtypes = [HWND, DWORD, ctypes.c_void_p, DWORD]
dwm.DwmGetWindowAttribute.restype = ctypes.c_long

k32.GetCurrentThreadId.restype = DWORD


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", DWORD), ("dwY", DWORD),
        ("dwXSize", DWORD), ("dwYSize", DWORD),
        ("dwXCountChars", DWORD), ("dwYCountChars", DWORD),
        ("dwFillAttribute", DWORD),
        ("dwFlags", DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", HANDLE), ("hStdOutput", HANDLE), ("hStdError", HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", HANDLE), ("hThread", HANDLE),
                ("dwProcessId", DWORD), ("dwThreadId", DWORD)]


k32.CreateProcessW.argtypes = [LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                               BOOL, DWORD, ctypes.c_void_p, LPCWSTR,
                               ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
k32.CreateProcessW.restype = BOOL
k32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
k32.WaitForSingleObject.restype = DWORD
k32.CloseHandle.argtypes = [HANDLE]
k32.TerminateProcess.argtypes = [HANDLE, ctypes.c_uint]


# ---------------------------------------------------------------- 小工具
def desktop_name() -> str:
    """当前线程所在桌面的名字。"""
    h = u32.GetThreadDesktop(k32.GetCurrentThreadId())
    if not h:
        return "(未知)"
    buf = ctypes.create_unicode_buffer(512)
    need = DWORD()
    if u32.GetUserObjectInformationW(h, UOI_NAME, buf, 512, ctypes.byref(need)):
        return buf.value
    return "(未知)"


def win_text(h: int) -> str:
    n = u32.GetWindowTextLengthW(h)
    b = ctypes.create_unicode_buffer(n + 2)
    u32.GetWindowTextW(h, b, n + 2)
    return b.value


def win_class(h: int) -> str:
    b = ctypes.create_unicode_buffer(256)
    u32.GetClassNameW(h, b, 256)
    return b.value


def win_rect(h: int) -> list[int]:
    r = wintypes.RECT()
    u32.GetWindowRect(h, ctypes.byref(r))
    return [r.left, r.top, r.right, r.bottom]


def pid_of(h: int) -> int:
    p = DWORD()
    u32.GetWindowThreadProcessId(h, ctypes.byref(p))
    return p.value


def enum_windows() -> list[int]:
    """枚举**当前桌面**的顶层窗口（含隐藏）。这是 ① 的判据。"""
    out: list[int] = []

    @ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)
    def _cb(hwnd, _lp):
        out.append(hwnd)
        return True

    u32.EnumWindows(_cb, 0)
    return out


def cloaked(h: int) -> int | None:
    """DWM 是否把这个窗口标记为 cloaked（0=否，1=是，2=被 shell 隐藏）。"""
    v = ctypes.c_int(0)
    hr = dwm.DwmGetWindowAttribute(h, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
    return v.value if hr == 0 else None


def spawn_on_desktop(exe: str, args: str, desk: str | None, cwd: str | None = None):
    """在指定桌面上启动进程，返回 (pid, hProcess)。

    `desk` 传 None 或空串 = **不指定**，进程落在调用者当前桌面上 ——
    这就是对照组（在用户桌面上跑同一套消息），用来把
    「消息序列本身对不对」与「有没有前台」这两个变量拆开。
    """
    cmd = f'"{exe}" {args}'.strip()
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = desk or None
    si.dwFlags = STARTF_USESHOWWINDOW
    si.wShowWindow = SW_SHOWNORMAL
    pi = PROCESS_INFORMATION()
    buf = ctypes.create_unicode_buffer(cmd)
    ok = k32.CreateProcessW(exe, buf, None, None, False,
                            CREATE_NO_WINDOW, None, cwd, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        err = ctypes.get_last_error()
        raise OSError(f"CreateProcessW 失败，GetLastError={err}（{ctypes.FormatError(err)}）")
    return pi.dwProcessId, pi.hProcess


# ---------------------------------------------------------------- host 角色
def find_qq_windows(wait_s: float = 30.0) -> list[dict]:
    """轮询等待目标应用的窗口出现在**本桌面**上。"""
    deadline = time.time() + wait_s
    last: list[dict] = []
    while time.time() < deadline:
        rows = []
        for h in enum_windows():
            cls = win_class(h)
            if cls not in ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0", "TXGuiFoundation"):
                continue
            rows.append({
                "hwnd": h, "class": cls, "title": win_text(h),
                "visible": bool(u32.IsWindowVisible(h)),
                "iconic": bool(u32.IsIconic(h)),
                "rect": win_rect(h), "pid": pid_of(h), "cloaked": cloaked(h),
            })
        if any(r["visible"] for r in rows):
            return rows
        last = rows
        time.sleep(0.7)
    return last


def find_child(hwnd: int, cls_name: str) -> list[int]:
    """在当前桌面枚举 hwnd 的子孙窗口，返回指定类名的那些。

    Chromium 在 Windows 上把键盘交给一个**子 HWND**（`Chrome_RenderWidgetHostHWND`），
    不是顶层那个 `Chrome_WidgetWin_1`。顶层收到 WM_CHAR 后要走
    `GetFocusedRenderWidgetHost()`，**没焦点就直接丢**（返回 1 但什么也不做，
    这正是上一轮"发了 7 个、落地 0 个"的原因）。所以要么把窗口弄成前台，
    要么直接把消息送到那个子窗口 —— 两条都试。
    """
    out: list[int] = []

    @ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)
    def _cb(h, _lp):
        b = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(h, b, 256)
        if b.value == cls_name and u32.IsWindowVisible(h):
            out.append(h)
        return True

    u32.EnumChildWindows(hwnd, _cb, 0)
    return out


def grab_window(hwnd: int, out_png: str) -> dict:
    """
    抓窗口画面并存成 PNG，返回一组**可以判断"是不是真的抓到东西"的统计量**。

    ## 为什么这件事必须单独验
      真实部署里 QQ 是**全新 profile**（独立 user-data-dir 才不会和用户自己的 QQ
      抢 profile），也就是**未登录** —— 用户必须扫一次二维码。
      而二维码画在那张用户看不见的桌面上。抓不到图 = 用户无法登录 = 方案落不了地。
      所以"能不能把非活动桌面上的窗口画面取出来"是 R4 的一个**独立风险点**，
      和"能不能读写消息"是两码事，必须单独证。

    ## 为什么是 PrintWindow + PW_RENDERFULLCONTENT
      `BitBlt` 从屏幕 DC 取不到 —— 那张桌面根本没参与屏幕合成。
      `PrintWindow(hwnd, hdc, 0)` 走 `WM_PRINT`/`WM_PRINTCLIENT`，
      Chromium 这类自绘窗口通常不处理，取回来是黑的。
      `PW_RENDERFULLCONTENT`(2) 改成直接取 **DWM 的重定向表面** ——
      对 Chromium/Electron 是唯一可靠的一路。

    ## 为什么要返回统计量而不是只存文件
      "PrintWindow 返回了 1" 不等于"图里有内容"（很可能全黑）。
      所以顺手算：非黑像素占比、颜色种类数、平均亮度 ——
      全黑画面这三个数会同时塌到 0，一眼就能识破假成功。
    """
    from PIL import Image

    g32 = ctypes.WinDLL("gdi32", use_last_error=True)
    g32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g32.CreateCompatibleDC.restype = wintypes.HDC
    g32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    g32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    g32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    g32.SelectObject.restype = wintypes.HGDIOBJ
    g32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g32.DeleteDC.argtypes = [wintypes.HDC]
    u32.GetWindowDC.argtypes = [HWND]
    u32.GetWindowDC.restype = wintypes.HDC
    u32.ReleaseDC.argtypes = [HWND, wintypes.HDC]
    u32.PrintWindow.argtypes = [HWND, wintypes.HDC, ctypes.c_uint]
    u32.PrintWindow.restype = BOOL

    r = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return {"ok": False, "error": f"窗口尺寸非法 {w}x{h}"}

    hdc_win = u32.GetWindowDC(hwnd)
    hdc_mem = g32.CreateCompatibleDC(hdc_win)
    hbm = g32.CreateCompatibleBitmap(hdc_win, w, h)
    old = g32.SelectObject(hdc_mem, hbm)
    info: dict = {"w": w, "h": h}
    try:
        # PW_RENDERFULLCONTENT = 2
        info["printwindow_ret"] = int(u32.PrintWindow(hwnd, hdc_mem, 2))

        class BMIH(ctypes.Structure):
            _fields_ = [("biSize", DWORD), ("biWidth", ctypes.c_long),
                        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", DWORD),
                        ("biSizeImage", DWORD), ("biXPelsPerMeter", ctypes.c_long),
                        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", DWORD),
                        ("biClrImportant", DWORD)]

        class BMI(ctypes.Structure):
            _fields_ = [("bmiHeader", BMIH), ("bmiColors", DWORD * 3)]

        bi = BMI()
        bi.bmiHeader.biSize = ctypes.sizeof(BMIH)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h           # 负高度 = 自上而下，省一次翻转
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0       # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        g32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint,
                                  ctypes.c_uint, ctypes.c_void_p,
                                  ctypes.POINTER(BMI), ctypes.c_uint]
        n = g32.GetDIBits(hdc_mem, hbm, 0, h, buf, ctypes.byref(bi), 0)
        info["getdibits_lines"] = int(n)
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        img.save(out_png)
        info["png"] = out_png
        info["png_bytes"] = os.path.getsize(out_png)
        # 三个"是不是全黑"的判据
        small = img.resize((64, 44))
        px = list(small.getdata())
        info["distinct_colors"] = len(set(px))
        info["mean_luma"] = round(sum(0.299 * a + 0.587 * b + 0.114 * c
                                      for a, b, c in px) / len(px), 2)
        info["nonblack_ratio"] = round(
            sum(1 for a, b, c in px if a + b + c > 24) / len(px), 3)
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            g32.SelectObject(hdc_mem, old)
            g32.DeleteObject(hbm)
            g32.DeleteDC(hdc_mem)
            u32.ReleaseDC(hwnd, hdc_win)
        except Exception:
            pass
    return info


def send_text(hwnd: int, text: str, gap: float = 0.04, mode: str = "char") -> dict:
    """
    用窗口消息把文本送进目标窗口（不经 OS 输入队列，因此不依赖前台）。

    ## 两种模式（必须对照，不能只测一种）

      char（默认）  只发 `WM_CHAR`，lParam=0
      key          发 `WM_KEYDOWN` + `WM_CHAR` + `WM_KEYUP`，
                   lParam 里带上 **scan code**

    为什么要有 key 模式：Chromium 把消息转成 `ui::KeyEvent`，
    scan code 是从 lParam 的高位段取的（`MapVirtualKeyW(VK, MAPVK_VK_TO_VSC) << 16`）。
    上一轮"返回值全 1、输入框一个字没进"，除了"没有前台"这个解释之外，
    还有"KeyEvent 不完整被丢弃"这个解释 —— **只测 char 模式无法区分两者**。
    所以换模式的对照是必须的。

    返回逐字符的投递结果——**只看"发了几个"会骗人**：
    `SendMessageTimeoutW` 返回 0 就说明对端超时/失败，这一条必须留证据。
    """
    res = ctypes.c_size_t(0)
    oks: list[int] = []

    def post(h, msg, wp, lp):
        r = u32.SendMessageTimeoutW(h, msg, wp, lp, SMTO_ABORTIFHUNG,
                                    SEND_TIMEOUT_MS, ctypes.byref(res))
        return int(r)

    t0 = time.time()
    for ch in text:
        # VkKeyScanW 收的是**字符**（WCHAR），不是码点；传 ord(ch) 会报
        # "unicode string expected instead of int instance"。
        # 返回 -1 = 当前键盘布局里没有这个字符对应的键（中文就是这种情况）→
        # 此时 vk 置 0，key 模式自动退化成只发 WM_CHAR。
        raw = u32.VkKeyScanW(ch)
        vk = 0 if raw == -1 else (raw & 0xFF)
        sc = u32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) & 0xFF if vk else 0
        if mode == "key" and vk:
            lp_down = 1 | (sc << 16)
            oks.append(post(hwnd, WM_KEYDOWN, vk, lp_down))
            oks.append(post(hwnd, WM_CHAR, ord(ch), 1 | (sc << 16)))
            oks.append(post(hwnd, WM_KEYUP, vk, lp_down | (1 << 30) | (1 << 31)))
        else:
            oks.append(post(hwnd, WM_CHAR, ord(ch), 0))
        time.sleep(gap)
    return {"text": text, "mode": mode, "sent": len(text), "calls": len(oks),
            "delivered": sum(1 for v in oks if v != 0),
            "returns": oks, "elapsed": round(time.time() - t0, 3)}


def poll_titles(hwnd: int, seconds: float, interval: float = 0.25) -> list[dict]:
    """
    按固定节奏采样窗口标题。

    为什么盯标题：标题是**唯一能跨桌面直读**的状态出口
    （`EnumWindows` 只枚举本桌面，但拿到 hwnd 之后 `GetWindowTextW` 照读不误）。
    测试页把「rAF 帧数 / visibilityState / hasFocus / input 事件数 / textarea 内容」
    全部塞进 document.title，于是这一串采样同时回答了：
      · renderer 有没有被节流（rAF 帧数是否仍在涨）
      · 可见性有没有被降级（visibilityState 是否还是 visible）
      · 窗口消息有没有真的落到输入框里（textarea 内容）
    """
    out: list[dict] = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        out.append({"t": round(time.time() - t0, 2), "title": win_text(hwnd)})
        time.sleep(interval)
    return out


def uia_find_text(target_pid: int, needle: str, max_nodes: int = 4000,
                  max_depth: int = 16) -> dict:
    """
    在本桌面的无障碍树里搜 needle，并**把命中的节点原样带回来**。

    这是"写入是否落地"的另一路证据。为什么不能只靠标题：
    标题是我们自己在测试页里拼的，属于"被测方自述"；
    无障碍树是 Chromium 自己投影出来的，属于独立信源。两个都对上才算数。

    读三处：`Name`、`ValuePattern.Value`、`LegacyIAccessiblePattern.Value`。
    Chromium 对 `<textarea>` 会暴露 ValuePattern；对 contenteditable 只给 TextPattern（只读）。
    """
    import uiautomation as auto

    auto.UIAutomationInitializerInThread()

    hits: list[str] = []
    scanned = 0
    root_ctrl = None
    try:
        for ctrl in auto.GetRootControl().GetChildren():
            try:
                if ctrl.ClassName != "Chrome_WidgetWin_1" or ctrl.ProcessId != target_pid:
                    continue
                if ctrl.BoundingRectangle.width() < 200:
                    continue
                root_ctrl = ctrl
                break
            except Exception:
                continue
    except Exception as exc:
        return {"found": False, "error": f"{type(exc).__name__}: {exc}"}

    if root_ctrl is None:
        return {"found": False, "error": "本桌面根下没找到目标进程的 Chrome_WidgetWin_1"}

    stack = [(root_ctrl, 0)]
    while stack and scanned < max_nodes:
        ctrl, d = stack.pop()
        scanned += 1
        vals: list[tuple[str, str]] = []
        try:
            if ctrl.Name:
                vals.append(("Name", ctrl.Name))
        except Exception:
            pass
        for pat_name in ("ValuePattern", "LegacyIAccessiblePattern"):
            try:
                pat = ctrl.GetPattern(auto.PatternId.__dict__[pat_name])
                if pat:
                    v = getattr(pat, "Value", "") or ""
                    if v:
                        vals.append((pat_name, v))
            except Exception:
                pass
        for src, v in vals:
            if needle in v:
                hits.append(f"{ctrl.ControlTypeName}[{ctrl.ClassName}] {src}={v!r}")
        if d >= max_depth:
            continue
        try:
            for ch in reversed(ctrl.GetChildren()):
                stack.append((ch, d + 1))
        except Exception:
            pass
    return {"found": bool(hits), "scanned": scanned, "hits": hits[:8]}


def uia_stats(target_pid: int = 0, max_nodes: int = 6000, max_depth: int = 14) -> dict:
    """
    统计本桌面上目标窗口的无障碍树规模。这是 ② 的判据。

    renderer 活着 → 树有内容；被冻结/空壳 → 节点极少。

    ⚠️ 必须按 **pid** 过滤：桌面根的子控件里不止一个 `Chrome_WidgetWin_1`
    （WorkBuddy 之类同样是 Electron），不按 pid 挑会统计到别的应用头上。
    """
    import uiautomation as auto

    auto.UIAutomationInitializerInThread()

    root_ctrl = None
    try:
        for ctrl in auto.GetRootControl().GetChildren():
            try:
                if ctrl.ClassName != "Chrome_WidgetWin_1":
                    continue
                if target_pid and ctrl.ProcessId != target_pid:
                    continue
                if ctrl.BoundingRectangle.width() < 200:
                    continue
                root_ctrl = ctrl
                break
            except Exception:
                continue
    except Exception:
        pass
    if root_ctrl is None:
        return {"found": False, "nodes": 0}

    nodes = 0
    ctrl_types: dict[str, int] = {}
    class_names: dict[str, int] = {}
    stack = [(root_ctrl, 0)]
    sample: list[str] = []
    while stack and nodes < max_nodes:
        ctrl, d = stack.pop()
        nodes += 1
        try:
            t = ctrl.ControlTypeName
            ctrl_types[t] = ctrl_types.get(t, 0) + 1
            cn = ctrl.ClassName or ""
            class_names[cn] = class_names.get(cn, 0) + 1
            if len(sample) < 25:
                sample.append(f"{'  ' * d}{t}/{cn}/{ctrl.Name[:24]!r}")
        except Exception:
            pass
        if d >= max_depth:
            continue
        try:
            for ch in reversed(ctrl.GetChildren()):
                stack.append((ch, d + 1))
        except Exception:
            pass
    return {
        "found": True,
        "nodes": nodes,
        "truncated": nodes >= max_nodes,
        "types": dict(sorted(ctrl_types.items(), key=lambda kv: -kv[1])[:12]),
        "classes": dict(sorted(class_names.items(), key=lambda kv: -kv[1])[:12]),
        "sample": sample,
        "has_button": ctrl_types.get("ButtonControl", 0) > 0,
    }


def role_host(out_path: str, wait_s: float, target_pid: int = 0, do_fg: bool = True,
              type_text: str = "", probe_secs: float = 4.0,
              send_modes: tuple[str, ...] = ("char",),
              restore_fg: bool = False,
              focus_plan: str = "full",
              shot_path: str = "") -> int:
    result: dict = {"desktop": desktop_name(), "pid": os.getpid()}

    fg_before = u32.GetForegroundWindow()
    result["fg_before"] = {"hwnd": fg_before, "class": win_class(fg_before),
                           "title": win_text(fg_before)}

    wins = find_qq_windows(wait_s)
    if target_pid:
        picked = [w for w in wins if w["pid"] == target_pid]
        if picked:
            wins = picked
    result["windows"] = wins
    visible = [w for w in wins if w["visible"]]
    result["visible_count"] = len(visible)

    if visible:
        target = visible[0]
        result["target"] = target
        try:
            result["uia"] = uia_stats(target["pid"])
        except Exception as exc:
            result["uia"] = {"found": False, "error": f"{type(exc).__name__}: {exc}"}

        # ③ 渲染存活采样（发送之前先采一段基线）
        #
        # 这一段回答的是「在隐藏桌面上，Chromium 的 renderer 有没有被节流」——
        # 也就是 R4 能不能拿到**实时**新消息。判据全在标题里（测试页自己写进去的）：
        # rAF 帧数是否在涨、visibilityState 是否还是 visible。
        # 如果这里就没在涨，后面写入测什么都白搭，所以放在最前面。
        hwnd = target["hwnd"]
        result["title_before_all"] = win_text(hwnd)
        if probe_secs > 0:
            result["baseline_titles"] = poll_titles(hwnd, min(probe_secs / 2, 2.0))

        if not do_fg:
            result["fg_skipped"] = "本次按 --no-fg 跳过激活/写入测试"
        else:
            # ④ 激活 + 写入测试：在这张桌面上尝试
            #
            # 为什么不是简单的一次 SetForegroundWindow：
            #   「写文本」的常规通路（剪贴板 + Ctrl+V）要窗口是会话前台；
            #   隐藏桌面上 GetForegroundWindow 返回 NULL，那条路直接断了。
            #   剩下的通路是**窗口消息** —— 实测它能写入 ProseMirror（含中文），
            #   但在用户桌面上做会把 QQ 顶成前台。所以这里要回答的是：
            #   **同样的操作在隐藏桌面上，会不会影响用户桌面的前台？**
            #
            # 三个证据缺一不可：
            #   a. 消息投递结果（SendMessageTimeoutW 的返回值，不是"我发了几个"）
            #   b. 读回（标题 timeline + 无障碍树里搜 needle）—— 证明真的落进去了
            #   c. 本桌面前台变化（另一个桌面由 launcher 独立核对）
            tid_target = u32.GetWindowThreadProcessId(hwnd, None)
            tid_me = k32.GetCurrentThreadId()
            prev_fg = u32.GetForegroundWindow()
            plan = FOCUS_PLANS.get(focus_plan) or FOCUS_PLANS["full"]
            log: dict = {"tid_target": tid_target, "tid_me": tid_me,
                         "focus_plan": focus_plan, "plan": plan}
            attached = False
            try:
                log["active_before"] = u32.GetActiveWindow()
                log["fg_before"] = prev_fg

                # 先记录一次"想把窗口弄成前台"的诊断。
                # 这一步**不进任何方案**：在隐藏桌面上它恒失败（见 run2/3/4 实测
                # `ret=0`、`fg=None`），而且对着一个无效目标反复调用没有意义。
                # 留着是为了让报告里"前台这条路走不通"有据可查。
                log["setforeground_ret"] = u32.SetForegroundWindow(hwnd)
                log["bringtotop_ret"] = u32.BringWindowToTop(hwnd)
                log["fg_after_fg_attempt"] = u32.GetForegroundWindow()

                steps: list[dict] = []
                if plan["attach"]:
                    attached = bool(u32.AttachThreadInput(tid_me, tid_target, True))
                    steps.append({"step": "AttachThreadInput", "ret": attached,
                                  "fg": u32.GetForegroundWindow(),
                                  "active": u32.GetActiveWindow(),
                                  "focus": u32.GetFocus()})
                    time.sleep(0.12)
                if plan["active"]:
                    r = u32.SetActiveWindow(hwnd)
                    steps.append({"step": "SetActiveWindow", "ret": r,
                                  "fg": u32.GetForegroundWindow(),
                                  "active": u32.GetActiveWindow(),
                                  "focus": u32.GetFocus()})
                    time.sleep(0.12)
                if plan["focus_top"]:
                    r = u32.SetFocus(hwnd)
                    steps.append({"step": "SetFocus(top)", "ret": r,
                                  "fg": u32.GetForegroundWindow(),
                                  "active": u32.GetActiveWindow(),
                                  "focus": u32.GetFocus()})
                    time.sleep(0.12)

                # 真正吃键盘的是 renderer 子窗口，焦点往往要再往里推一层
                renderer = 0
                kids = find_child(hwnd, "Chrome_RenderWidgetHostHWND")
                log["renderer_children"] = kids
                if kids:
                    renderer = kids[0]
                    if plan["focus_renderer"]:
                        r = u32.SetFocus(renderer)
                        steps.append({"step": "SetFocus(renderer child)", "ret": r,
                                      "fg": u32.GetForegroundWindow(),
                                      "active": u32.GetActiveWindow(),
                                      "focus": u32.GetFocus()})
                        time.sleep(0.12)
                log["focus_steps"] = steps
                log["attached"] = attached
                log["active_after"] = u32.GetActiveWindow()
                log["focus_after"] = u32.GetFocus()
                log["fg_after_activate"] = u32.GetForegroundWindow()

                # 投递只用顶层窗口：既然实测顶层就能落地，就没必要再往子窗口乱发 ——
                # 多发一处只是多一个变量、多一份副作用，不增加信息。
                targets = [(hwnd, "top")]

                # a. 投递。顶层与 renderer 子窗口**各发一次**，看哪一个能落地；
                #    每种目标再跑 char / key 两种消息序列 —— 这是把
                #    「序列不对」与「没前台」拆开的那一刀。
                if type_text:
                    log["sends"] = []
                    for mode in send_modes:
                        for thwnd, tname in targets:
                            row = {"target": tname, "hwnd": thwnd}
                            row.update(send_text(thwnd, type_text, mode=mode))
                            log["sends"].append(row)
                            time.sleep(0.4)
                            row["title_after"] = win_text(hwnd)
                    # b. 读回（Chromium 的 UIA 投影，独立于测试页自述的标题）
                    try:
                        log["uia_readback"] = uia_find_text(target["pid"], type_text)
                    except Exception as exc:
                        log["uia_readback"] = {"found": False,
                                               "error": f"{type(exc).__name__}: {exc}"}
                    # b'. 读回（标题 timeline）
                    if probe_secs > 0:
                        log["titles_after_send"] = poll_titles(hwnd, probe_secs)
                    else:
                        log["title_after_send"] = win_text(hwnd)

                # 截图放在最后：这时输入框里已经有刚写进去的字，
                # 于是这张 PNG 同时验证两件事 —— "非活动桌面的窗口能不能抓"
                # 以及"抓到的图里有没有刚才写进去的内容"。
                if shot_path:
                    try:
                        log["shot"] = grab_window(hwnd, shot_path)
                    except Exception as exc:
                        log["shot"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            except Exception as exc:
                log["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                if attached:
                    try:
                        u32.AttachThreadInput(tid_me, tid_target, False)
                    except Exception:
                        pass
                # 对照组跑在用户桌面上，实验会把它顶成前台 —— 结束后**还回去**。
                # （隐藏桌面上 fg_before 本来就是 None，这句是空操作，留着无害。）
                if restore_fg and prev_fg and prev_fg != u32.GetForegroundWindow():
                    for _ in range(3):
                        u32.SetForegroundWindow(prev_fg)
                        time.sleep(0.15)
                        if u32.GetForegroundWindow() == prev_fg:
                            break
                    log["fg_restored"] = u32.GetForegroundWindow() == prev_fg
            time.sleep(0.4)
            fg_after_try = u32.GetForegroundWindow()
            log["fg_on_this_desktop"] = {
                "hwnd": fg_after_try, "class": win_class(fg_after_try),
                "title": win_text(fg_after_try),
                "is_target": fg_after_try == hwnd,
            }
            result["activation_probe"] = log
    result["fg_after"] = {"hwnd": u32.GetForegroundWindow(),
                          "class": win_class(u32.GetForegroundWindow()),
                          "title": win_text(u32.GetForegroundWindow())}

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return 0


# ---------------------------------------------------------------- launcher 角色
def role_launcher(exe: str, args: str, desk: str, host_out: str,
                  host_wait: float, keep: bool, host_script: str,
                  type_text: str = "", probe_secs: float = 4.0,
                  send_modes: tuple[str, ...] = ("char",),
                  restore_fg: bool = False, focus_plan: str = "full",
                  shot_path: str = "") -> int:
    on_user_desktop = not desk
    if on_user_desktop:
        print("=" * 74)
        print("对照实验：**在用户桌面上**跑同一套消息序列")
        print("=" * 74)
        print("目的：把「消息序列本身不对」与「缺少前台」这两个变量拆开。")
        print("本模式下前台**会被占用**（这正是对照组要建立的条件），实验结束会还回去。")
    else:
        print("=" * 74)
        print("R4 实验：把应用启动到独立 Windows 桌面")
        print("=" * 74)

    fg0 = u32.GetForegroundWindow()
    print(f"\n[当前桌面] {desktop_name()}")
    print(f"[前台·操作前] {win_class(fg0)} / {win_text(fg0)!r}  hwnd={fg0}")

    hdesk = 0
    if not on_user_desktop:
        hdesk = u32.CreateDesktopW(desk, None, None, 0, GENERIC_ALL, None)
        if not hdesk:
            err = ctypes.get_last_error()
            print(f"[X] CreateDesktopW 失败：{err}（{ctypes.FormatError(err)}）")
            return 2
        print(f"[隐藏桌面] 已创建 {desk!r}  hDesk={hdesk}")

    try:
        pid, hproc = spawn_on_desktop(exe, args, desk or None, os.path.dirname(exe))
        print(f"[启动] 目标应用 pid={pid}"
              f"（在 {'用户桌面' if on_user_desktop else repr(desk)} 上）")
    except OSError as exc:
        print(f"[X] {exc}")
        if hdesk:
            u32.CloseDesktop(hdesk)
        return 2

    py = sys.executable
    hargs = (f'-X utf8 "{host_script}" --role host --out "{host_out}" '
             f'--wait {host_wait:.0f} --probe-secs {probe_secs:.1f} '
             f'--send-modes {",".join(send_modes)} --focus-plan {focus_plan}')
    if shot_path:
        hargs += f' --shot "{shot_path}"'
    if type_text:
        hargs += f' --type-text "{type_text}"'
    if restore_fg:
        hargs += ' --restore-fg'
    if on_user_desktop:
        hargs += f' --target-pid {pid}'
    print(f"[启动] host 观察进程（{'用户桌面' if on_user_desktop else '同一隐藏桌面'}）…")
    try:
        hpid, hh = spawn_on_desktop(py, hargs, desk or None)
    except OSError as exc:
        print(f"[X] host 启动失败：{exc}")
        if hdesk:
            u32.CloseDesktop(hdesk)
        return 2

    rc = k32.WaitForSingleObject(hh, int((host_wait + 20) * 1000))
    print(f"[host] 结束，WaitForSingleObject={rc}（0=正常结束，258=超时）")

    fg1 = u32.GetForegroundWindow()
    print(f"[前台·操作后] {win_class(fg1)} / {win_text(fg1)!r}  hwnd={fg1}")
    same = (fg1 == fg0)
    if on_user_desktop:
        # 对照组里前台**本来就会被占用**，这里不构成结论，只报告是否已归还。
        print(f"[前台是否已归还] {'是' if same else '否'}"
              f"　（对照模式下前台被占用是预期行为，不是缺陷）")
    else:
        print(f"[★ 用户前台是否未受影响] {'是 ✅' if same else '否 ❌（前台被改变了）'}")

    if os.path.isfile(host_out):
        with open(host_out, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        print("[X] host 没有产出结果文件")
        data = None

    if data:
        print("\n" + "-" * 74)
        print(f"host 所在桌面 = {data.get('desktop')!r}  pid={data.get('pid')}")
        print(f"本桌面枚举到的窗口数 = {len(data.get('windows', []))}"
              f"　其中可见 = {data.get('visible_count')}")
        for w in data.get("windows", []):
            if w["visible"]:
                print(f"  ★ hwnd={w['hwnd']} cls={w['class']} "
                      f"最小化={w['iconic']} cloaked={w['cloaked']} rect={w['rect']}")
                print(f"     标题={w['title']!r}")
        u = data.get("uia") or {}
        print(f"\n无障碍树：found={u.get('found')} 节点数={u.get('nodes')} "
              f"截断={u.get('truncated')} 有按钮={u.get('has_button')}")
        if u.get("classes"):
            print(f"  class 分布：{u['classes']}")
        if u.get("types"):
            print(f"  控件类型  ：{u['types']}")
        for line in (u.get("sample") or [])[:12]:
            print(f"    {line}")
        if u.get("error"):
            print(f"  [X] {u['error']}")
        ap = data.get("activation_probe")
        base = data.get("baseline_titles") or []
        if base:
            print("\n渲染存活基线（发送之前，标题采样）：")
            for row in base:
                print(f"  t={row['t']:>5}s  {row['title']}")
        if ap:
            print(f"\n激活 + 写入测试（激活方案 = {ap.get('focus_plan')}）：")
            print(f"  目标线程={ap.get('tid_target')}　本线程={ap.get('tid_me')}"
                  f"　AttachThreadInput={ap.get('attached')}")
            print(f"  renderer 子窗口 = {ap.get('renderer_children')}")
            print(f"  前台尝试：SetForegroundWindow={ap.get('setforeground_ret')}"
                  f"　BringWindowToTop={ap.get('bringtotop_ret')}"
                  f"　→ fg={ap.get('fg_after_fg_attempt')}"
                  "　（隐藏桌面上恒失败，不参与任何方案）")
            print("  本方案实际执行的动作（每步后的 前台/活动/焦点）：")
            for st in ap.get("focus_steps") or []:
                print(f"    {st['step']:<24} ret={st['ret']}"
                      f"　fg={st['fg']}　active={st['active']}　focus={st['focus']}")
            if not ap.get("focus_steps"):
                print("    （无 —— 这一档故意什么都不做，作为负对照）")
            print(f"  收尾：active={ap.get('active_after')}  focus={ap.get('focus_after')}"
                  f"  fg={ap.get('fg_after_activate')}")
            for snd in ap.get("sends") or []:
                print(f"  [a] 投给 {snd['target']}（{snd['mode']} 模式，hwnd={snd['hwnd']}）："
                      f"{snd['text']!r} 计 {snd['sent']} 字符 / {snd['calls']} 次调用，"
                      f"对端处理 {snd['delivered']} 个，耗时 {snd['elapsed']}s")
                print(f"      发后标题={snd.get('title_after')!r}")
            rb = ap.get("uia_readback")
            if rb:
                print(f"  [b] 无障碍树读回：命中={rb.get('found')}　扫描节点={rb.get('scanned')}")
                for h in rb.get("hits") or []:
                    print(f"      ★ {h}")
                if rb.get("error"):
                    print(f"      [X] {rb['error']}")
            aft = ap.get("titles_after_send") or []
            if aft:
                print("  [b'] 标题 timeline（发送之后）：")
                for row in aft:
                    print(f"      t={row['t']:>5}s  {row['title']}")
            elif ap.get("title_after_send") is not None:
                print(f"  [b'] 标题（发送之后）= {ap['title_after_send']!r}")
            print(f"  [c] 本桌面前台 = {ap.get('fg_on_this_desktop')}")
            sh = ap.get("shot")
            if sh:
                print(f"  [e] 窗口截图：{sh.get('w')}x{sh.get('h')}"
                      f"　PrintWindow={sh.get('printwindow_ret')}"
                      f"　GetDIBits 行数={sh.get('getdibits_lines')}")
                print(f"      PNG={sh.get('png')}（{sh.get('png_bytes')} 字节）"
                      f"　颜色种类={sh.get('distinct_colors')}"
                      f"　平均亮度={sh.get('mean_luma')}"
                      f"　非黑占比={sh.get('nonblack_ratio')}")
                if sh.get("error"):
                    print(f"      [X] {sh['error']}")
            if ap.get("fg_restored") is not None:
                print(f"  [d] 前台是否已归还 = {ap['fg_restored']}")
            if ap.get("error"):
                print(f"  [X] {ap['error']}")

            # 消融实验要的就是这一行：这一档到底能不能写进去。
            # 判据取自**标题里的 te=**（测试页自述）与**无障碍树的 EditControl**
            # （Chromium 投影），两者都对上才算通过 —— 单一信源不作数。
            last_title = ""
            for snd in ap.get("sends") or []:
                if snd.get("title_after"):
                    last_title = snd["title_after"]
            plan_marker = (ap.get("sends") or [{}])[-1].get("text", "")
            title_ok = bool(plan_marker) and f'"{plan_marker}' in last_title
            uia_ok = bool((ap.get("uia_readback") or {}).get("found"))
            print(f"\n  【本轮判定·{ap.get('focus_plan')}】"
                  f"标题读到写入={'是' if title_ok else '否'}　"
                  f"无障碍树读到写入={'是' if uia_ok else '否'}　"
                  f"→ {'✅ 写入成功' if (title_ok or uia_ok) else '❌ 写入失败'}")
        if data.get("fg_skipped"):
            print(f"\n{data['fg_skipped']}")

    if not keep:
        try:
            k32.TerminateProcess(hproc, 1)
            print(f"\n[清理] 已结束目标应用 pid={pid}")
        except Exception as exc:
            print(f"[清理] 失败：{exc}")
    if hdesk:
        u32.CloseDesktop(hdesk)
        print("[清理] 隐藏桌面已关闭")
    return 0


# ---------------------------------------------------------------- 入口
def main() -> int:
    ap = argparse.ArgumentParser(description="隐藏桌面方案探针（R4 成立性验证）")
    ap.add_argument("--role", choices=["launcher", "host"], default="launcher")
    ap.add_argument("--exe", default=r"D:\QQ.exe", help="要启动到隐藏桌面的程序")
    ap.add_argument("--args", default="", help="传给它的命令行参数")
    ap.add_argument("--desktop", default="QQAgentProbe",
                    help="隐藏桌面的名字；传空串 = 不建桌面，直接在用户桌面跑（对照组）")
    ap.add_argument("--host-out", default=os.path.join(os.path.dirname(__file__), "host-result.json"))
    ap.add_argument("--host-script", default=os.path.abspath(__file__))
    ap.add_argument("--wait", type=float, default=40.0, help="host 等待窗口出现的秒数")
    ap.add_argument("--keep", action="store_true", help="结束后不结束目标应用")
    ap.add_argument("--out", default="", help="（host 角色）结果写入路径")
    ap.add_argument("--target-pid", type=int, default=0,
                    help="（host 角色）只统计这个进程的窗口；0=自动挑最宽的那个")
    ap.add_argument("--no-fg", action="store_true", help="（host 角色）跳过抢前台测试")
    ap.add_argument("--type-text", default="",
                    help="写进目标窗口的文本；空=不写")
    ap.add_argument("--probe-secs", type=float, default=4.0,
                    help="标题采样时长（秒）；标题里带 rAF 帧数/visibilityState")
    ap.add_argument("--send-modes", default="char",
                    help="逗号分隔，取值 char / key；两种都测请写 char,key")
    ap.add_argument("--restore-fg", action="store_true",
                    help="（host 角色）实验后把前台还给原来的窗口")
    ap.add_argument("--focus-plan", default="full",
                    choices=sorted(FOCUS_PLANS),
                    help="激活方案的消融：每档只开一部分动作，用来找最小必需集合")
    ap.add_argument("--shot", default="",
                    help="把目标窗口抓成 PNG 存到这个路径（PrintWindow+PW_RENDERFULLCONTENT）")
    a = ap.parse_args()

    modes = tuple(m.strip() for m in a.send_modes.split(",") if m.strip())
    if a.role == "host":
        return role_host(a.out, a.wait, a.target_pid, not a.no_fg,
                         a.type_text, a.probe_secs, modes, a.restore_fg,
                         a.focus_plan, a.shot)
    return role_launcher(a.exe, a.args, a.desktop, a.host_out, a.wait, a.keep,
                         a.host_script, a.type_text, a.probe_secs,
                         modes, a.restore_fg, a.focus_plan, a.shot)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
