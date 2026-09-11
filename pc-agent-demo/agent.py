# -*- coding: utf-8 -*-
"""
agent.py — PC 端 QQ 私聊 AI 代理（最小 demo · 路线 A）

做什么：
    附着到你正在运行的 QQ 客户端窗口上，按「UIA → OCR」的链条读取新消息，
    调用 OpenAI 兼容接口拿到回复，再用「剪贴板粘贴」的方式把回复打进输入框发出去。
    不碰任何协议、不碰任何硬件模拟，纯粹是「替人在操作这台电脑上的 QQ」。

范围（刻意收窄）：
    - 只处理纯文本
    - 只处理私聊（群聊直接跳过）
    - 不处理图片 / 语音 / 文件 / 表情

用法：
    python agent.py --selftest          # 不开 QQ，自检配置 + 模型连通性 + 调教解析
    python agent.py --replay "在吗"      # 不开 QQ，跑一遍「收到消息 → 生成回复」的链路
    python agent.py --once              # 开 QQ，只跑一轮（调试用）
    python agent.py                     # 开 QQ，进入常驻循环
    python agent.py --dry-run           # 开 QQ，只读不发（确认读到的内容对不对）

运行前必做：
    1) QQ(NT版) 用 `--force-renderer-accessibility` 参数启动，否则 UIA 树是空的
    2) 先跑 python probe.py，确认能读到消息条目
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import hashlib
import json
import os
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    import uiautomation as auto
except ImportError:
    print("[X] 缺少 uiautomation。请先执行：pip install -r requirements.txt")
    raise SystemExit(2)

try:
    import pyperclip
except ImportError:
    pyperclip = None  # 发送时会退化为 UIA ValuePattern，功能降级但不致命

try:
    import requests
except ImportError:
    print("[X] 缺少 requests。请先执行：pip install -r requirements.txt")
    raise SystemExit(2)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")

auto.SetGlobalSearchTimeout(1.2)
if hasattr(auto, "SetGlobalSearchInterval"):
    auto.SetGlobalSearchInterval(0.25)


# ============================================================ 基础工具
def log(tag: str, msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {tag:<5} {msg}", flush=True)


def clip(text: str, n: int = 60) -> str:
    text = (text or "").replace("\n", "⏎")
    return text if len(text) <= n else text[: n - 1] + "…"


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def process_path(pid: int) -> str:
    if not pid:
        return ""
    h = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _k32.CloseHandle(h)


# ============================================================ 配置
DEFAULTS = {
    "llm": {
        "api_base": "https://api.deepseek.com",
        "api_key": "",
        "api_key_file": "",
        "api_key_json_keys": ["deepseek_api_key", "api_key", "key"],
        "model": "deepseek-chat",
        "temperature": 1.1,
        "max_tokens": 800,
        "timeout_seconds": 60,
        "system_prompt": "你是『小清澈』，一个温和真诚的 AI 陪伴者，回复口语化且简短。",
    },
    "chat": {
        "self_nickname": "",
        # false = 群聊也处理（默认已放开，方便测试）；true = 群聊一律跳过
        "private_chat_only": False,
        "always_reply": True,
        # 群聊里是否额外要求触发词。false = 群聊也自由发言（当前放开，方便测试）
        "group_requires_trigger": False,
        "trigger_prefixes": ["小清澈", "清澈"],
        "reply_cooldown_seconds": 1.5,
        "min_llm_interval_seconds": 2.0,
        "max_history_entries": 40,
        "send_with_ctrl_enter": False,
        "poll_interval_seconds": 0.8,
        "max_reply_chars": 500,
    },
    "teach": {
        "enabled": True,
        "names": ["小清澈", "Claritas-小清澈"],
        "max_chars": 1000,
        "honor_own_outgoing": False,
    },
    "uia": {
        "read_chain": ["uia", "ocr"],
        "direction_mode": "auto",
        "ocr_lang": "chi_sim+eng",
        "ocr_scale": 2.0,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    raw = {}
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as exc:
            log("WARN", f"config.json 解析失败，使用内置默认值：{exc}")
    return _deep_merge(DEFAULTS, raw)


_LEAF_KEY_NAMES = {"api_key", "apikey", "api_token", "apitoken", "token", "secret", "key"}
_PREF_SECTION_HINTS = {"llm", "openai", "deepseek", "ai", "chat", "model", "gpt"}


def _walk_json(obj, path: str = ""):
    """把嵌套 JSON 摊平成 (点分路径, 值) 的叶子序列。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                yield from _walk_json(v, f"{path}{k}.")
            else:
                yield f"{path}{k}", v
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_json(v, f"{path}{i}.")


def _pick_from_json(data, keys) -> tuple[str, str]:
    """从 JSON 里挑密钥，返回 (密钥, 来源路径)。支持 'llm.api_key' 这种点分路径。"""
    flat = dict(_walk_json(data))

    # 1) 显式配置的路径优先
    for k in keys or []:
        v = flat.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip(), k

    # 2) 自动发现：叶子名像密钥，且所在段落像 llm 的优先
    cands = []
    for p, v in flat.items():
        if not isinstance(v, str) or not v.strip():
            continue
        leaf = p.split(".")[-1].lower()
        if leaf not in _LEAF_KEY_NAMES:
            continue
        section = p.split(".")[0].lower()
        score = 0
        if section in _PREF_SECTION_HINTS:
            score += 10
        if "api" in leaf:
            score += 5
        cands.append((score, p, v.strip()))
    if cands:
        cands.sort(key=lambda x: (-x[0], x[1]))
        return cands[0][2], cands[0][1] + " (自动发现)"
    return "", ""


def resolve_api_key(cfg: dict, verbose: bool = False) -> str:
    """api_key 为空时，从 api_key_file 里捞。支持纯文本 / 扁平 JSON / 嵌套 JSON。"""
    key = (cfg["llm"].get("api_key") or "").strip()
    if key:
        if verbose:
            log("INFO", "密钥来源：config.json 内联 api_key")
        return key

    path = (cfg["llm"].get("api_key_file") or "").strip()
    if not path or not os.path.isfile(path):
        if verbose and path:
            log("WARN", f"api_key_file 不存在：{path}")
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except Exception as exc:
        if verbose:
            log("WARN", f"读取 api_key_file 失败：{exc}")
        return ""

    if path.lower().endswith(".json") or text.startswith("{"):
        try:
            data = json.loads(text)
        except Exception:
            return ""
        value, source = _pick_from_json(data, cfg["llm"].get("api_key_json_keys"))
        if verbose and value:
            log("INFO", f"密钥来源：{os.path.basename(path)} → {source}")
        return value
    return text


# ============================================================ 调教（teach）机制
# 用户发出以「小清澈：」开头的消息 → 不触发 AI 调用，直接作为 assistant 发言写入上下文
# 全角/半角冒号、中文/英文名字都兼容

def build_teach_pattern(names) -> re.Pattern:
    alts = "|".join(re.escape(n) for n in names if n)
    # 兼容：: ： ﹕ ∶ 四种冒号
    return re.compile(rf"^\s*(?:{alts})\s*[:：﹕∶]\s*([\s\S]*)$", re.I)


def parse_teach(text: str, cfg: dict) -> Optional[str]:
    """是调教语句则返回被调教的内容，否则 None。"""
    if not cfg["teach"].get("enabled"):
        return None
    if not text:
        return None
    pat = build_teach_pattern(cfg["teach"].get("names") or [])
    m = pat.match(text)
    if not m:
        return None
    body = (m.group(1) or "").strip()
    if not body:
        return None
    limit = int(cfg["teach"].get("max_chars") or 1000)
    return body[:limit]


