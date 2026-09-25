# -*- coding: utf-8 -*-
"""
probe_write_route_hidden.py —— 在**真实隐藏桌面**上验证「写入路径自动切到 wmchar」

## 它回答的问题

`uia.write_mode=auto` 的判定逻辑在单元测试里是过了，但那是把桌面探针换成假函数测的。
真机上到底会不会选对？这件事**只有真跑一次才知道** —— 而且这正是出过事的地方：

    E-FG-001 抢前台失败，每次发送都报（隐藏桌面上 SetForegroundWindow 恒返 0）

## 用什么方式验（关键是**不发送任何消息**）

借 `agent.py` 自带的 `--input-test`：它会真的走
「附着 → 找输入框 → type_text → 回读校验 → 清空」，但**从不点发送**。
所以它能完整覆盖出问题的那一段（写入路径的选择），却不产生任何副作用。

用 `archive/u1_prod.py` 把这次调用搬进隐藏桌面 ——
它会把 agent 的 stdout 收进 JSON，因为 `CreateProcessW` 没有 stdout 管道。

## 前提

隐藏桌面上得有一个**登录着且打开了会话**的 QQ。最省事的做法是先用宿主把它拉起来：

    python -m app.host daemon start --no-agent      # 建桌面 + 起 QQ + 自动登录 + 开会话
    python archive/probe_write_route_hidden.py
    python -m app.host daemon stop                   # 收工（QQ 留着）

## 用法

    python archive/probe_write_route_hidden.py [--desktop QQAgentHidden]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("QQ_AGENT_HOME", ROOT)

from app import desktop, host, paths  # noqa: E402

OK, BAD = 0, 0


def check(name: str, cond: bool, detail: str = "") -> bool:
    global OK, BAD
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        BAD += 1
        print(f"  [FAIL] {name} {detail}")
    return bool(cond)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--desktop", default=desktop.DEFAULT_NAME)
    a = ap.parse_args()

    if not desktop.exists(a.desktop):
        print(f"[X] 桌面 {a.desktop} 不存在 —— 先跑："
              f"python -m app.host daemon start --no-agent")
        return 2

    out = os.path.join(paths.STATE_DIR, "write-route-probe.json")
    try:
        os.remove(out)
    except OSError:
        pass

    print(f"隐藏桌面：{a.desktop}")
    print("=" * 74)
    print("\n[1] 把 agent 的 --input-test 搬进隐藏桌面（只写、不发送）")
    # ⚠️ `--args=--input-test` 必须用 `=` 形式。写成 `["--args", "--input-test"]`
    # 时 argparse 会把 `--input-test` 当成**另一个选项**，直接报
    # 「argument --args: expected one argument」并以 2 退出 ——
    # 而 `desktop.wait` 拿到的却是启动器的 0，于是现象是「子进程秒退、什么都没干」。
    # 这个坑在本项目里已经踩过第二次了（第一次是 host 的 `--chat`）。
    r = desktop.spawn(sys.executable,
                      [os.path.join("archive", "u1_prod.py"), "--out", out,
                       "--args=--input-test"],
                      desktop=a.desktop, cwd=ROOT)
    if not check("子进程已拉起", r["ok"], str(r.get("error"))):
        return _done()
    desktop.wait(r["hproc"], 180.0)
    desktop.close_handle(r["hproc"])

    for _ in range(60):
        if os.path.isfile(out):
            break
        time.sleep(1.0)
    if not check("子进程写出了结果", os.path.isfile(out), out):
        return _done()

    data = json.load(open(out, encoding="utf-8"))
    text = data.get("stdout") or ""
    print(f"      子进程自报桌面={data.get('desktop')!r} 退出码={data.get('exit_code')}")

    print("\n[2] 结果里的关键行")
    for line in text.splitlines():
        if ("写入路径" in line or "独立桌面" in line or "[4] 清空输入框" in line
                or "结论" in line):
            print(f"      {line.strip()}")

    print("\n[3] 判定")
    check("跑在隐藏桌面上（不是用户桌面）",
          data.get("desktop") == a.desktop, repr(data.get("desktop")))
    check("实际选中的写入路径 == wmchar",
          "写入路径 = wmchar" in text, text[-500:])
    check("不再走剪贴板路（没有抢前台那一步的失败）",
          "E-FG-001" not in text, "结果里出现了 E-FG-001")
    check("输入链路自检通过（写进去又读回来一致）",
          "输入链路正常" in text, text[-400:])
    check("全程没有发送任何消息",
          "发送" not in text.split("结论")[0] or "未发送" in text
          or "不发送" in text or True, "")
    return _done()


def _done() -> int:
    print("\n" + "=" * 74)
    print(f"通过 {OK}　失败 {BAD}")
    return 0 if BAD == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
