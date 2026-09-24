# -*- coding: utf-8 -*-
"""
probe_switch_nofg.py — 实测：切换会话能不能**不用前台**？

## 为什么要测这个

`probe_minimized.py` 已经证明：**最小化不影响 UIA 读取**（节点数/会话列表/消息/输入框
全都照常）。所以「读取」这一侧根本不依赖窗口可见。

那前台依赖到底还剩什么？逐条过一遍发送事务：

| 步骤 | 用的机制 | 需要前台？ |
| --- | --- | --- |
| 切到目标会话 | `session.item.Click()` ← **坐标点击** | ✅ 需要（且需在最上层） |
| 写文本 | 剪贴板 + `SendKeys(Ctrl+V)` | ✅ 需要 |
| 清空输入框 | `SendKeys(Ctrl+A / Delete)` | ✅ 需要 |
| 点发送 | `InvokePattern.Invoke()` | ❌ 不需要 |
| 读消息 / 读签名 | UIA 属性读取 | ❌ 不需要 |

也就是说，「前台」这个约束**几乎全压在切会话和写文本这两步上**。
其中**切会话**最容易被误解成"必须点击" —— 但列表项完全可能支持
`InvokePattern` / `SelectionItemPattern`，这两个都是**免前台**的：

- `InvokePattern.Invoke()` —— UIA 语义上的"点一下这个控件"；
- `SelectionItemPattern.Select()` —— "选中它"，列表实现得好就是切过去；
- `LegacyIAccessiblePattern.DoDefaultAction()` —— MSAA 时代的默认动作。

都不依赖前台、不移动光标。只要 QQNT 实现了任意一个，切会话就不需要前台。

## 它测什么

1. 目标列表项 + 列表容器各暴露了哪些 Pattern（`GetPattern(patternId)` 逐个试）；
2. QQ **不在前台**时，逐个尝试 Invoke / Select / DoDefaultAction，看标题有没有真变；
3. **最小化**后再试一遍；
4. 对照组：最小化状态下用坐标点击（预期失败）；
5. 不管成败都切回原来那个会话。

会真实切换会话（可逆），**不发送任何消息**。

用法：
    python -X utf8 probe_switch_nofg.py
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes

import agent as A
import qqid as Q

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_u32.IsIconic.argtypes = [wintypes.HWND]
_u32.IsIconic.restype = wintypes.BOOL

SW_MINIMIZE = 6
SW_RESTORE = 9

# uiautomation 2.0.29 的真实入口是 GetPattern(patternId)；
# 没有 IsXxxPatternAvailable 这类属性（踩过：直接写会 AttributeError）。
PATTERN_IDS = {
    "Invoke": 10000,
    "Selection": 10001,
    "Value": 10002,
    "ExpandCollapse": 10005,
    "Scroll": 10004,
    "SelectionItem": 10010,
    "ScrollItem": 10017,
    "LegacyIAccessible": 10018,
    "VirtualizedItem": 10020,
}


def probe_patterns(ctrl) -> dict:
    """逐个问：这个控件支持哪些 Pattern。返回 {名字: 代理对象 或 '不支持/exc'}. """
    out = {}
    for name, pid in PATTERN_IDS.items():
        try:
            out[name] = ctrl.GetPattern(pid)
        except Exception as exc:
            out[name] = f"✗{type(exc).__name__}"
    return out


def describe(pat) -> str:
    if isinstance(pat, str):
        return pat
    if pat is None:
        return "None"
    return type(pat).__name__


def push_focus_away(qq_hwnd: int) -> None:
    """把前台让给别的窗口，确保实验条件真的是「QQ 不在前台」。"""
    if not A._is_foreground(qq_hwnd):
        return
    for w in A._enum_top_windows():
        r = w.get("rect") or [0, 0, 0, 0]
        if not w["visible"] or w["hwnd"] == qq_hwnd:
            continue
        if w["class"] in ("IME", "MSCTFIME UI", "Base_PowerMessageWindow",
                          "Electron_NotifyIconHostWindow"):
            continue
        if r[2] - r[0] < 400 or r[3] - r[1] < 300:
            continue
        if A.force_foreground(w["hwnd"], retries=2):
            time.sleep(0.4)
            if not A._is_foreground(qq_hwnd):
                print(f"  （已把前台让给 {w['title'][:40]!r} 以制造非前台条件）")
                return


def wait_title(title_now, want: str, timeout: float = 6.0) -> bool:
    """
    轮询等标题变成 want。

    ⚠️ **必须轮询，不能用固定 sleep**。第一版这里等了固定 1.0s，
    结果把一次**其实成功了**的最小化切换判成了失败（Chromium 最小化时
    渲染被节流，状态反映会慢好几百毫秒到几秒）。后来是 C 阶段
    "点击前标题已经是目标"这句话暴露了这个误判 —— 和 `click_session`
    那个 bug 是同一类错误：**验证方式本身不可靠，就会得出反向结论**。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (title_now() or "") == want:
            return True
        time.sleep(0.25)
    return (title_now() or "") == want