def looks_like_trigger(text: str, cfg: dict) -> bool:
    """触发词模式：消息以触发词开头才回。"""
    t = (text or "").lstrip()
    for pre in cfg["chat"].get("trigger_prefixes") or []:
        if pre and t.startswith(pre):
            return True
    return False


def strip_trigger(text: str, cfg: dict) -> str:
    t = (text or "").lstrip()
    for pre in cfg["chat"].get("trigger_prefixes") or []:
        if pre and t.startswith(pre):
            t = t[len(pre):].lstrip("：: 　,")
            break
    return t


# ============================================================ 消息模型
def fingerprint_of(sender: str, content: str, extra: str = "") -> str:
    """消息去重用的内容指纹，用于 RuntimeId / AutomationId 都取不到时的兜底。"""
    raw = f"{sender}\x00{content}\x00{extra}"
    return "fp:" + hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()[:16]


@dataclass
class Message:
    sender: str
    content: str
    direction: str  # "me" | "other" | "unknown"
    key: str
    rect: tuple = (0, 0, 0, 0)
    kind: str = "text"   # "text" | "nontext"（图片/语音/文件等）
    ts: str = ""         # QQ 只在时间间隔较大时显示，可能为空


class History:
    """对话历史。带单调递增 _seq，避免同毫秒内写入导致顺序错乱。"""

    def __init__(self, max_entries: int = 40, system_prompt: str = ""):
        self.max_entries = max_entries
        self.system_prompt = system_prompt
        self._items: list[dict] = []
        self._seq = 0

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def push(self, role: str, content: str, source: str = "") -> dict:
        entry = {
            "role": role,
            "content": content,
            "seq": self.next_seq(),
            "_source": source,
            "_ts": time.time(),
        }
        self._items.append(entry)
        self._trim()
        return entry

    def _trim(self) -> None:
        # 只按条数裁剪，保证 system 不在列表里（system 单独走）
        if len(self._items) > self.max_entries:
            self._items = self._items[-self.max_entries:]

    def build_messages(self) -> list[dict]:
        msgs = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        for it in self._items:
            # 只把 role/content 交给 API，内部元数据不上行
            msgs.append({"role": it["role"], "content": it["content"]})
        return msgs

    def dump(self, n: int = 6) -> str:
        out = []
        for it in self._items[-n:]:
            tag = {"user": "对方", "assistant": "小清澈"}.get(it["role"], it["role"])
            tag = f"{tag}/{it['_source']}" if it.get("_source") else tag
            out.append(f"      #{it['seq']} {tag}: {clip(it['content'], 50)}")
        return "\n".join(out) or "      (空)"


# ============================================================ LLM
class LLMClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.api_key = resolve_api_key(cfg)
        base = (cfg["llm"].get("api_base") or "").rstrip("/")
        self.url = base + "/chat/completions"
        self.model = cfg["llm"].get("model")
        self.timeout = float(cfg["llm"].get("timeout_seconds") or 60)
        self._last_call = 0.0

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def wait_turn(self) -> None:
        gap = float(self.cfg["chat"].get("min_llm_interval_seconds") or 0)
        delta = time.time() - self._last_call
        if gap > 0 and delta < gap:
            time.sleep(gap - delta)

    def chat(self, messages: list[dict]) -> str:
        if not self.api_key:
            raise RuntimeError("api_key 为空，且 api_key_file 里也没找到可用密钥")
        self.wait_turn()
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": float(self.cfg["llm"].get("temperature") or 1.0),
            "max_tokens": int(self.cfg["llm"].get("max_tokens") or 800),
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        self._last_call = time.time()
        resp = requests.post(self.url, headers=headers, json=payload, timeout=self.timeout)
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"响应里没有 choices：{str(data)[:300]}")
        return (choices[0].get("message") or {}).get("content", "").strip()


# ============================================================ QQ 窗口驱动
# 下面的类名 / AutomationId 全部来自 probe.py 对本机 QQNT 的实测（2026-09-11）。
# 实测结构（缩进表示父子关系）：
#
#   WindowControl 'QQ' class='Chrome_WidgetWin_1'
#     ... DocumentControl '' id='RootWebArea'
#       GroupControl class='chat-header panel-header ...'
#         ButtonControl '寂静挽歌' class='chat-header__contact-name'
#           TextControl '寂静挽歌'
#           TextControl '(9)'                        ← 群人数，私聊没有这一段
#       GroupControl class='group-chat'              ← 群聊容器
#         GroupControl class='chat-msg-area'
#           PaneControl '消息列表' class='ml-area ...'
#             GroupControl class='... ml-root ...' id='ml-root'          ← 消息区根
#               GroupControl class='ml-list list'
#                 GroupControl class='ml-item' id='<消息ID>'              ← 消息条目
#                   GroupControl class='message__timestamp no-copy' → TextControl 时间
#                   GroupControl class='message-container ...'
#                     GroupControl '昵称' class='avatar-span'
#                     GroupControl class='user-name no-copy text-ellipsis' → TextControl 昵称
#                     GroupControl class='message-content__wrapper'
#                       GroupControl class='msg-content-container container--others ...'
#                         GroupControl class='message-content mix-message__inner' → 正文
#       GroupControl class='qq-msg-editor__root'
#         GroupControl class='ProseMirror is-empty ExEditor-qq-msg-editor'  ← 输入框
#       GroupControl class='send send--disabled'
#         ButtonControl '发送' class='send-msg'                          ← 发送按钮
#
# 三个与「常见 Electron 应用」直觉相反的结论：
#   1. 没有 ListControl / ListItemControl。消息条目是 GroupControl，靠 ClassName='ml-item'
#      和 AutomationId（18~19 位数字的消息 ID）识别 —— 这个 ID 天然唯一且稳定，
#      比 RuntimeId 更适合做去重键。
#   2. 输入框是 GroupControl（ProseMirror 富文本），不是 EditControl / DocumentControl。
#   3. 方向信息藏在 class 里：container--others / container--self。

AID_ML_ROOT = "ml-root"                  # 消息区根（同时是 AutomationId）
CLS_ML_LIST = "ml-list"                  # 消息条目容器
CLS_ML_ITEM = "ml-item"                  # 单条消息
CLS_EDITOR = "ExEditor-qq-msg-editor"    # 输入框
CLS_SEND_BTN = "send-msg"                # 发送按钮
CLS_SEND_DISABLED = "send--disabled"     # 发送按钮禁用态
CLS_CHAT_TITLE = "chat-header__contact-name"  # 会话标题
CLS_DIR_OTHER = "container--others"
CLS_DIR_SELF = "container--self"
# ⚠️ 踩坑记录：'group-chat' 这个 class 名具有误导性 —— 私聊的容器也叫 group-chat！
#    实测（2026-09-11）私聊「光みつる」的聊天区容器同样是 class='group-chat'，
#    所以它**绝不能**用来判断群聊。群聊只能靠「标题旁的 (人数)」和「群资料面板」判定。
CLS_GROUP_CHAT = "group-chat"            # 聊天区容器（群聊/私聊都用，勿当群聊标志）
CLS_MSG_CONTAINER_SELF = "message-container--self"    # 消息容器级的方向标记
CLS_MSG_CONTAINER_ALIGN_RIGHT = "message-container--align-right"
CLS_INNER = "mix-message__inner"
CLS_INNER_REPLY = "reply-message__inner"
CLS_AVATAR = "avatar-span"
CLS_TIMESTAMP = "message__timestamp"
CLS_USERNAME = "user-name"

SKIP_TYPES = {"ScrollBarControl", "ThumbControl", "TitleBarControl"}
SCAN_MAX_DEPTH = 24
SCAN_MAX_NODES = 8000


# ---------------------------------------------------------------- 控件读取小工具
def _rect(ctrl) -> tuple:
    try:
        r = ctrl.BoundingRectangle
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return (0, 0, 0, 0)


