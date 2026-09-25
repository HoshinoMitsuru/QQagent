# -*- coding: utf-8 -*-
"""
winmsg.py —— 窗口消息投递与画面抓取（与「在哪个桌面」无关的窗口原语）

## 这个模块解决什么问题

V1（附着式）写文本要走「抢前台 → 剪贴板 → 按键 → 归还前台」四步，
其中**抢前台是全链路里唯一必须打扰用户的一步**。
R4（独立桌面）把 QQ 启动到一张用户看不见的桌面上，那条路上没有「前台」可抢 ——
那张桌面上本来就只有一个窗口，它就是那里的前台。
于是写文本退化成「一次窗口消息」，本模块就是这条最小路径的实现。

## 为什么一律用 SendMessageTimeoutW 而不是 SendMessage

裸 `SendMessage` 是同步的：对端不泵消息队列时**本进程会被挂死**。
隐藏桌面上的窗口处在渲染节流期时，这种情况真的会发生（见 E-DESK-005）。
带超时的版本最坏也只是这一条投递失败，控制权还在我们手上。

## 为什么返回值只能算「参考」，不能算「成功」

`SendMessageTimeoutW` 返回非 0 只说明消息**进了队列**，不代表控件采纳了它 ——
Chromium 在拿不到焦点时会**返回 1 但什么也不做**，这正是上一轮
「发了 7 个、落地 0 个」的成因。所以本模块只报告「对端有没有超时」，
**真正的成功判据必须由调用方回读**（UIA 的 ValuePattern 或控件 Name）。

## 画面抓取为什么要单独验

真实部署里 QQ 用**独立 profile**（不与用户自己的 QQ 抢 profile），也就是**未登录** ——
用户必须扫一次二维码，而二维码画在那张看不见的桌面上。
抓不到图 = 用户无法登录 = 整个方案落不了地。所以「能不能取出非活动桌面的画面」
是 R4 的一个**独立风险点**，和「能不能读写消息」是两码事。
"""

from __future__ import annotations

import ctypes
import os
import struct
import time
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

HWND = wintypes.HWND
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL
HANDLE = wintypes.HANDLE

# ---------------------------------------------------------------- 常量
WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
SMTO_ABORTIFHUNG = 0x0002
SEND_TIMEOUT_MS = 3000
MAPVK_VK_TO_VSC = 0
DWMWA_CLOAKED = 14
PW_RENDERFULLCONTENT = 2

# ---------------------------------------------------------------- 函数签名
#
# ⚠️ 纯 ctypes 调用**一律显式声明** argtypes/restype。
# 不声明时 ctypes 默认把返回值当 C int（32 位）—— 在 64 位进程里
# 句柄高 32 位会被截断，而这是**静默**的：拿到一个看起来正常的错误句柄，
# 直到某次调用莫名其妙失败才暴露。
_user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
_user32.EnumWindows.restype = BOOL
_user32.EnumChildWindows.argtypes = [HWND, ctypes.c_void_p, wintypes.LPARAM]
_user32.EnumChildWindows.restype = BOOL
_user32.IsWindowVisible.argtypes = [HWND]
_user32.IsWindowVisible.restype = BOOL
_user32.IsIconic.argtypes = [HWND]
_user32.IsIconic.restype = BOOL
_user32.GetWindowTextLengthW.argtypes = [HWND]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = BOOL
_user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
_user32.GetWindowThreadProcessId.restype = DWORD
_user32.SendMessageTimeoutW.argtypes = [
    HWND, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t,
    ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t)]
_user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
_user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
_user32.MapVirtualKeyW.restype = ctypes.c_uint
_user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
_user32.VkKeyScanW.restype = ctypes.c_short
_user32.GetWindowDC.argtypes = [HWND]
_user32.GetWindowDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [HWND, wintypes.HDC]
_user32.ReleaseDC.restype = ctypes.c_int
_user32.PrintWindow.argtypes = [HWND, wintypes.HDC, ctypes.c_uint]
_user32.PrintWindow.restype = BOOL

_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteObject.restype = BOOL
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.DeleteDC.restype = BOOL
_gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint,
                             ctypes.c_uint, ctypes.c_void_p,
                             ctypes.c_void_p, ctypes.c_uint]