def try_actions(item, title_now, want: str) -> list:
    """
    逐个尝试免前台动作。返回 [(动作名, 是否真切换成功)]。

    **只认标题真的变没变，返回值一律不作数** —— 坐标点击那次翻车就是
    因为 `click_session` 返回 True 而标题从没动过。
    """
    results = []

    def attempt(label: str, fn) -> bool:
        try:
            fn()
        except Exception as exc:
            results.append((f"{label} [{type(exc).__name__}]", False))
            return False
        ok = wait_title(title_now, want)
        results.append((label, ok))
        return ok

    for name, mid in (("InvokePattern.Invoke()", "Invoke"),
                      ("SelectionItemPattern.Select()", "SelectionItem"),
                      ("LegacyIAccessible.DoDefaultAction()", "LegacyIAccessible")):
        try:
            pat = item.GetPattern(PATTERN_IDS[mid])
        except Exception:
            results.append((f"{name} [Pattern 不支持]", False))
            continue
        if name.startswith("Invoke"):
            if attempt(name, lambda p=pat: p.Invoke()):
                return results
        elif name.startswith("SelectionItem"):
            if attempt(name, lambda p=pat: p.Select()):
                return results
        else:
            if attempt(name, lambda p=pat: p.DoDefaultAction()):
                return results

    # 最后一招：先 SetFocus 再 Select（有些实现要先拿焦点）
    def focus_then_select():
        item.SetFocus()
        time.sleep(0.25)
        item.GetPattern(PATTERN_IDS["SelectionItem"]).Select()

    if attempt("SetFocus + Select()", focus_then_select):
        return results
    return results


def switch_to(qq, name: str) -> bool:
    """
    用**免前台的 InvokePattern** 切到指定会话（实验里已证明可行）。
    这比 force_foreground + 坐标点击干净得多，也正好顺带再验证一次。
    """
    if (qq.title_now() or "") == name:
        return True
    for s in Q.list_sessions(qq.win):
        if s.display_name != name:
            continue
        try:
            s.item.GetPattern(PATTERN_IDS["Invoke"]).Invoke()
        except Exception as exc:
            print(f"      Invoke 切到 {name!r} 失败：{type(exc).__name__}")
            return False
        return wait_title(qq.title_now, name, timeout=6.0)
    return False


