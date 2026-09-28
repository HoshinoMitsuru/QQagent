# -*- coding: utf-8 -*-
"""
test_conversations.py —— 会话持久化目录化的离线自测

背景：会话档案从单文件（state/conversations.json）改为目录
（state/conversations/，每会话一个 json）。这里离线验证：

    1. 旧单文件一次性迁移（拆分进目录、旧文件改名 .migrated-*）
    2. 目录已有内容时不迁移（防覆盖）
    3. per-scope 落盘（book → save → 档案文件带 _scope）
    4. forget：内存 + 档案文件一起删
    5. _prune 淘汰后再取用：从盘上恢复历史（空行覆盖是 bug）
    6. 损坏档案改名 .bad 保留现场
    7. Windows 非法字符 scope 的文件名替换
    8. 连续对话状态机的脏标记确实会触发落盘

运行：
    python test_conversations.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

TMP = tempfile.mkdtemp(prefix="qqagent-conv-")
os.environ["QQ_AGENT_HOME"] = TMP

import agent                       # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}  {detail}")


def make_store(home: str, max_scopes: int = 50,
               legacy: dict | None = None) -> "agent.ConversationStore":
    """在独立 QQ_AGENT_HOME 下构造一个 ConversationStore。"""
    old = os.environ.get("QQ_AGENT_HOME")
    os.environ["QQ_AGENT_HOME"] = home
    try:
        cfg = {
            "persist": {"enabled": True, "save_interval_seconds": 0,
                        "max_scopes": max_scopes,
                        # 绝对路径：agent.HERE 在 import 时已按首个 TMP 固定，
                        # 各用例用绝对路径互相隔离
                        "file": os.path.join(home, "state", "conversations.json")},
            "llm": {"system_prompt": "SP"},
            "chat": {"max_history_entries": 40},
        }
        if legacy is not None:
            os.makedirs(os.path.join(home, "state"), exist_ok=True)
            with open(os.path.join(home, "state", "conversations.json"),
                      "w", encoding="utf-8") as f:
                json.dump(legacy, f, ensure_ascii=False)
        return agent.ConversationStore(cfg)
    finally:
        if old is not None:
            os.environ["QQ_AGENT_HOME"] = old


def scope_dir(store) -> str:
    return store.dir


def section(title: str) -> None:
    print(f"\n== {title} ==")


# ---------------------------------------------------------------- §1 迁移
section("§1 旧单文件迁移：拆分 + 改名 .migrated-*")
home1 = os.path.join(TMP, "h1")
legacy = {"scopes": {
    "阿离": {"used_at": 111.0, "seq": 3,
             "history": [{"role": "user", "content": "你好"}]},
    "凡杜林矿坑": {"used_at": 222.0, "seq": 0, "history": []},
}}
s1 = make_store(home1, legacy=legacy)
check("迁移后目录存在", os.path.isdir(scope_dir(s1)))
fp1 = os.path.join(scope_dir(s1), s1._scope_file("阿离"))
fp2 = os.path.join(scope_dir(s1), s1._scope_file("凡杜林矿坑"))
check("阿离 档案落盘", os.path.isfile(fp1))
check("凡杜林矿坑 档案落盘", os.path.isfile(fp2))
check("旧单文件已改名 .migrated-*",
      not os.path.isfile(os.path.join(home1, "state", "conversations.json"))
      and any(n.startswith("conversations.json.migrated-")
              for n in os.listdir(os.path.join(home1, "state"))))
check("迁移保留 _scope 键", json.load(open(fp1, encoding="utf-8")).get("_scope") == "阿离")
check("迁移后内存可读", "阿离" in s1._book and "凡杜林矿坑" in s1._book)

# ---------------------------------------------------------- §2 防覆盖
section("§2 目录已有内容时不迁移（防覆盖）")
home2 = os.path.join(TMP, "h2")
os.makedirs(os.path.join(home2, "state", "conversations"), exist_ok=True)
keep = os.path.join(home2, "state", "conversations", "existing.json")
with open(keep, "w", encoding="utf-8") as f:
    json.dump({"_scope": "existing", "used_at": 1.0, "seq": 0, "history": []}, f)
legacy2 = {"scopes": {"newcomer": {"used_at": 1.0, "seq": 0, "history": []}}}
s2 = make_store(home2, legacy=legacy2)
check("既有档案未被覆盖", os.path.isfile(keep))
check("旧文件仍在（未迁移）",
      os.path.isfile(os.path.join(home2, "state", "conversations.json")))
check("既有 scope 已载入", "existing" in s2._book)
check("旧文件里的 scope 未混入", "newcomer" not in s2._book)

# ---------------------------------------------------------- §3 per-scope 落盘
section("§3 per-scope 落盘：book → save → 档案带 _scope")
home3 = os.path.join(TMP, "h3")
s3 = make_store(home3)
row = s3.book("测试会话A")
row["history"].push("user", "hi")
s3.save(force=True)
fpA = os.path.join(scope_dir(s3), s3._scope_file("测试会话A"))
check("档案文件生成", os.path.isfile(fpA))
data = json.load(open(fpA, encoding="utf-8"))
check("_scope 键正确", data.get("_scope") == "测试会话A")
check("历史已写入", "hi" in json.dumps(data, ensure_ascii=False))
# 只动一个 scope 时，另一个 scope 不应被重写（脏标记隔离）
rowB = s3.book("测试会话B")
rowB["history"].push("user", "B")
mtimeA = os.path.getmtime(fpA)
s3.save(force=True)
check("未变更的 A 不重写", os.path.getmtime(fpA) == mtimeA)

# ---------------------------------------------------------- §4 forget
section("§4 forget：内存 + 档案文件一起删")
check("forget 返回 True", s3.forget("测试会话B") is True)
check("内存已删", "测试会话B" not in s3._book)
check("档案文件已删", not os.path.isfile(
    os.path.join(scope_dir(s3), s3._scope_file("测试会话B"))))
check("forget 不存在的 scope 返回 False", s3.forget("不存在") is False)

# ---------------------------------------------------------- §5 淘汰恢复
section("§5 _prune 淘汰后再取用：从盘上恢复历史")
home5 = os.path.join(TMP, "h5")
s5 = make_store(home5, max_scopes=1)
r = s5.book("老会话")
r["history"].push("user", "重要记忆")
s5.save(force=True)
s5.book("新会话")                     # 触发 prune，老会话被逐出内存
check("老会话已被逐出内存", "老会话" not in s5._book)
check("档案文件仍在（淘汰≠删档案）", os.path.isfile(
    os.path.join(scope_dir(s5), s5._scope_file("老会话"))))
r2 = s5.book("老会话")
texts = json.dumps(r2["history"].to_dict(), ensure_ascii=False)
check("历史从盘上恢复（不被空行覆盖）", "重要记忆" in texts,
      f"实际内容：{texts[:120]}")

# ---------------------------------------------------------- §6 损坏档案
section("§6 损坏档案改名 .bad 保留现场")
home6 = os.path.join(TMP, "h6")
os.makedirs(os.path.join(home6, "state", "conversations"), exist_ok=True)
with open(os.path.join(home6, "state", "conversations", "broken.json"),
          "w", encoding="utf-8") as f:
    f.write("{不是 json")
s6 = make_store(home6)
state6 = os.path.join(home6, "state", "conversations")
check("损坏档案改名 .bad", os.path.isfile(os.path.join(state6, "broken.json.bad"))
      and not os.path.isfile(os.path.join(state6, "broken.json")))

# ---------------------------------------------------------- §7 文件名替换
section("§7 Windows 非法字符 scope → 文件名替换")
cases = {
    'a:b': 'a_b.json',
    'c\\d': 'c_d.json',
    'e*f?g"h<i>j|k': 'e_f_g_h_i_j_k.json',
    '正常会话': '正常会话.json',
}
for raw, want in cases.items():
    check(f"{raw!r} → {want}", agent.ConversationStore._scope_file(raw) == want)

# ---------------------------------------------------------- §8 状态机脏标记
section("§8 连续对话状态机的脏标记会触发落盘")
home8 = os.path.join(TMP, "h8")
s8 = make_store(home8)
s8.activate_continuous("状态机会话", now=1000.0)
fp8 = os.path.join(scope_dir(s8), s8._scope_file("状态机会话"))
check("activate 后档案未落盘（节流）", not os.path.isfile(fp8))
s8.save(force=True)
check("activate 落盘 active=true",
      json.load(open(fp8, encoding="utf-8"))["continuous"]["active"] is True)
s8.refresh_continuous("状态机会话", now=1100.0)
s8.save(force=True)
check("refresh 落盘 last_at 更新",
      json.load(open(fp8, encoding="utf-8"))["continuous"]["last_at"] == 1100.0)
s8.deactivate_continuous("状态机会话")
s8.save(force=True)
check("deactivate 落盘 active=false",
      json.load(open(fp8, encoding="utf-8"))["continuous"]["active"] is False)
# 超时分支
s8.activate_continuous("状态机会话", now=2000.0)
s8.save(force=True)
s8.is_continuous("状态机会话", now=2000.0 + 10**9, timeout=600.0)
s8.save(force=True)
check("超时自动退出落盘 active=false",
      json.load(open(fp8, encoding="utf-8"))["continuous"]["active"] is False)

# ---------------------------------------------------------------- 收尾
print(f"\n结果：{PASS} 过 / {FAIL} 挂")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
