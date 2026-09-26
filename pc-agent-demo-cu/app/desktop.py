# -*- coding: utf-8 -*-
"""
desktop.py —— 独立 Windows 桌面的创建与「把进程启动到那张桌面上」

## 这个模块是 V2（R4）存在的理由

V1 附着到**用户正在用的那个桌面**上的 QQ，于是「写文本」必须先把 QQ 弄成前台 ——
那是全链路里唯一会打扰用户的一步（也是「按键漏进别的窗口」这类事故的来源）。

R4 换了个前提：**让 QQ 跑在另一张桌面上**。收益有三条，而且是叠加的：

    1. 用户看不见它 —— 没有窗口挡在屏幕上，也就不会去误关它
    2. 它**算不出被遮挡** —— Chromium 的遮挡判定是「z 序枚举减法」，
       只在本桌面内算；别的桌面上的窗口不在它的枚举结果里，
       于是不会触发「最小化/被盖住 → 停止渲染 + JS 节流」那条自保逻辑
    3. **「前台」是 per-desktop 的概念** —— 在那张桌面上我们的窗口就是前台，
       写文本不再抢用户的前台，连「按键漏进用户终端」的老坑一起消掉

第 3 条是最关键的：它把 V1 里那个「抢前台 → … → 归还前台」的四步，
换成了「一次窗口消息」。这也是为什么这个方案值得单独作为一个版本。

## 为什么必须有「宿主进程」

`EnumWindows` **只枚举调用者所在桌面**的窗口（见 winmsg.enum_windows）。
壳进程活在用户桌面上，它**永远看不到**隐藏桌面上 QQ 的窗口。
所以任何要碰 QQ 的动作都必须由一个**同样被启动到那张桌面上**的进程来做。
`CreateProcessW` 的 `STARTUPINFO.lpDesktop` 就是这个「启动到某张桌面」的入口 ——
本模块把它封成了 `spawn()`。

## 已排除的路（别再试）

  · 把窗口移到屏幕外        —— 官方点名会触发算遮挡，等于自找节流
  · 做成 Windows 服务 / Session 0 —— UIA 看不到用户会话的窗口，且永久抢不到前台
  · 用 `MoveWindow` + 置顶  —— 仍在用户桌面上，仍然占屏幕

## 关于权限

`CreateDesktopW` 要求调用者所在的**窗口站**允许创建桌面。交互式登录的
WinSta0 允许，所以壳只要正常运行（双击 exe）就能建。
不需要管理员权限 —— 这是 R4 相对「装服务」的一个实际优势。
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL
LPCWSTR = wintypes.LPCWSTR

GENERIC_ALL = 0x10000000
DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
STARTF_USESHOWWINDOW = 0x00000001
SW_SHOWNORMAL = 1

#: 默认桌面名。起这个名字而不是随机名，是为了让
#: 「上次异常退出留下的桌面」在任务管理器/调试器里能被一眼认出来。
DEFAULT_NAME = "QQAgentHidden"

#: 拿不到结果时用来占位，避免把 None 传进需要字符串的地方
UNKNOWN = "(未知)"

# ---------------------------------------------------------------- 函数签名
#
# 全部显式声明。`CreateDesktopW` 返回的是**句柄**，不声明 restype 会被
# ctypes 当成 C int 截断成 32 位 —— 在 64 位进程里这是个静默故障。
_user32.CreateDesktopW.argtypes = [LPCWSTR, LPCWSTR, ctypes.c_void_p,
                                   DWORD, DWORD, ctypes.c_void_p]
_user32.CreateDesktopW.restype = HANDLE
_user32.OpenDesktopW.argtypes = [LPCWSTR, DWORD, BOOL, DWORD]
_user32.OpenDesktopW.restype = HANDLE
_user32.CloseDesktop.argtypes = [HANDLE]
_user32.CloseDesktop.restype = BOOL
_user32.GetThreadDesktop.argtypes = [DWORD]
_user32.GetThreadDesktop.restype = HANDLE
_user32.SetThreadDesktop.argtypes = [HANDLE]
_user32.SetThreadDesktop.restype = BOOL
_user32.GetUserObjectInformationW.argtypes = [HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, DWORD,
                                              ctypes.POINTER(DWORD)]
_user32.GetUserObjectInformationW.restype = BOOL

_kernel32.GetCurrentThreadId.argtypes = []
_kernel32.GetCurrentThreadId.restype = DWORD
_kernel32.CloseHandle.argtypes = [HANDLE]
_kernel32.CloseHandle.restype = BOOL
_kernel32.WaitForSingleObject.argtypes = [HANDLE, DWORD]
_kernel32.WaitForSingleObject.restype = DWORD
_kernel32.GetExitCodeProcess.argtypes = [HANDLE, ctypes.POINTER(DWORD)]
_kernel32.GetExitCodeProcess.restype = BOOL

#: `GetExitCodeProcess` 在进程还活着时返回这个值（不是 0，也不是 -1）
STILL_ACTIVE = 259


class STARTUPINFOW(ctypes.Structure):
    """`STARTUPINFOW`（必须是 W 版 —— A 版在中文 Windows 上会把路径写坏）。"""

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


_kernel32.CreateProcessW.argtypes = [
    LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    BOOL, DWORD, ctypes.c_void_p, LPCWSTR,
    ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)]
_kernel32.CreateProcessW.restype = BOOL


# ============================================================ 桌面句柄保活
#
# 为什么要留这个注册表：桌面对象的生命周期由**句柄**决定。
# 我们建完桌面、把 QQ 和宿主丢上去之后就撒手不管的话，
# 一旦本进程的句柄被垃圾回收/关闭，桌面就可能进销毁流程 ——
# 表现是「刚启动好的 QQ 毫无征兆地消失」。
# 所以：谁建的谁留着，进程活着桌面就活着。
_KEEP: dict[str, int] = {}


def _last_error() -> str:
    err = ctypes.get_last_error()
    return f"{err}（{ctypes.FormatError(err)}）"


# ============================================================ 当前桌面
def name_of(handle: int) -> str:
    """取一个桌面句柄的名字。取不到返回 UNKNOWN（不抛异常，诊断路径要能用）。"""
    if not handle:
        return UNKNOWN
    buf = ctypes.create_unicode_buffer(512)
    need = DWORD()
    if _user32.GetUserObjectInformationW(handle, UOI_NAME, buf, 512,
                                         ctypes.byref(need)):
        return buf.value
    return UNKNOWN


def current_name() -> str:
    """
    当前**线程**所在桌面的名字。

    这个函数给两个地方用，都是刚需：

      · 宿主进程启动后自报「我到底落在哪张桌面上」—— 这是验证
        `lpDesktop` 生效的**唯一**可信证据（命令行参数只是个请求，
        桌面归属由内核在创建进程时决定，两者不一致的情况真的存在）
      · 壳进程体检时确认自己还在用户桌面上
    """
    h = _user32.GetThreadDesktop(_kernel32.GetCurrentThreadId())
    return name_of(h) if h else UNKNOWN


# ============================================================ 创建 / 打开 / 关闭
def _probe_exists(name: str) -> bool:
    """一次性探测：这张桌面现在存不存在。拿到的句柄随手还掉，不进保活表。"""
    h = _user32.OpenDesktopW(name, 0, False, DESKTOP_READOBJECTS)
    if not h:
        return False
    _user32.CloseDesktop(h)
    return True


def create(name: str = DEFAULT_NAME, *, keep: bool = True) -> dict:
    """
    创建一张桌面。同名已存在时**返回已有的那张**（不报错、不重建）——
    这是 Windows 的行为，也正是我们要的：重启壳不该丢掉原来那张桌面上的 QQ。

    `keep=True` 时把句柄登记进保活表（见 `_KEEP` 的说明）。

    返回 `{ok, handle, name, existed, error}`。不抛异常 ——
    这条路径的调用方是启动流程，它需要的是「能不能继续」而不是一个回溯。

    `existed=True` 的含义是「建之前就有」——也就是**上一次运行留下的桌面**。
    这个信息有用：它说明那张桌面上可能还有上次没退干净的 QQ，
    值得在启动流程里提示一句，而不是让人纳闷「怎么一打开就有个 QQ」。
    """
    existing = _KEEP.get(name)
    if existing:
        return {"ok": True, "handle": existing, "name": name, "existed": True,
                "error": ""}
    existed = _probe_exists(name)
    h = _user32.CreateDesktopW(name, None, None, 0, GENERIC_ALL, None)
    if not h:
        return {"ok": False, "handle": 0, "name": name, "existed": existed,
                "error": f"CreateDesktopW 失败：{_last_error()}"}
    if keep:
        prev = _KEEP.get(name)
        if prev:
            # 竞态：两个线程同时建。关掉后建的那个，用先登记的。
            _user32.CloseDesktop(h)
            return {"ok": True, "handle": prev, "name": name, "existed": True,
                    "error": ""}
        _KEEP[name] = h
    return {"ok": True, "handle": h, "name": name, "existed": existed,
            "error": ""}


def open_existing(name: str = DEFAULT_NAME) -> dict:
    """打开一张已存在的桌面（不创建）。用于「桌面是上一次运行留下的」这种情形。"""
    cached = _KEEP.get(name)
    if cached:
        return {"ok": True, "handle": cached, "name": name, "error": ""}
    h = _user32.OpenDesktopW(name, 0, False, DESKTOP_READOBJECTS)
    if not h:
        return {"ok": False, "handle": 0, "name": name,
                "error": f"OpenDesktopW({name}) 失败：{_last_error()}"
                         "（多半是这张桌面还没被建过）"}
    return {"ok": True, "handle": h, "name": name, "error": ""}


def exists(name: str = DEFAULT_NAME) -> bool:
    if name in _KEEP:
        return True
    r = open_existing(name)
    if r["ok"]:
        # open_existing 拿到的一次性句柄用完要还 —— 只有 _KEEP 里的才留着
        if name not in _KEEP:
            _user32.CloseDesktop(r["handle"])
        return True
    return False


def close(name: str = DEFAULT_NAME) -> bool:
    """
    关闭我们持有的保活句柄。

    ## 语义（2026-09-26 更新：用户拍板反转，与 R4 统一）

    原设计刻意「不带走 QQ」；现在停止链路（host._cleanup_after_stop）的
    语义是**宿主退场 = QQ 一起收 + 桌面销毁**。QQ 死透 + 宿主已退出之后，
    本函数放掉的就是最后一批引用 —— CloseDesktop 之后桌面对象引用归零，
    **桌面被销毁**（exists() 探测随之失败）。

    返回 False = 本进程的保活表里没有这个句柄（比如壳重启过），不算错误：
    只要 QQ 和宿主都死了，桌面会因引用归零而自行销毁，不依赖本调用。
    """
    h = _KEEP.pop(name, None)
    if not h:
        return False
    return bool(_user32.CloseDesktop(h))


def set_thread_desktop(handle: int) -> dict:
    """
    把**当前线程**切到指定桌面。

    ## 什么时候该用它、什么时候不该

    R4 的主路径**不用**它：宿主进程是创建时带 `lpDesktop` 起的，
    一出生就在那张桌面上，比切过去更干净（切过去还要处理
    「线程已经拥有窗口/钩子就不能再切」这个限制）。

    它的正当用途是「壳想开一条工作线程直接把活儿干在隐藏桌面上」——
    那条线程必须①还没创建过任何窗口、②没装过任何钩子，
    否则 `SetThreadDesktop` 会失败（错误 170「资源正在使用」）。
    而且**线程退出前不能把它切回来**：一旦线程在桌面上拥有过窗口，
    桌面对象就会一直被这条线程引用住。

    失败时返回错误字符串，不抛异常。
    """
    if not handle:
        return {"ok": False, "error": "句柄为空"}
    if not _user32.SetThreadDesktop(handle):
        return {"ok": False,
                "error": f"SetThreadDesktop 失败：{_last_error()}"
                         "（该线程已经创建过窗口或装过钩子时必然失败）"}
    return {"ok": True, "error": "", "name": name_of(handle)}


# ============================================================ 把进程启动到指定桌面
def spawn(exe: str, args: list[str] | str = (), *,
          desktop: str | None = None, cwd: str | None = None,
          flags: int = CREATE_NO_WINDOW) -> dict:
    """
    在指定桌面上启动进程。

    `desktop=None` 表示**不指定** —— 进程落在调用者当前桌面上。
    这不是「没用」，而是**对照组**：同一套消息序列在用户桌面上跑一遍，
    就能把「消息序列本身不对」与「缺少前台」这两个变量拆开
    （上一轮做过这个对照，见归档的探针脚本）。

    ## 为什么必须用 CreateProcessW 而不是 subprocess

    `subprocess.Popen` **没有** `lpDesktop` 这个开关 —— 它不暴露
    `STARTUPINFOW`。想指定桌面就只能自己调 `CreateProcessW`。
    代价是拿不到 stdout 管道（本模块故意不接管子进程输出）：
    宿主进程靠**自己写文件/写心跳**把结果传出来，而不是靠管道。
    这也让宿主与壳的生命周期彻底解耦 —— 壳崩了宿主还能继续跑。

    ## 返回

    `{ok, pid, hproc, desktop, exe, args, error}`
    `hproc` 是进程句柄（已 `CloseHandle` 过的那个由调用方负责，见 `wait`）。
    """
    if not exe or not os.path.isfile(exe):
        return {"ok": False, "pid": 0, "hproc": 0, "desktop": desktop or "",
                "error": f"可执行文件不存在：{exe or '(空)'}"}
    arglist = [args] if isinstance(args, str) else list(args)
    # 命令行要把 exe 名字也放进去（CreateProcessW 的约定），
    # 每个参数各自加引号 —— 路径里有空格时漏一个引号就会解析成两个参数。
    cmdline = " ".join([f'"{exe}"'] + [f'"{a}"' if (" " in a or not a) else a
                                       for a in arglist])
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    si.lpDesktop = desktop or None
    si.dwFlags = STARTF_USESHOWWINDOW
    si.wShowWindow = SW_SHOWNORMAL
    pi = PROCESS_INFORMATION()
    buf = ctypes.create_unicode_buffer(cmdline)
    ok = _kernel32.CreateProcessW(exe, buf, None, None, False, flags,
                                  None, cwd, ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        return {"ok": False, "pid": 0, "hproc": 0, "desktop": desktop or "",
                "exe": exe, "args": arglist,
                "error": f"CreateProcessW 失败：{_last_error()}"}
    return {"ok": True, "pid": int(pi.dwProcessId), "hproc": int(pi.hProcess),
            "tid": int(pi.dwThreadId),
            "desktop": desktop or "(当前桌面)", "exe": exe, "args": arglist,
            "error": ""}


def wait(hproc: int, timeout: float) -> int:
    """
    等进程句柄。返回 0 = **已退出**，258 = 超时。

    ⚠️ 这里的 0 **不是退出码**（`WaitForSingleObject` 只回答「等到了没有」）。
    想知道子进程为什么退出，要再调一次 `exit_code()`。
    这两件事混起来会造成一种很典型的误判：子进程崩了（退出码 1）
    却被当成「正常结束」，于是去找根本不存在的问题。

    **调用方负责 CloseHandle**（本模块不在 wait 里关，因为同一个句柄可能还要再用）。
    """
    if not hproc:
        return 258
    return int(_kernel32.WaitForSingleObject(hproc, int(max(0.0, timeout) * 1000)))


def exit_code(hproc: int) -> int | None:
    """取进程退出码。返回 None = 句柄无效；返回 `STILL_ACTIVE` = 进程还在跑。"""
    if not hproc:
        return None
    code = DWORD()
    if not _kernel32.GetExitCodeProcess(hproc, ctypes.byref(code)):
        return None
    return int(code.value)


def close_handle(hproc: int) -> None:
    if hproc:
        _kernel32.CloseHandle(hproc)


def describe() -> dict:
    """给诊断报告用：当前线程在哪张桌面、保活了哪些桌面。"""
    return {
        "current_desktop": current_name(),
        "kept": {n: int(h) for n, h in _KEEP.items()},
        "default_name": DEFAULT_NAME,
    }


def wait_for_desktop_ready(name: str = DEFAULT_NAME, timeout: float = 2.0) -> bool:
    """
    建完桌面后确认它真的可用（能 OpenDesktop 到）。

    为什么要这一步：`CreateDesktopW` 成功只说明对象建出来了，
    而紧接着就要往上丢进程。中间加一次「真的能打开」的确认，
    能把「桌面没建好」与「进程启动失败」这两类失败分开报 ——
    否则用户拿到 `CreateProcessW 失败` 会去查 QQ 路径，纯属白折腾。
    """
    deadline = time.time() + max(0.0, timeout)
    while True:
        r = open_existing(name)
        if r["ok"]:
            if name not in _KEEP:
                _user32.CloseDesktop(r["handle"])
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.1)