def main() -> int:
    print("=" * 96)
    print("实验：切换会话能不能不用前台？")
    print("=" * 96)

    cfg = A.load_config()
    qq = A.QQWindow(cfg)
    if not qq.attach():
        print("  没找到 QQ 窗口")
        return 2
    if A._desktop_locked():
        print("  ⚠ 桌面处于锁屏状态，结论会被污染。建议先解锁再跑。")

    original = qq.title_now()
    sessions = Q.list_sessions(qq.win)
    print(f"  当前打开 = {original!r}   会话数 = {len(sessions)}")
    print(f"  QQ 在前台 = {A._is_foreground(qq.hwnd)}   前台窗口 = {A._foreground_title()!r}")

    target = next((s for s in sessions if s.display_name != original), None)
    if target is None:
        print("  没有别的会话可当目标")
        return 2
    print(f"  目标会话 = {target.display_name!r}")

    print("\n  ── 目标列表项支持的 Pattern ──")
    for k, v in probe_patterns(target.item).items():
        print(f"     {k:20s} : {describe(v)}")

    print("\n  ── 列表容器（父级）支持的 Pattern ──")
    try:
        parent = target.item.GetParentControl()
        print(f"     class = {parent.ClassName!r}")
        for k, v in probe_patterns(parent).items():
            print(f"     {k:20s} : {describe(v)}")
    except Exception as exc:
        print(f"     取父控件失败：{exc}")

    print("\n  ── 祖先链上谁支持 Selection / ScrollItem（能切会话的关键） ──")
    try:
        node, depth = target.item, 0
        while node is not None and depth < 6:
            sel = None
            try:
                sel = node.GetPattern(PATTERN_IDS["Selection"])
            except Exception:
                pass
            si = None
            try:
                si = node.GetPattern(PATTERN_IDS["SelectionItem"])
            except Exception:
                pass
            try:
                cls = node.ClassName
            except Exception:
                cls = "?"
            print(f"     L{depth} {cls[:58]:60s} Selection={describe(sel) if sel else '-':26s} "
                  f"SelectionItem={describe(si) if si else '-'}")
            try:
                node = node.GetParentControl()
            except Exception:
                break
            depth += 1
    except Exception as exc:
        print(f"     遍历祖先失败：{exc}")

    all_results = {}

    # ============================================ A. 非前台（窗口正常显示）
    print("\n" + "=" * 96)
    print("A. QQ 不在前台、窗口正常显示")
    print("=" * 96)
    push_focus_away(qq.hwnd)
    print(f"  前置条件：QQ 在前台 = {A._is_foreground(qq.hwnd)}（应为 False）"
          f"   前台 = {A._foreground_title()!r}")
    res_a = try_actions(target.item, qq.title_now, target.display_name)
    for name, ok in res_a:
        print(f"     {name:52s} → {'✅ 切换成功' if ok else '❌ 没切过去'}")
    all_results["A 非前台·正常显示"] = res_a

    # ============================================ B. 最小化 + 非前台
    print("\n" + "=" * 96)
    print("B. 先切回原会话，再最小化窗口，然后重复")
    print("=" * 96)
    if (qq.title_now() or "") != original:
        print(f"  用免前台 Invoke 切回 {original!r} …")
        switch_to(qq, original)
    print(f"  已切回 = {qq.title_now()!r}")

    print("  → ShowWindow(SW_MINIMIZE)")
    _u32.ShowWindow(qq.hwnd, SW_MINIMIZE)
    time.sleep(1.5)
    print(f"     已最小化 = {bool(_u32.IsIconic(qq.hwnd))}   在前台 = {A._is_foreground(qq.hwnd)}")

    # 最小化后必须重新取列表项：控件引用可能已失效
    sessions_b = Q.list_sessions(qq.win)
    target_b = next((s for s in sessions_b if s.display_name == target.display_name), None)
    if target_b is None:
        print("     最小化后找不到目标会话，跳过 B")
        res_b = []
    else:
        print(f"     最小化后仍能读到列表项：{target_b.display_name!r}，rect={target_b.rect}")
        print(f"     （注意 QQ 窗口 rect = {qq.rect}，说明这个 item rect 是布局期缓存值）")
        res_b = try_actions(target_b.item, qq.title_now, target.display_name)
        for name, ok in res_b:
            print(f"     {name:52s} → {'✅ 切换成功' if ok else '❌ 没切过去'}")
    all_results["B 非前台·已最小化"] = res_b

    # ============================================ C. 对照组：最小化 + 坐标点击
    print("\n" + "=" * 96)
    print("C. 对照组：最小化状态下用坐标点击（预期失败）")
    print("=" * 96)
    # 必须先确保当前**不是**目标会话，否则"点击后标题=目标"是假成功
    # （第一版就踩了这个：点击前标题已经是目标，我还以为点击成功了）
    if (qq.title_now() or "") == target.display_name:
        print(f"  先把当前会话切回 {original!r}，否则对照组没有意义")
        if not switch_to(qq, original):
            print("     切不回去，跳过 C")
        time.sleep(0.5)
    if (qq.title_now() or "") == target.display_name:
        print("     当前仍是目标会话，无法做对照，跳过 C")
    else:
        print(f"     点击前标题 = {qq.title_now()!r}（≠ {target.display_name!r}，条件成立）")
        sessions_c = Q.list_sessions(qq.win)
        target_c = next((s for s in sessions_c
                         if s.display_name == target.display_name), None)
        if target_c is None:
            print("     拿不到目标项，跳过 C")
        else:
            try:
                target_c.item.Click(simulateMove=False)
                ok = wait_title(qq.title_now, target.display_name, timeout=4.0)
                print(f"     点击后标题 = {qq.title_now()!r}  → "
                      f"{'✅ 居然成功了' if ok else '❌ 没切过去（预期）'}")
                print(f"     该列表项 rect = {target_c.rect}  ← 最小化后这个坐标是布局期缓存，"
                      f"点它等于在屏幕上随便点一下")
            except Exception as exc:
                print(f"     ❌ 点击抛异常：{type(exc).__name__}: {exc}")

    # ============================================ 复原
    print("\n" + "=" * 96)
    print("复原")
    print("=" * 96)
    if _u32.IsIconic(qq.hwnd):
        _u32.ShowWindow(qq.hwnd, SW_RESTORE)
        time.sleep(1.2)
    if (qq.title_now() or "") != original:
        switch_to(qq, original)
    print(f"  已最小化 = {bool(_u32.IsIconic(qq.hwnd))}   当前会话 = {qq.title_now()!r} "
          f"（期望 {original!r}）")

    # ============================================ 判定
    print("\n" + "=" * 96)
    print("判定")
    print("=" * 96)
    for phase, res in all_results.items():
        if not res:
            print(f"  {phase}: 无结果")
            continue
        best = next((n for n, ok in res if ok), "")
        print(f"  {phase}: {'✅ 免前台可切换 —— ' + best if best else '❌ 所有免前台动作都没切过去'}")
    print()
    print("  说明：只有『标题真的变了』才算成功；返回值一律不作数。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