_gdi32.GetDIBits.restype = ctypes.c_int

_dwmapi.DwmGetWindowAttribute.argtypes = [HWND, DWORD, ctypes.c_void_p, DWORD]
_dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

# 焦点 / 激活这一组。**全部要显式声明 restype** —— 它们返回的都是句柄，
# 不声明会被 ctypes 当 C int 截断成 32 位，而截断是静默的。
_user32.GetForegroundWindow.argtypes = []
_user32.GetForegroundWindow.restype = HWND
_user32.SetForegroundWindow.argtypes = [HWND]
_user32.SetForegroundWindow.restype = BOOL
_user32.BringWindowToTop.argtypes = [HWND]
_user32.BringWindowToTop.restype = BOOL
_user32.AttachThreadInput.argtypes = [DWORD, DWORD, BOOL]
_user32.AttachThreadInput.restype = BOOL
_user32.SetActiveWindow.argtypes = [HWND]
_user32.SetActiveWindow.restype = HWND
_user32.GetActiveWindow.argtypes = []
_user32.GetActiveWindow.restype = HWND
_user32.SetFocus.argtypes = [HWND]
_user32.SetFocus.restype = HWND
_user32.GetFocus.argtypes = []
_user32.GetFocus.restype = HWND
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = DWORD


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", DWORD),
                ("biSizeImage", DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", DWORD),
                ("biClrImportant", DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", DWORD * 3)]


_ENUM_PROC = ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)


# ============================================================ 窗口信息（只读）
def win_text(hwnd: int) -> str:
    n = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    _user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def win_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def win_rect(hwnd: int) -> list[int]:
    r = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(r))
    return [int(r.left), int(r.top), int(r.right), int(r.bottom)]


def pid_of(hwnd: int) -> int:
    p = DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
    return int(p.value)


def is_visible(hwnd: int) -> bool:
    return bool(_user32.IsWindowVisible(hwnd))


def is_iconic(hwnd: int) -> bool:
    return bool(_user32.IsIconic(hwnd))


def cloaked(hwnd: int) -> int | None:
    """
    DWM 是否把这个窗口标记为 cloaked。0=否，1=是，2=被 shell 隐藏。

    这一条是隐藏桌面方案的**健康指标**：cloked != 0 意味着 DWM 把窗口
    排除在合成之外，画面抓不到、渲染也会被节流。None = 拿不到该属性（正常，
    有些窗口不是 DWM 管理的）。
    """
    v = ctypes.c_int(0)
    hr = _dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED,
                                       ctypes.byref(v), ctypes.sizeof(v))
    return int(v.value) if hr == 0 else None


def describe_window(hwnd: int) -> dict:
    return {
        "hwnd": int(hwnd),
        "class": win_class(hwnd),
        "title": win_text(hwnd),
        "pid": pid_of(hwnd),
        "visible": is_visible(hwnd),
        "iconic": is_iconic(hwnd),
        "rect": win_rect(hwnd),
        "cloaked": cloaked(hwnd),
    }


def enum_windows() -> list[int]:
    """
    枚举**当前线程所在桌面**的顶层窗口（含隐藏的）。

    ⚠️ 这是 R4 整套架构的根因所在：`EnumWindows` **只枚举调用者所在桌面**。
    壳进程活在用户桌面上，所以它**永远看不到**隐藏桌面上 QQ 的窗口 ——
    这就是为什么必须另起一个「宿主进程」到那张桌面上，
    而不是让壳自己去看。任何「把 EnumWindows 结果缓存到壳里复用」的想法都会踩这个坑。
    """
    out: list[int] = []

    def _cb(hwnd, _lp):
        out.append(int(hwnd))
        return True

    _user32.EnumWindows(_ENUM_PROC(_cb), 0)
    return out


def find_windows(classes: tuple[str, ...] = (), *, visible_only: bool = True,
                 pid: int = 0) -> list[dict]:
    """
    按窗口类名（可选再按 pid）筛选当前桌面上的顶层窗口。

    `classes` 为空 = 不按类名过滤。QQ 主窗口的类名沿用 agent.attach 的规则：
    `Chrome_WidgetWin_1` / `Chrome_WidgetWin_0` / `TXGuiFoundation`。
    """
    want = set(classes)
    out: list[dict] = []
    for h in enum_windows():
        cls = win_class(h)
        if want and cls not in want:
            continue
        if pid and pid_of(h) != pid:
            continue
        info = describe_window(h)
        if visible_only and not info["visible"]:
            continue
        out.append(info)
    return out


