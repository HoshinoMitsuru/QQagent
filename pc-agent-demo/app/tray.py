# -*- coding: utf-8 -*-
"""
tray.py —— 系统托盘图标（纯 ctypes 实现，不引入 pystray/Pillow）

为什么非要这个不可：程序编译成 windowed exe 之后**没有窗口、没有控制台**。
如果只有「双击后弹出浏览器」这一个入口，用户关掉浏览器标签页之后就再也找不到它了 ——
任务管理器里会多出一个来路不明的进程，而界面上没有任何地方能告诉它「退出」。

它承担三件事：

    双击图标     打开控制台界面
    右键菜单     打开界面 / 打开数据目录 / 退出
    气泡提示     启动完成、出现警告时提示一次

## 实现上的两个硬要求

`NOTIFYICONDATAW` 必须按 Vista 之后的完整布局声明并把 `cbSize` 填成结构体大小，
少一个字段微软就会返回失败但**不报错**（Shell_NotifyIcon 的经典陷阱）。

`WNDPROC` 的 Python 包装对象必须被持有引用。一旦被垃圾回收，窗口过程就成了野指针，
表现为「图标点着点着程序就崩了」——这种崩溃最难归因，所以这里用模块级变量钉住它。
"""

from __future__ import annotations

import ctypes
import sys
import threading
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------------------------------------------------------- 函数原型
# **必须显式声明**，不能靠 ctypes 的默认推断。
# 在 64 位下 HWND / HMENU / LPARAM 都是指针宽度的量，而 ctypes 在没有 argtypes 时
# 会把 Python 整数按 32 位 C int 传 —— 于是真实句柄一大就抛
# `ArgumentError: int too long to convert`，或者更糟：静默截断。
# 我们真的踩到了：窗口过程里把 lparam 转交给 DefWindowProcW 时直接就炸了。
LRESULT = ctypes.c_ssize_t
_WPARAM_T = wintypes.WPARAM
_LPARAM_T = wintypes.LPARAM

_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, _WPARAM_T, _LPARAM_T]
_user32.DefWindowProcW.restype = LRESULT
_user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
_user32.RegisterClassExW.restype = wintypes.ATOM
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, _LPARAM_T]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.CreatePopupMenu.restype = wintypes.HMENU
_user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT,
                                ctypes.c_size_t, wintypes.LPCWSTR]
_user32.DestroyMenu.argtypes = [wintypes.HMENU]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                   ctypes.c_void_p]
_user32.TrackPopupMenu.restype = wintypes.BOOL
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, _WPARAM_T, _LPARAM_T]
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_user32.GetMessageW.argtypes = [ctypes.c_void_p, wintypes.HWND,
                                wintypes.UINT, wintypes.UINT]
_user32.GetMessageW.restype = ctypes.c_int
_user32.TranslateMessage.argtypes = [ctypes.c_void_p]
_user32.DispatchMessageW.argtypes = [ctypes.c_void_p]
_user32.DispatchMessageW.restype = LRESULT
_user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
_user32.LoadIconW.restype = wintypes.HICON
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_shell32.ExtractIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                  wintypes.UINT]
_shell32.ExtractIconW.restype = wintypes.HICON
_shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]
_shell32.Shell_NotifyIconW.restype = wintypes.BOOL

WM_DESTROY = 0x0002
WM_COMMAND = 0x0111
WM_CLOSE = 0x0010
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_NULL = 0x0000
WM_TRAY = 0x8000 + 1            # WM_APP + 1

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

IDI_APPLICATION = 32512

MENU_OPEN = 1001
MENU_DATA = 1002
MENU_QUIT = 1004


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_byte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                              _WPARAM_T, _LPARAM_T)
_WNDPROC_REF = None       # 必须钉住引用，否则窗口过程会被 GC 掉


