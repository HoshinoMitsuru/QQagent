# -*- coding: utf-8 -*-
"""
实验：对**用户桌面上、处于后台**的真实 QQ，只用窗口消息把文本写进输入框，
      全程**不调用任何激活 API**（不 AttachThreadInput、不 SetActiveWindow、不 SetForegroundWindow）。

## 为什么这个实验比隐藏桌面那套更重要

项目此前有一条认知（写在 `agent.py:3713`）：

    「chromium 在窗口被最小化/完全遮挡时会节流 renderer，
      此时 Invoke() 会静默失效…」—— 于是"写文本必须抢前台"成了默认前提，
    `_prepare_qq_for_switch()` / `force_foreground()` 那一整套都建立在这上面。

但隐藏桌面实验（`probe_hidden_desktop.py --focus-plan none`）证明了一件事：

    **在那张桌面上，前台恒为 NULL、什么激活动作都不做，`WM_CHAR` 照样写进输入框。**

那就顺出一个必须回答的问题：
**这个"免前台"是隐藏桌面独有的，还是窗口消息本身就不需要前台？**

- 若**窗口消息本身不需要前台** → `force_foreground()` 这套可以整个拆掉，
  agent 从此不再抢用户的焦点，"按键漏进终端"的坑一并消失。
  **收益立竿见影，且与 R4 无关 —— 不选 R4 也能拿到。**
- 若**只有隐藏桌面能免前台** → R4 的独有价值被坐实。

两者结论不同、动作不同，不能靠推测，必须实测。

## 安全边界（严格遵守）

  ① **只写输入框，绝不按 Enter、绝不点发送** —— 不会发出任何消息
  ② 写完立刻用 VK_BACK 抹掉，并读回确认为空
  ③ 输入框里**本来就有草稿**就中止（不碰用户的字）
  ④ 每个阶段前后都记录前台窗口，前台一旦被改变立刻报告
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

PROJ = r"D:\Psyche\Sealdice-AIChat\pc-agent-demo"
sys.path.insert(0, PROJ)
os.chdir(PROJ)

import uiautomation as auto  # noqa: E402

auto.UIAutomationInitializerInThread()

import agent as A  # noqa: E402

u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t

u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
u32.AttachThreadInput.restype = wintypes.BOOL
u32.SetFocus.argtypes = [wintypes.HWND]
u32.SetFocus.restype = wintypes.HWND
u32.GetFocus.restype = wintypes.HWND
u32.GetForegroundWindow.restype = wintypes.HWND
u32.SendMessageTimeoutW.argtypes = [wintypes.HWND, ctypes.c_uint, WPARAM, LPARAM,
                                    ctypes.c_uint, ctypes.c_uint,
                                    ctypes.POINTER(ctypes.c_size_t)]
u32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u32.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.LPARAM]
u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u32.IsWindowVisible.argtypes = [wintypes.HWND]
k32.GetCurrentThreadId.restype = wintypes.DWORD

WM_CHAR = 0x0102
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_BACK = 0x08
SMTO_ABORTIFHUNG = 0x0002
SEND_TIMEOUT_MS = 3000


def wtitle(h: int) -> str:
    b = ctypes.create_unicode_buffer(300)
    u32.GetWindowTextW(h, b, 300)
    return b.value


def send(hwnd: int, msg: int, wp: int, lp: int = 0) -> int:
    res = ctypes.c_size_t(0)
    return int(u32.SendMessageTimeoutW(hwnd, msg, wp, lp, SMTO_ABORTIFHUNG,
                                       SEND_TIMEOUT_MS, ctypes.byref(res)))


def find_renderer_child(hwnd: int) -> int:
    out: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(h, _lp):
        b = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(h, b, 256)
        if b.value == "Chrome_RenderWidgetHostHWND" and u32.IsWindowVisible(h):
            out.append(h)
        return True

    u32.EnumChildWindows(hwnd, _cb, 0)
    return out[0] if out else 0


def main() -> int:
    cfg = A.load_config()
    qq = A.QQWindow(cfg)
    if not qq.attach():
        code, ctx = qq.diagnose_attach()
        print(f"[X] attach 失败 {code} {ctx}")
        return 2

    hwnd = qq.hwnd
    tid_target = u32.GetWindowThreadProcessId(hwnd, None)
    tid_me = k32.GetCurrentThreadId()
    fg0 = u32.GetForegroundWindow()

    def is_empty() -> bool:
        """⚠️ 判"空"必须看 ClassName 里的 is-empty：
        输入框为空时 ProseMirror 会挂占位提示（"按住 Win + Alt，…"），
        collect_texts 会把它当正文读出来。"""
        try:
            return "is-empty" in (qq.editor.ClassName or "")
        except Exception:
            return not qq.editor_text().strip()

    print("=" * 76)
    print("实验：对用户桌面上处于后台的 QQ，纯窗口消息写入（不碰任何激活 API）")
    print("=" * 76)
    print(f"[窗口]   hwnd={hwnd}  title={wtitle(hwnd)!r}")
    print(f"[线程]   目标={tid_target}　本线程={tid_me}")
    print(f"[前台·0] hwnd={fg0} {wtitle(fg0)!r}")
    print(f"[QQ 是否在前台] {'是（本次实验前提不成立）' if fg0 == hwnd else '否 ✅ 在后台'}")
    print(f"[输入框] ClassName={qq.editor.ClassName!r}")
    print(f"         判定为空={is_empty()}　editor_text()={qq.editor_text()!r}（含占位符，仅供参考）")

    if not is_empty():
        print("\n[!] 输入框里有真实草稿，为不破坏你的内容，实验中止。请先清空 QQ 输入框。")
        return 3

    renderer = find_renderer_child(hwnd)
    print(f"[renderer 子窗口] {renderer}")

    results: list[dict] = []

    def stage(name: str, pre=None) -> dict:
        row: dict = {"stage": name, "fg_pre": u32.GetForegroundWindow()}
        attached = False
        try:
            if pre is not None:
                attached = pre()
            row["attached"] = attached
            row["focus_pre_send"] = u32.GetFocus()
            row["fg_before_send"] = u32.GetForegroundWindow()
            marker = f"NF{name}"
            t0 = time.time()
            rets = [send(hwnd, WM_CHAR, ord(c)) for c in marker]
            row["marker"] = marker
            row["returns"] = rets
            row["delivered"] = sum(1 for r in rets if r != 0)
            time.sleep(0.9)
            row["elapsed"] = round(time.time() - t0, 3)
            row["editor_text"] = qq.editor_text()
            row["is_empty_after"] = is_empty()
            row["landed"] = (not row["is_empty_after"]) and A.text_matches(
                marker, row["editor_text"])
            row["fg_after"] = u32.GetForegroundWindow()
            row["fg_changed"] = row["fg_after"] != fg0
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if attached:
                try:
                    u32.AttachThreadInput(tid_me, tid_target, False)
                except Exception:
                    pass
        # 清理：不管成败都抹掉，绝不把内容留在用户的输入框里
        for _ in range(len(row.get("marker", "")) + 6):
            send(hwnd, WM_KEYDOWN, VK_BACK)
            send(hwnd, WM_KEYUP, VK_BACK)
            time.sleep(0.03)
        time.sleep(0.5)
        row["cleared"] = is_empty()
        row["fg_final"] = u32.GetForegroundWindow()
        results.append(row)
        return row

    def report(row: dict) -> None:
        print(f"\n--- 阶段 {row['stage']} ---")
        print(f"  发送前：前台={row.get('fg_before_send')}　焦点={row.get('focus_pre_send')}"
              f"　attached={row.get('attached')}")
        print(f"  投递：{row.get('marker')!r}　返回值={row.get('returns')}"
              f"　对端处理 {row.get('delivered')} 个")
        print(f"  读回：editor_text={row.get('editor_text')!r}")
        print(f"  ★ 是否写入成功 = {'是 ✅' if row.get('landed') else '否 ❌'}")
        print(f"  ★ 前台是否被改变 = {'是 ❌' if row.get('fg_changed') else '否 ✅'}"
              f"（{row.get('fg_after')}）")
        print(f"  清理：已清空={row.get('cleared')}")
        if row.get("error"):
            print(f"  [X] {row['error']}")

    # 阶段 1：什么都不碰 —— 这是核心问题
    report(stage("plain"))

    # 阶段 2：仅在阶段 1 失败时才补一层 renderer 子窗口焦点（用 AttachThreadInput 才能跨线程 SetFocus）
    if not results[-1].get("landed"):
        print("\n[阶段 1 未成功] 追加阶段 2：AttachThreadInput + SetFocus(renderer 子窗口)"
              "（注意：不调用 SetActiveWindow / SetForegroundWindow）")

        def pre2() -> bool:
            ok = bool(u32.AttachThreadInput(tid_me, tid_target, True))
            if renderer:
                u32.SetFocus(renderer)
            return ok

        report(stage("a11yfocus", pre2))

    print("\n" + "=" * 76)
    print("汇总")
    print("=" * 76)
    for row in results:
        print(f"  {row['stage']:<12} 写入={'成功 ✅' if row.get('landed') else '失败 ❌'}"
              f"　前台被改={'是 ❌' if row.get('fg_changed') else '否 ✅'}"
              f"　已清空={row.get('cleared')}")
    print(f"\n[最终前台] hwnd={u32.GetForegroundWindow()} "
          f"{wtitle(u32.GetForegroundWindow())!r}"
          f"　（起始 = {fg0} {wtitle(fg0)!r}）")

    plain_ok = results[0].get("landed")
    print("\n" + "-" * 76)
    if plain_ok:
        print("结论：**窗口消息写入不需要前台**，连隐藏桌面都不需要。")
        print("      → agent 的 force_foreground() 在\"写文本\"这件事上是多余的，")
        print("        「抢用户前台」这个副作用可以从根上删掉。")
    else:
        print("结论：用户桌面上后台窗口**收不到** WM_CHAR（消息被 Chromium 丢弃）。")
        print("      → 写文本确实需要窗口处于\"被 Chromium 视为 active\"的状态；")
        print("        隐藏桌面天然满足这一点，这就是 R4 的独有价值。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