def main_window(classes: tuple[str, ...] = (), pid: int = 0) -> dict | None:
    """在一堆可见窗口里挑出「最大」的那个当主窗口。挑不到返回 None。"""
    best, best_area = None, 0
    for w in find_windows(classes, visible_only=True, pid=pid):
        r = w["rect"]
        area = max(0, r[2] - r[0]) * max(0, r[3] - r[1])
        if area > best_area:
            best, best_area = w, area
    return best


def find_child(hwnd: int, cls_name: str, *, visible_only: bool = True) -> list[int]:
    """
    枚举 hwnd 的子孙窗口，返回类名匹配的那些。

    `visible_only=False` 是**诊断用**的：Chromium 的 renderer 子窗口在某些版本上
    不带 `WS_VISIBLE`，只看可见的话会得到"没有 renderer 子窗口"的结论，
    然后误判成"只能投给顶层窗口" —— 而顶层窗口在拿不到焦点时会丢消息。
    排查"投递目标"这一步时务必两种都看。

    ## 为什么需要往下找一层

    Chromium 在 Windows 上把键盘输入交给一个**子 HWND**
    （`Chrome_RenderWidgetHostHWND`），而不是顶层那个 `Chrome_WidgetWin_1`。
    顶层窗口收到 `WM_CHAR` 之后要走 `GetFocusedRenderWidgetHost()` 转交，
    **没焦点就直接丢**（返回 1、什么也不做 —— 这正是「返回值全 1、输入框一个字没进」
    的成因）。所以定位写入目标时要能把这一层挖出来。
    """
    out: list[int] = []

    def _cb(h, _lp):
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(h, buf, 256)
        if buf.value == cls_name and (not visible_only or _user32.IsWindowVisible(h)):
            out.append(int(h))
        return True

    _user32.EnumChildWindows(hwnd, _ENUM_PROC(_cb), 0)
    return out


def child_classes(hwnd: int, limit: int = 40) -> list[dict]:
    """
    枚举子窗口的类名（诊断用）。

    为什么需要它：投递目标选错了，表现是"发了但没落地"，
    和"没焦点"长得一模一样 —— 无法从返回值上区分。
    所以排查时必须先把"这棵树里到底有哪些窗口"列出来。
    """
    out: list[dict] = []

    def _cb(h, _lp):
        if len(out) >= limit:
            return False
        buf = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(h, buf, 256)
        out.append({"hwnd": int(h), "class": buf.value,
                    "visible": bool(_user32.IsWindowVisible(h))})
        return True

    _user32.EnumChildWindows(hwnd, _ENUM_PROC(_cb), 0)
    return out


# ============================================================ 焦点与激活
def foreground_hwnd() -> int:
    """
    当前前台窗口。**注意这是「所在桌面」的前台** ——
    桌面不同则前台不同，隐藏桌面上有自己的前台和我们的窗口无关。
    """
    return int(_user32.GetForegroundWindow() or 0)


def thread_id_of(hwnd: int) -> int:
    """创建该窗口的线程 id（`AttachThreadInput` 要的是它，不是 pid）。"""
    return int(_user32.GetWindowThreadProcessId(hwnd, None) or 0)


def set_foreground(hwnd: int, *, bring_to_top: bool = True) -> dict:
    """
    把窗口切到前台。

    ## ⚠️ 这是**有副作用**的：在用户桌面上调用，等于偷走用户当前的焦点

    所以它在工程里的位置只有一个：**对照实验 / 诊断**。
    生产路径不该用它 —— R4 的全部价值就是让目标窗口**自己**在另一张桌面上成为前台，
    从而根本不需要抢用户的前台。

    Windows 只允许「拥有输入焦点归属的交互式进程」调用 `SetForegroundWindow`；
    被服务 / SSH / 计划任务拉起的进程会**静默失败**（返回值 0，不报错）。
    返回值 1 也可能只是"之前的前台窗口被最小化"这种幸运情况，
    所以是否真的成功请以 `verify=True` 的回读为准。
    """
    prev = foreground_hwnd()
    ret = 0
    if bring_to_top:
        _user32.BringWindowToTop(hwnd)
    ret = int(_user32.SetForegroundWindow(hwnd) or 0)
    time.sleep(0.12)
    now = foreground_hwnd()
    return {"ok": now == hwnd, "ret": ret, "prev": prev,
            "now": now, "hwnd": int(hwnd)}


