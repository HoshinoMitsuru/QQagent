# -*- coding: utf-8 -*-
"""
test_live_brain.py —— Brain 真机实测 harness（会真的操作 QQ，看清楚再跑）

## 实测授权（2026-09-25 苏霖韵拍板，写死在本文件头，改动需他本人确认）

| 执行面 | 对象 | 发送授权 |
| --- | --- | --- |
| hosted（R4 独立桌面） | 托管小号「苏霖韵」 | ✅ 允许真实发送（全自主） |
| attach（附着主号「嗅尘紫蝶」） | 仅群「我，我们」与小号「苏霖韵」 | ⚠️ 允许，但**每条发送都要控制台人工确认** |

attach 面的目标白名单是**硬闸**（cu/brain.py 的 open_chat_allow）：
名单外的会话在原语被调用之前就被拦截，模型重试也没用。

## 用法

    python test_live_brain.py --mode hosted --task "看看谁发了新消息，回复一句问候"
    python test_live_brain.py --mode attach --task "去测试群发一句『CU 链路测试』"

前置条件：
    - secrets 里已配好 DeepSeek Key（cu.api_key 或主 llm 密钥均可）
    - attach：本机 QQ 已登录主号且停在任一会话页
    - hosted：独立桌面常驻已启动（或允许本脚本只做只读动作）
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import agent                                       # noqa: E402
from cu.brain import Brain, load_cu_config          # noqa: E402
from cu.tools import dispatch                       # noqa: E402

#: attach 实测白名单（与用户拍板的授权一致）
ATTACH_ALLOW = ["我，我们", "苏霖韵"]

#: hosted 实测目标闸：全自主发送已授权，但测试期目标限定为主号（收信方
#: 是用户自己的主号「Psyche-嗅尘紫蝶」）与测试群，防模型漂到别的会话。
HOSTED_ALLOW = ["Psyche-嗅尘紫蝶", "我，我们"]


def main() -> int:
    ap = argparse.ArgumentParser(description="CU Brain 真机实测")
    ap.add_argument("--mode", required=True, choices=("attach", "hosted"))
    ap.add_argument("--task", required=True, help="下发给 Brain 的任务（中文自然语言）")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--max-steps", type=int, default=0, help="覆盖 cu.max_steps")
    a = ap.parse_args()

    cfg = agent.load_config()
    cu = load_cu_config(cfg)
    if a.base_url:
        cu["base_url"] = a.base_url
    if a.model:
        cu["model"] = a.model
    if a.max_steps:
        cu["max_steps"] = a.max_steps

    # ---- 执行面 + 安全策略（按授权表写死）----
    ex = None
    confirm = None
    if a.mode == "hosted":
        from cu.hosted import HostedExecutor
        ex = HostedExecutor(cfg)
        # hosted 全自主（require_confirmation=False），但测试期仍给目标闸：
        # 只许开「主号 / 测试群」，防模型把消息发进别的会话
        cu["open_chat_allow"] = HOSTED_ALLOW
        print("=" * 78)
        print(f"执行面 hosted（独立桌面小号）｜全自主发送已授权"
              f"｜目标闸：{'、'.join(HOSTED_ALLOW)}")
    else:
        from cu.attach import AttachExecutor
        ex = AttachExecutor(cfg)
        cu["open_chat_allow"] = ATTACH_ALLOW   # 必须在 Brain 构造之前注入

        def confirm(info: dict) -> bool:
            print("\n" + "!" * 78)
            print(f"!! 主号发送确认  目标会话标题：{info.get('chat_title') or '（未知）'}")
            print(f"!! 拟发送内容：{info.get('text')}")
            ans = input("!! 确认发送？(y=发送 / 其他=拒绝) > ").strip().lower()
            return ans == "y"

        print("=" * 78)
        print(f"执行面 attach（附着主号）｜白名单：{'、'.join(ATTACH_ALLOW)}"
              f"｜每条发送逐条人工确认")

    brain = Brain(cu, cfg)
    if not brain.api_key:
        print("[X] E-LLM-001 没有可用 API Key（cu.api_key / 主 llm 密钥都为空）")
        return 2

    print(f"模型 {brain.model} @ {brain.url}（Key 来源：{brain.key_src}）")
    print(f"任务：{a.task}")
    print("=" * 78)

    # ---- 预检：执行面体检（只读）----
    h = dispatch(ex, "health", {})
    print(f"[预检] health → {json.dumps(h, ensure_ascii=False)[:300]}")
    if not h.get("ok") and h.get("code") != "E-QQ-008":
        print("[X] 执行面体检未通过，先解决上面的错误码再实测（参错误码 fixes）")
        return 1
    if h.get("code") == "E-QQ-008":
        # 已登录但停在会话列表页 = 可工作状态：Brain 的第一个动作就是 open_chat
        print("[i] QQ 已登录、停在会话列表页（E-QQ-008）—— Brain 会自己开会话，继续")

    # ---- 跑 Brain ----
    r = brain.run(ex, a.task, confirm=confirm,
                  on_event=lambda e: print(f"  [step {e['step']}] {e['type']}: "
                                           f"{json.dumps({k: v for k, v in e.items() if k not in ('step', 'type')}, ensure_ascii=False)[:260]}"))

    print("=" * 78)
    if r["ok"]:
        print(f"[完成，{r['usage_steps']} 步] 最终答复：\n{r['answer']}")
    else:
        err = r["error"]
        print(f"[失败] {err.get('code')} {err.get('detail')}")
        if err.get("ctx"):
            print(f"       ctx: {json.dumps(err['ctx'], ensure_ascii=False)[:300]}")
    print(f"（trace 共 {len(r['steps'])} 条事件）")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
