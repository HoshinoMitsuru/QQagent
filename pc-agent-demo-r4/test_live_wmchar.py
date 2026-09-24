# -*- coding: utf-8 -*-
"""
test_live_wmchar.py —— U2 生死线验证：真实 QQ 的**消息编辑器**吃不吃窗口消息

## 为什么这一条是 V2 的生死线（不是一般的"验证杂事"）

V1 写文本收尾是 `auto.SendKeys("{Ctrl}v")`（agent.py:2422）—— 走 **OS 输入队列**，
所以必须先把 QQ 弄成前台。而 `SendInput` 只能投到**当前 input desktop**（用户那张），
进程线程若不在 input desktop 上根本投不进去。
→ **V1 的剪贴板通路在隐藏桌面上直接断掉，没有兜底。**
→ V2 唯一的写文本出路就是 `WM_CHAR` 直投。
→ 若真实 QQ 的编辑器不吃 `WM_CHAR`，**V2 架构即死**（CDP 已实测被客户端屏蔽）。

## 为什么必须专门验「编辑器」而不是"能写进去就行"

探针那次成功运行（`archive/host-result.json`）写入的是床页里的 `<textarea>`：
标题里 `ae=ta` 表示活动元素是 textarea，`te="R4中文测试"` 是它的值 ——
而床页**专门摆的** contenteditable（QQ 消息编辑器的对应物）拿到的是 **`td=""`（空）**。

这证明了一件事：字符只会进**活动元素**。
QQ 的消息编辑器是 contenteditable（ProseMirror），与 textarea 不是一回事。
所以"能投给 textarea"**不能**推出"能投给 QQ 的编辑器"。
本测试的回读走 `qq.editor_text()`，读的正是那个 contenteditable —— 判据对得上。

## 测法：阶梯式，从零副作用往上加

焦点改动是有副作用的（在用户桌面上等于偷用户的焦点），
所以按"最不打扰 → 最打扰"排序，每档都回读一次，先成功的那档就是推荐通路。

## 安全

- **测试前输入框必须是空的**，非空则直接中止 —— 绝不毁掉你正在打的草稿
- 全程**不碰发送按钮、不按 Enter**，绝不发出任何消息
- 清空优先走免前台的 `clear_editor_uia()`（ValuePattern.SetValue("")）
- 用到了前台的档位，结束时会把前台还给你原来的窗口

用法：
    python test_live_wmchar.py                    # 阶梯式全跑（第 5 档会跳过）
    python test_live_wmchar.py --allow-foreground # 连第 5 档一起跑（会短暂抢你的前台）
    python test_live_wmchar.py --only 5 --allow-foreground
    python test_live_wmchar.py --preview          # 不动手，只报告现场状态
"""

from __future__ import annotations

import os
import sys
import time

import agent as A
from app import winmsg

HERE = os.path.dirname(os.path.abspath(__file__))

#: 明显是测试串；中英混排是为了同时暴露"中文被丢"这个失败模式
MARKER = "wmchar-u2-测试"

OK = FAIL = 0


