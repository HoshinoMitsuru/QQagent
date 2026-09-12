# -*- coding: utf-8 -*-
"""
reply_queue.py — 回复排队 + 风控限速 + 补充消息合并

## 要解决什么

UIA 路线下「读 / 写 / 发」都必须发生在**当前打开的那个会话**上，所以 UI 是唯一的串行资源。
更硬的约束不是性能，而是**账号风控**：个人号持续高频发送会触发腾讯的风控。
于是设计目标从「尽快回」变成「**按安全频率回，但看起来仍像在正常聊天**」。

## 三条规则

1. **限速（风控）**：相邻两次发送间隔 ≥ `min_interval_seconds`，
   且 60 秒滑动窗口内发送次数 ≤ `max_replies_per_minute`。两条同时生效。
2. **静默窗口 + 硬上限**：一条消息入队后有防抖等待窗（沿用 agent.Debouncer 的自适应值），
   窗口里对方继续发就顺延 —— 但顺延**不超过 `max_hold_seconds`**，
   否则「每 20 秒来一句」的用户会让这一条永远结算不掉（饿死）。
3. **排队期间补充消息的 5 秒阈值**（本次新增的核心规则）：
   用户已在队列里（还没轮到），这时又发来一句：
   - 预计还要等 **> `merge_if_wait_over_seconds`（默认 5s）** → **并入**他待提交的那批上下文，
     不额外产生一次回复。反正他本来也要等，多等一句不亏，而且 AI 一次能看到全部内容。
   - 预计 **≤ 5s** 就能轮到他 → **不掺和**，让他照常排队，这条补充消息走正常路径、
     之后单独获得一次回复。

   为什么后者也可以接受：真人聊天里「话音未落对方又接一句」本来就常见，
   两条回复分别对应两段话，撞车是符合现实的（用户明确认可这个取舍）。

## 为什么把「预计还要等多久」算清楚是必要的

阈值判断依赖剩余等待时间。剩余等待由三部分构成：

    remaining = max(0, 防抖到期 - now)          # 这条消息自己的静默窗还没走完
              + 前面排队的项 × unit_cost         # 单通道：前面每个人都要占一次 UI 串行
              ⊕ max(remaining, 风控等待)         # 风控可能把整体往后推

`unit_cost_seconds` 是单次「切会话 + 读 + 写 + 发 + 模型」的经验耗时（默认 3.0s，
来自 `并发能力评估与优化方向.md` 的实测：UI ≈1.2s、deepseek-flash ≈1.3~1.8s）。

## 自检

    python reply_queue.py --selftest

不依赖 QQ、不依赖网络，纯逻辑单测。
"""

from __future__ import annotations

import argparse
import json
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


def _num(d: dict, key: str, default: float) -> float:
    """
    取数值配置，**只有 None 才算缺省**。

    为什么不能写 `d.get(key) or default`：那样 `0` / `0.0` 会被当成缺省值吞掉。
    实测踩过 —— `jitter_seconds: 0` 被 or 成 3.0，单测里表现为「明明配了 0 还是有抖动」。
    """
    v = (d or {}).get(key)
    return default if v is None else float(v)


def _int(d: dict, key: str, default: int) -> int:
    v = (d or {}).get(key)
    return default if v is None else int(v)


