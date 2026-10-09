# -*- coding: utf-8 -*-
"""
probe_clickat.py —— 在隐藏桌面上**真机验证** winmsg.click_at 的坐标链路
（2026-10-10 新增；归档原因：这是第三条点击路径唯一的一次真机取证）

## 它为什么必须存在

`click_at` 是这次新加的兜底点击路径（UIA 的 Invoke / DoDefaultAction 都断掉时用）。
仓库里其它地方走的都是 Invoke 或 WM_CHAR，**没有一处**用过窗口消息鼠标点击 ——
也就是说这条路径在真机上从来没跑过。而它恰好依赖一个很容易悄悄错的东西：
「UIA 给的矩形是物理屏幕坐标」+「本进程必须是 DPI-aware」，两者缺一就会点到别处。

## 判据（它就是来回答这三个问题的）

    1. UIA 的元素矩形与 GetWindowRect 是同一套基准吗？（都是物理屏幕坐标）
    2. 屏幕坐标 → 客户区坐标的换算落点对不对？
       —— 由「点会话条目 → 聊天区内容真的换人」这个**可观察结果**反证
    3. `Chrome_RenderWidgetHostHWND` 这一层在不在（消息要不要降级投顶层）

## 2026-10-10 实测结果（本机 150% 缩放）

    renderer_child : hwnd=1640136 rect=[560,284,2032,1276]（覆盖整窗，无需降级）
    picked         : recent-contact-item rect=[644,518,1019,614] → 中心 screen=[831,566]
    click          : client=[271,282]  ← 831-560=271、566-284=282，**精确吻合**
    returns        : [1, 1, 1]（WM_MOUSEMOVE / WM_LBUTTONDOWN / WM_LBUTTONUP 全部送达）
    changed        : True（text_count 15→30、nodes 244→344，内容切到了另一个会话）

## 怎么跑

它必须**在隐藏桌面上**执行（壳在用户桌面上枚举不到那张桌面的窗口），
所以用 app.desktop.spawn 把它丢过去，结果走 JSON 文件回来
（spawn 不接管 stdout，见 app/desktop.spawn 的说明）：

    python - <<'EOF'
    import os, sys
    sys.path.insert(0, os.getcwd())
    from app import desktop
    PY = sys.executable
    PROBE = os.path.abspath("archive/probe_clickat.py")
    os.environ["PROBE_OUT"] = os.path.abspath("archive/probe-clickat.json")
    boot = ("import sys, os; sys.path.insert(0, os.getcwd()); "
            "exec(compile(open(r'%s', encoding='utf-8').read(), 'probe', 'exec'))" % PROBE)
    r = desktop.spawn(PY, ["-c", boot], desktop="<你的隐藏桌面名>", cwd=os.getcwd())
    print("rc =", desktop.wait(r["hproc"], 90))
    EOF
    # 然后看 archive/probe-clickat.json

⚠️ 它会**真的点一下**会话列表里的一个条目（无害：只是打开那个会话）。
不要拿正在跑回复循环的实例去跑 —— 会干扰它。

余下是脚本本体（原样归档）。
"""

import json
import os
import sys
import time
import traceback

from app import hostagent as H, winmsg

OUT = os.environ.get("PROBE_OUT") or os.path.join(os.getcwd(), "probe-clickat.json")


def dump(o: dict) -> None:
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(o, f, ensure_ascii=False, indent=2)


res: dict = {"out": OUT}
try:
    res["dpi"] = winmsg.enable_dpi_awareness()
    res["desktop"] = __import__("app.desktop", fromlist=["desktop"]).current_name()

    wnd = H.find_qq_window()
    # ⚠️ find_qq_window 的返回是 {main, candidates, count} —— hwnd 在 main 里，
    # 顶层没有 hwnd（第一次探针就是栽在这上面，报了个假的「找不到 QQ 窗口」）。
    main = wnd.get("main") or {}
    hwnd = int(main.get("hwnd") or 0)
    res["window"] = {k: main.get(k) for k in ("hwnd", "pid", "class", "rect", "title")}
    res["window_count"] = wnd.get("count")
    if not hwnd:
        res["error"] = f"找不到 QQ 窗口（count={wnd.get('count')}）"
        dump(res)
        raise SystemExit(0)

    # 渲染层子窗口：click_at 优先投给它（和键盘同源）
    kids = winmsg.find_child(hwnd, "Chrome_RenderWidgetHostHWND", visible_only=False)
    res["renderer_child"] = [{"hwnd": k, "rect": winmsg.win_rect(k)} for k in kids]

    before = H.probe_uia(hwnd)
    res["before"] = {k: before.get(k) for k in
                     ("has_recent_list", "has_ml_list", "nickname",
                      "text_count", "nodes_scanned")}
    res["before_samples"] = (before.get("text_samples") or [])[:8]

    items = H._find_all_by(hwnd, cls_has=H.SESSION_ITEM_TOKEN)
    res["item_count"] = len(items)
    res["items"] = [H._describe(i) for i in items[:4]]
    if not items:
        res["error"] = "会话列表里没有条目"
        dump(res)
        raise SystemExit(0)

    # 已经开着聊天就换一条点，否则点第一条 —— 两种情况下「聊天区出现/换人」
    # 都是可观察的变化。
    idx = 1 if (before.get("has_ml_list") and len(items) > 1) else 0
    picked = H._describe(items[idx])
    r = picked.get("rect") or []
    res["picked_index"] = idx
    res["picked"] = picked
    if len(r) != 4:
        res["error"] = "拿不到条目矩形"
        dump(res)
        raise SystemExit(0)

    cx = (int(r[0]) + int(r[2])) // 2
    cy = (int(r[1]) + int(r[3])) // 2
    res["click"] = winmsg.click_at(hwnd, cx, cy)
    time.sleep(2.5)

    after = H.probe_uia(hwnd)
    res["after"] = {k: after.get(k) for k in
                    ("has_recent_list", "has_ml_list", "nickname",
                     "text_count", "nodes_scanned")}
    res["after_samples"] = (after.get("text_samples") or [])[:8]
    res["changed"] = (res["before"]["has_ml_list"] != res["after"]["has_ml_list"]
                      or res["before_samples"] != res["after_samples"])
    res["ok"] = True
except Exception as exc:
    res["error"] = f"{type(exc).__name__}: {exc}"
    res["traceback"] = traceback.format_exc()[-900:]
dump(res)
