# -*- coding: utf-8 -*-
"""
platform_win.py —— 与 Windows 打交道的那一层

只放「不依赖 uiautomation」的底层动作，理由很实际：**WebUI 必须能在 uiautomation 缺失
或 QQ 没起来的情况下正常打开**，否则用户看到的是一个双击就闪退的 exe，连报错都读不到。

包含：管理员检测与提权、单实例互斥、端口挑选、锁屏检测、进程枚举与结束、
任务计划自启、VM 加固（关自动锁屏/休眠）。
"""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import time
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

# ---------------------------------------------------------------- 函数原型
# 这些声明不是「顺手写全」的形式主义，是 64 位下的正确性要求：
# 未声明时 ctypes 会把返回值当 32 位 int、把参数当 32 位 int。
# 而 HWND / HANDLE / 桌面句柄在这里都是**指针宽度**的量 —— 结果就是句柄被截断，
# 然后我们拿着一个残缺的句柄去 CloseHandle / GetWindowText，行为不可预测。
# 这类问题在句柄值较小时完全看不出来，属于「跑很久才炸一次」的那种。
_HWND_T = wintypes.HWND
_HANDLE_T = wintypes.HANDLE
_LPARAM_T = wintypes.LPARAM
_WPARAM_T = wintypes.WPARAM