# ============================================================ 风控预算
class RateBudget:
    """
    账号风控预算。两条约束同时生效，取更严格的那个：

        1) 硬间隔：相邻两次发送之间至少 min_interval_seconds
        2) 频率：60 秒滑动窗口内不超过 max_replies_per_minute 次

    这两个数是**唯一直接决定「你到底能服务几个用户」的数**，
    调大之前请先读 `并发能力评估与优化方向.md` §9.5：对个人号，
    C=28 意味着每分钟约 28 条，几乎必然触发风控。
    """

    def __init__(self, cfg: dict):
        q = (cfg or {}).get("queue") or {}
        self.per_minute = max(1, _int(q, "max_replies_per_minute", 12))
        self.min_interval = max(0.0, _num(q, "min_interval_seconds", 5.0))
        self._sends: deque = deque()     # 已发送时刻（滑动窗口）
        self._last = 0.0

    def _prune(self, now: float) -> None:
        while self._sends and now - self._sends[0] >= 60.0:
            self._sends.popleft()

    def wait_seconds(self, now: Optional[float] = None) -> float:
        """还要等多久才能发下一条（0 = 现在就能发）。"""
        now = time.time() if now is None else now
        self._prune(now)
        w_interval = 0.0
        if self._last:
            w_interval = max(0.0, self._last + self.min_interval - now)
        w_window = 0.0
        if len(self._sends) >= self.per_minute:
            w_window = max(0.0, self._sends[0] + 60.0 - now)
        return max(w_interval, w_window)

    def note_send(self, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self._sends.append(now)
        self._last = now
        self._prune(now)

    def snapshot(self, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        self._prune(now)
        return {"used_in_window": len(self._sends), "per_minute": self.per_minute,
                "wait": round(self.wait_seconds(now), 2),
                "min_interval": self.min_interval}


# ============================================================ 队列项
@dataclass
class QueuedReply:
    """一个「待回复」的任务：某个会话攒下的一批对方消息，等待被结算成一次回复。"""
    scope: str                     # 身份主键，形如 private:3302676083 / group:1098451742
    display_name: str              # 会话列表上显示的名字（用于切会话）
    uin: str                       # QQ 号 / 群号（用于发送前复核）
    texts: list = field(default_factory=list)
    keys: list = field(default_factory=list)
    seqs: list = field(default_factory=list)
    first_at: float = 0.0
    ready_at: float = 0.0          # 防抖到期时刻（可被合并顺延）
    hard_deadline: float = 0.0     # 硬上限：first_at + max_hold_seconds
    merged: int = 0                # 被合并进来的补充消息条数
    attempts: int = 0              # 因发送前复核失败而重排的次数
    batch: list = field(default_factory=list)   # agent 侧的原始 PendingMessage 批次
    pushed: bool = False           # 这批消息是否已经写进会话上下文（重排后不重复写）

    # ---- 「降级方案」相关：发现即排队，正文推迟到出队时再总读取 ----
    # 桌面 UI 上每个会话只保留**最新一条消息的节选**，摘要不足以构造上下文，
    # 所以「发现 + 占位入队」和「读正文」必须分成两步。
    pending_read: bool = False     # True = 正文还没读，prepare 阶段要切过去做一次总读取
    unread_hint: int = 0           # 发现时的未读条数（首见会话建基线时用它决定保留最后几条）
    reply: str = ""                # prepare 阶段生成好的回复，等风控放行就发
    prepared: bool = False         # 是否已完成「读 + 生成」
    prepared_at: float = 0.0

    # ---- 「已定稿」状态：回复一旦生成，这一项就**冻结**，只等发出去 ----
    # drafted   : 回复已经粘进 QQ 输入框，但还没按发送。重试时**原地重发**即可，
    #             不必重新粘贴、更不必重新调模型。
    # fail_*    : 发送阶段失败的次数与最后一次原因（带错误码）。
    drafted: bool = False
    draft_at: float = 0.0
    fail_count: int = 0
    last_fail: str = ""

    # ---- 迟到消息：回复**生成之后**才到的消息 ----
    # 它们绝不能塞进 texts（那句话已经按旧内容写好了，塞进去就等于「没被回应」），
    # 也绝不能因为「这一项已经定稿」就被丢掉（那就是「消息永远没人回」）。
    # 所以单独攒着，等这一项发出去之后再结转成新的一条。
    late: list = field(default_factory=list)     # [(text, key, seq)]
    late_hint: int = 0                           # 迟到期间的未读条数（结转时用）
    late_from_discovery: bool = False            # 迟到消息里是否含「发现」路径来的（决定要不要重读）

    @property
    def count(self) -> int:
        return len(self.texts)

    @property
    def late_count(self) -> int:
        return len(self.late)

    @property
    def has_late(self) -> bool:
        """
        是否有「定稿之后才到的动静」需要在发完之后结转。

        两种来源：
          · 实时路径 —— 直接带着正文进 `late` 桶
          · 发现路径 —— 只有「有动静」这个事实（没有正文），标记在 `late_from_discovery`
        两者都必须能触发结转，否则发现路径来的迟到消息会被静默丢掉。
        """
        return bool(self.late) or bool(self.late_from_discovery)

    @property
    def frozen(self) -> bool:
        """已定稿待发：回复生成好了（或已粘进输入框），此时**不接受任何内容改动**。"""
        return bool(self.prepared or self.drafted)

    @property
    def is_group(self) -> bool:
        return self.scope.startswith("group:")

    def combined(self, fmt: str = "【{sender}】：{content}") -> str:
        return "\n".join(self.texts)

    def to_dict(self) -> dict:
        return {"scope": self.scope, "display_name": self.display_name, "uin": self.uin,
                "count": self.count, "merged": self.merged, "attempts": self.attempts,
                "pending_read": self.pending_read, "prepared": self.prepared,
                "drafted": self.drafted, "late": self.late_count,
                "fail_count": self.fail_count, "last_fail": self.last_fail,
                "ready_in": round(max(0.0, self.ready_at - time.time()), 2)}

    # ---------------------------------------------------- 落盘（重启不丢待发回复）
    def to_state(self) -> dict:
        """
        序列化「需要送达」的全部状态。

        为什么必须把 `reply` 也存下来：这个程序会被反复重启（调试、改配置、加固）。
        队列只在内存里的话，一次重启就让「已生成但没发出」的回复凭空消失 ——
        而那批消息的正文此时已经写进上下文、也被标成已读，重启后再也不会被认成新消息，
        于是**永远不会有回应**。这正是「尽可能避免消息丢失」要堵的洞。
        """
        return {
            "scope": self.scope, "display_name": self.display_name, "uin": self.uin,
            "texts": list(self.texts), "keys": list(self.keys), "seqs": list(self.seqs),
            "first_at": self.first_at, "ready_at": self.ready_at,
            "hard_deadline": self.hard_deadline, "merged": self.merged,
            "attempts": self.attempts, "pushed": self.pushed,
            "pending_read": self.pending_read, "unread_hint": self.unread_hint,
            "reply": self.reply, "prepared": self.prepared, "prepared_at": self.prepared_at,
            "drafted": self.drafted, "draft_at": self.draft_at,
            "fail_count": self.fail_count, "last_fail": self.last_fail,
            "late": [list(x) for x in self.late], "late_hint": self.late_hint,
            "late_from_discovery": self.late_from_discovery,
        }

    @classmethod
    def from_state(cls, d: dict) -> "QueuedReply":
        it = cls(scope=str(d.get("scope") or ""),
                 display_name=str(d.get("display_name") or ""),
                 uin=str(d.get("uin") or ""))
        it.texts = list(d.get("texts") or [])
        it.keys = list(d.get("keys") or [])
        it.seqs = [int(x) for x in (d.get("seqs") or [])]
        it.first_at = float(d.get("first_at") or 0.0)
        it.ready_at = float(d.get("ready_at") or 0.0)
        it.hard_deadline = float(d.get("hard_deadline") or 0.0)
        it.merged = int(d.get("merged") or 0)
        it.attempts = int(d.get("attempts") or 0)
        it.pushed = bool(d.get("pushed"))
        it.pending_read = bool(d.get("pending_read"))
        it.unread_hint = int(d.get("unread_hint") or 0)
        it.reply = str(d.get("reply") or "")
        it.prepared = bool(d.get("prepared"))
        it.prepared_at = float(d.get("prepared_at") or 0.0)
        it.drafted = bool(d.get("drafted"))
        it.draft_at = float(d.get("draft_at") or 0.0)
        it.fail_count = int(d.get("fail_count") or 0)
        it.last_fail = str(d.get("last_fail") or "")
        it.late = [tuple(x) for x in (d.get("late") or [])]
        it.late_hint = int(d.get("late_hint") or 0)
        it.late_from_discovery = bool(d.get("late_from_discovery"))
        return it


# ============================================================ 队列
class ReplyQueue:
    """
    单通道 FIFO 队列 + 风控预算。

    为什么是单通道：UI 一次只能操作一个会话（见 `并发能力评估与优化方向.md` §3）。
    队列的价值不是并发，而是**把「什么时候发」和「发什么」解耦** ——
    模型可以在等风控额度的时候先算好，UI 时间只用于真正必须独占前台的那 385ms。
    """

    def __init__(self, cfg: dict, seed: Optional[int] = None):
        q = (cfg or {}).get("queue") or {}
        self.cfg = cfg or {}
        self.enabled = bool(q.get("enabled", True))
        self.merge_threshold = _num(q, "merge_if_wait_over_seconds", 5.0)
        self.unit_cost = _num(q, "unit_cost_seconds", 3.0)
        self.max_hold = _num(q, "max_hold_seconds", 30.0)
        self.jitter = _num(q, "jitter_seconds", 3.0)
        self.max_attempts = _int(q, "max_attempts", 3)
        self.items: list[QueuedReply] = []
        self.budget = RateBudget(self.cfg)
        self._rng = random.Random(seed)
        self.stats = {"submitted": 0, "merged": 0, "served": 0,
                      "aborted": 0, "rejected_merge": 0, "duplicate_skipped": 0,
                      "deferred_to_late": 0, "send_failed": 0, "restored": 0,
                      "carried_over": 0}

    # ---------------------------------------------------- 查询
    def get(self, scope: str) -> Optional[QueuedReply]:
        for it in self.items:
            if it.scope == scope:
                return it
        return None

    def __len__(self) -> int:
        return len(self.items)

    def __bool__(self) -> bool:
        return bool(self.items)

    def scopes(self) -> list:
        return [it.scope for it in self.items]

    # ---------------------------------------------------- 入队
    def submit(self, scope: str, display_name: str, uin: str,
               text: str, key: str = "", seq: int = 0,
               now: Optional[float] = None, wait_seconds: float = 0.0) -> QueuedReply:
        """新建一条待回复项（正常路径：该 scope 当前不在队列里）。"""
        now = time.time() if now is None else now
        ready = now + max(0.0, wait_seconds)
        # 抖动：避免多个会话的窗口同时到期，一起涌入 UI 串行队列
        ready += self._rng.uniform(0.0, max(0.0, self.jitter))
        item = QueuedReply(
            scope=scope, display_name=display_name, uin=uin,
            # text 为空 = 发现路径的占位项，正文等 prepare 阶段读，所以 texts 留空
            texts=([text] if text else []), keys=([key] if key else []),
            seqs=[seq], first_at=now, ready_at=ready,
            hard_deadline=now + self.max_hold,
        )
        self.items.append(item)
        self.stats["submitted"] += 1
        return item

    def ensure_pending(self, scope: str, display_name: str, uin: str = "",
                       now: Optional[float] = None, wait_seconds: float = 0.0,
                       unread_hint: int = 0) -> tuple:
        """
        发现路径的入队：**只占位，不带正文**（正文留到 prepare 阶段做总读取）。

        返回 (item, created)。

        - 该 scope 不在队列里 → 新建一条 pending_read 项，返回 (item, True)
        - 已经在队列里 → 什么都不做，返回 (item, False)
        - 已经在队列里、但**回复都已经生成好了**，这时又发现新动静 →
          说明「读完之后」内容又变了：把备好的回复作废，让它重读重生成。
          仍然返回 (item, False)，不新增队列项（同一会话不该被回两次）。

        为什么发现阶段不做 5s 阈值合并判定：这一步手上只有「最新一条消息的节选」，
        没有正文，分不清「对方又发了一句」和「同一批消息的重渲染」。
        宁可少回一次（下一轮总读取会读到全部），也不要因为 UI 重渲染就连发两条。
        """
        now = time.time() if now is None else now
        it = self.get(scope)
        if it is None:
            item = self.submit(scope, display_name, uin, text="",
                               now=now, wait_seconds=wait_seconds)
            item.pending_read = True
            item.unread_hint = max(0, int(unread_hint))
            return item, True

        # 未读提示始终取较大者：它是「首见某会话时该保留最后几条」的依据，
        # 中途只增不减才不会漏掉对方后面又发的那几条。
        it.unread_hint = max(it.unread_hint, int(unread_hint))

        # ⚠️ 已定稿待发的项**不作废、不改动**。
        #
        # 原实现是 `it.prepared = False; it.reply = ""`（备好的回复对不上新内容了 → 作废重来）。
        # 在「打扰是主要成本」的前提下那样做合理；但 VM 场景下正确性优先 ——
        # 作废意味着**一条已经生成的回复被丢掉**，之后要么重新生成（内容会变、还要再花一次
        # 模型调用），要么在重试上限用尽后被撤单，那就是「这条消息永远没人回」。
        #
        # 现在改成：让它先把已定稿的那句发出去，新动静记到 late 桶里，
        # 等这一项发完再结转成新的一条。**老的先发，新的后发，两边都不丢。**
        if it.frozen:
            it.late_from_discovery = True
            it.late_hint = max(it.late_hint, int(unread_hint))
            self.stats["deferred_to_late"] += 1
            return it, False

        self.stats["duplicate_skipped"] += 1
        return it, False

    def remaining_wait(self, scope: str, now: Optional[float] = None) -> float:
        """
        该 scope 预计还要等多久才会被结算（秒）。

        构成： 防抖剩余 + 前面排队的项 × 单次成本，最后与风控等待取较大者。
        """
        now = time.time() if now is None else now
        it = self.get(scope)
        if it is None:
            return 0.0
        # 用对象身份定位，不要用 list.index —— QueuedReply 是 dataclass，
        # index 走的是字段值相等，两个内容相同的条目会互相误匹配。
        idx = next(i for i, x in enumerate(self.items) if x is it)
        ahead = sum(self.unit_cost for _ in self.items[:idx])
        w = max(0.0, it.ready_at - now) + ahead
        return max(w, self.budget.wait_seconds(now))

    def offer_followup(self, scope: str, text: str, key: str = "", seq: int = 0,
                       now: Optional[float] = None) -> str:
        """
        **核心规则**：用户排队期间又发来一句，决定「并入」还是「另开一次回复」。

        返回：
            'absent'    该 scope 不在队列里 → 调用方按正常路径 submit
            'merged'    已并入他待提交的上下文（→ 不会再为这句单独回一次）
            'deferred'  **这一项已经定稿**（回复生成好了/已粘进输入框）→
                        新消息记进 late 桶，等它发完之后结转成新的一条。
                        调用方**不要**再 submit —— 内容没丢，只是排到老的后面。
            'close'     剩余等待 ≤ 阈值 → 不掺和，调用方另 submit 一条（会单独回一次）
        """
        now = time.time() if now is None else now
        it = self.get(scope)
        if it is None:
            return "absent"

        # ⚠️ 已定稿的项：回复是按它当前的 texts 生成的，新消息**不能**并进去 ——
        # 并进去的结果是「发出去的那句话没涵盖新内容，而新内容已经进了 texts
        # 不会再被单独处理」，等于这条消息事实上没被回应。
        # 记到 late 桶里，等这一项发完再结转成新的一条。
        if it.frozen:
            it.late.append((text, key, seq))
            it.late_hint += 1
            self.stats["deferred_to_late"] += 1
            return "deferred"

        wait = self.remaining_wait(scope, now)
        if wait <= self.merge_threshold:
            self.stats["rejected_merge"] += 1
            return "close"

        if not it.pending_read:
            it.texts.append(text)
            it.keys.append(key)
            it.seqs.append(seq)
        # pending_read 的占位项不往里塞正文：它等的是「总读取」，到时候会把
        # 这一段完整读回来，塞摘要/单条反而会让上下文出现重复。
        # 但窗口一样要顺延，而且**不许越过硬上限** ——
        # 否则「每 20 秒一句」的用户永远结算不掉。
        it.merged += 1
        new_ready = now + self.unit_cost
        it.ready_at = min(max(it.ready_at, new_ready), it.hard_deadline)
        self.stats["merged"] += 1
        return "merged"

    # ---------------------------------------------------- 出队
    def pick(self, now: Optional[float] = None,
             ignore_budget: bool = False) -> Optional[QueuedReply]:
        """
        取下一个可以处理的项。

        必须满足：**该 scope 自己的静默窗口已到点**（ready_at <= now）。
        默认还要风控放行；`ignore_budget=True` 只给「读 + 生成」阶段用 ——
        那一步不发送、不消耗风控额度，可以提前做，让真正排到时能立刻发出去。

        ## 排序规则（VM 场景下按「正确性优先」重排过）

            1. **已定稿待发的项优先** —— 它们代表「已经答应要发、但还没发出去」的回复。
               让新来的项抢先会把这条承诺一直往后推（挤压），而要求是
               「先发出本应发出但未发出的」。
            2. 定稿但**连续失败多次**的项降权（`fail_count >= 3`）——
               否则一条永远发不出去的项会霸占循环，把其它会话全饿死。
               注意只是降权，**不丢弃**：它仍然留在队列里继续重试。
            3. 其余按 `first_at` FIFO，避免饿死先来的会话。
        """
        now = time.time() if now is None else now
        if not self.items:
            return None
        if not ignore_budget and self.budget.wait_seconds(now) > 0:
            return None
        cands = [it for it in self.items if it.ready_at <= now]
        if not cands:
            return None
        cands.sort(key=lambda x: (
            # 0 = 已定稿待发（先发老的）；1 = 普通；2 = 卡住的（让路，但不丢）
            2 if x.fail_count >= 3 else (0 if x.frozen else 1),
            x.first_at,
            x.seqs[0] if x.seqs else 0,
        ))
        return cands[0]

    def drop(self, scope: str, aborted: bool = False) -> Optional[QueuedReply]:
        it = self.get(scope)
        if it is None:
            return None
        self.items = [x for x in self.items if x is not it]
        if aborted:
            self.stats["aborted"] += 1
        return it

    def requeue(self, item: QueuedReply, delay: float = 0.0,
                now: Optional[float] = None, fail: str = "") -> None:
        """
        把这一项放回队尾重排（发送阶段的失败都走这里）。

        **绝不丢数据**：texts / reply / late 全部原样保留。
        `fail` 是这一次的失败原因（带错误码），会记在 item 上供界面与日志查看。

        注意不要把 texts 清掉：上下文还没交给模型、更没发出去，
        原样保留才能在下一次真的发对的时候用上。
        """
        now = time.time() if now is None else now
        item.attempts += 1
        if fail:
            item.fail_count += 1
            item.last_fail = fail
        item.ready_at = now + max(0.0, delay)
        item.hard_deadline = max(item.hard_deadline, now + self.max_hold)
        self.items = [x for x in self.items if x is not item]   # 身份比较，非值比较
        self.items.append(item)
        self.stats["aborted"] += 1

    def note_send_failure(self, item: QueuedReply, reason: str) -> None:
        """
        记录一次**发送阶段**的失败（不做重排，只记账）。

        与 `requeue` 分开的原因：`requeue` 会重置 ready_at（delay），
        而「发送失败」需要的是「尽快原地重试」——因为回复已经生成好、
        甚至已经粘进输入框了，重试路径极短（校验 + Invoke）。
        """
        item.fail_count += 1
        item.last_fail = reason
        self.stats["send_failed"] += 1

    # ---------------------------------------------------- 待发队列落盘
    def dump_state(self) -> dict:
        return {"version": 1, "saved_at": time.time(),
                "items": [it.to_state() for it in self.items]}

    def load_state(self, data: dict) -> int:
        """
        从磁盘恢复待发队列，返回恢复了几条。

        只恢复「还有事没做完」的项；已经在 `sent` 状态的不存在（发送成功即 drop）。
        """
        if not isinstance(data, dict):
            return 0
        n = 0
        for row in (data.get("items") or []):
            try:
                it = QueuedReply.from_state(row)
            except Exception:
                continue
            if not it.scope:
                continue
            # 恢复后的时间戳可能已经很旧（上次运行留下的）：
            # 让它们立刻可发，而不是按旧的 ready_at 再等一次
            now = time.time()
            it.ready_at = min(it.ready_at or now, now)
            it.hard_deadline = max(it.hard_deadline, now + self.max_hold)
            it.fail_count = max(it.fail_count, 0)
            self.items = [x for x in self.items if x.scope != it.scope]
            self.items.append(it)
            n += 1
        if n:
            self.stats["restored"] = n
        return n

    def unsent(self) -> list:
        """「已经生成好回复、但还没发出去」的项 —— 界面与诊断要看的就是这些。"""
        return [it for it in self.items if it.frozen]

    def stuck(self) -> list:
        """连续失败多次、可能永远发不出去的项（仍然留在队列里，不丢）。"""
        return [it for it in self.items if it.fail_count >= 3]

    def served(self, now: Optional[float] = None) -> None:
        """结算成功：记一笔风控用量。"""
        self.budget.note_send(now)
        self.stats["served"] += 1

    # ---------------------------------------------------- 观察
    def snapshot(self, now: Optional[float] = None) -> str:
        now = time.time() if now is None else now
        if not self.items:
            return "队列空"
        rows = []
        for i, it in enumerate(self.items):
            stage = ("→已备好" if it.prepared
                     else "→待总读" if it.pending_read
                     else "→待发")
            rows.append(f"{i}:{it.display_name}({it.count}条"
                        f"{'+' + str(it.merged) if it.merged else ''}{stage}"
                        f",还需{self.remaining_wait(it.scope, now):.1f}s)")
        b = self.budget.snapshot(now)
        return (f"队列 {len(self.items)} 项 | {' '.join(rows)} | "
                f"风控 {b['used_in_window']}/{b['per_minute']}·{b['wait']}s")


# ============================================================ 自检
def _cfg(**kw) -> dict:
    q = {"enabled": True, "max_replies_per_minute": 12, "min_interval_seconds": 5.0,
         "merge_if_wait_over_seconds": 5.0, "unit_cost_seconds": 3.0,
         "max_hold_seconds": 30.0, "jitter_seconds": 0.0}
    q.update(kw)
    return {"queue": q}


def selftest() -> int:
    ok = fail = 0

    def case(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
            print(f"  [PASS] {name}")
        else:
            fail += 1
            print(f"  [FAIL] {name} {extra}")

    print("=" * 70)
    print("ReplyQueue 自检")
    print("=" * 70)

    # ---------------- 1. 风控：硬间隔 ----------------
    print("\n[1] 风控预算 · 硬间隔")
    rq = ReplyQueue(_cfg(), seed=1)
    t = 1000.0
    case("初始无需等待", rq.budget.wait_seconds(t) == 0.0)
    rq.served(t)
    case("刚发完要等满 min_interval", abs(rq.budget.wait_seconds(t) - 5.0) < 1e-6,
         f"got {rq.budget.wait_seconds(t)}")
    case("t+4.9 还没放行", rq.budget.wait_seconds(t + 4.9) > 0)
    case("t+5.0 已放行", rq.budget.wait_seconds(t + 5.0) == 0.0)

    # ---------------- 2. 风控：滑动窗口 ----------------
    print("\n[2] 风控预算 · 60s 滑动窗口（上限 3/分）")
    rq = ReplyQueue(_cfg(max_replies_per_minute=3, min_interval_seconds=0.0), seed=1)
    for i in range(3):
        rq.served(t + i * 1.0)
    # 窗口里已有 3 条 → 第 4 条必须等到第 1 条满 60s
    case("窗口满时等到最老一条过期",
         abs(rq.budget.wait_seconds(t + 3.0) - 57.0) < 1e-6,
         f"got {rq.budget.wait_seconds(t + 3.0)}")
    case("窗口滑过后放行", rq.budget.wait_seconds(t + 60.0) == 0.0)

    # ---------------- 3. 5s 阈值：合并 ----------------
    print("\n[3] 补充消息合并 · 剩余等待 > 5s → merged")
    rq = ReplyQueue(_cfg(jitter_seconds=0.0), seed=1)
    # 先让另外两个人排在前面，阿离排在队尾
    rq.submit("private:1002", "小北", "1002", "【小北】：你好", now=t, wait_seconds=0.0)
    rq.submit("private:1003", "阿茶", "1003", "【阿茶】：hi", now=t, wait_seconds=0.0)
    rq.submit("private:1001", "阿离", "1001", "【阿离】：在吗",
              now=t, wait_seconds=5.0)
    w = rq.remaining_wait("private:1001", t)
    case(f"剩余等待 {w:.1f}s 确实 > 5s", w > 5.0, f"got {w:.1f}")
    r = rq.offer_followup("private:1001", "【阿离】：算了没事", "k2", 2, now=t)
    case("判定为 merged", r == "merged", f"got {r}")
    it = rq.get("private:1001")
    case("并入后条目数 = 2", it.count == 2, f"got {it.count}")
    case("merged 计数 +1", it.merged == 1)
    case("不新增队列项", len(rq) == 3, f"got {len(rq)}")
    case("队列项里的文本顺序正确", it.texts == ["【阿离】：在吗", "【阿离】：算了没事"],
         f"{it.texts}")

    # ---------------- 4. 5s 阈值：不合并 ----------------
    print("\n[4] 补充消息合并 · 剩余等待 ≤ 5s → close（另开一次回复）")
    rq = ReplyQueue(_cfg(jitter_seconds=0.0), seed=1)
    rq.submit("private:2001", "小满", "2001", "【小满】：早", now=t, wait_seconds=2.0)
    w = rq.remaining_wait("private:2001", t)
    case(f"剩余等待 {w:.1f}s ≤ 5s", w <= 5.0)
    r = rq.offer_followup("private:2001", "【小满】：今天有空吗", "k2", 2, now=t)
    case("判定为 close", r == "close", f"got {r}")
    case("原条目未被动", rq.get("private:2001").count == 1)
    case("rejected_merge 计数 +1", rq.stats["rejected_merge"] == 1)
    # 调用方此时会另 submit 一条 → 同一 scope 两个条目、两次回复
    rq.submit("private:2001", "小满", "2001", "【小满】：今天有空吗", "k2", 2,
              now=t, wait_seconds=5.0)
    case("同一 scope 允许两条队列项", len(rq.items) == 2)

    # ---------------- 5. 不在队列里 ----------------
    print("\n[5] 补充消息合并 · 不在队列 → absent")
    rq = ReplyQueue(_cfg(), seed=1)
    case("返回 absent", rq.offer_followup("private:9", "x", now=t) == "absent")

    # ---------------- 6. 硬上限：不许无限顺延 ----------------
    print("\n[6] 静默窗硬上限（max_hold=30s）")
    rq = ReplyQueue(_cfg(jitter_seconds=0.0, max_hold_seconds=30.0,
                         unit_cost_seconds=1.0), seed=1)
    rq.submit("private:3001", "话痨", "3001", "【话痨】：1", now=t, wait_seconds=5.0)
    it = rq.get("private:3001")
    # 模拟对方每 20 秒来一句，持续 5 分钟
    for i in range(15):
        t2 = t + i * 20.0
        rq.offer_followup("private:3001", f"【话痨】：{i + 2}", now=t2)
        if len(rq.items) > 1:
            break
    case("硬上限内 ready_at 不越界", it.ready_at <= it.hard_deadline + 1e-9,
         f"ready={it.ready_at} hard={it.hard_deadline}")
    case("到点后可被 pick（不会饿死）", rq.pick(t + 35.0) is not None)

    # ---------------- 7. FIFO 与 ready 条件 ----------------
    print("\n[7] 出队顺序与就绪条件")
    rq = ReplyQueue(_cfg(jitter_seconds=0.0), seed=1)
    rq.submit("private:A", "A", "1", "a", now=t, wait_seconds=10.0)
    rq.submit("private:B", "B", "2", "b", now=t + 1, wait_seconds=0.0)
    case("A 没到点 → 只能先服务 B", rq.pick(t + 2) is not None
         and rq.pick(t + 2).scope == "private:B")
    case("A 到点后轮到 A", rq.pick(t + 11).scope == "private:A")

    # ---------------- 8. 风控挡在出队前 ----------------
    print("\n[8] 风控优先于队列")
    rq = ReplyQueue(_cfg(jitter_seconds=0.0), seed=1)
    rq.submit("private:C", "C", "3", "c", now=t, wait_seconds=0.0)
    rq.served(t)                      # 刚发过一条
    case("风控未放行 → pick 返回 None", rq.pick(t + 1.0) is None)
    case("风控放行 → pick 成功", rq.pick(t + 5.0) is not None)

    # ---------------- 9. 重排保留上下文 ----------------
    print("\n[9] 发送前复核失败 → 重排，上下文不丢")
    rq = ReplyQueue(_cfg(jitter_seconds=0.0), seed=1)
    it = rq.submit("private:D", "D", "4", "d1", now=t, wait_seconds=0.0)
    rq.offer_followup("private:D", "d2", now=t + 6)   # 确保能合并
    picked = rq.pick(t + 6)
    rq.drop(picked.scope)
    rq.requeue(picked, delay=1.0, now=t + 6)
    back = rq.get("private:D")
    case("重排后仍在队列", back is not None)
    case("上下文一条没丢", back is not None and back.count == len(it.texts),
         f"got {back.count if back else None}")
    case("attempts 已累加", back is not None and back.attempts == 1)

    # ---------------- 10. 抖动 ----------------
    print("\n[10] 抖动：避免同时到期")
    rq = ReplyQueue(_cfg(jitter_seconds=5.0), seed=7)
    for i in range(6):
        rq.submit(f"private:J{i}", f"J{i}", str(i), "x", now=t, wait_seconds=0.0)
    rts = sorted(round(it.ready_at - t, 3) for it in rq.items)
    case("到期时刻已分散", len(set(rts)) > 1, f"{rts}")
    case("抖动都落在 [0,5] 内", all(0 <= r <= 5.0 + 1e-9 for r in rts), f"{rts}")

    # ---------------- 11. 发现路径：占位入队 ----------------
    print("\n[11] ensure_pending · 发现即排队（只占位，不带正文）")
    rq = ReplyQueue(_cfg(jitter_seconds=0.0), seed=1)
    it, created = rq.ensure_pending("private:9001", "小螺", "9001",
                                    now=t, wait_seconds=5.0, unread_hint=3)
    case("首次 → 新建", created is True)
    case("标记为待总读", it.pending_read is True)
    case("正文为空（不拿摘要当内容）", it.count == 0, f"got {it.count}")
    case("未读条数已记录", it.unread_hint == 3)
    case("5s 后才到点", abs(it.ready_at - (t + 5.0)) < 1e-6, f"got {it.ready_at - t}")
    it2, created2 = rq.ensure_pending("private:9001", "小螺", "9001",
                                      now=t + 1.0, wait_seconds=5.0, unread_hint=4)
    case("再来一次不新增队列项", created2 is False and len(rq) == 1, f"len={len(rq)}")
    case("也不顺延窗口（发现阶段没有正文可合并）",
         abs(it2.ready_at - (t + 5.0)) < 1e-6, f"got {it2.ready_at - t}")
    case("未读提示取较大者", it2.unread_hint == 4)
    case("duplicate_skipped 计数 +1", rq.stats["duplicate_skipped"] == 1)
    it2.prepared = True
    it2.reply = "旧回复"
    rq.ensure_pending("private:9001", "小螺", "9001", now=t + 2.0,
                      wait_seconds=5.0, unread_hint=4)
    # ⚠️ 契约在 VM 场景下反过来了：已定稿的回复**不再被作废**。
    # 作废 = 丢掉一条已经生成的回复 = 「这条消息可能永远没人回」。
    # 现在改成：让它先把定稿那句发出去，新动静记到 late 桶，发完再结转成新的一条。
    case("已定稿的回复**不**被作废（改为 late 桶）",
         it2.prepared is True and it2.reply == "旧回复",
         f"prepared={it2.prepared} reply={it2.reply!r}")
    case("新动静记进了 late（发现路径没有正文，标记在 late_from_discovery）",
         it2.has_late is True and it2.late_from_discovery is True,
         f"has_late={it2.has_late} late={it2.late_count}")
    case("标记为「来自发现路径」（结转时要重读）", it2.late_from_discovery is True)
    case("deferred_to_late 计数 +1", rq.stats["deferred_to_late"] == 1)

    # ---------------- 11b. 定稿后不接受并入（否则新消息事实上没被回应）----------------
    print("\n[11b] 定稿的项不并入新内容，改为 late 桶（保证「都被回应」）")
    rq = ReplyQueue(_cfg(jitter_seconds=0.0), seed=1)
    it = rq.submit("private:9100", "乙", "9100", "第一句", now=t, wait_seconds=0.0)
    it.prepared, it.reply = True, "已定稿的回复"
    verdict = rq.offer_followup("private:9100", "第二句", "k2", 2, now=t + 1.0)
    case("已定稿 → 判定为 deferred", verdict == "deferred", f"got {verdict}")
    case("新内容没并进 texts（那句话已经写好了）", it.count == 1, f"count={it.count}")
    case("新内容进了 late 桶，不会丢", it.late_count == 1, f"late={it.late_count}")
    verdict2 = rq.offer_followup("private:9100", "第三句", "k3", 3, now=t + 2.0)
    case("再来一句仍然进 late 桶", verdict2 == "deferred" and it.late_count == 2)
    # 未定稿的项行为不变：仍然按 5s 阈值决定并入还是另起
    it2 = rq.submit("private:9101", "丙", "9101", "甲句", now=t, wait_seconds=20.0)
    case("未定稿 → 仍然可以并入",
         rq.offer_followup("private:9101", "乙句", "k4", 4, now=t + 1.0) == "merged")

    # ---------------- 11c. 优先级：已定稿的排前面（先发老的）----------------
    print("\n[11c] pick 优先级 · 已定稿待发的先发（挤压时不越过未发出的）")
    rq = ReplyQueue(_cfg(jitter_seconds=0.0), seed=1)
    old = rq.submit("private:9200", "老", "9200", "老消息", now=t, wait_seconds=0.0)
    old.prepared, old.reply = True, "老的回复"
    new = rq.submit("private:9201", "新", "9201", "新消息", now=t + 5.0, wait_seconds=0.0)
    case("已定稿的项优先（即使它 first_at 更早、新项更晚）",
         rq.pick(t + 10.0, ignore_budget=True) is old,
         f"got {rq.pick(t + 10.0, ignore_budget=True).scope}")
    # 但连续失败多次的项要降权，否则一条永远发不出去的项会饿死所有人
    old.fail_count = 3
    case("连续失败多次的定稿项降权（避免霸占循环，但不丢弃）",
         rq.pick(t + 10.0, ignore_budget=True) is new)
    case("降权的项仍在队列里（不丢）", old in rq.items)
    case("stuck() 能列出卡住的项", [x.scope for x in rq.stuck()] == ["private:9200"])

    # ---------------- 11d. 发送失败只记账，不动 ready_at（原地尽快重试）----------------
    print("\n[11d] note_send_failure · 发送失败要原地尽快重试，而不是排到队尾")
    rq = ReplyQueue(_cfg(jitter_seconds=0.0), seed=1)
    it = rq.submit("private:9300", "丁", "9300", "x", now=t, wait_seconds=0.0)
    before = it.ready_at
    rq.note_send_failure(it, "E-SEND-002 按钮未恢复")
    case("fail_count +1", it.fail_count == 1)
    case("记下了失败原因（带码）", "E-SEND-002" in it.last_fail, it.last_fail)
    case("没有改动 ready_at（回复已生成，重试路径极短）", it.ready_at == before)
    case("send_failed 计数 +1", rq.stats["send_failed"] == 1)

    # ---------------- 11e. 待发队列落盘 / 恢复（重启不丢已生成的回复）----------------
    print("\n[11e] 待发队列落盘 · 重启后继续把没发出的回复发出去")
    rq = ReplyQueue(_cfg(jitter_seconds=0.0), seed=1)
    a = rq.submit("private:9400", "戊", "9400", "原文", now=t, wait_seconds=0.0)
    a.prepared, a.reply, a.texts = True, "生成好但没发出去的话", ["原文"]
    a.drafted, a.fail_count, a.last_fail = True, 2, "E-SEND-003"
    b = rq.ensure_pending("private:9401", "己", "9401", now=t, unread_hint=2)[0]
    state = rq.dump_state()
    case("落盘包含全部待发项", len(state["items"]) == 2, str(len(state["items"])))

    rq2 = ReplyQueue(_cfg(jitter_seconds=0.0), seed=1)
    n = rq2.load_state(state)
    case("恢复条数正确", n == 2, str(n))
    ra = rq2.get("private:9400")
    case("**已生成的回复被完整恢复**（这就是「重启不丢」的关键）",
         ra is not None and ra.reply == "生成好但没发出去的话" and ra.prepared is True,
         f"{getattr(ra, 'reply', None)!r}")
    case("drafted / fail_count / last_fail 一并恢复",
         ra.drafted is True and ra.fail_count == 2 and ra.last_fail == "E-SEND-003")
    case("恢复后立刻可发（不等旧的时间戳）", ra.ready_at <= time.time() + 1e-6)
    rb = rq2.get("private:9401")
    case("占位项也恢复，且保留未读提示", rb is not None and rb.pending_read and rb.unread_hint == 2)
    case("unsent() 列出「已生成但没发出去」的项", [x.scope for x in rq2.unsent()] == ["private:9400"])

    # ---------------- 12. 两阶段：读+生成可早于风控放行 ----------------
    print("\n[12] pick(ignore_budget) · 读+生成可提前，发送仍等风控")
    rq = ReplyQueue(_cfg(jitter_seconds=0.0), seed=1)
    rq.submit("private:7001", "甲", "7001", "x", now=t, wait_seconds=1.0)
    rq.served(t)                       # 刚发过一条 → 风控锁到 t+5
    case("到点但风控未放行 → 默认 pick 拿不到", rq.pick(t + 1.5) is None)
    case("忽略风控 → 可以提前拿去读+生成",
         rq.pick(t + 1.5, ignore_budget=True) is not None)
    case("还没到点则谁都拿不到", rq.pick(t + 0.5, ignore_budget=True) is None)
    case("风控放行后正式 pick 成功", rq.pick(t + 5.0) is not None)

    # ---------------- 13. 重排次数上限 ----------------
    print("\n[13] max_attempts · 发送前复核连续失败要放弃")
    rq = ReplyQueue(_cfg(jitter_seconds=0.0, max_attempts=3), seed=1)
    itch = rq.submit("private:8001", "乙", "8001", "x", now=t, wait_seconds=0.0)
    for _ in range(3):
        rq.requeue(itch, delay=0.0, now=t)
    case("attempts 累加到上限", itch.attempts == 3, f"got {itch.attempts}")
    case("调用方可据此放弃", itch.attempts >= rq.max_attempts)

    print("\n" + "=" * 70)
    print(f"结果：{ok} 通过 / {fail} 失败")
    print("=" * 70)
    return 0 if fail == 0 else 1


def simulate(cfg: dict, users: int = 15, turns_per_minute: float = 0.75,
             minutes: float = 10.0, seed: int = 42, unit_cost: Optional[float] = None,
             burst: int = 2, verbose: bool = False) -> dict:
    """
    离线仿真：C 个用户同时来找，当前风控配置下能撑成什么样。

    模型（刻意简化，但抓住三个真实瓶颈）：
      - 每个用户按 turns_per_minute 发起一轮对话，一轮连发 1~burst 条；
      - 用户发来消息时，若他已在队列里 → 走 offer_followup 的 5s 阈值判定；
      - 「服务」一次要花 unit_cost 秒（模型 + UI 串行），期间风控时钟照走。

    返回统计。这是回答「这套配置能稳定带几个人」最直接的工具 ——
    比看文档里的估算值靠谱，因为阈值合并、抖动、硬上限都真实参与了。
    """
    import heapq

    rq = ReplyQueue(cfg, seed=seed)
    if unit_cost is not None:
        rq.unit_cost = float(unit_cost)
    rng = random.Random(seed)

    per_user_interval = 60.0 / max(1e-6, turns_per_minute)
    # 每个用户下一次发起对话的时刻
    nxt = {i: rng.uniform(0, per_user_interval) for i in range(users)}
    waits: list = []
    served_users: dict = {}
    per_user_replies: dict = {}
    step = 0.25
    now = 0.0
    end = minutes * 60.0
    pending_until = 0.0          # 正在「服务」的忙时

    while now < end:
        # ---- 1) 到达 ----
        for u in range(users):
            if nxt[u] <= now:
                scope = f"private:{1000 + u}"
                first = f"【用户{u}】：第1句"
                if rq.get(scope) is not None:
                    rq.offer_followup(scope, f"【用户{u}】：补充", now=now)
                else:
                    rq.submit(scope, f"用户{u}", str(1000 + u), first,
                              now=now, wait_seconds=rq.unit_cost * 2)
                waits.append(now)
                nxt[u] = now + per_user_interval * rng.uniform(0.6, 1.4)
                # 一轮连发：把剩下的几条也塞进来（模拟连打）
                for k in range(2, max(2, burst + 1)):
                    if rng.random() < 0.6:
                        if rq.get(scope) is not None:
                            rq.offer_followup(scope, f"【用户{u}】：第{k}句", now=now)
                        else:
                            rq.submit(scope, f"用户{u}", str(1000 + u),
                                      f"【用户{u}】：第{k}句", now=now, wait_seconds=0.0)

        # ---- 2) 服务 ----
        if now >= pending_until:
            item = rq.pick(now)
            if item is not None:
                w = now - item.first_at
                rq.drop(item.scope)
                rq.served(now)
                served_users[item.scope] = served_users.get(item.scope, 0) + 1
                per_user_replies[item.scope] = per_user_replies.get(item.scope, 0) + item.count
                pending_until = now + rq.unit_cost
                if verbose:
                    print(f"  t={now:7.1f}s  服务 {item.display_name}"
                          f"（{item.count} 条，含合并 {item.merged}，等了 {w:.1f}s）")
        now += step

    b = rq.budget.snapshot(now)
    unserved = len(rq)
    total_replies = sum(served_users.values())
    res = {
        "users": users, "minutes": minutes, "unit_cost": rq.unit_cost,
        "budget": {"per_minute": rq.budget.per_minute,
                   "min_interval": rq.budget.min_interval},
        "replies_sent": total_replies,
        "effective_per_minute": round(total_replies / (minutes), 2),
        "served_us": len(served_users),
        "never_served": [u for u in range(users) if f"private:{1000 + u}" not in served_users],
        "queue_left": unserved,
        "merged": rq.stats["merged"],
        "rejected_merge": rq.stats["rejected_merge"],
        "msgs_absorbed_per_reply": round(
            (sum(per_user_replies.values()) / total_replies), 2) if total_replies else 0,
    }
    return res


def _sim_cfg(**kw) -> dict:
    base = {"enabled": True, "max_replies_per_minute": 12, "min_interval_seconds": 5.0,
            "merge_if_wait_over_seconds": 5.0, "unit_cost_seconds": 3.0,
            "max_hold_seconds": 30.0, "jitter_seconds": 3.0}
    base.update(kw)
    return {"queue": base}


def sim_report(users_list=(5, 10, 15, 20, 30, 40), minutes=10.0,
               turns_per_minute=0.75, per_minute=12, min_interval=5.0) -> int:
    """按「每分钟 12 条」的安全配置，扫一遍不同并发人数，看哪里开始崩。"""
    print("=" * 78)
    print(f"排队容量仿真｜风控 {per_minute} 条/分，硬间隔 {min_interval}s，"
          f"每用户 {turns_per_minute}/分 轮次，模拟 {minutes:.0f} 分钟")
    print("=" * 78)
    print(f"{'并发用户':>8} {'实发条数':>9} {'实际频率':>9} {'服务到人数':>10} "
          f"{'合并条数':>9} {'队列残留':>9} {'单回复吸收':>10}")
    for c in users_list:
        r = simulate(_sim_cfg(max_replies_per_minute=per_minute,
                              min_interval_seconds=min_interval),
                     users=c, turns_per_minute=turns_per_minute, minutes=minutes)
        print(f"{c:>8} {r['replies_sent']:>9} {r['effective_per_minute']:>8}/分 "
              f"{r['served_us']:>10} {r['merged']:>9} {r['queue_left']:>9} "
              f"{r['msgs_absorbed_per_reply']:>10}")
    print("\n判读：『实发条数』会顶到风控上限（≈ %.0f/分）就说明瓶颈是风控而非架构；"
          % per_minute)
    print("     『队列残留』开始明显大于 0、且『服务到人数』< 并发用户，说明已经积压。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="回复排队与风控限速")
    ap.add_argument("--selftest", action="store_true", help="跑离线单测")
    ap.add_argument("--simulate", type=int, metavar="USERS", help="仿真指定并发人数")
    ap.add_argument("--report", action="store_true", help="扫描多组并发人数")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--turns", type=float, default=0.75, help="每个用户每分钟的对话轮次")
    ap.add_argument("--per-minute", type=int, default=12, help="风控上限（条/分）")
    ap.add_argument("--min-interval", type=float, default=5.0)
    ap.add_argument("--verbose", action="store_true", help="逐条打印服务过程")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.report:
        return sim_report(minutes=args.minutes, turns_per_minute=args.turns,
                          per_minute=args.per_minute, min_interval=args.min_interval)
    if args.simulate:
        r = simulate(_sim_cfg(max_replies_per_minute=args.per_minute,
                              min_interval_seconds=args.min_interval),
                     users=args.simulate, minutes=args.minutes,
                     turns_per_minute=args.turns, verbose=args.verbose)
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