def _area(rect: tuple) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def _cls(ctrl) -> str:
    try:
        return ctrl.ClassName or ""
    except Exception:
        return ""


def _aid(ctrl) -> str:
    try:
        return getattr(ctrl, "AutomationId", "") or ""
    except Exception:
        return ""


def _name(ctrl) -> str:
    try:
        return ctrl.Name or ""
    except Exception:
        return ""


def _ctype(ctrl) -> str:
    try:
        return ctrl.ControlTypeName
    except Exception:
        return ""


def _kids(ctrl) -> list:
    try:
        return ctrl.GetChildren()
    except Exception:
        return []


def _has_cls(ctrl, token: str) -> bool:
    return token in _cls(ctrl)


def _visible(ctrl) -> bool:
    """
    控件是否真的占地方。Chromium 会把换出去的界面留在 DOM 里（rect 全 0），
    判定特征时必须加这个过滤，否则会命中残留节点 —— 实测踩过。
    """
    return _area(_rect(ctrl)) > 0


def iter_bfs(root, maxdepth: int, limit: int = SCAN_MAX_NODES):
    """广度优先遍历后代，产出 (控件, 深度)。带深度与数量上限，避免在巨型树里失控。"""
    queue = collections.deque([(root, 0)])
    n = 0
    while queue and n < limit:
        ctrl, depth = queue.popleft()
        n += 1
        if depth >= maxdepth:
            continue
        for ch in _kids(ctrl):
            yield ch, depth + 1
            queue.append((ch, depth + 1))


def collect_texts(ctrl, maxdepth: int = 6) -> list[str]:
    """
    递归收集子节点文本，保持文档顺序。

    QQ 把一条消息的整段正文放在**单个** TextControl 里（含换行），
    而 @ 提醒会被拆成独立的 text-element--at，所以用空串拼接才是对的。
    """
    out: list[str] = []

    def rec(c, d):
        if d > maxdepth:
            return
        t = _ctype(c)
        if t in SKIP_TYPES:
            return
        if t == "TextControl":
            # 保留原始空白：@ 提醒被拆成独立节点，正文节点以空格开头，
            # 这里如果 strip 掉，拼出来就会变成 "@某人你好" 而不是 "@某人 你好"
            nm = _name(c)
            if nm.strip():
                out.append(nm)
            return
        if t == "ImageControl":
            nm = _name(c).strip()
            out.append(f"[{nm}]" if nm else "[图片]")
            return
        for ch in _kids(c):
            rec(ch, d + 1)

    rec(ctrl, 0)
    return out


# ---------------------------------------------------------------- 不依赖第三方库的剪贴板写入
def set_clipboard(text: str) -> bool:
    """
    直接写 Windows 剪贴板（CF_UNICODETEXT）。
    中文**必须**走剪贴板：SendKeys 打不出中文，逐字符模拟还会丢字。
    """
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u32.SetClipboardData.restype = wintypes.HANDLE
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = wintypes.HANDLE
    k32.GlobalLock.argtypes = [wintypes.HANDLE]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [wintypes.HANDLE]

    if not u32.OpenClipboard(None):
        return False
    try:
        u32.EmptyClipboard()
        buf = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(buf)
        handle = k32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        ptr = k32.GlobalLock(handle)
        if not ptr:
            return False
        ctypes.memmove(ptr, buf, size)
        k32.GlobalUnlock(handle)
        return bool(u32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        u32.CloseClipboard()


def copy_to_clipboard(text: str) -> bool:
    if pyperclip is not None:
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            pass
    return set_clipboard(text)


# ---------------------------------------------------------------- 窗口枚举
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_u32.IsWindowVisible.argtypes = [wintypes.HWND]
_u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]


class _WRECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_WRECT)]


def _enum_top_windows() -> list[dict]:
    """
    枚举所有顶层窗口（含隐藏窗口）。

    必须用 EnumWindows：QQ 点关闭只是缩到托盘，uiautomation 的可见窗口枚举
    会直接漏掉它，导致误报「找不到 QQ 窗口」。
    """
    rows: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        n = _u32.GetWindowTextLengthW(hwnd)
        tbuf = ctypes.create_unicode_buffer(n + 1)
        _u32.GetWindowTextW(hwnd, tbuf, n + 1)
        cbuf = ctypes.create_unicode_buffer(256)
        _u32.GetClassNameW(hwnd, cbuf, 256)
        r = _WRECT()
        _u32.GetWindowRect(hwnd, ctypes.byref(r))
        rows.append(
            {
                "hwnd": hwnd,
                "pid": pid.value,
                "visible": bool(_u32.IsWindowVisible(hwnd)),
                "class": cbuf.value,
                "title": tbuf.value,
                "rect": [r.left, r.top, r.right, r.bottom],
            }
        )
        return True

    _u32.EnumWindows(_cb, 0)
    return rows


def control_from_hwnd(hwnd):
    if hasattr(auto, "ControlFromHandle"):
        try:
            return auto.ControlFromHandle(hwnd)
        except Exception:
            pass
    try:
        return auto.Control(Handle=hwnd)
    except Exception:
        return None


# ---------------------------------------------------------------- 前台窗口控制
# ⚠️ 这里踩过一个很危险的坑：Windows 默认禁止后台进程抢前台窗口，
#    单纯调 SetForegroundWindow 会**静默失败**（last error 5 = ACCESS_DENIED）。
#    失败之后 SendKeys 的按键不会消失 —— 它们会打进当时真正的那个前台窗口！
#    实测：{Enter} 打进了终端，把半截命令行当命令提交了，表现为「程序卡死」。
#    所以规矩是：**发按键之前必须验证目标窗口确实在前台，否则一个键都不许发。**
_SW_RESTORE = 9


