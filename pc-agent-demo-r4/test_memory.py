# -*- coding: utf-8 -*-
"""
test_memory.py —— 长期记忆（跨天归档 + 按需召回）的自测

全面离线：不碰 QQ、不联网、不发消息。归档那一步本来要调模型，这里把
「归档专用客户端」换成假对象（`mem._alt_client`），所以**模型不可用**这条
降级路径也能被确定地复现 —— 那正是线上最可能出问题的分支。

覆盖：
     1) 词元估算与线索抽取（召回的地基）
     2) 文件名规则：会话档案与记忆档案必须同名（否则删不干净）
     3) 当日流水：真写盘、按天切、按会话切、system 不记、空 scope 不记
     4) 归档（模型可用）：事件 + 画像落档、流水删除、计数正确
     5) 归档降级（模型不可用）：原文存档 + E-MEM-003，一条不丢
     6) 启动补算 / tick：只归档「早于今天」的流水
     7) 召回：带线索时命中相关往事，无线索时给最近几条
     8) 超容量：把最老的事件**先并进画像再删**（E-MEM-002），不是直接丢
     9) 遗忘：档案 + 未归档流水一起删
    10) 坏档：改名 .bad 保留现场（E-MEM-001），程序继续跑
    11) Agent 集成：记忆注入在 system 之后、对话之前
    12) 控制台侧：统计与连带删除（壳进程不能 import agent，走独立实现）

运行：
    python -X utf8 test_memory.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 整个数据目录挪到临时地方。agent.py 的 HERE 与 app.paths.DATA_DIR 都认这个
# 环境变量，所以两边绝对写同一处 —— 也保证不会往仓库里的 state/ 写任何东西。
TMP = tempfile.mkdtemp(prefix="qqagent-mem-")
os.environ["QQ_AGENT_HOME"] = TMP

import agent as A                     # noqa: E402
import error_codes as EC              # noqa: E402
import app.server as S                # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, extra: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"    {'✓' if cond else '✗'} {name}" + (f"   {extra}" if extra else ""))


# ---------------------------------------------------------------- 报错捕获
# 长期记忆中有些分支**只能**靠错误码验证（坏档、降级、压缩）。把它们抓下来，
# 既确认「报了」，也确认「报的是那一个码」—— 报错太笼统等于没报。
class Reports:
    def __init__(self):
        self.rows: list[tuple[str, str, dict]] = []
        self._orig = (A.report, A.report_exc)

    def __enter__(self):
        A.report = lambda code, detail="", **kw: self.rows.append(
            (code, detail, kw.get("ctx") or {}))
        A.report_exc = lambda exc, code, **kw: self.rows.append(
            (code, f"{type(exc).__name__}: {exc}", kw.get("ctx") or {}))
        return self

    def __exit__(self, *exc):
        A.report, A.report_exc = self._orig
        return False

    def codes(self) -> list[str]:
        return [c for c, _, _ in self.rows]

    def has(self, code: str) -> bool:
        return code in self.codes()

    def first(self, code: str) -> str:
        for c, d, _ in self.rows:
            if c == code:
                return d
        return ""

    def ctx_of(self, code: str) -> dict:
        """报错时带上的现场（文件路径、原因…）。

        值得单独断言：错误码只说明「哪一类坏了」，真正能修的人需要的是
        「哪个文件、备份到哪了」—— 这些只存在于 ctx 里。
        """
        for c, _, ctx in self.rows:
            if c == code:
                return ctx
        return {}


class FakeLLM:
    """归档专用客户端的替身。

    两种回复形态都按「system 提示词是谁」来分：归档要 JSON，合并画像要散文。
    真实链路上这就是两个不同的提示词，替身必须照这个分，否则测出来的行为
    跟线上不是一回事（合并那步拿到 JSON 也会被当成画像正文存进去，看起来"通过"）。
    """

    def __init__(self, payload=None, broken: bool = False):
        self.ready = True
        self.payload = payload
        self.broken = broken
        self.calls: list[list] = []

    def chat(self, messages):
        self.calls.append(messages)
        first = str((messages[0] or {}).get("content") or "")
        if first == A.MERGE_SYSTEM:
            return "（合并后的画像）"
        if self.broken:
            return "今天聊得挺开心的，没什么特别的。"     # 不是 JSON
        return json.dumps(self.payload or {}, ensure_ascii=False)


def make_cfg(**over):
    """只把会话/轮转/取号这些文件挪走，**故意不动 memory.dir**。

    因为 QQ_AGENT_HOME 已经把整个数据目录指到临时目录了，agent 与 app 两侧
    都会把 "state/memory" 解析到同一个地方 —— 这本身就是要测的一件事
    （见 [12]：壳进程没有这段代码，它得自己算出同一个目录）。
    """
    cfg = A._deep_merge(A.DEFAULTS, {
        "persist": {"file": os.path.join(TMP, "conversations.json")},
        "discovery": {"state_file": os.path.join(TMP, "rotation.json")},
        "identity": {"store": os.path.join(TMP, "uid-map.json")},
    })
    return A._deep_merge(cfg, over)


def new_mem(**over) -> A.LongTermMemory:
    return A.init_memory(make_cfg(**over))


TODAY = time.strftime("%Y-%m-%d")
YDAY = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
DBYDAY = time.strftime("%Y-%m-%d", time.localtime(time.time() - 2 * 86400))


def write_journal(mem, scope: str, day: str, rows) -> str:
    """直接造一份「某天的流水」。测归档时不能靠 push —— push 只写今天。"""
    d = mem._journal_dir(scope)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, day + ".jsonl")
    with open(p, "w", encoding="utf-8") as f:
        for i, (role, content) in enumerate(rows):
            f.write(json.dumps({
                "ts": time.time() - 3600 + i * 60, "scope": scope, "role": role,
                "content": content, "teach": False, "source": "incoming",
            }, ensure_ascii=False) + "\n")
    return p


# ================================================================ 1
def test_terms():
    print("\n[1] 词元估算与线索抽取")
    check("中文按字估：『你好』= 2", A.token_estimate("你好") == 2,
          str(A.token_estimate("你好")))
    check("非中文按 4 字符估：5 个字母 = 2", A.token_estimate("hello") == 2,
          str(A.token_estimate("hello")))
    check("空串 = 0", A.token_estimate("") == 0)

    t = A.extract_terms("今天我们在聊 Python 异步")
    check("提到的话题被抽成词", "python" in t and "异步" in t, str(sorted(t))[:80])
    check("高频词被当停用词丢掉（否则会把 top-k 名额挤满）",
          "今天" not in t and "我们" not in t, str(sorted(t))[:80])
    check("空线索得到空集合", A.extract_terms("") == set())


# ================================================================ 2
def test_names():
    print("\n[2] 文件名规则：会话档案与记忆档案必须同名")
    check("非法字符被替换（注意 `:` 也在替换表里）",
          A.scope_slug(r"private:甲/乙\丙") == "private_甲_乙_丙",
          A.scope_slug(r"private:甲/乙\丙"))
    check("首尾空格**不**被吃掉（老档案不能改名）",
          A.scope_slug("private: 带空格 ") == "private_ 带空格 ",
          repr(A.scope_slug("private: 带空格 ")))
    check("空 scope 给一个安全名", A.scope_slug("") == "_" and A.scope_slug("   ") == "_")

    scope = r"group:桌游群/2026"
    check("会话档案名 = slug + .json",
          A.ConversationStore._scope_file(scope) == A.scope_slug(scope) + ".json",
          A.ConversationStore._scope_file(scope))
    check("记忆档案名与会话档案名一致（这正是不用 slug+记忆目录各写一套的理由）",
          S._memory_file_name(scope) == A.ConversationStore._scope_file(scope),
          S._memory_file_name(scope))


# ================================================================ 3
def test_journal():
    print("\n[3] 当日流水")
    mem = new_mem()
    h = A.History(max_entries=40, system_prompt="", scope="private:1001")
    e1 = h.push("user", "【小明】：周四要交 C 语言作业", source="incoming")
    e2 = h.push("assistant", "记得了，周四。", source="auto")
    h.push("assistant", "以后叫我阿澈", source="teach", teach=True)
    with Reports():
        h.push("system", "这行不该进流水")
        A.History(scope="").push("user", "没有 scope，也不该进流水")

    p = os.path.join(mem._journal_dir("private:1001"), TODAY + ".jsonl")
    check("流水文件按 <scope>/<日期>.jsonl 落盘", os.path.isfile(p), p)
    rows = [json.loads(x) for x in open(p, encoding="utf-8").read().splitlines() if x.strip()]
    check("只记 user/assistant 两类（system 不进）", len(rows) == 3, str(len(rows)))
    check("正文原样保留", rows[0]["content"].startswith("【小明】"))
    check("teach 标记被带上", rows[2]["teach"] is True and rows[0]["teach"] is False)
    check("scope 写进了每一行（归档时靠它反查真实会话名）",
          all(r["scope"] == "private:1001" for r in rows))
    check("条目自带 ts（日志时间是归档时排时间线的唯一依据）",
          isinstance(rows[0]["ts"], (int, float)) and rows[0]["ts"] > 0)

    h2 = A.History(scope="group:桌游群")
    h2.push("user", "【小红】：开团吗")
    check("不同会话各写各的目录",
          os.path.isfile(os.path.join(mem._journal_dir("group:桌游群"), TODAY + ".jsonl")))
    check("两个会话的目录不互相覆盖",
          not os.path.isfile(os.path.join(mem._journal_dir("private:1001"),
                                          TODAY + ".jsonl.bak")))

    # enabled=False 时一行都不该写
    mem2 = new_mem(memory={"enabled": False})
    A.History(scope="private:1002").push("user", "关了就不该留痕")
    check("memory.enabled=false → 不写流水",
          not os.path.isdir(mem2._journal_dir("private:1002")))


# ================================================================ 4
def test_archive_ok():
    print("\n[4] 归档（模型可用）")
    mem = new_mem()
    scope = "private:2001"
    write_journal(mem, scope, YDAY, [
        ("user", "【小明】：我周四要交 C 语言作业，还差一道二叉树遍历"),
        ("assistant", "那我陪你把中序切、前序定根那套再走一遍。"),
    ])
    mem._alt_client = FakeLLM(payload={
        "digest": "小明在准备 NCRE 二级 C，把助手叫作「阿澈」，喜欢直接给结论。",
        "events": [
            {"text": "小明周四要交 C 语言作业。", "keywords": ["C语言", "作业", "周四"]},
            {"text": "聊过二叉树遍历：中序切、前序定根。", "keywords": ["二叉树", "遍历"]},
        ],
    })
    with Reports() as rep:
        ok = mem.archive(scope, YDAY)
    check("归档返回成功", ok is True)
    check("没有报任何错（正常路径不该有噪音）", rep.rows == [], str(rep.rows))

    rec = mem._load(scope)
    check("画像落档", "阿澈" in (rec.get("digest") or {}).get("text", ""),
          (rec.get("digest") or {}).get("text", "")[:40])
    check("事件落档 2 条", len(rec["events"]) == 2, str(len(rec["events"])))
    check("事件带日期与编号（界面上要能看出是哪天的）",
          rec["events"][0]["date"] == YDAY and rec["events"][0]["id"] == 1,
          str({k: rec["events"][0][k] for k in ("date", "id")}))
    check("关键词被保留", "二叉树" in rec["events"][1]["keywords"],
          str(rec["events"][1]["keywords"]))
    check("last_archive_date 记为刚归档的那天", rec["last_archive_date"] == YDAY)
    check("流水文件归档后已删除（长期只留摘要）",
          not os.path.isfile(os.path.join(mem._journal_dir(scope), YDAY + ".jsonl")))
    check("归档计数 +1", mem.stats["archived"] == 1, str(mem.stats))

    fp = mem._path(scope)
    check("档案真的写到盘上（不是只在内存里）", os.path.isfile(fp), fp)
    check("档案目录在 memory.dir 下（没有写到别处）",
          os.path.dirname(os.path.abspath(fp)) == os.path.abspath(mem.dir))

    # 重新加载（模拟重启）后内容还在
    fresh = new_mem()
    rec2 = fresh._load(scope)
    check("重启后档案可读（走的是磁盘，不是缓存）",
          len(rec2["events"]) == 2 and "阿澈" in (rec2.get("digest") or {}).get("text", ""))


# ================================================================ 5
def test_archive_fallback():
    print("\n[5] 归档降级（模型不可用 / 返回不可解析）")
    mem = new_mem()
    scope = "private:2002"
    write_journal(mem, scope, YDAY, [("user", "【甲】：这句必须被记住")])
    mem._alt_client = False                 # = 客户端构造不出来（没密钥 / 网络不通）
    with Reports() as rep:
        ok = mem.archive(scope, YDAY)
    check("模型不可用也算归档成功（否则流水会被反复重试到天荒地老）", ok is True)
    check("报了 E-MEM-003（降级必须留痕，不能悄悄变差）", rep.has("E-MEM-003"),
          str(rep.codes()))
    rec = mem._load(scope)
    check("原文被截断存成一条事件（这一天不白丢）",
          len(rec["events"]) == 1 and "这句必须被记住" in rec["events"][0]["text"],
          str(rec["events"])[:80])
    check("降级计数 +1", mem.stats["fallback"] == 1, str(mem.stats))

    # 另一种降级：模型活着但吐回来的不是 JSON
    mem2 = new_mem()
    scope2 = "private:2012"          # 每个子用例各用一个 scope，否则会叠到同一份档案上
    write_journal(mem2, scope2, YDAY, [("user", "【乙】：模型今天不好好说话")])
    mem2._alt_client = FakeLLM(broken=True)
    with Reports() as rep2:
        ok2 = mem2.archive(scope2, YDAY)
    check("返回不是 JSON 时同样降级为原文存档", ok2 is True and rep2.has("E-MEM-003"),
          str(rep2.codes()))
    check("此时额外报 E-LLM-008（说清是『解析不出来』而不是『连不上』）",
          rep2.has("E-LLM-008"), str(rep2.codes()))

    # summarize=False：连降级报错都不该有（用户是主动选的原文存档）
    mem3 = new_mem(memory={"summarize": False})
    scope3 = "private:2022"
    write_journal(mem3, scope3, YDAY, [("user", "【丙】：我选了不走模型")])
    mem3._alt_client = FakeLLM(payload={"events": [], "digest": ""})
    with Reports() as rep3:
        mem3.archive(scope3, YDAY)
    check("summarize=false → 不报降级（这是用户的明示选择）",
          not rep3.has("E-MEM-003"), str(rep3.codes()))
    check("但内容照样进档案",
          "我选了不走模型" in mem3._load(scope3)["events"][0]["text"],
          str(mem3._load(scope3)["events"])[:80])

    # 空流水：应该直接清掉，不报错
    mem4 = new_mem()
    check("没有流水的日期 → 归档返回 False 且不报错",
          mem4.archive("private:2003", YDAY) is False)


# ================================================================ 6
def test_catch_up():
    print("\n[6] 启动补算与跨天 tick")
    mem = new_mem()
    a, b = "private:3001", "private:3002"
    write_journal(mem, a, DBYDAY, [("user", "【甲】：前天的事")])
    write_journal(mem, a, YDAY, [("user", "【甲】：昨天的事")])
    write_journal(mem, b, YDAY, [("user", "【乙】：昨天的事")])
    write_journal(mem, b, TODAY, [("user", "【乙】：今天的事")])   # 今天的不许动
    mem._alt_client = FakeLLM(payload={"digest": "画像", "events": [{"text": "事件", "keywords": []}]})

    with Reports():
        n = mem.archive_due()
    check("一次把漏掉的三天都补上（a 的前天 + a 的昨天 + b 的昨天）", n == 3, str(n))
    check("今天的流水保持不动（当天还没结束，不能压成摘要）",
          os.path.isfile(os.path.join(mem._journal_dir(b), TODAY + ".jsonl")))
    check("补算后昨天的流水都清掉了",
          not os.path.isfile(os.path.join(mem._journal_dir(a), YDAY + ".jsonl"))
          and not os.path.isfile(os.path.join(mem._journal_dir(b), YDAY + ".jsonl")))
    check("同一天归档到各自的档案里（不会串台）",
          len(mem._load(a)["events"]) == 2 and len(mem._load(b)["events"]) == 1,
          f"a={len(mem._load(a)['events'])} b={len(mem._load(b)['events'])}")

    # tick：只在自己「启动后第一次」或「跨天」时才扫盘，其余轮次直接返回。
    # 一个 tick 周期都没走完就换对象，是因为「启动补算」这件事在真实时序上就是
    # 由一个**新建的** LongTermMemory 完成的（进程刚起来，_ticked 还是 False）。
    memA = new_mem()
    check("没有流水时 tick 不启动归档", memA.tick() is False)
    check("同一分钟内再问一次也不扫盘（每轮都扫是白耗 IO）",
          memA.tick() is False and memA._next_tick > time.time())

    memB = new_mem()
    write_journal(memB, "private:3003", YDAY, [("user", "【丙】：昨天")])
    memB._alt_client = FakeLLM(payload={"digest": "", "events": [
        {"text": "e", "keywords": []}]})
    with Reports():
        started = memB.tick(now=time.time())
        check("启动时发现昨天有流水 → 丢到后台线程归档（不阻塞读写发送）",
              started is True)
        time.sleep(0.8)                     # 等后台线程跑完（归档只碰文件，很快）
    check("后台归档真的落盘了",
          os.path.isfile(memB._path("private:3003"))
          and not os.path.isfile(os.path.join(memB._journal_dir("private:3003"),
                                              YDAY + ".jsonl")))

    # catch_up_on_start=False：启动那一次不补
    mem3 = new_mem(memory={"catch_up_on_start": False})
    write_journal(mem3, "private:3004", YDAY, [("user", "【丁】：昨天")])
    check("catch_up_on_start=false → 启动这次不补算", mem3.tick() is False)

    # enabled=False：整个子系统停摆
    mem4 = new_mem(memory={"enabled": False})
    write_journal(mem4, "private:3005", YDAY, [("user", "【戊】：昨天")])
    check("memory.enabled=false → 不归档", mem4.archive_due() == 0)


# ================================================================ 7
def test_recall():
    print("\n[7] 召回")
    mem = new_mem()
    scope = "private:4001"
    mem._alt_client = FakeLLM(payload={
        "digest": "小明是备考 NCRE 二级 C 的学生。",
        "events": [
            {"text": "小明提到下周一要交数据结构实验报告。",
             "keywords": ["数据结构", "实验报告"]},
            {"text": "聊过二叉树遍历：中序切、前序定根。", "keywords": ["二叉树", "遍历"]},
            {"text": "她不太喜欢被反复追问同一件事。", "keywords": ["追问"]},
        ],
    })
    write_journal(mem, scope, YDAY, [("user", "【小明】：随便聊聊")])
    with Reports():
        mem.archive(scope, YDAY)

    hit = mem.recall(scope, "二叉树的前序和中序怎么定根")
    check("带线索时命中相关往事", bool(hit) and "二叉树" in hit[0]["text"],
          str([e["text"][:12] for e in hit]))
    check("不相关的往事不会被召回",
          all("实验报告" not in e["text"] for e in hit),
          str([e["text"][:12] for e in hit]))
    check("线索完全不沾边 → 返回空（宁可不召回也不要塞无关内容）",
          mem.recall(scope, "今天天气怎么样") == [],
          str(mem.recall(scope, "今天天气怎么样")))
    check("没有线索时给最近的几条（保底上下文）",
          len(mem.recall(scope, "")) == 3)
    check("k=0 → 一条都不要", mem.recall(scope, "二叉树", 0) == [])

    blk = mem.block(scope, "二叉树")
    check("提示词片段同时带画像与相关往事",
          "【长期记忆 · 你对这个人的了解】" in blk
          and "【长期记忆 · 与当前话题相关的往事】" in blk)
    check("片段里写了「别把没提过的事说成发生过」（防止模型拿记忆编故事）",
          "别把没提过的事说成发生过" in blk)
    check("召回计数在涨", mem.stats["recalled"] >= 1, str(mem.stats))

    # 没有任何记忆时不该硬塞一个空段落
    mem2 = new_mem()
    check("没有记忆时 block 返回空串（调用方什么都不用加）",
          mem2.block("private:4002", "你好") == "")

    # recall_max_items=0：只给画像
    mem3 = new_mem(memory={"recall_max_items": 0})
    mem3._alt_client = FakeLLM(payload={"digest": "只留画像。", "events": [
        {"text": "一条事件", "keywords": []}]})
    write_journal(mem3, scope, YDAY, [("user", "【小明】：x")])
    with Reports():
        mem3.archive(scope, YDAY)
    blk3 = mem3.block(scope, "事件")
    check("recall_max_items=0 → 只有画像、没有往事段",
          "只留画像" in blk3 and "与当前话题相关的往事" not in blk3, blk3[:60])


# ================================================================ 8
def test_compress():
    print("\n[8] 超容量压缩（先并进画像再删，不是直接丢）")
    mem = new_mem(memory={"capacity_tokens": 300})
    scope = "private:5001"
    check("容量下限被兜住（配置写 1 也不会把库压成 0）",
          new_mem(memory={"capacity_tokens": 1}).capacity == 300)

    old_text = "很久很久以前的那件事"
    rec = mem._load(scope)
    rec["digest"] = {"text": "旧画像", "updated_at": 0.0, "covered_until": ""}
    rec["events"] = [{"id": i, "date": DBYDAY, "text": (old_text if i == 1 else f"第{i}件事" * 30),
                      "keywords": [], "created_at": time.time()}
                     for i in range(1, 13)]
    before = mem._total_tokens(rec)
    mem.summarize = False          # 走「模型不可用」的合并分支，画像里能直接看到被并进来的原文
    with Reports() as rep:
        dropped = mem._compress(rec)
    check("超限时确实压缩了", dropped > 0, f"丢了 {dropped} 条")
    check("报 E-MEM-002（容量水位是运维信息，必须能看见）", rep.has("E-MEM-002"),
          str(rep.codes()))
    check("压缩后回到容量以内", mem._total_tokens(rec) <= mem.capacity,
          f"{mem._total_tokens(rec)} / {mem.capacity}")
    check("最老那条的原文被并进了画像（不是丢掉）",
          old_text in (rec.get("digest") or {}).get("text", ""),
          (rec.get("digest") or {}).get("text", "")[:40])
    check("画像本身也被上限约束（否则每次请求都要为它付钱）",
          A.token_estimate((rec.get("digest") or {}).get("text", "")) <= mem.digest_max,
          f"{A.token_estimate((rec.get('digest') or {}).get('text', ''))} / {mem.digest_max}")
    check("事件列表被真正裁短（不是只改了计数）",
          len(rec["events"]) < 12 and before > mem.capacity,
          f"{before} → {mem._total_tokens(rec)}")
    check("压缩计数被记账", mem.stats["compressed"] == dropped, str(mem.stats))


# ================================================================ 9
def test_forget():
    print("\n[9] 遗忘")
    mem = new_mem()
    scope = "private:6001"
    mem._alt_client = FakeLLM(payload={"digest": "画像", "events": [
        {"text": "一件事", "keywords": []}]})
    write_journal(mem, scope, YDAY, [("user", "【甲】：昨天")])
    with Reports():
        mem.archive(scope, YDAY)
    A.History(scope=scope).push("user", "今天还没归档的话")
    check("删之前：档案和流水都在",
          os.path.isfile(mem._path(scope))
          and os.path.isfile(os.path.join(mem._journal_dir(scope), TODAY + ".jsonl")))

    with Reports() as rep:
        ok = mem.forget(scope)
    check("遗忘成功", ok is True and rep.rows == [], str(rep.rows))
    check("档案被删掉", not os.path.isfile(mem._path(scope)))
    check("尚未归档的流水也一起删（否则下次归档又会把它记回来）",
          not os.path.isdir(mem._journal_dir(scope)))
    check("内存缓存也清了（不然同进程内还会召回已删的记忆）",
          scope not in mem._cache and mem.stats_of(scope)["events"] == 0)
    check("删一个本来就不存在的会话不报错", mem.forget("private:不存在") is True)


# ================================================================ 10
def test_broken():
    print("\n[10] 坏档")
    mem = new_mem()
    scope = "private:7001"
    os.makedirs(mem.dir, exist_ok=True)
    fp = mem._path(scope)
    with open(fp, "w", encoding="utf-8") as f:
        f.write('{"digest": {"text": "被截断的档案"')      # 半截 JSON
    with Reports() as rep:
        rec = mem._load(scope)
    check("坏档被识别并报了 E-MEM-001", rep.has("E-MEM-001"), str(rep.codes()))
    check("原文件改名 .bad 保留现场（不直接覆盖，坏了还能查）",
          os.path.isfile(fp + ".bad") and not os.path.isfile(fp))
    check("程序从空档案继续，不抛异常", rec["events"] == [] and rec["digest"]["text"] == "")
    ctx = rep.ctx_of("E-MEM-001")
    check("现场里写了备份到哪、以及为什么失败（只说『档案坏了』没法修）",
          ctx.get("备份为") == fp + ".bad" and ctx.get("原因"),
          str(ctx))

    # 字段类型不对（手工改过档案）不该把程序带崩
    bad = new_mem()
    os.makedirs(bad.dir, exist_ok=True)
    with open(bad._path("private:7002"), "w", encoding="utf-8") as f:
        json.dump({"digest": "这里是字符串不是对象", "events": "也不是数组",
                   "next_id": "abc"}, f, ensure_ascii=False)
    with Reports():
        r2 = bad._load("private:7002")
    check("字段类型不对时降级为空档案，而不是抛异常",
          isinstance(r2["events"], list) and isinstance(r2["digest"], dict))
    check("next_id 坏掉时按事件数兜底（新事件不会撞 id）",
          isinstance(r2["next_id"], int) and r2["next_id"] >= 1, str(r2["next_id"]))


# ================================================================ 11
def test_agent_integration():
    print("\n[11] Agent 集成")
    cfg = make_cfg()
    cfg["llm"]["system_prompt"] = "你是小清澈。"
    ag = A.Agent(cfg, dry_run=True)
    check("Agent 构造时把长期记忆挂上了（History.push 的钩子靠它）",
          A._MEMORY is ag.mem and isinstance(ag.mem, A.LongTermMemory))
    check("自检行能一眼看出记忆开着还是关着",
          "长期记忆: 开" in ag.mem.summary_line(), ag.mem.summary_line()[:40])

    scope = "private:8001"
    hist = ag.store.history(scope)
    check("History 拿到了自己的 scope（否则写流水时不知道该记给谁）",
          hist.scope == scope, hist.scope)
    hist.push("user", "【小明】：我叫小明")
    check("Agent 的 History.push 也会写流水",
          os.path.isfile(os.path.join(ag.mem._journal_dir(scope), TODAY + ".jsonl")))

    # 塞一段记忆进去，看注入位置
    rec = ag.mem._load(scope)
    rec["digest"] = {"text": "小明把助手叫作阿澈。", "updated_at": time.time(),
                     "covered_until": YDAY}
    rec["events"] = [{"id": 1, "date": YDAY, "text": "她周四要交 C 语言作业。",
                      "keywords": ["C语言"], "created_at": time.time()}]
    hist.push("assistant", "记得的。")
    hist.push("user", "【小明】：C 语言的作业你记得吗")
    hist.push("assistant", "记得，周四交。")
    msgs = ag._messages_with_memory(scope, hist)
    check("第一条仍是人格设定（记忆不能把人设挤掉）",
          msgs[0]["content"] == "你是小清澈。", str(msgs[0])[:40])
    check("记忆紧跟人设之后、排在对话之前（它属于背景，不是谁说过的话）",
          msgs[1]["role"] == "system" and "长期记忆" in msgs[1]["content"],
          str(msgs[1]["content"])[:40])
    check("对话原文次序不变",
          [m["role"] for m in msgs[2:]] == ["user", "assistant", "user", "assistant"],
          str([m["role"] for m in msgs[2:]]))
    check("召回线索取自**最后一条对方消息**（不是整段上下文）",
          hist.last_user_text() == "【小明】：C 语言的作业你记得吗",
          hist.last_user_text())
    check("相关往事被召回进提示词",
          "与当前话题相关的往事" in msgs[1]["content"]
          and "作业" in msgs[1]["content"],
          msgs[1]["content"][-100:])

    check("Agent.forget_memory 直接落到档案上",
          ag.forget_memory(scope) is True and not os.path.isfile(ag.mem._path(scope)))

    # 一条记忆都没有时，messages 必须与改造前**逐字节一致**（不能凭空多一条 system）
    bare = "private:8002"
    h2 = ag.store.history(bare)
    h2.push("user", "【甲】：你好")
    plain = A.History(max_entries=40, system_prompt="你是小清澈。", scope=bare)
    plain.push("user", "【甲】：你好")
    check("没有长期记忆时，给模型的 messages 与旧行为完全一致",
          ag._messages_with_memory(bare, h2) == plain.build_messages(),
          str(ag._messages_with_memory(bare, h2))[:60])

    # 注入失败也不能影响回复（记忆是锦上添花）
    with Reports() as rep:
        ag.mem.block = lambda scope, query="": (_ for _ in ()).throw(RuntimeError("boom"))
        msgs2 = ag._messages_with_memory(scope, hist)
        check("注入炸了也要把对话照常发出去", len(msgs2) == len(hist.build_messages()))
        check("并且报 E-MEM-008（不许静默降级）", rep.has("E-MEM-008"), str(rep.codes()))


# ================================================================ 12
def test_server_side():
    print("\n[12] 控制台侧：统计与连带删除")
    mem = new_mem()
    scope = "private:9001"
    mem._alt_client = FakeLLM(payload={
        "digest": "小明在备考。", "events": [{"text": "一条往事", "keywords": []}]})
    write_journal(mem, scope, YDAY, [("user", "【甲】：昨天")])
    with Reports():
        mem.archive(scope, YDAY)
    A.History(scope=scope).push("user", "今天的记录")

    st = S.memory_stats(scope)
    check("壳进程与 agent 解析出的是**同一个**记忆目录（两边各有一套路径逻辑，"
          "漂了就会『界面看不到记忆』或『删不干净』）",
          os.path.normcase(os.path.abspath(S._memory_dir()))
          == os.path.normcase(os.path.abspath(mem.dir)),
          f"{S._memory_dir()} vs {mem.dir}")
    check("壳进程能读出记忆条数与用量（不用 import agent）",
          st["exists"] and st["events"] == 1 and st["tokens"] > 0, str(st))
    check("画像有无被标出来", st["has_digest"] is True)
    check("待归档天数被标出来（界面上要能看出『有内容但还没压成记忆』）",
          st["journal_days"] == 1, str(st["journal_days"]))
    check("shell 侧的 token 口径与 agent 一致（否则界面上的水位是错的）",
          S._tok("你好") == A.token_estimate("你好")
          and S._tok("hello world") == A.token_estimate("hello world"),
          f"{S._tok('hello world')} vs {A.token_estimate('hello world')}")
    check("没有记忆的会话不报错，exists=False",
          S.memory_stats("private:9999") == {
              "exists": False, "events": 0, "tokens": 0, "has_digest": False,
              "last_archive_date": "", "journal_days": 0},
          str(S.memory_stats("private:9999")))

    r = S._delete_memory(scope)
    check("连带删除成功", r["deleted"] and r["files"] >= 2, str(r))
    check("档案与流水都没了",
          not os.path.isfile(os.path.join(S._memory_dir(), S._memory_file_name(scope)))
          and not os.path.isdir(os.path.join(S._memory_dir(), "journal",
                                             S._memory_file_name(scope)[:-5])))
    check("重复删不报错（界面上连点两次是常事）",
          S._delete_memory(scope) == {"deleted": True, "files": 0, "error": ""},
          str(S._delete_memory(scope)))

    # 列表接口把记忆统计挂在每一项上
    cdir = S._conversations_dir()
    os.makedirs(cdir, exist_ok=True)
    with open(os.path.join(cdir, S._conv_file_name(scope)), "w", encoding="utf-8") as f:
        json.dump({"_scope": scope, "history": [{"role": "user", "content": "你好"}],
                   "used_at": time.time(), "continuous": {"active": False}}, f,
                  ensure_ascii=False)
    S._memory_dir()                                    # 确认目录解析不抛异常
    data = S.list_conversations()
    row = next((i for i in data["items"] if i["scope"] == scope), None)
    check("会话列表每项都带 memory 字段", row is not None and "memory" in row, str(row))
    check("列表里报出了记忆目录（界面上要能直接看到数据在哪）",
          data.get("memory_dir") == S._memory_dir(), str(data.get("memory_dir")))


# ================================================================ 13
def test_error_catalog():
    print("\n[13] 错误码目录")
    for code in [f"E-MEM-00{i}" for i in range(1, 9)] + ["E-WEB-006"]:
        it = EC.get(code)
        check(f"{code} 有标题、有判据、有动作",
              it["title"] != "未知错误码" and it["causes"] and it["fixes"],
              it.get("title", ""))
    check("MEM 域已登记（错误码速查页要按域分组）",
          EC.DOMAINS.get("MEM") == "长期记忆", str(EC.DOMAINS.get("MEM")))


def main() -> int:
    print("=" * 74)
    print(f"长期记忆自测（数据目录 {TMP}）")
    print("=" * 74)
    try:
        test_terms()
        test_names()
        test_journal()
        test_archive_ok()
        test_archive_fallback()
        test_catch_up()
        test_recall()
        test_compress()
        test_forget()
        test_broken()
        test_agent_integration()
        test_server_side()
        test_error_catalog()
    finally:
        print("\n" + "=" * 74)
        print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
        for name in FAIL:
            print(f"  ✗ {name}")
        print("=" * 74)
        shutil.rmtree(TMP, ignore_errors=True)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