class Tray:
    """
    托盘图标。`on_open` / `on_quit` 是纯 Python 回调，菜单点击时在消息循环里执行。

    `run()` 会**占住调用它的线程**（Windows 的消息循环就是这样，没有别的办法），
    所以主程序必须把它放在主线程的最后一步。
    """

    def __init__(self, tooltip: str, on_open=None, on_quit=None, on_data=None):
        self.tooltip = tooltip[:127]
        self.on_open = on_open
        self.on_quit = on_quit
        self.on_data = on_data
        self.hwnd = None
        self._nid = None
        self._menu = None
        self.ok = False
        self.error = ""
        self._quit_requested = False

    # ---------------------------------------------------------- 图标
    def _load_icon(self):
        try:
            if getattr(sys, "frozen", False):
                h = _shell32.ExtractIconW(None, sys.executable, 0)
                if h and h != 1:
                    return h
        except Exception:
            pass
        try:
            return _user32.LoadIconW(None, wintypes.LPCWSTR(IDI_APPLICATION))
        except Exception:
            return None

    # ---------------------------------------------------------- 窗口过程
    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAY:
            event = lparam & 0xFFFF
            if event in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self._safe(self.on_open)
            elif event == WM_RBUTTONUP:
                self._popup_menu()
            return 0
        if msg == WM_COMMAND:
            cmd = wparam & 0xFFFF
            if cmd == MENU_OPEN:
                self._safe(self.on_open)
            elif cmd == MENU_DATA:
                self._safe(self.on_data)
            elif cmd == MENU_QUIT:
                self._quit_requested = True
                self._remove()
                _user32.PostQuitMessage(0)
            return 0
        if msg == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _safe(self, fn):
        if not fn:
            return
        try:
            fn()
        except Exception:
            pass

    def _popup_menu(self):
        try:
            _user32.SetForegroundWindow(self.hwnd)
            cmd = _user32.TrackPopupMenu(
                self._menu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                ctypes.c_int(0), ctypes.c_int(0), 0, self.hwnd, None)
            if cmd:
                # 直接派发给窗口过程，省掉一遍消息投递
                self._wndproc(self.hwnd, WM_COMMAND, cmd, 0)
            _user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        except Exception:
            pass

    # ---------------------------------------------------------- 生命周期
    def _remove(self):
        if self._nid:
            try:
                _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            except Exception:
                pass
            self._nid = None

    def notify(self, title: str, text: str) -> None:
        """弹一个气泡提示（图标已存在时用 NIM_MODIFY 更新）。"""
        if not self._nid:
            return
        try:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_INFO
            nid.szInfo = text[:255]
            nid.szInfoTitle = title[:63]
            nid.dwInfoFlags = 0x00000001     # NIIF_INFO
            _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
        except Exception:
            pass

    def setup(self) -> bool:
        """建隐藏窗口、注册托盘图标。失败不抛异常，只把原因记在 self.error 里。"""
        try:
            hinst = _kernel32.GetModuleHandleW(None)
            cls_name = "QQAgentTrayWnd"

            global _WNDPROC_REF
            _WNDPROC_REF = _WNDPROC(self._wndproc)

            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = ctypes.cast(_WNDPROC_REF, ctypes.c_void_p)
            wc.hInstance = hinst
            wc.lpszClassName = cls_name
            wc.hIcon = self._load_icon()
            if not _user32.RegisterClassExW(ctypes.byref(wc)):
                err = ctypes.get_last_error()
                # 1409 = 类已注册，重复注册不算失败
                if err != 1409:
                    self.error = f"RegisterClassExW 失败，错误码 {err}"
                    return False

            self.hwnd = _user32.CreateWindowExW(
                0, cls_name, "QQAgent", 0, 0, 0, 0, 0, None, None, hinst, 0)
            if not self.hwnd:
                self.error = f"CreateWindowExW 失败，错误码 {ctypes.get_last_error()}"
                return False

            self._menu = _user32.CreatePopupMenu()
            _user32.AppendMenuW(self._menu, MF_STRING, MENU_OPEN, "打开控制台界面")
            _user32.AppendMenuW(self._menu, MF_STRING, MENU_DATA, "打开数据目录")
            _user32.AppendMenuW(self._menu, MF_SEPARATOR, 0, None)
            _user32.AppendMenuW(self._menu, MF_STRING, MENU_QUIT, "退出 QQAgent")

            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = 1
            nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            nid.uCallbackMessage = WM_TRAY
            nid.hIcon = self._load_icon()
            nid.szTip = self.tooltip
            self._nid = nid
            if not _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                self.error = f"Shell_NotifyIconW 失败，错误码 {ctypes.get_last_error()}"
                self._nid = None
                return False

            self.ok = True
            return True
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return False

    def quit(self) -> None:
        """让消息循环退出（供外部收尾流程调用，线程安全）。"""
        try:
            if self.hwnd:
                _user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        except Exception:
            pass

    def run(self) -> None:
        """消息循环，会阻塞当前线程直到「退出」被点击或 quit() 被调用。"""
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))
        self._remove()


def start_in_thread(tooltip: str, **kwargs) -> Tray | None:
    """
    在没有主线程可用时（比如测试脚本）把托盘放到后台线程。

    正式运行时**不要**用这个：消息循环应该待在主线程，
    后台线程里跑消息循环会在进程退出时表现得很诡异。
    """
    tray = Tray(tooltip, **kwargs)
    if tray.setup():
        threading.Thread(target=tray.run, daemon=True).start()
        return tray
    return None