def _fg_hwnd() -> int:
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.GetForegroundWindow.restype = wintypes.HWND
    try:
        return int(u32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _is_foreground(hwnd: int) -> bool:
    return bool(hwnd) and _fg_hwnd() == int(hwnd)


def force_foreground(hwnd: int, retries: int = 5) -> bool:
    """
    尽力把窗口切到前台，并**返回是否真的成功**（以 GetForegroundWindow 为准）。

    用了三招组合拳：AttachThreadInput 打通输入队列 + BringWindowToTop + SetForegroundWindow。
    如果调用方是前台进程（比如你从自己的终端启动），这些都能成功；
    如果调用方是后台/沙盒进程，会被系统拒绝 —— 那就如实返回 False，由调用方决定放弃。
    """
    if not hwnd:
        return False
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.SetForegroundWindow.restype = wintypes.BOOL
    u32.GetForegroundWindow.restype = wintypes.HWND
    u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    k32.GetCurrentThreadId.restype = wintypes.DWORD
    hwnd = int(hwnd)

    for _ in range(max(1, retries)):
        if _is_foreground(hwnd):
            return True
        try:
            if u32.IsIconic(hwnd):
                u32.ShowWindow(hwnd, _SW_RESTORE)
        except Exception:
            pass
        fg = _fg_hwnd()
        tid_fg = u32.GetWindowThreadProcessId(fg, None) if fg else 0
        tid_me = k32.GetCurrentThreadId()
        attached = False
        if tid_fg and tid_fg != tid_me:
            attached = bool(u32.AttachThreadInput(tid_fg, tid_me, True))
        try:
            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
            u32.SetFocus(hwnd)
        except Exception:
            pass
        finally:
            if attached:
                try:
                    u32.AttachThreadInput(tid_fg, tid_me, False)
                except Exception:
                    pass
        time.sleep(0.1)
    return _is_foreground(hwnd)


def restore_foreground(prev_hwnd: int, retries: int = 3) -> bool:
    """
    把前台窗口还给 prev_hwnd —— 方案 C（降低打扰）的收尾动作。

    为什么不能直接 SetForegroundWindow(prev) 就完事：
    抢前台成功之后，「前台进程」其实是 QQ 而不是我们，系统照样可能拒绝我们
    的 SetForegroundWindow。所以这里复用同一套 AttachThreadInput 组合拳，
    并如实返回是否成功 —— 失败不是致命错误，只意味着 QQ 仍留在最上层。

    安全性：只对调用前真实存在过的窗口操作，且用 IsWindow 兜住「窗口已关闭」。
    """
    if not prev_hwnd:
        return False
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.SetForegroundWindow.restype = wintypes.BOOL
    u32.IsWindow.argtypes = [wintypes.HWND]
    u32.IsWindow.restype = wintypes.BOOL
    u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    k32.GetCurrentThreadId.restype = wintypes.DWORD
    prev_hwnd = int(prev_hwnd)

    for _ in range(max(1, retries)):
        if _is_foreground(prev_hwnd):
            return True
        if not u32.IsWindow(prev_hwnd):
            return False          # 原窗口已被关掉，没什么可还的
        tid_prev = u32.GetWindowThreadProcessId(prev_hwnd, None)
        tid_me = k32.GetCurrentThreadId()
        attached = False
        if tid_prev and tid_prev != tid_me:
            attached = bool(u32.AttachThreadInput(tid_prev, tid_me, True))
        try:
            u32.SetForegroundWindow(prev_hwnd)
            u32.SetFocus(prev_hwnd)
        except Exception:
            pass
        finally:
            if attached:
                try:
                    u32.AttachThreadInput(tid_prev, tid_me, False)
                except Exception:
                    pass
        time.sleep(0.1)
    return _is_foreground(prev_hwnd)


# ---------------------------------------------------------------- 窗口驱动
class QQWindow:
    """
    把 QQ 窗口封成『读消息 / 发消息』两个动作。

    性能设计（实测数据）：完整遍历一次 UIA 树约 0.40s（586 节点），
    而按锚点做有界 BFS 只要 0.04s。所以：
      - 锚点（消息列表 / 输入框 / 发送按钮 / 标题）缓存，每 5 秒或失效时才重扫；
      - 每轮轮询只读缓存的 ml-list 的直接子节点。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.win = None
        self.ml_root = None
        self.ml_list = None
        self.editor = None
        self.send_btn = None
        self.send_holder = None
        self.title_btn = None
        self.dialog_title = ""
        self.is_group = False
        self.member_count = 0
        self.self_nickname = ""
        self._layout_at = 0.0
        self._seen = collections.OrderedDict()   # key -> True
        self._seen_max = 600
        self.ocr = OcrReader(cfg)
        self._ocr_warned = False

    # ---------------------------------------------------- 附着
    def attach(self) -> bool:
        """挑最大的**可见** QQ 主窗口。隐藏窗口没有无障碍树，读了也是空。"""
        best, best_area = None, 0
        for row in _enum_top_windows():
            if not row["visible"]:
                continue
            if row["class"] not in ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0", "TXGuiFoundation"):
                continue
            p = process_path(row["pid"]).lower()
            if "qq.exe" not in p and "qqnt" not in p and row["class"] != "TXGuiFoundation":
                continue
            a = _area(tuple(row["rect"]))
            if a > best_area:
                cand = control_from_hwnd(row["hwnd"])
                if cand is not None:
                    best, best_area = cand, a
        if best is None:
            return False
        self.win = best
        self.refresh_layout(force=True)
        return True

    @property
    def rect(self) -> tuple:
        return _rect(self.win) if self.win is not None else (0, 0, 0, 0)

    # ---------------------------------------------------- 锚点扫描
    def _scan_anchors(self) -> dict:
        """一次有界 BFS 把所有锚点找齐，避免多次全树遍历。"""
        res: dict = {}
        for ctrl, _d in iter_bfs(self.win, SCAN_MAX_DEPTH):
            cls = _cls(ctrl)
            if "ml_list" not in res and CLS_ML_LIST in cls:
                res["ml_list"] = ctrl
            if CLS_EDITOR in cls:
                # 优先拿「占地方」的那个：换过会话后，旧输入框可能还留在 DOM 里（rect 全 0）
                if "editor" not in res or (not _visible(res["editor"]) and _visible(ctrl)):
                    res["editor"] = ctrl
            if "send_btn" not in res and _ctype(ctrl) == "ButtonControl" and CLS_SEND_BTN in cls:
                res["send_btn"] = ctrl
            if "send_holder" not in res and (cls == "send" or cls.startswith("send ")):
                res["send_holder"] = ctrl
            if "title_btn" not in res and CLS_CHAT_TITLE in cls:
                res["title_btn"] = ctrl
            if "group_mark" not in res and _visible(ctrl):
                # 只有「占地方」的群资料面板才算数，挡掉残留 DOM
                nm = _name(ctrl)
                if nm == "群公告" or nm.startswith("群聊成员"):
                    res["group_mark"] = ctrl
            if "self_nick" not in res and "user-profile-card__nickname" in cls:
                # 顶栏个人卡片里就写着自己的昵称，不用手填配置
                texts = [t.strip() for t in collect_texts(ctrl, 3) if t.strip()]
                if texts:
                    res["self_nick"] = texts[-1]
            if len(res) >= 7:
                break
        return res

    def refresh_layout(self, force: bool = False) -> None:
        if not force and time.time() - self._layout_at < 5.0:
            return
        self._layout_at = time.time()
        if self.win is None:
            return

        res = self._scan_anchors()
        self.ml_list = res.get("ml_list")
        self.editor = res.get("editor")
        self.send_btn = res.get("send_btn")
        self.send_holder = res.get("send_holder")
        self.title_btn = res.get("title_btn")
        self.ml_root = None  # 按需使用，保留字段便于调试
        # 自己的昵称：配置优先，配置为空则用界面顶栏自动识别的
        self.self_nickname = (self.cfg["chat"].get("self_nickname") or "").strip() \
            or res.get("self_nick", "")

        # 会话标题 + 群人数：标题按钮下若有形如 "(9)" 的文本就是群聊
        self.dialog_title = ""
        self.member_count = 0
        if self.title_btn is not None:
            self.dialog_title = _name(self.title_btn).strip()
            for ch in _kids(self.title_btn):
                nm = _name(ch).strip()
                if re.fullmatch(r"\(\d+\)", nm):
                    self.member_count = int(nm[1:-1])

        # 群聊判定：只认「正向且可见」的两个信号
        #   1) 标题旁有形如 "(9)" 的人数标记
        #   2) 界面上真实存在群资料面板（群公告 / 群聊成员 N）
        # 绝对不要用容器 class='group-chat' —— 私聊的容器也叫这个名字（实测）
        self.is_group = bool(self.member_count > 0 or res.get("group_mark"))

    # ---------------------------------------------------- 读消息
    def read_messages(self, limit: int = 12) -> list[Message]:
        if self.ml_list is None:
            self.refresh_layout(force=True)
        if self.ml_list is None:
            return []

        items = [c for c in _kids(self.ml_list) if _has_cls(c, CLS_ML_ITEM)]
        if not items:
            # 切会话后容器可能被整个替换，强制重扫一次
            self.refresh_layout(force=True)
            if self.ml_list is None:
                return []
            items = [c for c in _kids(self.ml_list) if _has_cls(c, CLS_ML_ITEM)]
        items = items[-limit:]
        if not items:
            # UIA 完全读不到 → 按 read_chain 决定要不要走 OCR 兜底
            return self._read_via_ocr(limit)

        lr = _rect(self.ml_list)
        center_x = (lr[0] + lr[2]) / 2
        self_nick = self.self_nickname
        mode = self.cfg["uia"].get("direction_mode") or "auto"
        total = len(items)
        msgs = [
            self._parse_item(item, idx, total, center_x, self_nick, mode)
            for idx, item in enumerate(items)
        ]
        # 私聊里对端昵称缺失时用会话标题兜底 —— 1-1 会话的对端就是标题本身
        if not self.is_group and self.dialog_title:
            for m in msgs:
                if not m.sender and m.direction == "other":
                    m.sender = self.dialog_title
        return msgs

    def _read_via_ocr(self, limit: int) -> list[Message]:
        """
        UIA 读不到消息时的兜底：截取消息区做 OCR。

        能力边界要讲清楚 —— OCR 没有控件信息，分不出昵称和消息归属。
        因此它只在 direction_mode='last_only' 下有意义：把最后一行当成对方刚发的。
        其它模式下一律返回空，避免把满屏历史当成新消息乱回。
        """
        chain = self.cfg["uia"].get("read_chain") or []
        if "ocr" not in chain or not self.ocr.ok:
            return []
        mode = self.cfg["uia"].get("direction_mode") or "auto"
        area = _rect(self.ml_list) if self.ml_list is not None else _rect(self.win)
        if _area(area) <= 0:
            return []
        lines = self.ocr.read_region(area)
        if not lines:
            return []
        if not self._ocr_warned:
            self._ocr_warned = True
            log("WARN", "UIA 读不到消息，已降级到 OCR 兜底（仅 last_only 模式可用）")
        if mode != "last_only":
            log("WARN", "OCR 兜底需要 direction_mode='last_only'，当前是 "
                        f"{mode!r}，已放弃本轮")
            return []
        tail = lines[-limit:]
        return [
            Message(sender="", content=ln,
                    direction="other" if i == len(tail) - 1 else "unknown",
                    key=fingerprint_of("", ln, str(i)), kind="text")
            for i, ln in enumerate(tail)
        ]

    def _parse_item(self, item, idx: int, total: int, center_x: float,
                    self_nick: str, mode: str) -> Message:
        msg_id = _aid(item)
        key = "aid:" + msg_id if msg_id else ""

        sender = ""
        ts = ""
        content = ""
        direction = "unknown"
        avatar_left = None

        for node, _d in iter_bfs(item, 8):
            cls = _cls(node)
            if not ts and CLS_TIMESTAMP in cls:
                ts = "".join(collect_texts(node, 2)).strip()
            if CLS_AVATAR in cls:
                if avatar_left is None:
                    avatar_left = _rect(node)[0]
                if not sender:
                    # 私聊里根本没有 user-name 元素，昵称就挂在 avatar-span 的 Name 上
                    sender = _name(node).strip()
            if direction == "unknown":
                # 方向有多重标记，都认：msg-content-container 上的 container--self/--others，
                # 以及上层 message-container 上的 --self / --align-right
                if (CLS_DIR_SELF in cls
                        or CLS_MSG_CONTAINER_SELF in cls
                        or CLS_MSG_CONTAINER_ALIGN_RIGHT in cls):
                    direction = "me"
                elif CLS_DIR_OTHER in cls:
                    direction = "other"
            if CLS_USERNAME in cls:
                # 群聊里 user-name 更准（可能带「群主」标签，取最后一段纯文本才是昵称）
                cand = [t.strip() for t in collect_texts(node, 3) if t.strip()]
                if cand:
                    sender = cand[-1]
            if not content and (CLS_INNER in cls or CLS_INNER_REPLY in cls):
                content = "".join(collect_texts(node, 6)).strip()

        # ---- 方向判定：class > 昵称 > 头像水平位置 ----
        if mode == "last_only":
            direction = "other" if idx == total - 1 else "unknown"
        else:
            if direction == "unknown" and self_nick and sender == self_nick:
                direction = "me"
            if direction == "unknown" and mode in ("auto", "position") and avatar_left is not None:
                # 对方头像在左（x≈1049），自己头像在右；用消息区中线切分最稳
                direction = "me" if avatar_left > center_x else "other"
            if direction == "unknown" and mode == "auto":
                direction = "other"

        # ---- 非文本判定：去掉 [图片]/[语音] 这类占位后如果什么都不剩 ----
        kind = "text" if re.sub(r"\[[^\]]*\]", "", content).strip() else "nontext"

        if not key:
            key = fingerprint_of(sender, content, str(idx))
        return Message(
            sender=sender, content=content, direction=direction,
            key=key, rect=_rect(item), kind=kind, ts=ts,
        )

    # ---------------------------------------------------- 新消息切分
    def mark_seen(self, msgs: list[Message]) -> None:
        for m in msgs:
            self._seen[m.key] = True
        while len(self._seen) > self._seen_max:
            self._seen.popitem(last=False)

    def split_new(self, msgs: list[Message]) -> list[Message]:
        fresh = [m for m in msgs if m.key not in self._seen]
        self.mark_seen(msgs)
        return fresh

    def baseline(self, skip_last: int = 0) -> int:
        """
        把已有消息标记为已读，避免启动时对着历史记录刷屏。
        skip_last=N 时保留最后 N 条不标记，留给本次处理（调试用）。
        """
        msgs = self.read_messages(limit=30)
        target = msgs[: len(msgs) - skip_last] if skip_last > 0 else msgs
        self.mark_seen(target)
        return len(target)

    # ---------------------------------------------------- 发消息
    def _send_disabled(self) -> bool:
        if self.send_holder is None:
            return False
        return CLS_SEND_DISABLED in _cls(self.send_holder)

    # ---------------------------------------------------- 输入（不发送）
    @property
    def hwnd(self) -> int:
        try:
            return int(self.win.NativeWindowHandle) if self.win is not None else 0
        except Exception:
            return 0

    def is_foreground(self) -> bool:
        return _is_foreground(self.hwnd)

    def editor_text(self) -> str:
        """当前输入框里的文本（用来验证粘贴是否真的成功）。"""
        if self.editor is None:
            return ""
        return "".join(collect_texts(self.editor, 4))

    def clear_editor(self) -> None:
        """清空输入框。**只能在校验过前台之后调用**（靠 Ctrl+A / Delete）。"""
        auto.SendKeys("{Ctrl}a", waitTime=0.02)
        time.sleep(0.03)
        auto.SendKeys("{Delete}", waitTime=0.02)
        time.sleep(0.05)

    def _release_foreground(self, prev_fg: int) -> None:
        """把前台还给调用前的窗口（方案 C）。best-effort，失败只记日志，绝不抛异常。"""
        if not self.cfg["uia"].get("restore_foreground", True):
            return
        if not prev_fg or prev_fg == self.hwnd:
            return
        time.sleep(0.05)          # 先让 Ctrl+V 的粘贴落地，再交还焦点
        if restore_foreground(prev_fg):
            log("INFO", "前台窗口已归还给调用前的那个窗口")
        else:
            log("WARN", "前台窗口归还失败（系统拒绝了 SetForegroundWindow），QQ 会继续留在最上层")

    def type_text(self, text: str) -> bool:
        """
        把文本写进输入框：聚焦 → 清空 → 剪贴板粘贴。**只写不发送**。

        安全前提：必须先确认 QQ 在前台。否则 Ctrl+A / Delete / Ctrl+V 会打进
        当时真正的那个前台窗口 —— 你可能正在终端里，那就是一顿乱改。

        打扰控制（方案 C）：
          1) 动手前记下当时的原前台窗口，写完立刻归还，把「QQ 顶在最上层」的
             时间压到最短（可把 config 的 uia.restore_foreground 设为 false 关掉）；
          2) 刻意**不调用** editor.Click() —— 真实鼠标点击会二次提升窗口、还会
             顺手挪走你的光标位置；UIA 的 editor.SetFocus() 已经足够拿到键盘焦点。
             万一某些版本 SetFocus 不够，send_text 里的回读校验会兜住，不会发出错内容。
        """
        if self.editor is None:
            self.refresh_layout(force=True)
        if self.editor is None:
            log("ERR", "找不到 QQ 输入框（ExEditor-qq-msg-editor）。请把目标会话窗口显示出来。")
            return False

        prev_fg = _fg_hwnd()
        if not force_foreground(self.hwnd):
            log("ERR", "无法把 QQ 窗口切到前台，已放弃输入（防止按键漏到其它窗口）")
            log("ERR", "请从你自己的终端启动本程序（子进程才有抢前台的权限），并别让 QQ 被完全挡住")
            return False

        try:
            try:
                self.editor.SetFocus()      # 只设焦点，不发鼠标点击
            except Exception:
                pass
            time.sleep(0.05)
            if not self.is_foreground():
                log("ERR", "聚焦后 QQ 仍不在前台，放弃输入")
                return False

            self.clear_editor()
            if not copy_to_clipboard(text):
                log("ERR", "写入剪贴板失败，无法输入中文")
                return False
            time.sleep(0.05)
            auto.SendKeys("{Ctrl}v", waitTime=0.05)
            return True
        finally:
            self._release_foreground(prev_fg)   # 无论成功失败，都把焦点还回去

    def wait_send_enabled(self, timeout: float = 1.0) -> bool:
        """
        等发送按钮从禁用态恢复。
        这同时是「文本是否真的进了输入框」最可靠的信号 —— 比等固定时长强。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._send_disabled():
                return True
            time.sleep(0.05)
        return False

    # ---------------------------------------------------- 发送
    def send_text(self, text: str) -> bool:
        if not self.type_text(text):
            return False

        if not self.wait_send_enabled():
            log("WARN", "发送按钮仍是禁用态，文本可能没进输入框，已中止发送")
            return False

        # 回读校验：确认输入框里真的是我们要发的内容，防止发出错误内容
        got = self.editor_text()
        want = text.strip()
        if want and want not in got:
            log("WARN", f"输入框回读不符，已中止发送：期望 {clip(want, 40)!r}，实际 {clip(got, 40)!r}")
            return False

        # 首选 InvokePattern 触发发送按钮：走 UIA 调用，**不需要前台窗口，也不产生任何按键**
        if self.send_btn is not None:
            try:
                self.send_btn.GetInvokePattern().Invoke()
                return True
            except Exception as exc:
                log("WARN", f"InvokePattern 发送失败，准备按键兜底：{exc}")

        # 兜底才用回车，且必须确认前台 —— 否则宁可失败也不发（见文件上方踩坑说明）
        if not self.is_foreground():
            log("ERR", "QQ 不在前台，拒绝发送回车（防止按键漏到其它窗口），本条中止")
            return False
        auto.SendKeys("{Enter}")
        return True