def focus_steps(top_hwnd: int, child_hwnd: int = 0, *,
                attach: bool = True, active: bool = True,
                focus_top: bool = True, focus_renderer: bool = True,
                gap: float = 0.12) -> dict:
    """
    按计划逐步建立「能收到 `WM_CHAR`」的条件，每步之后记录 前台 / 活动 / 焦点。

    ## 为什么做成可消融的开关，而不是一整套固定动作

    这几步里有两步是**有副作用的**：

      · `AttachThreadInput` 把我们的输入队列接到目标线程上 —— 接错会影响真实输入
      · `SetActiveWindow` / `SetFocus` 会改 OS 焦点 —— **在用户桌面上跑就等于偷用户的焦点**

    所以「最小必需集合」直接决定工程里要不要用它们。多留一步 = 多一个副作用点，
    必须逐个证伪，而不是"能跑就行"。

    已有结论（两边**相反**，别混着用）：

      · **隐藏桌面上**：探针做过消融，结论是**一个都不需要** —— 那张桌面上只有它
        一个窗口，Windows 在显示时就把焦点给了它，Chromium 从一开始就认为自己是 active
      · **用户桌面上**：有竞争前台，结论相反，需要 `SetFocus` 到 renderer 子窗口

    ⚠️ **焦点状态有粘性**：renderer 一旦被聚焦过，后面所有方案都会"成功"，消融就废了。
    所以消融必须**每档一次独立进程、一个全新窗口**，不能在一个进程里连着跑。

    返回时已自动解除 `AttachThreadInput` —— 这一步**不能留着**，
    留着就是长期把两条输入队列串在一起。
    """
    tid_target = thread_id_of(top_hwnd)
    tid_me = int(_kernel32.GetCurrentThreadId())
    steps: list[dict] = []
    attached = False

    def snap(step: str, ret) -> None:
        steps.append({"step": step, "ret": int(ret or 0),
                      "fg": foreground_hwnd(),
                      "active": int(_user32.GetActiveWindow() or 0),
                      "focus": int(_user32.GetFocus() or 0)})

    try:
        if attach and tid_target and tid_target != tid_me:
            attached = bool(_user32.AttachThreadInput(tid_target, tid_me, True))
            snap("AttachThreadInput", int(attached))
            if attached:
                time.sleep(gap)
        snap("初始", 1)
        if active:
            snap("SetActiveWindow(top)", _user32.SetActiveWindow(top_hwnd))
            time.sleep(gap)
        if focus_top:
            snap("SetFocus(top)", _user32.SetFocus(top_hwnd))
            time.sleep(gap)
        if focus_renderer and child_hwnd:
            snap("SetFocus(renderer)", _user32.SetFocus(child_hwnd))
            time.sleep(gap)
    finally:
        if attached:
            try:
                _user32.AttachThreadInput(tid_target, tid_me, False)
            except Exception:
                pass
    return {"tid_target": tid_target, "tid_me": tid_me,
            "attached_used": attached, "steps": steps}