def case(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


#: 阶梯：从零焦点改动往上加。第 4 档才是探针在**用户桌面**上用的那一套。
#: `target` 的取值：
#:   renderer  只投给 renderer 子窗口；没有则该档跳过
#:   auto      有 renderer 子窗口就用它，没有就退回顶层窗口（真实 QQ 属于后者）
#:   top       固定投给顶层窗口
PLANS = [
    {"id": 1, "label": "WM_CHAR → renderer 子窗口（没有则退回顶层，零焦点改动）",
     "target": "auto", "mode": "char", "focus": None},
    {"id": 2, "label": "WM_CHAR → 顶层窗口（零焦点改动）",
     "target": "top", "mode": "char", "focus": None},
    {"id": 3, "label": "key 模式（带 scan code）→ 同上（零焦点改动）",
     "target": "auto", "mode": "key", "focus": None},
    {"id": 4, "label": "先 AttachThreadInput+SetActiveWindow+SetFocus，再 WM_CHAR → 同第 1 档",
     "target": "auto", "mode": "char", "focus": "full"},
    # 第 5 档是**对照实验**，也是唯一能分开两个成因的一档。
    # 前四档都在「QQ 不是前台」的条件下跑，而探针早已证明那种条件下裸 WM_CHAR 必然被丢 ——
    # 它们失败**不出意料**，也**不能**据此判定 ProseMirror 不吃 WM_CHAR。
    {"id": 5, "label": "【对照·会短暂抢前台】SetForegroundWindow 之后再 WM_CHAR",
     "target": "auto", "mode": "char", "focus": "foreground"},
]

RENDERER_CLS = "Chrome_RenderWidgetHostHWND"


def main() -> int:
    argv = sys.argv[1:]
    only = 0
    preview = "--preview" in argv
    allow_fg = "--allow-foreground" in argv
    cleanup = "--cleanup" in argv
    if "--only" in argv:
        try:
            only = int(argv[argv.index("--only") + 1])
        except (IndexError, ValueError):
            print("[X] --only 后面要跟档位号")
            return 2

    print("=" * 72)
    print("U2 生死线验证：真实 QQ 的消息编辑器吃不吃窗口消息（WM_CHAR）")
    print("=" * 72)
    print(f"项目根：{HERE}")
    print(f"测试串：{MARKER!r}（{len(MARKER)} 个字符，中英混排）")
    print("安全：绝不发送、绝不按 Enter；清空走免前台的 UIA 通路。")
    if preview:
        print("模式：--preview（只报告现场状态，不动手）")

    cfg = A.load_config()
    qq = A.QQWindow(cfg)
    if not qq.attach():
        code, ctx = qq.diagnose_attach()
        A.diag(code, "无法附着到 QQ 窗口", ctx)
        return 2

    print(f"\n[窗口] title={qq.dialog_title!r}  群聊={qq.is_group}  hwnd={qq.hwnd}")
    print(f"[前台] QQ 在前台 = {qq.is_foreground()}  当前前台 hwnd={winmsg.foreground_hwnd()}")
    if qq.editor is None:
        print("[X] 找不到输入框（ExEditor-qq-msg-editor）—— 请先打开一个会话的聊天界面")
        return 2
    print(f"[输入框] class={A._cls(qq.editor)!r}")

    children = winmsg.find_child(qq.hwnd, RENDERER_CLS)
    children_any = winmsg.find_child(qq.hwnd, RENDERER_CLS, visible_only=False)
    print(f"[renderer 子窗口] 可见={children or '无'}　含不可见={children_any or '无'}")
    if not children and children_any:
        children = children_any
        print("    可见的那批里没有 renderer 子窗口，改用不可见的 —— "
              "它能不能收到消息，由回读说了算。")
    if not children:
        print("    ⚠️ 完全没找到 renderer 子窗口 → 只能投给顶层窗口；")
        print("       而顶层窗口在拿不到焦点时会直接丢消息，所以第 4 档（建立焦点）是关键。")
        print("    子窗口类名清单（找投递目标用）：")
        for c in winmsg.child_classes(qq.hwnd, limit=24):
            print(f"      {c['hwnd']}  visible={c['visible']}  {c['class']}")

    # ---- 独立的清理入口 ----
    #
    # 为什么要单开一个入口：QQ 的编辑器不支持 ValuePattern，免前台清空**恒失败**，
    # 于是上一档真的把测试串留在了输入框里。而清理**绝不能**用 `auto.SendKeys` ——
    # 那走 OS 输入队列，会打到当时前台的那个窗口上（别人的程序），
    # Ctrl+A + Delete 落在那里就是删掉用户正在写的东西。
    if cleanup:
        print("\n[--cleanup] 用窗口消息清空输入框（Ctrl+A → Delete），**不走 SendKeys**")
        print(f"  清空前回读 = {qq.editor_text()!r}")

        # 清空本身也是生产必需的能力 —— 发回复前必须先清掉输入框里原有的草稿，
        # 而 QQ 上免前台清空（ValuePattern）恒失败。所以这里值得多试几组。
        kids = winmsg.find_child(qq.hwnd, RENDERER_CLS)
        targets = ([("renderer 子窗口", kids[0])] if kids else []) + [("顶层窗口", qq.hwnd)]
        n = max(1, len(qq.editor_text()))
        attempts = [
            ("Ctrl+A → Delete", 0x41, winmsg.VK_DELETE, 1),
            ("Ctrl+A → Backspace", 0x41, winmsg.VK_BACK, 1),
            ("逐字 Backspace", None, winmsg.VK_BACK, n),
        ]
        for tname, target in targets:
            for aname, sel_vk, clr_vk, times in attempts:
                A.force_foreground(qq.hwnd)      # 复现第 5 档那组有效条件
                time.sleep(0.12)
                if sel_vk is not None:
                    winmsg.send_vk(target, sel_vk, ctrl=True)
                    time.sleep(0.1)
                for _ in range(times):
                    winmsg.send_vk(target, clr_vk)
                time.sleep(0.3)
                after = qq.editor_text()
                print(f"  {tname:<16} {aname:<20} → 回读 {after!r}")
                if not after.strip():
                    print(f"  ✅ 已清空（{tname} + {aname}）。全程未发送任何消息。")
                    print("  ★ 生产里清空输入框应走这一组。")
                    return 0
        print("  [X] 以上组合都没清干净 —— 请手动删除，它**没有**被发出去")
        return 1

    # ---- 硬保护：测试前输入框必须为空 ----
    #
    # 这一条不能省。若你正在某个会话里打字，我们写进去再清空，
    # 会**毁掉你正在打的草稿** —— 而那正是这套系统最该避免的那类事故。
    base = qq.editor_text()
    if base.strip():
        print(f"\n[X] 输入框不为空（{base[:60]!r}），为避免毁掉你正在打的内容，直接中止。")
        print("    请先把输入框清空（或切到另一个输入框为空的会话），再重跑。")
        return 2
    print("[基线] 输入框为空 OK")
    case("测试前输入框是空的（可以安全写入）", True)

    if preview:
        print("\n（--preview：到此为止，未写入任何内容）")
        return 0

    results: list[dict] = []

    for plan in PLANS:
        if only and plan["id"] != only:
            continue
        print("\n" + "-" * 72)
        print(f"第 {plan['id']} 档：{plan['label']}")
        print("-" * 72)

        if plan["target"] in ("renderer", "auto"):
            if not children:
                if plan["target"] == "renderer":
                    print("  跳过：没有 renderer 子窗口")
                    continue
                target = qq.hwnd
                print(f"  投递目标：没有 renderer 子窗口 → 退回顶层窗口 {qq.hwnd}")
            else:
                target = children[0]
                print(f"  投递目标：renderer 子窗口 {target}")
        else:
            target = qq.hwnd
            print(f"  投递目标：顶层窗口 {target}")
        fg_before = winmsg.foreground_hwnd()

        have_fg = False
        focus_log = None
        if plan["focus"] == "full":
            focus_log = winmsg.focus_steps(qq.hwnd, children[0] if children else 0)
            print(f"  焦点：tid_target={focus_log['tid_target']} "
                  f"attached={focus_log['attached_used']}")
            for st in focus_log["steps"]:
                print(f"    {st['step']:<22} ret={st['ret']} "
                      f"fg={st['fg']} active={st['active']} focus={st['focus']}")
        elif plan["focus"] == "foreground":
            # ⚠️ 这一档会**短暂偷走你的前台**（约 1 秒），结束立即归还。
            # 它是唯一能把「ProseMirror 不吃 WM_CHAR」与「只是没有前台」
            # 这两个成因分开的实验，所以必须做，但要显式开启。
            if not allow_fg:
                print("  跳过：这一档会短暂抢你的前台，需要显式加 --allow-foreground")
                continue
            # 用 agent.force_foreground 而不是裸 SetForegroundWindow。
            # 差别不是小事：裸调用会被系统直接拒绝（ret=0，不报错），
            # 而它先 `AttachThreadInput(tid_fg → 我)` 再 BringWindowToTop + SetForegroundWindow，
            # 这一步才是关键 —— 项目里那条路径是实测能成的，别自己重写一遍。
            fg_pre = winmsg.foreground_hwnd()
            got = A.force_foreground(qq.hwnd)
            print(f"  force_foreground = {got}　{fg_pre} → {winmsg.foreground_hwnd()}")
            have_fg = bool(got) and qq.is_foreground()
            if not have_fg:
                print("  仍然拿不到前台 —— 调用方进程多半不在前台链上"
                      "（从终端/沙盒/远程拉起的进程会被系统拒绝），这一档测不出结论。")
        time.sleep(0.15)

        res = winmsg.send_text(target, MARKER, mode=plan["mode"], gap=0.04)
        print(f"  投递：{res.get('delivered')}/{res.get('sent')} 条未超时，"
              f"调用 {res.get('calls')} 次，耗时 {res.get('elapsed')}s")
        time.sleep(0.5)

        got = qq.editor_text()
        print(f"  回读：{got!r}")

        hit = (got.strip() == MARKER)
        partial = bool(got.strip()) and not hit
        print(f"  完全一致 = {hit}" + ("　（部分落地！）" if partial else ""))

        # 中文有没有被丢？逐个比对能直接暴露这种失败模式
        if got:
            print(f"  逐字符：写入 {list(MARKER)}")
            print(f"          读回 {list(got)}")

        results.append({"id": plan["id"], "label": plan["label"],
                        "delivered": res.get("delivered"), "sent": res.get("sent"),
                        "got": got, "hit": hit, "partial": partial})

        # ---- 清理 + 归还前台 ----
        #
        # QQ 的编辑器不支持 ValuePattern（项目已知：`IsValuePatternAvailable=False`），
        # 所以免前台的 `clear_editor_uia()` 在 QQ 上**恒失败**，它的 False 不说明任何事。
        # 真正能清空的只有键盘（Ctrl+A / Delete），而键盘需要前台。
        if have_fg:
            qq.clear_editor()          # Ctrl+A + Delete（走键盘，需要前台）
            time.sleep(0.25)
            cleared = not qq.editor_text().strip()
        else:
            cleared = qq.clear_editor_uia()
        time.sleep(0.15)
        print(f"  清空 = {'键盘' if have_fg else 'UIA(QQ 上恒失败)'}{cleared}　"
              f"清空后回读 = {qq.editor_text()!r}")
        if not cleared:
            print(f"  [!] 没清掉 —— 输入框里可能还留着 {MARKER!r}，请手动删除。"
                  f"（它**没有**被发出去，只是留在输入框里）")

        fg_after = winmsg.foreground_hwnd()
        if fg_after != fg_before:
            print(f"  前台被改动：{fg_before} → {fg_after}，正在归还…")
            if qq.cfg["uia"].get("restore_foreground", True):
                A.restore_foreground(fg_before)
            time.sleep(0.2)
            print(f"  归还后前台 = {winmsg.foreground_hwnd()}")
        else:
            print(f"  前台未受影响（始终是 {fg_before}）")

    # ---------------- 结论
    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    if not results:
        print("没有可执行的档位。")
        return 2

    for r in results:
        mark = "✅ 完全落地" if r["hit"] else ("⚠ 部分落地" if r["partial"] else "❌ 没落地")
        print(f"  第 {r['id']} 档  {mark}   {r['label']}")
        if not r["hit"]:
            print(f"          回读 = {r['got']!r}")

    hits = [r for r in results if r["hit"]]
    case("至少有一档能把 WM_CHAR 送进真实 QQ 的编辑器", bool(hits),
         "全部档位都没落地 —— 这是 V2 的架构级问题，需要换通路")

    final = qq.editor_text()
    case("收尾后输入框为空（没在你的会话里留下痕迹）", not final.strip(),
         f"残留 {final!r}（未发送，但请手动清理）")

    if hits:
        best = hits[0]
        print(f"\n[★ 推荐通路] 第 {best['id']} 档：{best['label']}")
        print("   V2 的宿主应优先采用这一档；档位越靠前，对用户的打扰越小。")
        return 0 if OK and not FAIL else 1

    print("\n[X] 没有任何一档成功 —— 在真机上补测之前，不要继续开发 V2 的消息管线。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