# ============================================================ OCR 兜底（可选）
class OcrReader:
    """
    UIA 读不到正文时的兜底。需要 pytesseract + tesseract-ocr 本体 + chi_sim 语言包。
    没装就静默降级，不阻塞主流程。

    注意能力边界：OCR 拿不到控件信息，也就分不出消息归属和昵称。
    所以它只在 direction_mode='last_only' 下有意义（把最后一行当成对方消息）。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ok = False
        self._pytesseract = None
        self._ImageGrab = None
        try:
            import pytesseract
            from PIL import ImageGrab

            self._pytesseract = pytesseract
            self._ImageGrab = ImageGrab
            self.ok = True
        except Exception:
            pass

    def read_region(self, rect: tuple) -> list[str]:
        if not self.ok:
            return []
        try:
            img = self._ImageGrab.grab(bbox=rect)
            scale = float(self.cfg["uia"].get("ocr_scale") or 2.0)
            if scale != 1.0:
                img = img.resize((int(img.width * scale), int(img.height * scale)))
            text = self._pytesseract.image_to_string(
                img, lang=self.cfg["uia"].get("ocr_lang") or "chi_sim+eng"
            )
            return [ln.strip() for ln in text.splitlines() if ln.strip()]
        except Exception as exc:
            log("WARN", f"OCR 失败：{exc}")
            return []


# ============================================================ 主流程
class Agent:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.qq = QQWindow(cfg)
        self.llm = LLMClient(cfg)
        self.ocr = OcrReader(cfg)
        self.history = History(
            max_entries=int(cfg["chat"].get("max_history_entries") or 40),
            system_prompt=cfg["llm"].get("system_prompt") or "",
        )
        self._last_reply_at = 0.0
        self._last_msg_key = ""

    # ---------------------------------------------------- 一轮处理
    def step(self) -> int:
        """处理一轮新消息，返回处理的条数。"""
        self.qq.refresh_layout()
        msgs = self.qq.read_messages()
        fresh = self.qq.split_new(msgs)
        if not fresh:
            return 0

        handled = 0
        for m in fresh:
            if not m.content:
                continue

            # 只关心对方发来的
            if m.direction != "other":
                if m.direction == "me" and self.cfg["teach"].get("honor_own_outgoing"):
                    body = parse_teach(m.content, self.cfg)
                    if body:
                        self.history.push("assistant", body, source="teach-self")
                        log("TEACH", f"（我方手输）写入上下文：{clip(body)}")
                        handled += 1
                continue

            # 白名单：群聊直接跳过（本 demo 只做私聊）
            if self.cfg["chat"].get("private_chat_only") and self.qq.is_group:
                log("SKIP", f"群聊『{self.qq.dialog_title}』已跳过")
                continue

            # 本 demo 只处理纯文本，图片/语音/文件一律跳过
            if m.kind != "text":
                log("SKIP", f"非文本消息已跳过（{clip(m.content, 20)}）")
                continue

            log("RECV", f"{m.sender or '对方'}: {clip(m.content)}")

            # ---- 调教语句：不触发 AI，直接写进上下文 ----
            body = parse_teach(m.content, self.cfg)
            if body is not None:
                self.history.push("assistant", body, source="teach")
                log("TEACH", f"用户替 AI 说：{clip(body)}")
                handled += 1
                continue

            # ---- 触发词判定 ----
            # 群聊默认额外要求触发词：群里消息密集，不加限制会吵到所有人；
            # 想改成群聊也自由发言，把 chat.group_requires_trigger 设为 false
            need_trigger = (not self.cfg["chat"].get("always_reply")) or (
                self.qq.is_group and self.cfg["chat"].get("group_requires_trigger", True)
            )
            if need_trigger:
                if not looks_like_trigger(m.content, self.cfg):
                    log("SKIP", f"未命中触发词（{'群聊' if self.qq.is_group else '触发词模式'}）")
                    continue
                user_text = strip_trigger(m.content, self.cfg)
            else:
                user_text = m.content

            if not user_text:
                continue

            # ---- 冷却 ----
            cooldown = float(self.cfg["chat"].get("reply_cooldown_seconds") or 0)
            if cooldown > 0 and time.time() - self._last_reply_at < cooldown:
                log("SKIP", "命中冷却窗口，本条丢弃")
                continue
            if m.key == self._last_msg_key:
                continue

            self.history.push("user", user_text, source="incoming")
            handled += 1

            if self.dry_run:
                log("DRY", "dry-run：只记录，不调用模型")
                continue

            reply = self.generate_reply()
            if reply:
                self.deliver(reply)
                self._last_reply_at = time.time()
                self._last_msg_key = m.key
        return handled

    def generate_reply(self) -> str:
        try:
            reply = self.llm.chat(self.history.build_messages())
        except Exception as exc:
            log("ERR", f"模型调用失败：{exc}")
            # 把刚推进去的 user 消息留在历史里没关系，下一轮还会带上
            return ""

        reply = (reply or "").strip()
        limit = int(self.cfg["chat"].get("max_reply_chars") or 500)
        if len(reply) > limit:
            reply = reply[:limit]
        if not reply:
            log("WARN", "模型返回空内容")
            return ""
        return reply

    def deliver(self, reply: str) -> None:
        log("SEND", clip(reply, 70))
        self.history.push("assistant", reply, source="auto")
        if self.dry_run:
            return
        if not self.qq.send_text(reply):
            log("ERR", "发送失败，本条回复未送达")

    # ---------------------------------------------------- 常驻循环
    def run_forever(self) -> int:
        if not self.qq.attach():
            log("ERR", "找不到 QQ 窗口。请确认 QQ 已启动，且带 --force-renderer-accessibility 参数。")
            return 2
        log("INFO", f"已附着 QQ 窗口，标题={self.qq.dialog_title!r}，群聊={self.qq.is_group}")
        if self.qq.message_list is None:
            log("WARN", "没定位到消息列表控件，UIA 读取可能拿不到内容（考虑启用 OCR 兜底）")

        n = self.qq.baseline()
        log("INFO", f"基线建立完成，忽略已有 {n} 条历史消息")
        log("INFO", f"进入循环（poll={self.cfg['chat'].get('poll_interval_seconds')}s，"
                    f"always_reply={self.cfg['chat'].get('always_reply')}）")
        log("INFO", "按 Ctrl+C 退出")

        interval = float(self.cfg["chat"].get("poll_interval_seconds") or 0.8)
        while True:
            try:
                before = self.qq.dialog_title
                self.step()
                self.qq.refresh_layout()
                if self.qq.dialog_title != before:
                    log("INFO", f"会话已切换 → {self.qq.dialog_title!r}（群聊={self.qq.is_group}）")
                    self.qq.baseline()
            except KeyboardInterrupt:
                log("INFO", "收到退出信号，结束")
                return 0
            except Exception as exc:
                log("ERR", f"循环异常（已忽略继续）：{type(exc).__name__}: {exc}")
            time.sleep(interval)


# ============================================================ 自检 / 回放
def selftest(cfg: dict) -> int:
    print("=" * 72)
    print("自检：配置 / 密钥 / 模型连通性 / 调教解析")
    print("=" * 72)

    print("\n[1] 配置")
    print(f"    api_base : {cfg['llm']['api_base']}")
    print(f"    model    : {cfg['llm']['model']}")
    print(f"    key 来源 : {'内联 api_key' if cfg['llm'].get('api_key') else 'api_key_file=' + str(cfg['llm'].get('api_key_file'))}")
    print(f"    私聊限定 : {cfg['chat']['private_chat_only']}   always_reply: {cfg['chat']['always_reply']}")

    print("\n[2] 密钥")
    key = resolve_api_key(cfg, verbose=True)
    if not key:
        print("    [X] 没拿到密钥。请在 config.json 里填 api_key，或修正 api_key_file 路径。")
    else:
        print(f"    [✓] 已获取密钥（长度 {len(key)}，内容不打印）")

    print("\n[3] 模型连通性")
    if not key:
        print("    跳过（无密钥）")
    else:
        client = LLMClient(cfg)
        try:
            t0 = time.time()
            out = client.chat(
                [
                    {"role": "system", "content": cfg["llm"]["system_prompt"]},
                    {"role": "user", "content": "只回复两个字：收到"},
                ]
            )
            print(f"    [✓] {time.time() - t0:.2f}s 返回：{out!r}")
        except Exception as exc:
            print(f"    [X] 调用失败：{exc}")

    print("\n[4] 调教解析（应中 4 个，不中 2 个）")
    cases = [
        ("小清澈：今天有点累", True),
        ("小清澈:今天有点累", True),          # 半角冒号
        ("小清澈：   多空格也认", True),
        ("  小清澈：前面空格", True),
        ("小清澈今天有点累", False),           # 缺冒号 → 不认
        ("清澈：会被认成普通消息吗", False),   # 不在 names 里 → 不认
    ]
    ok = 0
    for text, expect in cases:
        got = parse_teach(text, cfg)
        hit = got is not None
        flag = "✓" if hit == expect else "✗"
        if hit == expect:
            ok += 1
        print(f"    {flag} {text!r:<28} → {got!r}")
    print(f"    {ok}/{len(cases)} 通过")

    print("\n[5] UIA 依赖")
    print(f"    uiautomation : OK")
    print(f"    pyperclip    : {'OK' if pyperclip else '缺失（发送会退化为 ValuePattern）'}")
    print(f"    OCR 兜底     : {'可用' if OcrReader(cfg).ok else '不可用（未装 pytesseract/Tesseract，属正常）'}")
    print("\n自检结束。")
    return 0


def replay(cfg: dict, text: str) -> int:
    print("=" * 72)
    print(f"回放：模拟收到一条消息 → 生成回复（不会真的发送）")
    print("=" * 72)

    history = History(
        max_entries=int(cfg["chat"].get("max_history_entries") or 40),
        system_prompt=cfg["llm"].get("system_prompt") or "",
    )
    print(f"\n收到：{text!r}")

    body = parse_teach(text, cfg)
    if body is not None:
        history.push("assistant", body, source="teach")
        print(f"\n[TEACH] 判定为调教语句，不调用模型，写入上下文：{body!r}")
        print("\n当前上下文：")
        print(history.dump())
        return 0

    if not cfg["chat"].get("always_reply") and not looks_like_trigger(text, cfg):
        print("\n[SKIP] 未命中触发词，不回复。")
        return 0
    user_text = text if cfg["chat"].get("always_reply") else strip_trigger(text, cfg)
    history.push("user", user_text, source="incoming")

    print("\n交给模型的消息（不含任何内部元数据）：")
    for m in history.build_messages():
        print(f"    {m['role']:<9} {clip(m['content'], 60)}")

    client = LLMClient(cfg)
    if not client.ready:
        print("\n[X] 无密钥，跳过模型调用。")
        return 2
    try:
        t0 = time.time()
        reply = client.chat(history.build_messages())
        print(f"\n模型回复（{time.time() - t0:.2f}s）：\n    {reply}")
    except Exception as exc:
        print(f"\n[X] 调用失败：{exc}")
        return 2
    return 0


def input_test(cfg: dict, text: str) -> int:
    """
    只验证「能不能把文字写进输入框」：写入 → 回读 → 清空，**绝不发送**。
    用来把「输入链路」和「发送动作」分开排查。
    """
    print("=" * 72)
    print("输入测试：写入输入框 → 回读 → 清空（绝不发送）")
    print("=" * 72)

    qq = QQWindow(cfg)
    if not qq.attach():
        print("[X] 找不到**可见**的 QQ 主窗口（请把目标会话窗口显示出来）。")
        return 2
    print(f"\n[窗口] title={qq.dialog_title!r}  群聊={qq.is_group}  群人数={qq.member_count}")
    if qq.editor is None:
        print("[X] 找不到输入框（ExEditor-qq-msg-editor）。")
        return 2
    print(f"[输入框] class={_cls(qq.editor)!r}")
    print(f"[前台]   QQ 在前台 = {qq.is_foreground()}  (QQ hwnd={qq.hwnd}, 当前前台 hwnd={_fg_hwnd()})")
    print(f"[发送按钮] 存在={qq.send_btn is not None}  写入前禁用={qq._send_disabled()}")

    print(f"\n[1] 写入文本：{text!r}")
    if not qq.type_text(text):
        print("[X] 写入失败 —— 常见原因：")
        print("    · 上面 [前台] 显示 False：进程没抢到前台权限。")
        print("      从你自己的终端启动即可（子进程才被允许抢前台），别用后台/服务方式启动。")
        print("    · QQ 窗口被别的窗口完全挡住了。")
        return 2
    time.sleep(0.4)

    got = qq.editor_text()
    print(f"[2] 回读输入框：{got!r}")
    print(f"    与写入一致 = {got == text}")

    print(f"[3] 写入后 发送按钮禁用 = {qq._send_disabled()}")
    if not qq.wait_send_enabled(1.0):
        print("    ⚠️ 发送按钮仍是禁用态 —— 文本可能没真的进输入框")

    # type_text 已经把焦点还给你了，这里要清空就得再借一次前台，清完立刻归还
    prev_fg = _fg_hwnd()
    cleared = False
    if force_foreground(qq.hwnd):
        cleared = qq.clear_editor()
        time.sleep(0.4)
        restore_foreground(prev_fg)
    print(f"[4] 清空输入框 = {cleared}，发送按钮禁用 = {qq._send_disabled()}")

    ok = got == text
    print("\n结论：输入链路 " + ("正常 ✅（以上全程未发送任何消息）" if ok else "异常 ❌"))
    return 0 if ok else 1


# ============================================================ entry
def peek(cfg: dict, limit: int = 12) -> int:
    """只读诊断：把 QQWindow 解析出来的消息原样打印，用来验证读取层是否正确。"""
    print("=" * 72)
    print("只读诊断：附着 QQ 并解析最近消息（不发送任何内容）")
    print("=" * 72)

    qq = QQWindow(cfg)
    if not qq.attach():
        print("[X] 找不到**可见**的 QQ 主窗口。")
        print("    注意：窗口缩在托盘里时 Chromium 不构建无障碍树，读到的是空壳。")
        print("    请把 QQ 主窗口显示在屏幕上并停在目标会话，再重试。")
        return 2

    print(f"\n[窗口] title={qq.dialog_title!r}  群聊={qq.is_group}  群人数={qq.member_count}")
    print(f"[账号] 自动识别的自己昵称={qq.self_nickname!r}")
    print(
        "[锚点] 消息列表={}  输入框={}  发送按钮={}  发送按钮禁用={}".format(
            "OK" if qq.ml_list is not None else "缺失",
            "OK" if qq.editor is not None else "缺失",
            "OK" if qq.send_btn is not None else "缺失",
            qq._send_disabled(),
        )
    )

    msgs = qq.read_messages(limit=limit)
    if not msgs:
        print("\n[X] 没读到任何消息条目。可能原因：")
        print("    - 当前没有打开任何会话（请点进一个聊天窗口）")
        print("    - QQ 未带 --force-renderer-accessibility 启动")
        return 1

    print(f"\n共解析 {len(msgs)} 条（文档顺序 = 时间顺序）：")
    print("（正文按原样完整打印，不做任何截断；末尾没有省略号就说明数据是完整的）\n")
    for m in msgs:
        mark = {"me": "我方", "other": "对方", "unknown": "未知"}.get(m.direction, m.direction)
        flags = [f"kind={m.kind}"]
        if m.ts:
            flags.append("时间=" + m.ts)
        print(f"  【{mark}】{m.sender or '(无昵称)'}  key={m.key}  {'  '.join(flags)}")
        print(f"        长度={len(m.content)}  正文：{m.content}")
        print()

    print("核对要点：")
    print("  1) 「我方/对方」是否与 QQ 界面上气泡的左右位置一致")
    print("  2) 昵称对不对、正文末尾是否完整（对照 QQ 里那一条读一遍）")
    print("  3) 最后一条是否就是 QQ 里最新那条")
    return 0


def send_once(cfg: dict, text: str, force: bool = False) -> int:
    """
    向当前会话发一条文本（唯一会真的产生副作用的模式）。
    默认拒绝在群聊里发，必须显式 --force —— 避免调试时误发到群里。
    """
    print("=" * 72)
    print(f"发送测试：{text!r}")
    print("=" * 72)

    qq = QQWindow(cfg)
    if not qq.attach():
        print("[X] 找不到**可见**的 QQ 主窗口（请把目标会话窗口显示出来）。")
        return 2

    print(f"\n[窗口] title={qq.dialog_title!r}  群聊={qq.is_group}  群人数={qq.member_count}")
    if qq.is_group and cfg["chat"].get("private_chat_only") and not force:
        print("\n[X] config 里 private_chat_only=true，已拒绝在群聊里发送。")
        print("    要放开：把 config.json 的 chat.private_chat_only 改成 false，或临时加 --force。")
        return 2

    ok = qq.send_text(text)
    print(f"\n[{'✓' if ok else 'X'}] send_text 返回 {ok}")
    if ok:
        # 回读一次，确认消息确实进了列表
        time.sleep(0.6)
        qq.refresh_layout(force=True)
        newest = qq.read_messages(limit=3)
        print("回读最近 3 条（确认是否已出现在列表里）：")
        for m in newest:
            mark = {"me": "我方", "other": "对方", "unknown": "未知"}.get(m.direction, m.direction)
            print(f"    【{mark}】{m.sender or '(无昵称)'}: {clip(m.content, 50)}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="PC 端 QQ 私聊 AI 代理（路线A 最小 demo）")
    ap.add_argument("--selftest", action="store_true", help="不开 QQ，自检配置与模型连通性")
    ap.add_argument("--replay", metavar="TEXT", help="不开 QQ，回放一条消息看模型会怎么回")
    ap.add_argument("--peek", type=int, nargs="?", const=12, default=None, metavar="N",
                    help="只读诊断：解析并打印最近 N 条消息（默认 12），不发送任何内容")
    ap.add_argument("--input-test", nargs="?", const="输入框测试文本 ABC123，不会发送",
                    default=None, metavar="TEXT",
                    help="只验证能不能把文字写进输入框：写入→回读→清空，绝不发送")
    ap.add_argument("--send", metavar="TEXT", help="向当前会话发一条文本（会真的发出去！）")
    ap.add_argument("--force", action="store_true", help="与 --send 配合，允许在群聊里发送")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出（调试用）")
    ap.add_argument("--dry-run", action="store_true", help="只读不发，确认读取是否准确")
    ap.add_argument("--config", default=CONFIG_PATH, help="配置文件路径")
    args = ap.parse_args()

    cfg = load_config()
    if args.config != CONFIG_PATH and os.path.isfile(args.config):
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = _deep_merge(DEFAULTS, json.load(f))

    if args.selftest:
        return selftest(cfg)
    if args.replay is not None:
        return replay(cfg, args.replay)
    if args.peek is not None:
        return peek(cfg, args.peek)
    if args.input_test is not None:
        return input_test(cfg, args.input_test)
    if args.send is not None:
        return send_once(cfg, args.send, force=args.force)

    agent = Agent(cfg, dry_run=args.dry_run)
    if args.once:
        if not agent.qq.attach():
            log("ERR", "找不到 QQ 窗口。")
            return 2
        kept = agent.qq.baseline(skip_last=1)
        log("INFO", f"已附着（{agent.qq.dialog_title!r}，群聊={agent.qq.is_group}）")
        log("INFO", f"忽略 {kept} 条历史，只把最新 1 条当作新消息")
        if not agent.dry_run:
            log("WARN", "未加 --dry-run，会真的回复并发送！建议先加 --dry-run")
        handled = agent.step()
        log("INFO", f"本轮处理 {handled} 条")
        print("\n最近 3 条解析结果：")
        for m in agent.qq.read_messages(limit=3):
            mark = {"me": "我方", "other": "对方", "unknown": "未知"}.get(m.direction, m.direction)
            print(f"    【{mark}】{m.sender or '?'}: {clip(m.content, 60)}")
        return 0
    return agent.run_forever()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[i] 已退出")