# ============================================================ 文本投递
def send_text(hwnd: int, text: str, *, mode: str = "char",
              gap: float = 0.04) -> dict:
    """
    用窗口消息把文本送进目标窗口（不经 OS 输入队列，因此**不依赖前台**）。

    ## 两种模式

      char（默认）  只发 `WM_CHAR`，lParam=0
      key          发 `WM_KEYDOWN` + `WM_CHAR` + `WM_KEYUP`，lParam 里带 **scan code**

    为什么两种都要留着：Chromium 把消息转成 `ui::KeyEvent`，scan code 取自
    lParam 高位段。上一轮「返回值全 1、输入框一个字没进」除了「没有前台」这个解释，
    还有「KeyEvent 不完整被丢弃」这个解释 —— **只测一种模式无法把两者分开**。

    中文走不了 key 模式：`VkKeyScanW` 对中文返回 -1（当前键盘布局没有对应键），
    此时自动退化成只发 `WM_CHAR`。

    返回逐字符的投递结果。`delivered` 只统计「对端没超时」的条数 ——
    **它不是「成功」**，成功必须由调用方回读（见模块开头说明）。
    """
    if mode not in ("char", "key"):
        return {"ok": False, "error": f"未知的投递模式：{mode}（只支持 char / key）"}

    res = ctypes.c_size_t(0)
    returns: list[int] = []

    def post(h, msg, wp, lp) -> int:
        r = _user32.SendMessageTimeoutW(h, msg, wp, lp, SMTO_ABORTIFHUNG,
                                        SEND_TIMEOUT_MS, ctypes.byref(res))
        return int(r)

    t0 = time.time()
    for ch in text:
        # ⚠️ VkKeyScanW 收的是**字符**（WCHAR）而不是码点；传 ord(ch) 会
        # 报 "unicode string expected instead of int instance"。
        raw = _user32.VkKeyScanW(ch)
        vk = 0 if raw == -1 else (raw & 0xFF)
        sc = (_user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC) & 0xFF) if vk else 0
        if mode == "key" and vk:
            down = 1 | (sc << 16)
            returns.append(post(hwnd, WM_KEYDOWN, vk, down))
            returns.append(post(hwnd, WM_CHAR, ord(ch), 1 | (sc << 16)))
            returns.append(post(hwnd, WM_KEYUP, vk, down | (1 << 30) | (1 << 31)))
        else:
            returns.append(post(hwnd, WM_CHAR, ord(ch), 0))
        if gap:
            time.sleep(gap)
    return {
        "ok": True, "text": text, "mode": mode,
        "sent": len(text), "calls": len(returns),
        "delivered": sum(1 for v in returns if v != 0),
        "returns": returns, "elapsed": round(time.time() - t0, 3),
    }


# ============================================================ 按键投递（非字符键）
VK_CONTROL = 0x11
VK_MENU = 0x12            # Alt
VK_SHIFT = 0x10
VK_BACK = 0x08
VK_DELETE = 0x2E
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B


def send_vk(hwnd: int, vk: int, *, ctrl: bool = False, shift: bool = False,
            alt: bool = False, gap: float = 0.04) -> dict:
    """
    投递一个**虚拟键**（Ctrl+A / Delete / Enter 这类没有字符的键）。

    ## 为什么必须走窗口消息，不能用 `auto.SendKeys`

    `uiautomation` 的 `SendKeys` 底层是 `SendInput` —— 它投的是 **OS 输入队列**，
    而输入队列只服务于**当前 input desktop 的前台窗口**。两条后果：

      1. 隐藏桌面上根本投不进去（我们的进程不在那张桌面）
      2. 在用户桌面上投，会打到**当时前台的那个窗口**上 ——
         也就是别人的程序。Ctrl+A 再 Delete 落在那里，等于删掉用户正在写的东西。

    所以非字符键也必须走窗口消息，和字符走同一条路。

    ⚠️ 这一条同时也是「发送」动作的实现基础（Enter / Ctrl+Enter）。
    在把它接到真实发送链路上之前，请确认目标窗口就是你想发的那个。
    """
    res = ctypes.c_size_t(0)
    returns: list[int] = []

    def post(msg, wp, lp) -> int:
        return int(_user32.SendMessageTimeoutW(
            hwnd, msg, wp, lp, SMTO_ABORTIFHUNG, SEND_TIMEOUT_MS,
            ctypes.byref(res)))

    def scan_of(v: int) -> int:
        return (_user32.MapVirtualKeyW(v, MAPVK_VK_TO_VSC) & 0xFF) if v else 0

    mods = [(VK_CONTROL, ctrl), (VK_MENU, alt), (VK_SHIFT, shift)]
    try:
        for m, on in mods:
            if on:
                returns.append(post(WM_KEYDOWN, m, 1 | (scan_of(m) << 16)))
        sc = scan_of(vk)
        down = 1 | (sc << 16)
        returns.append(post(WM_KEYDOWN, vk, down))
        returns.append(post(WM_KEYUP, vk, down | (1 << 30) | (1 << 31)))
        for m, on in reversed(mods):
            if on:
                returns.append(post(WM_KEYUP, m,
                                    1 | (scan_of(m) << 16) | (1 << 30) | (1 << 31)))
        if gap:
            time.sleep(gap)
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "vk": int(vk), "mods": [m for m, on in mods if on],
            "calls": len(returns),
            "delivered": sum(1 for v in returns if v != 0),
            "returns": returns}


