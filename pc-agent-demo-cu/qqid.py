# -*- coding: utf-8 -*-
"""
qqid.py — 从 QQ 界面抓取「稳定用户标识（QQ 号）」并缓存

## 为什么必须做这件事

agent.py 原来的会话主键是**昵称**（`private:<昵称>`）。昵称可由对方随时修改，
也可以和「备注」脱节，一旦重名就会把 A 的上下文喂给 B —— 这是并发化的头号正确性隐患。

## 实测结论（2026-09-11，本机 QQNT）

| 通路 | 能否拿到 QQ 号 | 证据 |
|---|---|---|
| 会话列表项 `recent-contact-item` 及其**全部后代** | ❌ | AutomationId 全空；Name/HelpText/ItemStatus/FullDescription 全空 |
| UIA 通用属性（AriaRole / PositionInSet / SizeOfSet / Level） | ❌ | Chromium 对 group 角色一律填 `group` 与 0，无任何业务信息 |
| 全 UIA 树（192 节点）扫 AutomationId | ❌ | 只有 5 个：RenderWidget / RootWebArea / drag-area / app / loading |
| 全树扫「5~12 位数字」文本 | ❌ | 0 个 |
| **资料卡（独立顶层窗口 `资料卡` / class=`buddy-profile`）** | ✅ | `buddy-profile__header-uid` → TextControl `'QQ 3302676083'` |
| CDP（`--remote-debugging-port=9222`） | ⏳ 未开启 | 端口未监听；需重启 QQ 加参数 |

**结论：拿 QQ 号的唯一现成通路是「打开资料卡」。**

## 资料卡的三个关键性质（都已实测）

1. **它是独立顶层窗口**，不在 QQ 主窗口的 UIA 子树里。
   标题 = `资料卡`，class = `Chrome_WidgetWin_1`，pid 与 QQ 主进程相同。
   → 只扫主窗口会漏掉它（我第一版探查就是这么漏的）。
2. **唤出它不需要鼠标**：对 `chat-header__contact-name` 调 `InvokePattern.Invoke()` 即可。
   （鼠标 `Click` 也行，但 InvokePattern 更稳，且不移动光标。）
3. **唤出动作会把 QQ 顶到前台**（弹窗激活）。所以取号是一次**有打扰**的操作
   → 一次性把号存下来，之后只读缓存；这正是 `UidStore` 存在的意义。

## 用法

    python qqid.py --list                  # 列出会话列表 + 已缓存的 QQ 号
    python qqid.py --enroll 0 1 3          # 给第 0/1/3 个会话取号（会抢前台）
    python qqid.py --enroll-all            # 给所有还没号的会话取号
    python qqid.py --enroll-all --no-click # 只对「已打开的那个会话」取号（最小打扰）
    python qqid.py --check                # 重号诊断：不同会话是否映射到同一 QQ 号
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import agent as A

# 直接复用 agent 的解析结果，而不是自己再算一遍 __file__：
# 打包成 exe 后 __file__ 指向临时解压目录，自己算会把缓存写到重启就丢的地方。
# 路径解析只允许有一个事实来源，所以这里是引用、不是复制。
HERE = A.HERE
DEFAULT_STORE = os.path.join(HERE, "state", "uid-map.json")

# ---------------- 控件常量（实测得到，见 card-tree.txt / uid-tree.txt） ----------------
CLS_RECENT_LIST = "recent-contact-list"
CLS_RECENT_ITEM = "recent-contact-item"
CLS_RECENT_ITEM_SELECTED = "recent-contact-item--selected"
CLS_ITEM_INFO = "item__info"
CLS_HEADER_NAME = "chat-header__contact-name"
CLS_CARD_ROOT = "buddy-profile"
AID_CARD_ROOT = "mini-buddy-profile"
CLS_CARD_UID = "buddy-profile__header-uid"
CLS_CARD_NAME = "buddy-profile__header-name"
CLS_CARD_LABEL = "buddy-profile__details-item-label"
CLS_CARD_VALUE = "buddy-profile__details-item-value"
CLS_CARD_REMARK_BTN = "remark__content"

CARD_WINDOW_TITLE = "资料卡"
# 'QQ 3302676083' / '群号 123456' / 纯数字
RE_UID_TEXT = re.compile(r"(?:QQ|群|群号|QQ号)\s*[:：]?\s*(\d{5,12})")
RE_BARE_NUM = re.compile(r"^(\d{5,12})$")
# 未读徽标：'3条未读' / '99+条未读'
# 实测：这个 Name 挂在 item__info 的**直接子节点**（一个 GroupControl）上，
# 而且那个子节点还**顺带包住了时间/发送者/预览文本** —— 见 list_sessions 的注释。
RE_UNREAD = re.compile(r"^(\d+|99\+)\s*条未读$")

# 时间戳 token。会话项里它和预览文本是平级的 TextControl，必须单独切出来，
# 否则「日期翻页」('13:40' → '昨天20:04') 会被当成有新消息 → 假触发切会话。
# 实测形态（7/7 全覆盖）：'13:40' '昨天20:04' '08/28' '07/30'
RE_TIMEISH = re.compile(
    r"^(\d{1,2}:\d{2}"
    r"|昨天.*|今天.*"
    r"|星期[一二三四五六日].*|周[一二三四五六日].*"
    r"|\d{1,2}/\d{1,2}(/\d{2,4})?"
    r"|刚刚|上午.*|下午.*|晚上.*"
    r"|\d{4}-\d{1,2}-\d{1,2})$"
)


# ============================================================ 数据
@dataclass
class SessionCard:
    """
    一个会话列表项在当前 UIA 树里能读到的全部信息（无 id，只有文本）。

    ⚠️ 这里**只拿得到「最新一条消息的节选」**，拿不到完整对话。
    所以本模块的产物只配用来判「有没有新东西」，
    **绝不能拿来当 AI 的上下文** —— 那正是「降级方案」要推迟到出队时做总读取的原因。
    """
    index: int
    display_name: str
    preview_texts: tuple = ()  # 预览文本（已剔掉时间 token），如 ('光みつる：', '在吗')
    ts: str = ""               # 时间戳 token，仅用于日志展示，**不参与指纹**
    selected: bool = False
    rect: tuple = (0, 0, 0, 0)
    unread: int = 0            # 未读条数（'3条未读' / '99+条未读'）；0 = 没有徽标
    item: object = None        # 活控件引用，仅在当次调用内有效

    @property
    def key(self) -> str:
        """稳定主键：显示名。QQ 好友备注在同一账号内唯一，昵称重名时靠备注消歧。"""
        return (self.display_name or "").strip()

    @property
    def summary(self) -> str:
        """给人看的摘要串（日志/打印用）。**不要用它做判据**，用 fingerprint()。"""
        return "".join(self.preview_texts)

    @property
    def looks_group(self) -> bool:
        """
        从预览猜是不是群聊：群消息的预览首段是「发送者：」的形式。

        实测 6/6 命中（群→True），是拿不到 QQ 号缓存时的兜底。
        私聊里朋友本人发「XX：」会误判 → 所以优先读 UidStore 里的 is_group。
        """
        return bool(self.preview_texts) and self.preview_texts[0].endswith(("：", ":"))

    def to_dict(self) -> dict:
        return {"index": self.index, "display_name": self.display_name,
                "preview_texts": list(self.preview_texts), "summary": self.summary,
                "ts": self.ts, "selected": self.selected,
                "unread": self.unread, "rect": list(self.rect)}

    def fingerprint(self) -> tuple:
        """
        「这个会话有没有新东西」的判据。

        只用 (预览文本元组, 未读数)，**时间 token 已经被剔掉了**。

        历史教训：早期版本写成 `(summary, unread)`，而 summary 是
        `collect_texts(summary-main, 3)` 拼出来的 —— 那把同一个块里的时间
        也拼了进去（'13:40' + '测试' = '13:40测试'），于是「日期翻页」照样假触发，
        「特意排除 ts」的努力被抵消了。现在 preview_texts 在解析阶段就切干净了。
        """
        return (tuple(self.preview_texts), int(self.unread))


@dataclass
class UidInfo:
    uin: str
    card_name: str = ""        # 资料卡上的真实昵称（与备注名可能不同！）
    remark: str = ""
    signature: str = ""
    region: str = ""
    is_group: bool = False
    member_count: int = 0
    source: str = "card"       # card | cache
    at: float = 0.0

    @property
    def kind(self) -> str:
        return "group" if self.is_group else "private"

    def to_dict(self) -> dict:
        return {"uin": self.uin, "card_name": self.card_name, "remark": self.remark,
                "signature": self.signature, "region": self.region,
                "is_group": self.is_group, "member_count": self.member_count,
                "source": self.source, "at": self.at}

    @classmethod
    def from_dict(cls, d: dict) -> "UidInfo":
        return cls(uin=str(d.get("uin", "")), card_name=d.get("card_name", ""),
                   remark=d.get("remark", ""), signature=d.get("signature", ""),
                   region=d.get("region", ""),
                   is_group=bool(d.get("is_group", False)),
                   member_count=int(d.get("member_count", 0) or 0),
                   source=d.get("source", "cache"), at=float(d.get("at", 0)))


class UidStore:
    """`显示名 → QQ号` 缓存。取号有打扰，所以取一次就够，之后只读缓存。"""

    def __init__(self, path: str = DEFAULT_STORE):
        self.path = path
        self.map: dict = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.map = {k: UidInfo.from_dict(v) for k, v in (raw.get("by_name") or {}).items()}
        except FileNotFoundError:
            self.map = {}
        except Exception as e:
            A.log("WARN", f"uid 缓存读取失败（按空处理）：{e}")
            self.map = {}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        payload = {
            "version": 1,
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "by_name": {k: v.to_dict() for k, v in self.map.items()},
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self, display_name: str) -> Optional[UidInfo]:
        return self.map.get(display_name.strip())

    def put(self, display_name: str, info: UidInfo) -> None:
        self.map[display_name.strip()] = info
        self.save()

    def uin_of(self, display_name: str) -> str:
        info = self.get(display_name)
        return info.uin if info else ""

    def name_of(self, uin: str) -> str:
        for n, i in self.map.items():
            if i.uin == uin:
                return n
        return ""

    def conflicts(self) -> dict:
        """重号诊断：同一个 QQ 号被两个显示名映射（说明重名或改过备注）。"""
        by_uin: dict = {}
        for n, i in self.map.items():
            by_uin.setdefault(i.uin, []).append(n)
        return {u: ns for u, ns in by_uin.items() if len(ns) > 1}

    def missing(self, cards: list[SessionCard]) -> list[str]:
        return [c.display_name for c in cards if c.display_name and c.display_name not in self.map]


# ============================================================ 界面读取
def find_qq_main_window():
    """找 QQ 主窗口：class=Chrome_WidgetWin_1 / title=QQ / 面积够大。"""
    best = None
    for w in A._enum_top_windows():
        if w.get("class") != "Chrome_WidgetWin_1" or w.get("title") != "QQ":
            continue
        r = w.get("rect") or [0, 0, 0, 0]
        if (r[2] - r[0]) < 600:
            continue
        if best is None or A._area(tuple(r)) > A._area(tuple(best["rect"])):
            best = w
    if best is None:
        return None, 0
    return best, best["hwnd"]


def card_windows(qq_pid: int, visible_only: bool = True) -> list:
    """
    资料卡是独立顶层窗口 —— 只在 QQ 主窗口子树里找会永远找不到。

    ⚠️ 必须带可见性过滤：Chromium **不会销毁**资料卡窗口，只会把它隐藏
    （实测关掉后窗口对象仍在，visible=False，rect 仍在）。
    不过滤的话，切换会话后会立刻「找到」上一次的旧卡片，读到上一个用户的资料。
    """
    out = []
    for w in A._enum_top_windows():
        if w.get("title") != CARD_WINDOW_TITLE:
            continue
        if qq_pid and w.get("pid") != qq_pid:
            continue
        if visible_only and not w.get("visible"):
            continue
        out.append(w)
    return out


def _info_node(item, maxdepth: int = 4):
    """定位 item__info（实测层级：item > item__content > item__info）。"""
    for node, _d in A.iter_bfs(item, maxdepth, limit=60):
        if CLS_ITEM_INFO in A._cls(node):
            return node
    return None


def _block_texts(block, maxdepth: int = 3) -> list[str]:
    """
    收集一个子块内部的文本（文档顺序）。实测块内层级很浅（≤2）。

    手动 DFS 而不是用 A.iter_bfs：iter_bfs **不会剪枝**，跳过某个节点后
    它的子孙照样会被吐出来。徽标数字就藏在 `q-badge-sub ... q-badge__red` 里面，
    必须整棵剪掉，否则未读数的变化会混进预览、造成一次多余的假触发。
    """
    out: list[str] = []
    stack = [(c, 1) for c in reversed(A._kids(block))]
    while stack:
        c, d = stack.pop()
        if d > maxdepth:
            continue
        cls = A._cls(c)
        if "avatar" in cls or "q-badge" in cls:
            continue                    # 头像 / 徽标数字不属于「消息预览」
        if A._ctype(c) == "TextControl":
            t = A._name(c).strip()
            if t:
                out.append(t)
            continue
        for k in reversed(A._kids(c)):
            stack.append((k, d + 1))
    return out


def list_sessions(win) -> list[SessionCard]:
    """
    读会话列表。

    每个会话项只暴露三样东西：**显示名 / 最新一条消息的预览 / 时间**，没有 id。
    预览是**节选**（过长会被截断），所以本函数的产物只配用来判「有没有新东西」，
    绝不能拿来当 AI 的上下文 —— 那正是「降级方案」要推迟到出队时做总读取的原因。

    ⚠️ 实测树形（QQNT 9.x + `--force-renderer-accessibility`）：

        recent-contact-item
          └ item__content
              ├ item__avatar
              └ item__info
                  ├ avatar                      (GroupControl，无文本)
                  ├ TextControl   '光みつる'      ← 显示名
                  ├ secondary-info              ← **空的**，没有文本子节点
                  ├ summary-main                → '13:40' '测试'
                  └ GroupControl name='1条未读'   ← 有未读时才出现
                       ├ '22:34'
                       ├ '人类牧师(战争) 格林：'
                       ├ '[/舔屏]'
                       └ q-badge-sub q-badge-num q-badge__red

    两个反直觉的点，都踩过：

      1. **未读徽标是 item__info 的直接儿子**（不是兄弟位）。而且它不是单纯一个徽标 ——
         它把**时间 / 发送者 / 预览也一起包住了**。
      2. 有未读时 `summary-main` 会变空，内容整块挪到上面那个徽标块下面。
         早期版本只认 `summary-main`，于是「有未读的会话摘要恒为空」，
         fingerprint 退化成 ('', N)：『99+』时再收消息就完全漏检了。

    所以这里改成：遍历 item__info 的**直接儿子**，逐块收集块内文本，
    最后统一用 RE_TIMEISH 把时间 token 切出去，剩下的才是预览。
    """
    lst = None
    for ctrl, _d in A.iter_bfs(win, 22, limit=8000):
        if CLS_RECENT_LIST in A._cls(ctrl):
            lst = ctrl
            break
    if lst is None:
        return []

    out: list[SessionCard] = []
    items = [c for c in A._kids(lst) if CLS_RECENT_ITEM in A._cls(c)]
    for idx, item in enumerate(items):
        info = _info_node(item)
        name, unread, texts = "", 0, []
        if info is not None:
            for ch in A._kids(info):
                cls = A._cls(ch)
                if "avatar" in cls:
                    continue                    # 头像块可能带 ImageControl，别混进预览
                m = RE_UNREAD.match(A._name(ch))
                if m:
                    unread = int(m.group(1)) if m.group(1).isdigit() else 99
                if A._ctype(ch) == "TextControl":
                    t = A._name(ch).strip()
                    if t and not name:
                        name = t
                    continue
                texts.extend(_block_texts(ch))
            if unread == 0:
                # 兜底：万一新版把徽标挪到更深处，再整体扫一遍
                for c, _d in A.iter_bfs(info, 4, limit=80):
                    m = RE_UNREAD.match(A._name(c))
                    if m:
                        unread = int(m.group(1)) if m.group(1).isdigit() else 99
                        break
        # 同一个文本可能被多层节点重复上报，按出现顺序去重
        dedup: list[str] = []
        for t in texts:
            if t not in dedup:
                dedup.append(t)
        ts = next((t for t in dedup if RE_TIMEISH.match(t)), "")
        preview = tuple(t for t in dedup if not RE_TIMEISH.match(t))
        out.append(SessionCard(
            index=idx, display_name=name, preview_texts=preview, ts=ts,
            selected=CLS_RECENT_ITEM_SELECTED in A._cls(item),
            unread=unread, rect=A._rect(item), item=item,
        ))
    return out


def header_title(win) -> str:
    """当前打开会话的标题（= 会话列表里的显示名 / 备注名）。"""
    for ctrl, _d in A.iter_bfs(win, 26, limit=8000):
        if CLS_HEADER_NAME in A._cls(ctrl) and A._visible(ctrl):
            return _looks_like_nick(A._name(ctrl))
    return ""


def _looks_like_nick(text: str) -> str:
    """标题按钮的 Name 偶尔会带上操作提示语，剥掉。"""
    t = (text or "").strip()
    for suffix in ("的个人主页", "的资料卡"):
        if t.endswith(suffix):
            t = t[: -len(suffix)]
    for prefix in ("查看",):
        if t.startswith(prefix):
            t = t[len(prefix):]
    return t.strip()


def _dfs(root, maxdepth: int = 14, limit: int = 3000):
    """
    **文档顺序**深度优先遍历。

    为什么不能用 agent.iter_bfs 读资料卡的 label/value 配对：
    BFS 按层输出，卡片里所有 `details-item-label` 都在同一层，
    于是「备注/签名/所在地」三个 label 会先全部吐出来，再去吐它们的 value，
    配对必然整体错位（实测把「签名」的值配到了「所在地」上）。DFS 才是文档顺序。
    """
    stack = [(root, 0)]
    n = 0
    while stack and n < limit:
        ctrl, d = stack.pop()
        if d > maxdepth:
            continue
        n += 1
        yield ctrl, d
        for ch in reversed(A._kids(ctrl)):
            stack.append((ch, d + 1))


def _card_root(win):
    for ctrl, _d in A.iter_bfs(win, 20, limit=4000):
        if CLS_CARD_ROOT in A._cls(ctrl) or A._aid(ctrl) == AID_CARD_ROOT:
            return ctrl
    return None


def read_card(card_win) -> Optional[UidInfo]:
    """
    从资料卡窗口读 QQ 号。

    实测结构（card-tree.txt）：
        WindowControl '资料卡' class='buddy-profile' aid='mini-buddy-profile'
          ├ ButtonControl  '...的头像'
          ├ GroupControl   'buddy-profile__header-name-wrap'
          │   └ ButtonControl '查看XXX的个人主页'
          ├ GroupControl   'buddy-profile__header-uid'      ← 这里
          │   └ TextControl 'QQ 3302676083'
          └ ... 备注 / 签名 / 所在地
    """
    ctrl = A.auto.ControlFromHandle(card_win["hwnd"])
    if ctrl is None:
        return None
    root = _card_root(ctrl)
    if root is None:
        return None

    uin = ""
    card_name = ""
    remark = signature = region = ""
    is_group = False
    member_count = 0

    # 1) QQ 号 / 群号
    #    - 私聊卡片：TextControl 'QQ 3302676083'（带前缀）
    #    - 群聊卡片：TextControl '1098451742' + ' (7人)'（**裸数字**，两个独立节点）
    #    所以既要从拼接串里找前缀，也要逐个文本找纯数字。
    uid_node = None
    for node, _d in A.iter_bfs(root, 12, limit=2000):
        if CLS_CARD_UID in A._cls(node):
            uid_node = node
            break
    uid_texts: list[str] = []
    if uid_node is not None:
        uid_texts = [t.strip() for t in A.collect_texts(uid_node, 5) if t.strip()]
        joined = "".join(uid_texts)
        m = RE_UID_TEXT.search(joined)
        if m:
            uin = m.group(1)
        else:
            for t in uid_texts:
                m2 = RE_BARE_NUM.match(t)
                if m2:
                    uin = m2.group(1)
                    break
        for t in uid_texts:
            mg = re.search(r"\((\d+)\s*人\)", t)
            if mg:
                member_count = int(mg.group(1))
                is_group = True

    # 群卡片比私聊卡片多一层 header-sub-wrap（且带「二维码」按钮）
    if not is_group:
        for node, _d in A.iter_bfs(root, 12, limit=2000):
            if "header-sub-wrap" in A._cls(node):
                is_group = True
                break

    if not uin:
        for node, _d in A.iter_bfs(root, 14, limit=3000):
            if A._ctype(node) != "TextControl":
                continue
            raw = A._name(node)
            m = RE_UID_TEXT.search(raw) or RE_BARE_NUM.match(raw.strip())
            if m:
                uin = m.group(1)
                break

    # 2) 名字：头像按钮的 Name（'XXX的头像'）最干净且私聊/群聊通用
    for node, _d in _dfs(root, 14, 3000):
        nm = A._name(node)
        if nm.endswith("的头像"):
            card_name = nm[:-3].strip()
            break
    if not card_name:
        for node, _d in _dfs(root, 14, 3000):
            cls = A._cls(node)
            if CLS_CARD_NAME in cls or "header-name-wrap" in cls:
                nm = _looks_like_nick(A._name(node)) or _looks_like_nick(
                    "".join(A.collect_texts(node, 3)))
                if nm and nm not in ("群主页", "个人主页"):
                    card_name = nm
                    break

    # 3) 备注 / 签名 / 所在地 / 群介绍：靠「label 文本 → 紧随其后的 value」配对（必须用 DFS）
    pairs: list[tuple[str, str]] = []
    cur_label = ""
    for node, _d in _dfs(root, 14, 3000):
        cls = A._cls(node)
        if CLS_CARD_LABEL in cls:
            cur_label = "".join(A.collect_texts(node, 3)).strip()
        elif cur_label and (CLS_CARD_VALUE in cls or CLS_CARD_REMARK_BTN in cls):
            val = "".join(A.collect_texts(node, 4)).strip() or _looks_like_nick(A._name(node))
            if val:
                pairs.append((cur_label, val))
                cur_label = ""
    for lb, val in pairs:
        if lb == "备注" and not remark:
            # 未设置备注时这里是占位按钮文本：'设置好友备注' / '设置群聊备注'
            remark = "" if (val.startswith("设置") and val.endswith("备注")) else val
        elif lb == "签名" and not signature:
            signature = val
        elif lb == "所在地" and not region:
            region = val

    if not uin and not card_name:
        return None
    return UidInfo(uin=uin, card_name=card_name, remark=remark,
                   signature=signature, region=region,
                   is_group=is_group, member_count=member_count,
                   source="card", at=time.time())


# ============================================================ 交互动作
def open_card(win, timeout: float = 3.0) -> Optional[dict]:
    """
    唤出资料卡。用 InvokePattern（不移动光标），但**会把 QQ 顶到前台**。

    返回资料卡窗口描述；失败返回 None。

    实现要点：先确认当前**没有**可见的资料卡。
    否则 invoke 之后第一轮轮询就会命中上一次残留（已隐藏）的卡片，
    读到上一个会话的资料 —— 这正是「发错人」的另一种形态，必须挡掉。
    """
    try:
        pid = int(win.ProcessId)
    except Exception:
        pid = 0

    # 关掉已打开/残留的卡片
    stale = card_windows(pid)
    if stale:
        for c in stale:
            close_card(c)
        if card_windows(pid):
            time.sleep(0.5)

    btn = None
    for ctrl, _d in A.iter_bfs(win, 26, limit=8000):
        if CLS_HEADER_NAME in A._cls(ctrl) and A._visible(ctrl):
            btn = ctrl
            break
    if btn is None:
        A.log("WARN", "找不到会话标题按钮，无法唤出资料卡")
        return None

    try:
        btn.GetInvokePattern().Invoke()
    except Exception as e:
        A.log("WARN", f"InvokePattern 唤出资料卡失败：{e}")
        return None

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.2)
        cards = card_windows(pid)
        if cards:
            return cards[0]
    return None


def close_card(card_win) -> bool:
    """关掉资料卡。它是焦点弹窗，Esc 最稳；判据是**可见性**而不是窗口对象是否存在。"""
    pid = card_win.get("pid", 0) or 0
    try:
        ctrl = A.auto.ControlFromHandle(card_win["hwnd"])
        if ctrl is not None:
            try:
                ctrl.SetFocus()
            except Exception:
                pass
            time.sleep(0.05)
            A.auto.SendKeys("{Esc}", waitTime=0.05)
            time.sleep(0.35)
            if not card_windows(pid):
                return True
            # Esc 无效时点一下主窗口的会话列表区（中性位置）让它失焦
            bg = card_win.get("rect") or [0, 0, 0, 0]
            A.auto.SetCursorPos(bg[0] - 120, bg[1] + 40)
            time.sleep(0.05)
            A.auto.Click(bg[0] - 120, bg[1] + 40)
            time.sleep(0.35)
    except Exception:
        pass
    return not card_windows(pid)


INVOKE_PATTERN_ID = 10000          # UIA_InvokePatternId


def _wait_title(win, want: str, timeout: float) -> bool:
    """轮询等标题变成 want。**必须轮询** —— 固定 sleep 会把慢半拍的成功判成失败。"""
    deadline = time.time() + timeout
    while True:
        if header_title(win) == want:
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.2)


def try_invoke(ctrl) -> bool:
    """
    对控件发一次 UIA Invoke。

    **免前台、不移动光标、不产生任何按键** —— 这是它相对坐标点击的全部价值。
    `GetPattern` 对不支持的 Pattern 可能返回 None 而不是抛异常，两种情况都要挡。
    """
    try:
        pat = ctrl.GetPattern(INVOKE_PATTERN_ID)
    except Exception as exc:
        A.log("WARN", f"取 InvokePattern 失败：{type(exc).__name__}: {exc}")
        return False
    if pat is None:
        A.log("WARN", "该控件不支持 InvokePattern")
        return False
    try:
        pat.Invoke()
        return True
    except Exception as exc:
        A.log("WARN", f"Invoke 调用失败：{type(exc).__name__}: {exc}")
        return False


def switch_session(session: SessionCard, win, prefer_noop: bool = True,
                   timeout: float = 6.0) -> bool:
    """
    切到指定会话。**优先走 InvokePattern（免前台）**，失败才退回坐标点击。

    ## 为什么优先 Invoke（2026-09-11 实测）

    | 方式 | 需要前台？ | 需要窗口非最小化？ | 移动你的光标？ | 抢焦点？ |
    | --- | --- | --- | --- | --- |
    | `InvokePattern.Invoke()` | ❌ **不需要** | ✅ | ❌ | ✅（Chromium 内部会 SetFocus，见下） |
    | `item.Click()`（坐标点击） | ✅ | ✅ | ✅ | ✅ |

    `Invoke` 这条路径是实测出来的：会话列表项**支持** `InvokePattern`，
    在 QQ 完全不在前台（前台是别的程序）时调用它，会话**确实切过去了**。
    也就是说「切会话必须抢前台」这个前提根本不成立 —— 以前一直用坐标点击是走了弯路。

    ⚠️ 但**免前台 ≠ 不抢焦点**：`Invoke` 之后 QQ 会被激活、顶到最上层
    （实测前台从别的程序变成了 QQ，且**连等 4 秒也还不回去** —— 因为前台
    "所有权"是 QQ 自己拿的，我们不是发起者）。
    所以调用方要在切换后显式归还前台（`agent._ensure_session` 会做）。

    比坐标点击强在：**不依赖窗口在最上层**（被盖住也照样切过去）、
    **不移动光标**、且成败可验证（见下面的判据），不会出现"点空了却报成功"。

    ## 成功判据：标题必须**等于目标名**

    以前这里写的是 `if header_title(win): return True` —— 只要标题非空就算成功，
    可**旧会话的标题也是非空的**。于是"点空了"会被判成成功，故障一路漂到下游的
    「切换后渲染未稳定」才暴露（README 里的坑 7）。现在只认 `标题 == 目标名`。

    ## 最小化时会怎样（实测）

    窗口最小化时 `Invoke()` 会**静默失效** —— Chromium 在窗口被遮挡/最小化时
    会节流 renderer，DOM 事件不处理。此时必须先把窗口还原（`SW_RESTORE`）。
    注意：**读取不受影响**（读的是 a11y 树缓存，实测最小化后节点数/会话/消息全一样）。
    所以「窗口不能最小化」这条只对 Invoke/点击成立，对读取不成立。

    ⚠️ 别用 `prefer_noop=False`，除非你确实想切换到一个**已选中的**会话 ——
    QQ 里点击"当前已选中"的项会把这个会话**关掉**（toggle），不是重新打开。
    """
    name = session.display_name

    # 已经是这个会话 → 什么都不做
    if header_title(win) == name:
        if prefer_noop:
            A.log("INFO", f"会话 {name!r} 已是当前打开状态，跳过切换")
        return True

    # ---- 主路径：InvokePattern（不需要前台、不动光标）----
    if try_invoke(session.item):
        if _wait_title(win, name, timeout):
            A.log("INFO", f"已切换到 {name!r}（InvokePattern：无需前台，但会激活 QQ）")
            return True
        A.log("WARN", f"{name!r} 的 Invoke 已发出但标题未变"
                      f"（窗口被最小化时 Chromium 会这样）→ 改用坐标点击兜底")

    # ---- 兜底：坐标点击。这一步**必须**抬前台，否则会点在盖住 QQ 的那个窗口上 ----
    hwnd = getattr(win, "hwnd", 0)
    if hwnd:
        prev = A._fg_hwnd()
        if prev and prev != hwnd:
            # 记下用户原来的前台窗口：抬升之后 type_text 再读 _fg_hwnd() 读到的
            # 就是 QQ 自己了，「用完即还」会静默失效。交给输入路径用它归还。
            win.fg_before_switch = prev
        if not A.force_foreground(hwnd, retries=3):
            A.log("WARN", "抬不起 QQ 前台，坐标点击可能点空")

    for _ in range(2):
        try:
            session.item.Click(simulateMove=False)
        except Exception as exc:
            A.log("WARN", f"点击会话失败：{type(exc).__name__}: {exc}")
            break
        if _wait_title(win, name, timeout=2.0):
            A.log("INFO", f"已切换到 {name!r}（坐标点击，兜底路径）")
            return True

    A.log("ERR", f"切换会话失败：{name!r}（当前标题 {header_title(win)!r}）")
    return False


# 兼容旧名：语义早就是"切到某个会话"，不只是"点一下"
click_session = switch_session


def enroll(win, store: UidStore, session: SessionCard,
           restore_fg: bool = True, verbose: bool = True) -> Optional[UidInfo]:
    """
    给一个会话取号并写入缓存。

    打扰成本：一次会话切换点击 + 一次资料卡弹出，约 2~4 秒。**同类操作只做一次。**
    """
    prev_fg = A._fg_hwnd()
    try:
        if not switch_session(session, win):
            if verbose:
                A.log("WARN", f"无法切到会话 {session.display_name!r}，放弃取号")
            return None
        cur = header_title(win)
        if verbose:
            A.log("INFO", f"当前会话标题 = {cur!r}（目标 {session.display_name!r}）")
        card = open_card(win)
        if card is None:
            if verbose:
                A.log("WARN", "资料卡没弹出，放弃取号")
            return None
        info = read_card(card)
        close_card(card)
        if info is None or not info.uin:
            if verbose:
                A.log("WARN", f"资料卡里没读到 QQ 号（card_name={info.card_name if info else ''!r}）")
            return None
        store.put(session.display_name, info)
        if verbose:
            A.log("OK", f"取号成功：{session.display_name!r} → QQ {info.uin}"
                        f"（真实昵称 {info.card_name!r}）")
        return info
    finally:
        if restore_fg and prev_fg:
            time.sleep(0.15)
            A.restore_foreground(prev_fg)


def enroll_all(win, store: UidStore, only_missing: bool = True,
               limit: int = 0) -> dict:
    """批量取号。返回统计。"""
    sessions = list_sessions(win)
    todo = [s for s in sessions if s.display_name
            and (not only_missing or s.display_name not in store.map)]
    if limit:
        todo = todo[:limit]
    A.log("INFO", f"会话 {len(sessions)} 个，待取号 {len(todo)} 个")
    ok = 0
    for s in todo:
        if enroll(win, store, s):
            ok += 1
        time.sleep(0.25)
    return {"total": len(sessions), "todo": len(todo), "ok": ok}


# ============================================================ CLI
def cmd_list(win, store: UidStore) -> int:
    sessions = list_sessions(win)
    if not sessions:
        A.diag("E-QQ-007", "读不到任何会话（会话列表为空）",
               {"建议": "确认 QQ 停在「消息」标签页；若刚重启过，稍等列表渲染完"})
        return 2
    print(f"{'':<2}{'#':>3}  {'显示名':<24} {'QQ号':<13} {'真实昵称':<20} 摘要")
    for s in sessions:
        info = store.get(s.display_name)
        mark = "★" if s.selected else " "
        uin = info.uin if info else "—"
        cn = (info.card_name if info else "") or ""
        print(f"{mark:<2}{s.index:>3}  {s.display_name:<24} {uin:<13} {cn:<20} {s.summary[:26]}")
    miss = store.missing(sessions)
    print(f"\n共 {len(sessions)} 个会话，已取号 {len(sessions) - len(miss)} 个"
          f"，未取号 {len(miss)} 个：{miss}")
    conf = store.conflicts()
    if conf:
        print(f"[!] 重号告警：{conf}")
    return 0


def cmd_check(store: UidStore, win) -> int:
    conf = store.conflicts()
    sessions = list_sessions(win)
    print(f"缓存条目 {len(store.map)} 个")
    for n, i in sorted(store.map.items(), key=lambda kv: kv[1].uin):
        print(f"  {i.uin:<13} ← {n!r}（真实昵称 {i.card_name!r}，取于 "
              f"{time.strftime('%m-%d %H:%M', time.localtime(i.at)) if i.at else '?'}）")
    if conf:
        print(f"\n[!] 以下 QQ 号被多个显示名映射，说明存在重名/改备注，需要人工消歧：")
        for u, ns in conf.items():
            print(f"    QQ {u} ← {ns}")
        return 1
    print("\n[OK] 无重号")
    live = {s.display_name for s in sessions}
    stale = [n for n in store.map if n not in live]
    if stale:
        print(f"[i] 缓存里已不在当前会话列表的名字（可能只是没滚到）：{stale}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="QQ 号抓取与缓存")
    ap.add_argument("--list", action="store_true", help="列出会话与已缓存 QQ 号")
    ap.add_argument("--enroll", type=int, nargs="*", metavar="IDX", help="给指定序号的会话取号")
    ap.add_argument("--enroll-all", action="store_true", help="给所有会话取号")
    ap.add_argument("--force", action="store_true", help="已有缓存也重新取号")
    ap.add_argument("--limit", type=int, default=0, help="批量取号上限")
    ap.add_argument("--check", action="store_true", help="重号诊断")
    ap.add_argument("--store", default=DEFAULT_STORE, help="缓存文件路径")
    args = ap.parse_args()

    win_desc, hwnd = find_qq_main_window()
    if not hwnd:
        # 用与常驻相同的分流：QQ 没启动 / 窗口在托盘 / 无障碍没生效，三种原因不同动作
        code, ctx = A.QQWindow(A.load_config()).diagnose_attach()
        A.diag(code, "取号需要 QQ 主窗口可见", ctx)
        return 2
    win = A.auto.ControlFromHandle(hwnd)
    store = UidStore(args.store)
    print(f"[i] QQ 主窗口 rect={win_desc['rect']} pid={win_desc['pid']}")

    if args.check:
        return cmd_check(store, win)
    if args.enroll_all:
        st = enroll_all(win, store, only_missing=not args.force, limit=args.limit)
        print(f"[OK] {st}")
        return 0 if st["ok"] else 1
    if args.enroll is not None and args.enroll != []:
        sessions = list_sessions(win)
        for i in args.enroll:
            if i < 0 or i >= len(sessions):
                print(f"[X] 序号 {i} 越界（0~{len(sessions) - 1}）")
                continue
            enroll(win, store, sessions[i])
        return 0
    return cmd_list(win, store)


if __name__ == "__main__":
    raise SystemExit(main())