_user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_user32.OpenInputDesktop.restype = _HANDLE_T
_user32.CloseDesktop.argtypes = [_HANDLE_T]
_user32.GetForegroundWindow.restype = _HWND_T
_user32.GetWindowTextLengthW.argtypes = [_HWND_T]
_user32.GetWindowTextLengthW.restype = ctypes.c_int
_user32.GetWindowTextW.argtypes = [_HWND_T, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowTextW.restype = ctypes.c_int
_user32.GetClassNameW.argtypes = [_HWND_T, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowRect.argtypes = [_HWND_T, ctypes.c_void_p]
_user32.IsWindowVisible.argtypes = [_HWND_T]
_user32.IsIconic.argtypes = [_HWND_T]
_user32.GetWindowThreadProcessId.argtypes = [_HWND_T, ctypes.c_void_p]
_user32.EnumWindows.argtypes = [ctypes.c_void_p, _LPARAM_T]
_user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
_user32.LoadIconW.restype = wintypes.HICON

_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = _HANDLE_T
_kernel32.Process32FirstW.argtypes = [_HANDLE_T, ctypes.c_void_p]
_kernel32.Process32NextW.argtypes = [_HANDLE_T, ctypes.c_void_p]
_kernel32.CloseHandle.argtypes = [_HANDLE_T]
_kernel32.TerminateProcess.argtypes = [_HANDLE_T, wintypes.UINT]
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = _HANDLE_T

_shell32.IsUserAnAdmin.restype = wintypes.BOOL
_shell32.ShellExecuteW.argtypes = [_HWND_T, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
_shell32.ShellExecuteW.restype = _HANDLE_T

ERROR_ALREADY_EXISTS = 183

_mutex_handles: list = []       # 必须持有引用，被 GC 掉互斥体就释放了


# ============================================================ 管理员
def is_admin() -> bool:
    try:
        return bool(_shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_system_user() -> bool:
    """计划任务「不管用户是否登录都要运行」跑在 Session 0，此时没有前台权限。"""
    try:
        return os.environ.get("USERNAME", "").upper() in ("SYSTEM", "$SYSTEM")
    except Exception:
        return False


def relaunch_as_admin(extra_args: list[str] | None = None) -> bool:
    """
    用 runas 重新拉一份自己（提权）。成功返回 True —— 注意调用方应当**随后退出**，
    否则会同时存在两个实例（新实例抢不到单实例锁，会自己走「已在运行」分支）。
    """
    if is_admin():
        return False
    try:
        exe = sys.executable if getattr(sys, "frozen", False) else os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "qq-agent.py")
        if not getattr(sys, "frozen", False):
            # 源码模式下要提权的是 python.exe，参数里带上脚本
            params = subprocess.list2cmdline([exe] + list(extra_args or []))
            target = sys.executable
        else:
            params = subprocess.list2cmdline(list(extra_args or []))
            target = exe
        rc = _shell32.ShellExecuteW(None, "runas", target, params, os.getcwd(), 1)
        return int(rc) > 32
    except Exception:
        return False


# ============================================================ 单实例
def acquire_single_instance(name: str = "Global\\QQAgent-WebUI") -> bool:
    """拿不到就说明已经有一个在跑。返回 True = 我是唯一实例。"""
    try:
        h = _kernel32.CreateMutexW(None, False, name)
        if not h:
            return True
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            _kernel32.CloseHandle(h)
            return False
        _mutex_handles.append(h)
        return True
    except Exception:
        return True


def kill_pid(pid: int, tree: bool = True) -> bool:
    """
    结束一个进程（默认连子进程一起）。

    用 taskkill 而不是 TerminateProcess：壳会派生 agent 子进程，
    只杀父进程会把子进程丢下继续跑（那个子进程会一直占着 QQ 的输入框）。
    """
    if not pid or pid == os.getpid():
        return False
    try:
        import subprocess
        cmd = ["taskkill", "/F"]
        if tree:
            cmd.append("/T")
        cmd += ["/PID", str(pid)]
        r = subprocess.run(cmd, capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


# ============================================================ 端口
def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(preferred: int, host: str = "127.0.0.1", tries: int = 40) -> int:
    for p in range(preferred, preferred + tries):
        if is_port_free(p, host):
            return p
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def lan_ip() -> str:
    """拿本机在局域网里的地址，用于提示「从宿主机浏览器怎么打开」。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("223.5.5.5", 80))
            return s.getsockname()[0]
    except Exception:
        return ""


# ============================================================ 桌面状态
def desktop_state() -> str:
    """
    'unlocked' / 'locked' / 'unknown'

    原理：OpenInputDesktop 只有在「当前进程所在的桌面就是用户正在输入的桌面」时才成功。
    锁屏后输入桌面会切到 Winlogon，这个调用就失败了 —— 这是判断锁屏最可靠的办法，
    比看前台窗口类名靠谱（那个会被各种全屏程序干扰）。
    """
    try:
        DESKTOP_SWITCHDESKTOP = 0x0100
        h = _user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if h:
            _user32.CloseDesktop(h)
            return "unlocked"
        return "locked"
    except Exception:
        return "unknown"


def foreground_title() -> str:
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        n = _user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


# ============================================================ 进程
class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def list_processes(names: tuple[str, ...] | None = None) -> list[dict]:
    """按进程名列出进程（不区分大小写）。names=None 表示全部。"""
    out: list[dict] = []
    want = {n.lower() for n in names} if names else None
    TH32CS_SNAPPROCESS = 0x00000002
    INVALID = ctypes.c_void_p(-1).value
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID or not snap:
        return out
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = entry.szExeFile
            if want is None or name.lower() in want:
                out.append({"pid": int(entry.th32ProcessID), "name": name})
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    except Exception:
        pass
    finally:
        _kernel32.CloseHandle(snap)
    return out


def pid_alive(pid: int) -> bool:
    """
    这个 pid 现在还有没有对应进程。便宜（一次 OpenProcess），可以放心轮询。

    ## 为什么不用 list_processes 去比对

    那是整机快照 + 遍历，比这里贵两个数量级；而这里只是「宿主还在吗」这种
    每秒都要问一次的问题。

    ## 为什么不拿它当唯一判据

    pid 会**复用**：进程死了之后，同一个数字可能被一个完全无关的进程拿到。
    所以它的正确用法是「配合心跳时间戳一起看」（见 `host.daemon_status`），
    单独用它判断「我们那个进程还在」是会出错的。

    另外：僵尸进程（已退出但句柄没关）用 `PROCESS_QUERY_LIMITED_INFORMATION`
    仍能打开，所以这里再补一道 `GetExitCodeProcess`，还在跑才算活着。
    """
    if not pid:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE,
                                             ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not _kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return int(code.value) == 259      # STILL_ACTIVE
    except Exception:
        return False
    finally:
        _kernel32.CloseHandle(h)


def process_path(pid: int) -> str:
    """取进程的完整路径。QQ 的主窗口进程常常取不到（权限/位数），失败返回空串。"""
    if not pid:
        return ""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    except Exception:
        return ""
    finally:
        _kernel32.CloseHandle(h)


def process_command_lines(names: tuple[str, ...]) -> list[dict]:
    """
    读进程命令行（用来确认 QQ 到底有没有带 --force-renderer-accessibility）。

    这是**诊断用**的慢操作（PowerShell 起一个进程约 1 秒），不要放进状态轮询里。
    """
    if not names:
        return []
    cond = " OR ".join(f"Name='{n}'" for n in names)
    ps = (
        f"Get-CimInstance Win32_Process -Filter \"{cond}\" -ErrorAction SilentlyContinue | "
        f"ForEach-Object {{ $_.ProcessId.ToString() + [char]9 + $_.CommandLine }}"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        text = r.stdout.decode("utf-8", "replace") or ""
    except Exception:
        return []
    out = []
    for line in text.splitlines():
        if "\t" not in line:
            continue
        pid, cmd = line.split("\t", 1)
        pid = pid.strip()
        if pid.isdigit():
            out.append({"pid": int(pid), "cmdline": cmd.strip()})
    return out


def kill_processes(names: tuple[str, ...], wait_seconds: float = 6.0) -> int:
    """
    强杀指定进程名的所有进程，返回杀掉的个数。

    ⚠️ 只在「修复 QQ 启动参数」这个动作里调用，且必须先经用户确认 ——
    这会关掉用户正在用的 QQ（登录态会保留，但正在打的字会丢）。
    """
    targets = list_processes(names)
    if not targets:
        return 0
    PROCESS_TERMINATE = 0x0001
    killed = 0
    for row in targets:
        h = _kernel32.OpenProcess(PROCESS_TERMINATE, False, row["pid"])
        if h:
            try:
                if _kernel32.TerminateProcess(h, 1):
                    killed += 1
            finally:
                _kernel32.CloseHandle(h)
        else:
            for name in names:
                subprocess.run(["taskkill", "/F", "/PID", str(row["pid"])],
                               capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                killed += 1
                break
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not list_processes(names):
            break
        time.sleep(0.3)
    return killed


def launch_detached(args: list[str], cwd: str | None = None) -> int:
    """
    起一个与 UI 进程生命周期无关的进程（QQ 就是这种）。

    用 DETACHED_PROCESS 而不是 subprocess 默认方式：QQ 常驻到托盘、随时可能重启自己，
    让它挂在我们的进程树上，会让「关掉 UI 顺便把 QQ 也带走」这种意外发生。
    """
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    try:
        p = subprocess.Popen(
            args, cwd=cwd,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return p.pid
    except Exception:
        try:
            os.startfile(args[0])       # 最后兜底：至少把主程序拉起来（参数会丢）
            return -1
        except Exception:
            return 0


# ============================================================ 开机自启（计划任务）
AUTOSTART_TASK = "QQAgent-WebUI"


def autostart_status() -> dict:
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", AUTOSTART_TASK],
                           capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        ok = r.returncode == 0
        return {"installed": ok, "task": AUTOSTART_TASK}
    except Exception as exc:
        return {"installed": False, "task": AUTOSTART_TASK, "error": str(exc)}


def install_autostart(target_exe: str, args: str = "") -> dict:
    """
    注册登录自启。用计划任务而不是启动文件夹，是为了能带「最高权限」标记 ——
    以最高权限运行的登录任务**不会弹 UAC**，这对无人值守的 VM 是决定性的。
    """
    if not is_admin():
        return {"ok": False, "error": "需要管理员权限才能注册计划任务"}
    tr = f'"{target_exe}"' + (f" {args}" if args else "")
    try:
        r = subprocess.run(
            ["schtasks", "/Create", "/TN", AUTOSTART_TASK, "/TR", tr,
             "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return {"ok": r.returncode == 0,
                "output": (r.stdout or b"").decode("utf-8", "replace").strip(),
                "error": (r.stderr or b"").decode("utf-8", "replace").strip()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def remove_autostart() -> dict:
    if not is_admin():
        return {"ok": False, "error": "需要管理员权限"}
    try:
        r = subprocess.run(["schtasks", "/Delete", "/TN", AUTOSTART_TASK, "/F"],
                           capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"ok": r.returncode == 0,
                "error": (r.stderr or b"").decode("utf-8", "replace").strip()}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ============================================================ VM 加固
# 目的：让 guest 永远不会自己锁屏/休眠 —— 锁屏会让 UIA 的点击与按键全部失效。
# 全部走 powercfg / reg，都是可逆的，且**必须由用户在界面上显式确认**。
_POWER_CHANGES = [
    ("显示器自动关闭", ["powercfg", "/change", "monitor-timeout-ac", "0"]),
    ("硬盘自动关闭", ["powercfg", "/change", "disk-timeout-ac", "0"]),
    ("系统自动睡眠", ["powercfg", "/change", "standby-timeout-ac", "0"]),
    ("系统自动休眠", ["powercfg", "/change", "hibernate-timeout-ac", "0"]),
    ("电池下显示器关闭", ["powercfg", "/change", "monitor-timeout-dc", "0"]),
    ("电池下系统睡眠", ["powercfg", "/change", "standby-timeout-dc", "0"]),
]


def apply_vm_hardening() -> dict:
    if not is_admin():
        return {"ok": False, "error": "需要管理员权限（powercfg 的 /change 需要提权）"}
    results = []
    for label, cmd in _POWER_CHANGES:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            results.append({"item": label, "ok": r.returncode == 0})
        except Exception as exc:
            results.append({"item": label, "ok": False, "error": str(exc)})
    # 屏保 + 锁屏（NoLockScreen 需要专业版；家庭版失败不影响其它项）
    try:
        r = subprocess.run(
            ["reg", "add", r"HKCU\Control Panel\Desktop", "/v", "ScreenSaveActive",
             "/t", "REG_SZ", "/d", "0", "/f"],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        results.append({"item": "屏保", "ok": r.returncode == 0})
    except Exception as exc:
        results.append({"item": "屏保", "ok": False, "error": str(exc)})
    try:
        r = subprocess.run(
            ["reg", "add", r"HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization",
             "/v", "NoLockScreen", "/t", "REG_DWORD", "/d", "1", "/f"],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        results.append({"item": "锁屏界面（组策略，仅专业版生效）", "ok": r.returncode == 0})
    except Exception as exc:
        results.append({"item": "锁屏界面（组策略，仅专业版生效）", "ok": False, "error": str(exc)})
    ok = sum(1 for x in results if x.get("ok"))
    return {"ok": ok > 0, "applied": results, "ok_count": ok, "total": len(results),
            "note": "重启或重新登录后完全生效；锁屏/睡眠设置可在「电源和睡眠」里手动改回。"}


def open_in_explorer(path: str) -> bool:
    try:
        os.startfile(path)
        return True
    except Exception:
        return False


def clipboard_snapshot() -> str:
    """读剪切板文本，用于报告「我们的写入有没有覆盖用户内容」。"""
    try:
        import pyperclip
        return pyperclip.paste() or ""
    except Exception:
        return ""


def memory_str() -> dict:
    class _MEMSTATUS(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD),
                    ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    try:
        st = _MEMSTATUS()
        st.dwLength = ctypes.sizeof(_MEMSTATUS)
        if _kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            gb = 1024 ** 3
            return {"load_percent": int(st.dwMemoryLoad),
                    "total_gb": round(st.ullTotalPhys / gb, 1),
                    "avail_gb": round(st.ullAvailPhys / gb, 1)}
    except Exception:
        pass
    return {}