# ============================================================ 画面抓取
def _blackness_stats(buf: bytes, w: int, h: int) -> dict:
    """
    从 BGRX 原始缓冲里算出三个「是不是全黑」的判据。

    ## 为什么要算这个

    `PrintWindow` 返回 1 **不等于**图里有内容（很可能全黑，Chromium 自绘窗口
    在不支持的那条路径上就是黑的）。全黑画面的三个统计量会**同时塌到 0**，
    一眼就能识破假成功 —— 这比「文件存在」有意义得多。

    抽样而不是全量：一张 1800x1230 的图有 220 万像素，
    全量算三遍只是让诊断变慢，抽样到几千个点结论完全一样。
    """
    total = w * h
    if total <= 0 or len(buf) < total * 4:
        return {"distinct_colors": 0, "mean_luma": 0.0, "nonblack_ratio": 0.0,
                "samples": 0}
    step = max(1, total // 4000)
    colors = set()
    luma_sum = 0.0
    nonblack = 0
    n = 0
    for i in range(0, total, step):
        o = i * 4
        b = buf[o]
        g = buf[o + 1]
        r = buf[o + 2]
        colors.add((r, g, b))
        luma_sum += 0.299 * r + 0.587 * g + 0.114 * b
        if r + g + b > 24:
            nonblack += 1
        n += 1
    return {"distinct_colors": len(colors),
            "mean_luma": round(luma_sum / n, 2),
            "nonblack_ratio": round(nonblack / n, 3),
            "samples": n}


def capture(hwnd: int) -> dict:
    """
    抓窗口画面到内存（BGRX 原始缓冲 + 统计量）。不做任何编码、不落盘。

    ## 为什么是 PrintWindow + PW_RENDERFULLCONTENT

      `BitBlt` 从屏幕 DC 取不到 —— 那张桌面根本没参与屏幕合成。
      `PrintWindow(hwnd, hdc, 0)` 走 `WM_PRINT`/`WM_PRINTCLIENT`，
      Chromium 这类自绘窗口通常不处理，取回来是黑的。
      `PW_RENDERFULLCONTENT`(2) 改成直接取 **DWM 的重定向表面** ——
      对 Chromium/Electron 是唯一可靠的一路。

    返回的 `buf` 是**自上而下**的 BGRX（GetDIBits 的负高度就是为这个），
    每行 w*4 字节。`save_bmp` 会负责翻成 BMP 要的自下而上。
    """
    r = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return {"ok": False, "error": f"GetWindowRect 失败（hwnd={hwnd} 已失效？）"}
    w, h = int(r.right - r.left), int(r.bottom - r.top)
    if w <= 0 or h <= 0:
        return {"ok": False, "error": f"窗口尺寸非法 {w}x{h}（最小化时会是这个结果）"}

    hdc_win = _user32.GetWindowDC(hwnd)
    if not hdc_win:
        return {"ok": False, "error": "GetWindowDC 失败"}
    hdc_mem = _gdi32.CreateCompatibleDC(hdc_win)
    hbm = _gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    if not hdc_mem or not hbm:
        _user32.ReleaseDC(hwnd, hdc_win)
        return {"ok": False, "error": "CreateCompatibleDC / CreateCompatibleBitmap 失败"}

    old = _gdi32.SelectObject(hdc_mem, hbm)
    info: dict = {"ok": False, "w": w, "h": h}
    try:
        info["printwindow_ret"] = int(_user32.PrintWindow(hwnd, hdc_mem,
                                                          PW_RENDERFULLCONTENT))
        bi = _BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h          # 负高度 = 自上而下，省一次翻转
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0       # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        lines = _gdi32.GetDIBits(hdc_mem, hbm, 0, h, buf,
                                 ctypes.byref(bi), 0)
        info["getdibits_lines"] = int(lines)
        if lines != h:
            info["error"] = f"GetDIBits 只取回 {lines} 行（应为 {h} 行）"
            return info
        raw = buf.raw
        info["buf"] = raw
        info.update(_blackness_stats(raw, w, h))
        info["ok"] = True
        return info
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info
    finally:
        try:
            _gdi32.SelectObject(hdc_mem, old)
            _gdi32.DeleteObject(hbm)
            _gdi32.DeleteDC(hdc_mem)
            _user32.ReleaseDC(hwnd, hdc_win)
        except Exception:
            pass


def save_bmp(cap: dict, path: str) -> dict:
    """
    把 `capture()` 的结果写成 BMP 文件。

    ## 为什么手写 BMP 而不是用 Pillow

    Pillow 在 requirements.txt 里是**可选**依赖（OCR 兜底才需要）。
    而「把二维码取出来给用户扫」是主链路，不能让主链路依赖一个可选包。
    BMP 的头只有 54 字节、格式固定，手写比引入依赖划算得多；
    浏览器和 WebUI 都能直接显示 BMP。

    输入是**自上而下**的行序（GetDIBits 负高度的结果），
    而 BMP 文件要求**自下而上**，所以这里要倒着写。
    """
    if not cap.get("ok"):
        return {"ok": False, "error": cap.get("error") or "抓图未成功，没有可写的内容"}
    w, h, buf = int(cap["w"]), int(cap["h"]), cap.get("buf") or b""
    row = w * 4
    if len(buf) < row * h:
        return {"ok": False, "error": f"缓冲不足：{len(buf)} < {row * h}"}
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        size = 14 + 40 + row * h
        head = bytearray(b"BM")
        head += struct.pack("<IHHI", size, 0, 0, 54)
        head += struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, row * h,
                            2835, 2835, 0, 0)
        with open(path, "wb") as f:
            f.write(head)
            for y in range(h - 1, -1, -1):
                f.write(buf[y * row:(y + 1) * row])
        return {"ok": True, "path": path, "bytes": os.path.getsize(path),
                "w": w, "h": h, "format": "bmp"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def save_png(cap: dict, path: str) -> dict:
    """
    有 Pillow 时存成 PNG（体积小得多，适合走 HTTP 传给 WebUI）。
    没有 Pillow 就明确回失败，让调用方退回 `save_bmp`。
    """
    if not cap.get("ok"):
        return {"ok": False, "error": cap.get("error") or "抓图未成功"}
    try:
        from PIL import Image
    except Exception:
        return {"ok": False, "error": "未安装 Pillow，请改用 save_bmp"}
    try:
        w, h, buf = int(cap["w"]), int(cap["h"]), cap["buf"]
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        img.save(path)
        return {"ok": True, "path": path, "bytes": os.path.getsize(path),
                "w": w, "h": h, "format": "png"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def grab_any(hwnd: int, path: str) -> dict:
    """
    抓一张图并落盘：优先 PNG（小、WebUI 友好），没有 Pillow 就退成 BMP。

    这是给「取二维码」用的便捷入口 —— 调用方只关心「文件在哪、是不是空白」。
    """
    cap = capture(hwnd)
    if not cap.get("ok"):
        return {"ok": False, "error": cap.get("error") or "抓图失败",
                "stats": {k: cap.get(k) for k in
                          ("w", "h", "printwindow_ret", "getdibits_lines")}}
    stats = {k: cap.get(k) for k in
             ("w", "h", "printwindow_ret", "getdibits_lines",
              "distinct_colors", "mean_luma", "nonblack_ratio")}
    out = save_png(cap, path if path.lower().endswith(".png")
                   else os.path.splitext(path)[0] + ".png")
    if not out.get("ok"):
        out = save_bmp(cap, path if path.lower().endswith(".bmp")
                       else os.path.splitext(path)[0] + ".bmp")
    if not out.get("ok"):
        return {"ok": False, "error": out.get("error") or "落盘失败", "stats": stats}
    out["stats"] = stats
    # 三个统计量同时塌到 0 = 全黑。文件写出来了，但里面什么都没有 ——
    # 这种情况必须报出来，否则用户拿到一张黑图会以为是别的问题。
    out["blank"] = bool(stats.get("distinct_colors", 0) <= 1
                        and stats.get("nonblack_ratio", 0.0) < 0.001)
    return out
