# -*- coding: utf-8 -*-
"""
agent.py — PC 端 QQ 私聊 AI 代理（最小 demo · 路线 A）

做什么：
    附着到你正在运行的 QQ 客户端窗口上，按「UIA → OCR」的链条读取新消息，
    调用 OpenAI 兼容接口拿到回复，再用「剪贴板粘贴」的方式把回复打进输入框发出去。
    不碰任何协议、不碰任何硬件模拟，纯粹是「替人在操作这台电脑上的 QQ」。

范围（刻意收窄）：
    - 只处理纯文本（图片 / 语音 / 文件 / 表情一律跳过，继续悬置）
    - 私聊 / 群聊都支持（群聊可用触发词约束）
    - 已从 小清澈3.0.js 移植：防抖聚合、连续对话模式、上下文按会话持久化

用法：
    python agent.py --selftest          # 不开 QQ，自检配置 + 模型连通性 + 调教解析
    python agent.py --replay "在吗"      # 不开 QQ，跑一遍「收到消息 → 生成回复」的链路
    python agent.py --state             # 不开 QQ，查看已持久化的会话上下文
    python agent.py --forget "光みつる"   # 不开 QQ，清空某个会话的上下文
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

try:
    from reply_queue import ReplyQueue
except ImportError:
    print("[X] 缺少 reply_queue.py（回复排队与风控限速模块），请确认它与 agent.py 同目录")
    raise SystemExit(2)

# 错误码目录（与 app/ 控制台壳共用同一份）。
#
# 刻意做成「缺失也能跑」：这个文件是**诊断**用的，它自己绝不该成为新的启动失败点。
# 所以导入失败时给它一个最小替身 —— 报错路径退化，但功能照常。
try:
    import error_codes as EC
except ImportError:
    class _ECShim:
        """error_codes.py 缺失时的替身。只保留被调用到的那几个接口。"""

        class AppError(RuntimeError):
            def __init__(self, code: str, detail: str = "", context: dict | None = None):
                self.code = code
                self.detail_text = detail
                self.context = context or {}
                super().__init__(f"{code} {detail}")

        @staticmethod
        def severity(code: str) -> str:
            return "warn" if code.endswith("000") else "error"

        @staticmethod
        def describe(code: str, detail: str = "", context: dict | None = None) -> str:
            ctx = "  ".join(f"{k}={v}" for k, v in (context or {}).items())
            return f"{code} {detail}" + (f"  [{ctx}]" if ctx else "")

        @staticmethod
        def wrap(exc, default):
            return _ECShim.AppError(default, f"{type(exc).__name__}: {exc}")

        class Throttle:
            def __init__(self, window: float = 60.0):
                self.window = window

            def should_emit(self, code, now=None):
                return True, 0

    EC = _ECShim()

def _resolve_home() -> str:
    """
    数据根目录的解析顺序（顺序不能改，改了打包版会把状态写到临时解压目录里）：

        1. 环境变量 QQ_AGENT_HOME  —— 由 exe 壳指定，允许把数据挪到可写位置
        2. 冻结运行时：exe 所在目录 —— 打包后 `__file__` 指向 _MEIPASS 临时目录，
                                    写进去的文件重启就没了，必须换成 exe 旁边
        3. 源码运行时：本文件所在目录
    """
    env = (os.environ.get("QQ_AGENT_HOME") or "").strip()
    if env:
        return os.path.normpath(os.path.abspath(env))
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


HERE = _resolve_home()
CONFIG_PATH = os.path.join(HERE, "config.json")

# 心跳文件：exe 壳（WebUI）据此展示「常驻到第几轮、队列多长、当前在服务谁」。
# 没设这个环境变量时整个心跳机制是死的，不影响命令行直接使用。
HEARTBEAT_PATH = (os.environ.get("QQ_AGENT_HEARTBEAT") or "").strip()

# 停止哨兵：exe 壳（WebUI）要停掉常驻进程时，就创建这个文件。
# 为什么不发 Ctrl+C —— 子进程是用 CREATE_NO_WINDOW 起的，**没有控制台**，
# 控制台控制事件根本送不到，信号这条路是走不通的。用文件当协议反而最可靠。
STOP_PATH = (os.environ.get("QQ_AGENT_STOP") or "").strip()

auto.SetGlobalSearchTimeout(1.2)
if hasattr(auto, "SetGlobalSearchInterval"):
    auto.SetGlobalSearchInterval(0.25)


# ============================================================ 基础工具
def log(tag: str, msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {tag:<5} {msg}", flush=True)


def clip(text: str, n: int = 60) -> str:
    text = (text or "").replace("\n", "⏎")
    return text if len(text) <= n else text[: n - 1] + "…"


# 错误抑制：常驻循环每 ~0.8s 一轮，持续性故障会以每秒一条的速度刷屏，
# 几分钟就把日志埋掉 —— 连第一现场都找不回来。见 error_codes.Throttle 的说明。
THROTTLE = EC.Throttle(window=60.0)

# 最近一次报出的错误码。
# 用途：队列项需要能说明「自己为什么没发出去」——只留一个失败计数没有排查价值。
_LAST_CODE: dict = {"code": ""}


def report(code: str, detail: str = "", *, ctx: dict | None = None,
           force: bool = False) -> None:
    """
    带错误码的报错（带抑制）。

    `code` 是 `error_codes.CATALOG` 里的稳定编号。每个码**只对应一个根因** ——
    这是本文件报错的基本原则：一句「找不到 QQ 窗口」压在三种根因上时，
    人会按错误的动作去修（去重启 QQ，而实际问题只是窗口缩在托盘）。

    `ctx` 是现场数据（会话数、窗口类名、当前前台…），它决定了事后能不能复盘，
    所以关键分支都要填。
    """
    _LAST_CODE["code"] = code
    emit, suppressed = THROTTLE.should_emit(code)
    if not emit and not force:
        return

    sev = EC.severity(code)
    tag = {"error": "ERR", "warn": "WARN", "info": "INFO"}.get(sev, "ERR")
    if suppressed:
        log(tag, f"（同一问题在最近 60 秒内被抑制了 {suppressed} 次，下面是完整说明）")
    # 逐行输出：控制台的日志面板按行着色，整体塞进一条会让后续行丢掉级别
    for line in EC.describe(code, detail, ctx).splitlines():
        log(tag, line)


def report_exc(exc: BaseException, default_code: str, *, ctx: dict | None = None) -> str:
    """把异常映射成错误码并报出来，返回最终使用的码。"""
    err = EC.wrap(exc, default_code)
    report(err.code, err.detail_text, ctx=ctx)
    return err.code


def diag_line(code: str) -> str:
    """单行摘要，适合塞进本来就紧凑的输出里（例如自检表格）。"""
    return f"[{code}] {EC.describe(code).splitlines()[0].split(' ', 1)[-1]}"


# 心跳的累计状态。字段是**累加**的：只传变化的那几个即可。
_HB: dict = {}


def heartbeat(**fields) -> None:
    """
    把当前状态原子地写进 `QQ_AGENT_HEARTBEAT` 指向的文件（未设置则什么都不做）。

    用 `os.replace` 换文件而不是原地覆盖：WebUI 随时可能在读，边写边读会读到半截 JSON。
    """
    if not HEARTBEAT_PATH:
        return
    try:
        _HB.update(fields)
        payload = {"ts": time.time(), "pid": os.getpid()}
        payload.update(_HB)
        tmp = HEARTBEAT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, HEARTBEAT_PATH)
    except Exception:
        pass        # 心跳是纯观测，失败绝不能影响主循环


def set_phase(name: str, budget: float = 20.0, **extra) -> None:
    """
    在**每个可能慢的阶段入口**记下「现在卡在哪一步、这一步最长能有多久」。

    ## 为什么必须这样

    原来心跳只在整轮循环结束时写一次，于是它只能证明「循环还在转」，
    完全不能回答「卡在哪一步」。而循环里有几个阶段天生就慢：

        调模型        最长 = llm.timeout_seconds（默认 60s）
        切会话 + 发消息 1~3s（Invoke/抢前台/写剪贴板/按键，还带若干 sleep）
        扫会话列表     随会话数量线性增长
        重扫锚点       ≈100ms 起，节点多或虚拟机上更久

    **而且主循环是串行的** —— 模型调用期间既不读消息也不写心跳。
    实测后果：界面上的「心跳已停更」提示其实是**必然误报** ——
    只要一次模型调用超过 30s（阈值写死 30s），它就会出现一次。

    现在把阶段名与该阶段自己的上限一起写进心跳，于是：

        · WebUI 能直接显示「当前卡在：调模型（已 42s，上限 75s）」
        · stale 判定改用这个上限，而不是一个跟实际耗时毫无关系的固定值
        · 每次切阶段顺带记下**上一阶段的耗时**，慢在哪一步一目了然
    """
    now = time.time()
    upd: dict = {"phase": name, "phase_since": now, "stale_after": float(budget)}
    prev, since = _HB.get("phase"), _HB.get("phase_since")
    if prev and since:
        cost = now - since
        upd["prev_phase"] = prev
        upd["prev_phase_seconds"] = round(cost, 2)
        # 每个阶段的累计耗时/次数/最大单次 —— 这是「卡在哪一步」的决定性证据：
        # 只看「心跳停更」永远看不出来，而一张「谁最慢」的榜一眼就够。
        stats = dict(_HB.get("phase_stats") or {})
        total, count, worst = (stats.get(prev) or [0.0, 0, 0.0])
        stats[prev] = [round(total + cost, 1), count + 1, round(max(worst, cost), 2)]
        upd["phase_stats"] = stats
    upd.update(extra)
    heartbeat(**upd)


def stop_requested() -> bool:
    """
    外部要求优雅退出（见 `QQ_AGENT_STOP`）。

    走「文件当协议」而不是信号，是因为 exe 壳把常驻进程起成无控制台进程，
    CTRL_C / CTRL_BREAK 都送不进去。文件这条路不挑环境。
    """
    if not STOP_PATH:
        return False
    try:
        return os.path.isfile(STOP_PATH)
    except Exception:
        return False


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def abs_here(path: str) -> str:
    """把配置里的相对路径按「本脚本所在目录」解析，避免受当前工作目录影响。"""
    p = (path or "").strip()
    if not p:
        return ""
    return p if os.path.isabs(p) else os.path.normpath(os.path.join(HERE, p))


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
        # 相对路径按本脚本所在目录解析；该文件已被 .gitignore 排除
        "api_key_file": "secrets.local.json",
        "api_key_json_keys": ["llm.api_key", "deepseek.api_key", "openai.api_key",
                              "api_key", "DEEPSEEK_API_KEY"],
        "model": "deepseek-flash",
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
        # 粘贴之后等发送按钮从禁用态恢复的时间。
        # 别调回 1 秒：慢速虚拟机上 QQ 同步按钮状态经常超过 1 秒，
        # 那会让每一次都判成「文本没进输入框」→ 中止 + 清理，
        # 表现就是「它粘了字又回来删掉」这种毫无意义的动作。
        "send_button_wait_seconds": 3.0,
        # 发送中止后是否保留输入框里的草稿。
        # 默认 true（VM 场景）：草稿是**可恢复的进度**，队列会带着同一条回复继续重试；
        # 删掉它等于丢掉一条已生成的回复，那批消息可能因此永远没人回。
        "keep_draft_on_abort": True,
        "min_llm_interval_seconds": 2.0,
        "max_history_entries": 40,
        "send_with_ctrl_enter": False,
        "poll_interval_seconds": 0.8,
        "max_reply_chars": 500,
        # 多媒体（图片/语音/文件/动画表情）怎么处理：
        #   "skip"     = 一律跳过（默认）。最省模型额度，但「对方只发了个表情」时
        #                整条链路会走到「总读取没拿到新消息 → 撤单」，一声不吭。
        #   "describe" = 拿 QQ 气泡里的占位文本（如 `[动画表情]`）当正文继续走，
        #                让 AI 自己决定回不回。陪伴场景建议开。
        "nontext_policy": "skip",
    },
    # ---------------------------------------------------------------- 防抖聚合
    # 移植自 小清澈3.0.js 的 setupDebounceTimer：对方连发多条时合并成一次模型调用。
    # 等待时长 = 基础等待 + 该用户的「习惯性额外停顿」EMA + 冷启动惩罚。
    #
    # 基础等待是**自适应**的：
    #   默认 base_wait_ms(5s)；
    #   若同一会话连续 fast_after_single_rounds(3) 轮都只有单条消息提交给 AI，
    #   说明对方不是「连发型」，切到 fast_wait_ms(2s)；
    #   之后只要出现某轮 ≥2 条（又在连发），立刻回落 5s 并重新计数。
    "aggregate": {
        "enabled": True,
        "base_wait_ms": 5000,
        "fast_wait_ms": 2000,
        "fast_after_single_rounds": 3,
        "cold_start_penalty_ms": 6000,
        "decay_turns": 4,
        "cold_start_gap_ms": 60000,
        "habit_min_interval_ms": 500,
        "habit_max_interval_ms": 15000,
        "habit_ema_alpha": 0.3,
    },
    # -------------------------------------------------------------- 连续对话模式
    # 移植自 小清澈3.0.js 的 continuous 状态机：AI 在某个会话开口后，
    # 该会话在 timeout 秒内免触发词；超时自动退出。
    "continuous": {
        "enabled": True,
        "timeout_seconds": 1800,
    },
    # ---------------------------------------------------------------- 持久化
    # 上下文按会话（scope）隔离落盘，重启不丢。文件已被 .gitignore 排除。
    "persist": {
        "enabled": True,
        "file": "state/conversations.json",
        "save_interval_seconds": 3.0,
        "max_scopes": 50,
    },
    # ------------------------------------------------------------ 会话身份 / 取号
    # 主键用 QQ 号而不是昵称：昵称能改、也会重名，拿它当主键迟早串味。
    "identity": {
        "enabled": True,
        "store": "state/uid-map.json",
        "enroll_on_demand": True,
        "require_qq_uin": False,
    },
    # -------------------------------------------------------------- 排队 / 风控
    "queue": {
        "enabled": True,
        "max_replies_per_minute": 12,
        "min_interval_seconds": 5.0,
        "merge_if_wait_over_seconds": 5.0,
        "unit_cost_seconds": 3.0,
        "max_hold_seconds": 30.0,
        "jitter_seconds": 3.0,
        "max_attempts": 3,
    },
    # ------------------------------------------------------------------ 多会话发现
    # 桌面 UI 上每个会话只保留「最新一条消息的节选」，摘要不足以构造上下文，
    # 所以这里只做发现（扫未读徽标/摘要指纹 → 占位排队），
    # 真正的上下文总读取推迟到出队时（Agent.prepare）。
    "discovery": {
        "enabled": True,
        "scan_interval_seconds": 2.0,
        "trigger_on_unread": True,
        "trigger_on_preview_change": True,
        "trigger_on_first_sight_unread": True,
        "max_enqueue_per_scan": 3,
        "read_limit": 30,
        "state_file": "state/rotation.json",
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
        # 用完后把前台还给你原来的窗口。
        #
        # **默认已改为 false**（2026-09-12，VM 场景）：
        # 归还前台本身不产生正确性，却引入了一整类失败 —— 下次操作要重新抢，
        # 而抢前台会被系统拒绝（E-FG-001）、被别的窗口抢走（E-FG-002）。
        # 机器是专用的虚拟机时，让 QQ 一直留在前台反而**更可靠**：
        # 发送链路不再有抢前台的环节，重试也更容易成功。
        # 在你自己每天用的电脑上跑，就改回 true。
        "restore_foreground": False,
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
        except json.JSONDecodeError as exc:
            # 报出行列号 —— 「JSON 语法错」这句话本身没有可操作性，位置才有
            report("E-CFG-001", f"{CONFIG_PATH} 第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}",
                   ctx={"文件": CONFIG_PATH})
        except Exception as exc:
            report_exc(exc, "E-CFG-001", ctx={"文件": CONFIG_PATH})
    else:
        report("E-CFG-005", f"没有找到 {CONFIG_PATH}，全部使用内置默认值",
               ctx={"数据目录": HERE})
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
    raw_path = path
    # 相对路径**一律**按数据目录解析，绝不看当前工作目录。
    #
    # 这里原来写的是 `if path and not os.path.isabs(path) and not os.path.isfile(path)`——
    # 也就是说「CWD 下刚好存在同名文件」时会优先用 CWD 那个。那是个隐蔽的坑：
    # 从项目目录启动会读到项目里的密钥文件，从别处启动就读到别的（或读不到），
    # 表现为「同样的配置，换个目录结果就不一样」。这类依赖 CWD 的行为必须掐掉。
    if path and not os.path.isabs(path):
        path = abs_here(path)
    if not path or not os.path.isfile(path):
        if verbose:
            report("E-LLM-001",
                   f"api_key 为空，且 api_key_file 指向的文件不存在：{raw_path or '(未配置)'}",
                   ctx={"解析后路径": path or "(空)", "数据目录": HERE})
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
    except Exception as exc:
        if verbose:
            report_exc(exc, "E-PATH-002", ctx={"文件": path})
        return ""

    if path.lower().endswith(".json") or text.startswith("{"):
        try:
            data = json.loads(text)
        except Exception as exc:
            if verbose:
                report("E-PATH-003",
                       f"密钥文件不是合法 JSON：{path}（{exc}）",
                       ctx={"文件": path})
            return ""
        value, source = _pick_from_json(data, cfg["llm"].get("api_key_json_keys"))
        if verbose and value:
            log("INFO", f"密钥来源：{os.path.basename(path)} → {source}")
        elif verbose:
            report("E-LLM-001",
                   f"密钥文件里没找到可用字段：{path}",
                   ctx={"找过这些键": " / ".join(cfg['llm'].get('api_key_json_keys') or [])})
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
    """
    单个会话（scope）的对话历史。

    与插件 小清澈3.0.js 的 pushHistoryEntry 对齐的两点：
      1) 每条记录带单调递增的 seq —— 同一毫秒内到达的两条消息不会出现平局，
         而「谁先发生」恰恰是上下文因果顺序的判定依据；
      2) 调教条目是「立即落盘」的，若此时防抖缓冲里还压着更早的用户消息，
         那些用户消息要能回插到调教条目之前，否则会出现
         「AI 先说了这句话、用户才来提问」的因果颠倒。
    """

    def __init__(self, max_entries: int = 40, system_prompt: str = ""):
        self.max_entries = max_entries
        self.system_prompt = system_prompt
        self._items: list[dict] = []
        self._seq = 0

    # ---------------------------------------------------- 事件序号
    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @property
    def seq(self) -> int:
        return self._seq

    def set_seq(self, value: int) -> None:
        self._seq = max(self._seq, int(value or 0))

    # ---------------------------------------------------- 写入
    def _make(self, role: str, content: str, source: str, teach: bool,
              seq: Optional[int]) -> dict:
        return {
            "role": role,
            "content": content,
            "seq": int(seq) if seq else self.next_seq(),
            "source": source,
            "teach": bool(teach),
            "ts": time.time(),
        }

    def push(self, role: str, content: str, source: str = "",
             teach: bool = False, seq: Optional[int] = None) -> dict:
        entry = self._make(role, content, source, teach, seq)
        self._items.append(entry)
        self._trim()
        return entry

    def push_before_teach(self, before_seq: Optional[int], role: str, content: str,
                          source: str = "", teach: bool = False,
                          seq: Optional[int] = None) -> dict:
        """插到「seq 比 before_seq 晚、且带 teach 标记」的第一条之前（插件同款回插）。"""
        entry = self._make(role, content, source, teach, seq)
        idx = len(self._items)
        if before_seq:
            for i, it in enumerate(self._items):
                if it.get("teach") and int(it.get("seq") or 0) > int(before_seq):
                    idx = i
                    break
        self._items.insert(idx, entry)
        self._trim()
        return entry

    def _trim(self) -> None:
        # 只按条数裁剪，保证 system 不在列表里（system 单独走）
        if len(self._items) > self.max_entries:
            self._items = self._items[-self.max_entries:]

    # ---------------------------------------------------- 读取
    def build_messages(self) -> list[dict]:
        msgs = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        for it in self._items:
            # 只把 role/content 交给 API，seq/teach/source 属于本地元数据，不上行
            content = str(it.get("content") or "")
            if not it.get("role") or not content:
                continue
            msgs.append({"role": it["role"], "content": content})
        return msgs

    def __len__(self) -> int:
        return len(self._items)

    def teach_entries(self) -> list[dict]:
        return [it for it in self._items if it.get("teach")]

    def clear_teach(self) -> int:
        n = len(self.teach_entries())
        self._items = [it for it in self._items if not it.get("teach")]
        return n

    def drop_last_teach(self) -> Optional[str]:
        for i in range(len(self._items) - 1, -1, -1):
            if self._items[i].get("teach"):
                return self._items.pop(i).get("content")
        return None

    def clear(self) -> int:
        n = len(self._items)
        self._items = []
        return n

    def dump(self, n: int = 6) -> str:
        out = []
        for it in self._items[-n:]:
            tag = {"user": "对方", "assistant": "小清澈"}.get(it["role"], it["role"])
            if it.get("source"):
                tag = f"{tag}/{it['source']}"
            if it.get("teach"):
                tag += "·教"
            out.append(f"      #{it['seq']} {tag}: {clip(it.get('content') or '', 50)}")
        return "\n".join(out) or "      (空)"

    # ---------------------------------------------------- 序列化
    def to_dict(self) -> list[dict]:
        return [dict(it) for it in self._items]

    @classmethod
    def from_dict(cls, rows, max_entries: int, system_prompt: str) -> "History":
        h = cls(max_entries=max_entries, system_prompt=system_prompt)
        for r in rows or []:
            if not isinstance(r, dict):
                continue
            role = r.get("role")
            if role not in ("user", "assistant", "system"):
                continue
            h._items.append({
                "role": role,
                "content": str(r.get("content") or ""),
                "seq": int(r.get("seq") or 0),
                "source": str(r.get("source") or "restored"),
                "teach": bool(r.get("teach")),
                "ts": float(r.get("ts") or 0.0),
            })
            h.set_seq(int(r.get("seq") or 0))
        h._trim()
        return h


# ============================================================ 会话仓库（按 scope 隔离 + 持久化）
class ConversationStore:
    """
    按会话隔离的上下文仓库（对齐插件「一个 scope 一份历史 + 一份连续对话状态」的做法）。

    scope 命名与插件一致：
        私聊 → `private:<对方昵称>`      群聊 → `group:<群名>`
    PC 端拿不到 QQ 号，只能拿窗口标题当身份 —— 改昵称 / 改群名就等于换了一个 scope。

    落盘策略：内存里改，写盘做节流（默认 3s 最多一次），原子替换（先写 .tmp 再 rename），
    退出时强制写一次。文件损坏时自动备份原文件后从空开始，不会把程序带崩。
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        p = cfg["persist"]
        self.enabled = bool(p.get("enabled", True))
        self.interval = float(p.get("save_interval_seconds") or 3.0)
        self.max_scopes = max(1, int(p.get("max_scopes") or 50))
        self.path = abs_here(p.get("file") or "state/conversations.json")
        self.system_prompt = cfg["llm"].get("system_prompt") or ""
        self.max_entries = int(cfg["chat"].get("max_history_entries") or 40)
        self._book: dict[str, dict] = {}
        self._dirty = False
        self._last_save = 0.0
        self.load()

    # ---------------------------------------------------- 载入 / 落盘
    def load(self) -> None:
        if not self.enabled or not self.path or not os.path.isfile(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            bad = self.path + ".bad"
            try:
                os.replace(self.path, bad)
                report("E-PATH-003", "上下文文件损坏，已备份后重建（历史会丢）",
                       ctx={"损坏文件": self.path,
                            "备份为": os.path.basename(bad),
                            "原因": f"{type(exc).__name__}: {exc}"})
            except Exception as exc2:
                report("E-PATH-003", "上下文文件损坏且备份失败（历史会丢，且坏文件还在）",
                       ctx={"文件": self.path,
                            "损坏原因": f"{type(exc).__name__}: {exc}",
                            "备份失败": f"{type(exc2).__name__}: {exc2}"})
            return

        for scope, row in (data.get("scopes") or {}).items():
            if not isinstance(row, dict):
                continue
            cont = row.get("continuous") or {}
            hist = History.from_dict(row.get("history") or [], self.max_entries, self.system_prompt)
            hist.set_seq(int(row.get("seq") or 0))
            self._book[str(scope)] = {
                "history": hist,
                "cont": {"active": bool(cont.get("active")),
                         "last_at": float(cont.get("last_at") or 0.0)},
                "used_at": float(row.get("used_at") or 0.0),
            }
        if self._book:
            log("INFO", f"已载入 {len(self._book)} 个会话的上下文（{self.path}）")

    def save(self, force: bool = False) -> None:
        if not self.enabled or not self.path or not self._dirty:
            return
        now = time.time()
        if not force and now - self._last_save < self.interval:
            return
        self._last_save = now
        self._dirty = False

        data = {
            "_说明": "pc-agent-demo 自动生成：按会话隔离的上下文与连续对话状态。"
                     "删除本文件 = 让所有会话失忆，不影响其它配置。",
            "_version": 1,
            "scopes": {},
        }
        for scope, row in self._book.items():
            data["scopes"][scope] = {
                "used_at": row["used_at"],
                "seq": row["history"].seq,
                "continuous": {"active": row["cont"]["active"],
                               "last_at": row["cont"]["last_at"]},
                "history": row["history"].to_dict(),
            }
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:
            report_exc(exc, "E-PATH-003",
                       ctx={"阶段": "上下文落盘", "文件": self.path,
                            "后果": "本次没写成功，会保留脏标记下次重试；运行不受影响"})
            self._dirty = True

    # ---------------------------------------------------- 取用
    def book(self, scope: str) -> dict:
        row = self._book.get(scope)
        if row is None:
            row = {
                "history": History(self.max_entries, self.system_prompt),
                "cont": {"active": False, "last_at": 0.0},
                "used_at": time.time(),
            }
            self._book[scope] = row
            self._prune()
        row["used_at"] = time.time()
        self._dirty = True
        return row

    def history(self, scope: str) -> History:
        return self.book(scope)["history"]

    def scopes(self) -> list[str]:
        return sorted(self._book, key=lambda s: -self._book[s]["used_at"])

    def forget(self, scope: str) -> bool:
        if scope not in self._book:
            return False
        del self._book[scope]
        self._dirty = True
        self.save(force=True)
        return True

    def _prune(self) -> None:
        # 刚取用的 scope 是 used_at 最大的，天然不会被淘汰
        while len(self._book) > self.max_scopes:
            victim = min(self._book, key=lambda s: self._book[s]["used_at"])
            log("INFO", f"会话数超上限（{self.max_scopes}），淘汰最久未用的：{victim}")
            del self._book[victim]

    # ---------------------------------------------------- 连续对话状态机
    def is_continuous(self, scope: str, now: float, timeout: float) -> bool:
        row = self._book.get(scope)
        if not row or not row["cont"]["active"]:
            return False
        if now - row["cont"]["last_at"] <= timeout:
            return True
        row["cont"].update(active=False, last_at=0.0)   # 超时 → 自动退出
        self._dirty = True
        return False

    def activate_continuous(self, scope: str, now: float) -> None:
        row = self.book(scope)
        row["cont"].update(active=True, last_at=float(now))
        self._dirty = True

    def refresh_continuous(self, scope: str, now: float) -> bool:
        """已激活则续期，避免「从第一条消息起算固定窗口」聊到一半被强制断开。"""
        row = self._book.get(scope)
        if not row or not row["cont"]["active"]:
            return False
        row["cont"]["last_at"] = float(now)
        self._dirty = True
        return True

    def deactivate_continuous(self, scope: str) -> None:
        row = self._book.get(scope)
        if not row:
            return
        row["cont"].update(active=False, last_at=0.0)
        self._dirty = True


# ============================================================ 防抖聚合
@dataclass
class PendingMessage:
    """已通过准入判定、等待聚合结算的一条消息。"""
    sender: str
    content: str
    key: str
    seq: int


class Debouncer:
    """
    自适应防抖聚合（逐条等价移植自 小清澈3.0.js 的 setupDebounceTimer）。

    插件原逻辑：
        等待 = BASE_WAIT + 该用户的「习惯性额外停顿」EMA + 冷启动惩罚
        习惯性停顿：两条消息间隔在 0.5s~15s 之间时，把 (间隔 - 基础等待) 用 EMA(0.7/0.3) 累积
        冷启动惩罚：距上次发言超过 60s，则按「已连续轮数」递减，最多 4 轮衰减到 0
    基础等待原为固定 10s，这里改成两档自适应（见 note_round / current_base）。

    差别只有一点：插件用 setTimeout 定时器，这里改成「到期时间戳」，
    由同步轮询循环检查是否到点 —— 不引入线程，和现有结构最贴合。
    """

    def __init__(self, cfg: dict):
        a = cfg["aggregate"]
        self.enabled = bool(a.get("enabled", True))
        self.base = float(a.get("base_wait_ms") or 5000)
        self.fast_wait = float(a.get("fast_wait_ms") or 2000)
        self.fast_after = max(1, int(a.get("fast_after_single_rounds") or 3))
        self.cold_penalty = float(a.get("cold_start_penalty_ms") or 6000)
        self.decay = max(1, int(a.get("decay_turns") or 4))
        self.cold_gap = float(a.get("cold_start_gap_ms") or 60000)
        self.h_min = float(a.get("habit_min_interval_ms") or 500)
        self.h_max = float(a.get("habit_max_interval_ms") or 15000)
        self.alpha = float(a.get("habit_ema_alpha") or 0.3)
        self.pending: list[PendingMessage] = []
        self.due_at = 0.0
        self._habits: dict[str, dict] = {}
        # scope -> {"singles": 连续「单条成一轮」的计数, "fast": 是否已切到快档}
        # 只存内存：重启后回到 5s，最多再观察 3 轮就重新判定。
        self._rounds: dict[str, dict] = {}

    # ---------------------------------------------------- 两档自适应
    def _round_state(self, scope: str) -> dict:
        return self._rounds.setdefault(scope or "", {"singles": 0, "fast": False})

    def current_base(self, scope: str) -> float:
        """当前生效的基础等待（毫秒）。"""
        return self.fast_wait if self._round_state(scope)["fast"] else self.base

    def note_round(self, scope: str, count: int) -> None:
        """
        每结算一轮就记一笔：这一轮实际提交给 AI 的是几条消息。
        连续 fast_after 轮都是「单条」→ 切快档；出现一轮多条 → 回落并重新计数。
        """
        if not self.enabled:
            return
        st = self._round_state(scope)
        if count <= 1:
            st["singles"] += 1
            if st["singles"] >= self.fast_after and not st["fast"]:
                st["fast"] = True
                log("AGG", f"连续 {st['singles']} 轮都是单条消息 → 基础等待降到 "
                           f"{self.fast_wait / 1000:.1f}s")
        else:
            if st["fast"]:
                log("AGG", f"本轮是 {count} 条连发 → 基础等待回到 {self.base / 1000:.1f}s")
            st["singles"] = 0
            st["fast"] = False

    # ---------------------------------------------------- 计时
    def add(self, msg: PendingMessage, scope: str = "") -> float:
        """收下一条消息并顺延到期时间，返回本次的等待秒数。"""
        self.pending.append(msg)
        if not self.enabled:
            self.due_at = 0.0
            return 0.0
        wait_ms = self._compute_wait(msg.sender, scope)
        self.due_at = time.time() + wait_ms / 1000.0
        return wait_ms / 1000.0

    def _compute_wait(self, sender: str, scope: str = "") -> float:
        key = sender or ""
        base = self.current_base(scope)
        now_ms = time.time() * 1000.0
        h = self._habits.setdefault(key, {"last_ms": now_ms - 120000.0, "extra": 0.0, "turns": 0})

        interval = now_ms - h["last_ms"]
        h["last_ms"] = now_ms

        cold = 0.0
        if interval > self.cold_gap:
            h["turns"] = min(h["turns"] + 1, self.decay)
            cold = self.cold_penalty * (1 - h["turns"] / self.decay)
        else:
            h["turns"] = 0

        if self.h_min < interval < self.h_max:
            extra = max(0.0, interval - base)
            h["extra"] = h["extra"] * (1 - self.alpha) + extra * self.alpha

        return round(base + h["extra"] + cold)

    def summary(self, scope: str = "") -> str:
        st = self._round_state(scope)
        return (f"{self.current_base(scope) / 1000:.1f}s"
                f"（{'快档' if st['fast'] else '常规'}，"
                f"单条轮次 {min(st['singles'], self.fast_after)}/{self.fast_after}）")

    @property
    def ready(self) -> bool:
        return bool(self.pending) and time.time() >= self.due_at

    def take(self) -> list[PendingMessage]:
        out = self.pending
        self.pending = []
        self.due_at = 0.0
        return out

    def clear(self) -> None:
        self.pending = []
        self.due_at = 0.0

    def countdown(self) -> float:
        return max(0.0, self.due_at - time.time())


# ============================================================ 发现 / 轮转状态
class RotationState:
    """
    多会话「发现」所需的持久化状态。

        baselined    : 已经建过基线的 scope 集合（每个会话只建一次）
        fingerprints : 会话列表展示名 -> 上次看到的指纹（预览文本元组, 未读数）

    **为什么必须落盘**：

      - `baselined` 丢了 → 重启后每个会话都会重新建一次基线，
        重启那一刻积压的未读全被当历史吞掉；
      - `fingerprints` 丢了 → 重启后第一次扫描没有「变化前的样子」可对比，
        只能整批按首见处理（按设计不动作），于是重启瞬间的未读
        要等到**下一次**变化才被发现 —— 用户会觉得「刚重启那会儿它聋了」。
    """

    def __init__(self, path: str):
        self.path = path
        self.baselined: set = set()
        self.fingerprints: dict = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self.baselined = set(raw.get("baselined") or [])
            fp = {}
            for name, v in (raw.get("fingerprints") or {}).items():
                try:
                    fp[name] = (tuple(v[0]), int(v[1]))
                except Exception:
                    continue
            self.fingerprints = fp
        except FileNotFoundError:
            pass
        except Exception as exc:
            report_exc(exc, "E-PATH-003", ctx={"文件": self.path, "处理": "按空状态继续（会重建基线）"})

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            payload = {
                "version": 1,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "baselined": sorted(self.baselined),
                "fingerprints": {k: [list(v[0]), int(v[1])]
                                 for k, v in self.fingerprints.items()},
            }
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:
            report_exc(exc, "E-PATH-003", ctx={"文件": self.path, "后果": "重启后可能重复建基线并吞掉那一瞬间的未读"})

    def is_baselined(self, scope: str) -> bool:
        return scope in self.baselined

    def mark_baselined(self, scope: str) -> None:
        self.baselined.add(scope)

    def fp_of(self, key: str):
        return self.fingerprints.get(key)

    def set_fp(self, key: str, fp) -> None:
        self.fingerprints[key] = fp

    def forget(self, scope: str, display_name: str = "") -> None:
        self.baselined.discard(scope)
        if display_name:
            self.fingerprints.pop(display_name, None)


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
        """
        调一次模型。失败时抛带错误码的 `AppError`。

        ## 为什么要按状态码分类

        原来这里是 `if resp.status_code != 200: raise RuntimeError(f"HTTP {code}: {text}")`，
        于是 401（密钥错）、404（模型名错）、429（限流）、500（服务端挂了）
        在日志里长成一个样子 —— 而这四件事的**处理动作完全不同**：
        改密钥 / 改模型名 / 等一会儿 / 什么都不用做。

        分类之后，界面和日志都能直接告诉你该动哪里。
        """
        if not self.api_key:
            raise EC.AppError("E-LLM-001",
                              "config.json 的 llm.api_key 为空，且 api_key_file 里也没找到可用密钥",
                              {"api_base": self.url, "model": self.model})
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

        try:
            resp = requests.post(self.url, headers=headers, json=payload, timeout=self.timeout)
        except requests.exceptions.ConnectTimeout as exc:
            raise EC.AppError("E-LLM-003", f"连接阶段超时（{self.timeout}s）：{exc}",
                              {"url": self.url}) from exc
        except requests.exceptions.ReadTimeout as exc:
            raise EC.AppError("E-LLM-003", f"等待响应超时（{self.timeout}s）：{exc}",
                              {"url": self.url, "max_tokens": payload["max_tokens"]}) from exc
        except requests.exceptions.SSLError as exc:
            raise EC.AppError("E-LLM-002", f"TLS 握手失败：{exc}", {"url": self.url}) from exc
        except requests.exceptions.ProxyError as exc:
            raise EC.AppError("E-LLM-002", f"代理异常：{exc}", {"url": self.url}) from exc
        except requests.exceptions.ConnectionError as exc:
            raise EC.AppError("E-LLM-002", f"连接失败（DNS 或网络不通）：{exc}",
                              {"url": self.url}) from exc
        except requests.exceptions.Timeout as exc:
            raise EC.AppError("E-LLM-003", f"请求超时：{exc}", {"url": self.url}) from exc
        except Exception as exc:
            raise EC.wrap(exc, "E-LLM-002") from exc

        if resp.status_code != 200:
            snippet = resp.text[:300]
            ctx = {"HTTP": resp.status_code, "url": self.url, "model": self.model}
            if resp.status_code in (401, 403):
                raise EC.AppError("E-LLM-004",
                                  f"服务端拒绝了这次鉴权：{snippet}", ctx)
            if resp.status_code == 404:
                raise EC.AppError("E-LLM-005",
                                  f"接口或模型不存在：{snippet}", ctx)
            if resp.status_code == 429:
                raise EC.AppError("E-LLM-006",
                                  f"被限流或额度用尽：{snippet}", ctx)
            if 500 <= resp.status_code < 600:
                raise EC.AppError("E-LLM-007",
                                  f"服务端错误：{snippet}", ctx)
            # 其余 4xx：多半是请求体不合法（模型名、参数越界），归到地址/模型那一类最好排查
            raise EC.AppError("E-LLM-005",
                              f"HTTP {resp.status_code}（请求被拒）：{snippet}", ctx)

        try:
            data = resp.json()
        except Exception as exc:
            raise EC.AppError("E-LLM-008",
                              f"响应不是合法 JSON：{resp.text[:200]}") from exc

        choices = data.get("choices") or []
        if not choices:
            err = (data.get("error") or {})
            raise EC.AppError("E-LLM-008",
                              f"响应里没有 choices：{str(data)[:300]}",
                              {"error.message": err.get("message", "") if isinstance(err, dict) else ""})
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


def _foreground_title() -> str:
    """当前前台窗口的标题（诊断用）。"""
    try:
        hwnd = _fg_hwnd()
        if not hwnd:
            return "(无)"
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        n = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 2)
        u32.GetWindowTextW(hwnd, buf, n + 2)
        return buf.value or "(空标题)"
    except Exception:
        return "(读取失败)"


def _desktop_locked() -> bool:
    """
    桌面是不是锁着（或屏保挡着）。

    为什么要单独判一下：锁屏时 **UIA 读取照常工作**（读无障碍树不需要前台），
    但 `Click()` 走的是坐标 + 鼠标事件，点不到被锁屏盖住的 QQ。
    症状是「发送前复核一连失败好几次」，很容易被误判成「三道闸太严」。
    实际上闸是对的 —— 是环境点不动。实测就踩过这个：
    锁屏下 test_send_guard 会 6 项全挂，但 `--sessions` 只读诊断完全正常。
    """
    try:
        hwnd = _fg_hwnd()
        if not hwnd:
            return True
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        buf = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, buf, 256)
        return buf.value == "Windows.UI.Core.CoreWindow"
    except Exception:
        return False


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

    ## 实现说明：直接复用 `force_foreground`（2026-09-11 实测后合并）

    这里原来有**另一份**几乎一样的实现，唯一的差别是它 `AttachThreadInput` 到
    **目标窗口的线程**，而 `force_foreground` 附加到**当前前台窗口的线程**。
    这个差别不是小事：

    | 场景 | 旧 restore_foreground | force_foreground |
    | --- | --- | --- |
    | 坐标点击切换后归还 | ✅ 成功 | ✅ 成功 |
    | `InvokePattern` 切换后归还（QQ 是自己抢的前台） | ❌ **等 4s、重试 5 次仍失败** | ✅ **一次成功** |

    原因是「前台归属权」握在**当前前台窗口**的那个线程手里，要改它就得先和它握手；
    去和目标窗口握手是没有用的。所以统一走 `force_foreground`，
    只额外保留「别对已关闭的窗口操作」这一层保护。
    """
    if not prev_hwnd:
        return False
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.IsWindow.argtypes = [wintypes.HWND]
    u32.IsWindow.restype = wintypes.BOOL
    if not u32.IsWindow(int(prev_hwnd)):
        return False          # 原窗口已被关掉，没什么可还的
    return force_foreground(prev_hwnd, retries=retries)


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
        # scope -> OrderedDict(msg_key -> True)
        # **每个会话各一份**，不能共用。UID 路线下窗口要在多个会话之间切来切去，
        # 共用一个集合时，「刚切过去的那个会话的历史消息」会被别人的 key 挤掉，
        # 反过来又可能把别的会话的 key 当成自己已读 —— 两个方向都是错的。
        self._seen: dict[str, collections.OrderedDict] = {}
        self._seen_max = 400        # 每个会话各自保留的条数上限
        # 「切会话」之前用户的前台窗口。切会话是坐标点击，必须先把 QQ 抬到最上层，
        # 于是用户原来的窗口就被我们顶掉了 —— 存下来交给输入路径用完即还。
        # 不存的话 type_text 只会把前台「还」给 QQ 自己（它那时看到的前台就是 QQ）。
        self.fg_before_switch: int = 0
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

    def _qq_top_windows(self) -> list[dict]:
        """
        属于 QQ 的全部顶层窗口（含隐藏的）。

        判定规则**必须与 `attach()` 完全一致**，否则诊断给出的结论会和实际行为对不上
        —— 那种"诊断说没问题但就是跑不起来"的情况比没有诊断更糟。
        """
        out = []
        for row in _enum_top_windows():
            cls = row["class"]
            if cls not in ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0", "TXGuiFoundation"):
                continue
            p = process_path(row["pid"]).lower()
            if "qq.exe" not in p and "qqnt" not in p and cls != "TXGuiFoundation":
                continue
            out.append(row)
        return out

    def diagnose_attach(self) -> tuple[str, dict]:
        """
        `attach()` 失败之后调用，判定**到底卡在哪一步**。

        ## 为什么必须拆开

        原来这里只有一句话：

            找不到 QQ 窗口。请确认 QQ 已启动，且带 --force-renderer-accessibility 参数。

        它同时压在三个根因上，而三者的处理动作完全不同：

        | 真实根因 | 正确动作 | 按那句话去做的后果 |
        | --- | --- | --- |
        | QQ 根本没启动 | 启动 QQ | 没损失 |
        | QQ 在跑，窗口缩在托盘/最小化 | 把窗口显示出来 | **白重启一次，正在输入的文字会丢** |
        | 窗口可见但无障碍树是空壳 | 完全退出后带参重启 | （另一种情况会去白重启） |

        判据用的是**窗口层面的客观事实**（有几个 QQ 顶层窗口、几个可见），
        不是猜。
        """
        wins = _enum_top_windows()
        qq_wins = self._qq_top_windows()
        visible = [w for w in qq_wins if w["visible"]]
        ctx = {
            "顶层窗口": len(wins),
            "QQ窗口": len(qq_wins),
            "可见": len(visible),
            "类名": ",".join(sorted({w["class"] for w in qq_wins})) or "(无)",
            "前台": _foreground_title() or "(未知)",
        }
        if not qq_wins:
            # 连一个顶层窗口都没有 → QQ 进程没跑（或只有纯后台进程）
            return "E-QQ-002", ctx
        if not visible:
            # 有窗口但全不可见 → 缩在托盘 / 被最小化
            return "E-QQ-003", {**ctx, "全部隐藏": "是（托盘或最小化）"}
        # 有可见窗口却挑不出主窗口：控件转换失败，多为权限或位数不一致
        return "E-QQ-003", {**ctx, "说明": "有可见窗口但无法转成控件，可能是权限或位宽不一致"}

    def dom_exposed(self) -> bool:
        """
        DOM 有没有真的暴露出来（= 无障碍参数有没有生效）。

        判据：**能不能找到输入框或消息列表锚点**。

        这是与「找不到窗口」完全不同的一件事，必须分开报：窗口找到得很顺利、
        但里面什么都没有，特征就是 `--force-renderer-accessibility` 没生效。
        以前这两种情况混在一起，导致「明明是参数问题，却让人去检查 QQ 有没有启动」。
        """
        if self.win is None:
            return False
        self.refresh_layout(force=True)
        return self.ml_list is not None or self.editor is not None

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
    def _seen_of(self, scope: str) -> collections.OrderedDict:
        return self._seen.setdefault(scope or "", collections.OrderedDict())

    def mark_seen(self, msgs: list[Message], scope: str = "") -> None:
        d = self._seen_of(scope)
        for m in msgs:
            d[m.key] = True
        while len(d) > self._seen_max:
            d.popitem(last=False)

    def split_new(self, msgs: list[Message], scope: str = "") -> list[Message]:
        d = self._seen_of(scope)
        fresh = [m for m in msgs if m.key not in d]
        self.mark_seen(msgs, scope)
        return fresh

    def forget_scope(self, scope: str) -> None:
        """丢掉某个会话的已读集合（调试 / 换账号时用）。"""
        self._seen.pop(scope or "", None)

    def has_seen(self, scope: str) -> bool:
        """
        **本进程**有没有给这个会话建过已读集合。

        ⚠️ 判断「要不要建基线」必须用这个，不能用落盘的 `RotationState.baselined`：
        `_seen` 只活在内存里，重启就空了，而 `baselined` 是落盘的。
        早期版本拿落盘标记当判据，于是重启后第一次访问某个老会话时，
        因为「标记说建过基线」而跳过建基线 —— 结果是内存里空的已读集合
        把最近 30 条历史全判成新消息，一股脑灌进上下文并触发回复。
        """
        return (scope or "") in self._seen

    def baseline(self, skip_last: int = 0, scope: str = "") -> int:
        """
        把已有消息标记为已读，避免启动时对着历史记录刷屏。
        skip_last=N 时保留最后 N 条不标记，留给本次处理（调试用）。

        ⚠️ **只应该在一个会话「第一次被打开」时调用**（见 Agent._sync_scope）。
        以前每切一次会话都调，于是「我不在的那段时间到达的消息」在切回去的瞬间
        就被记成已读、静默丢弃 —— 这就是并发场景下的 D5 缺陷。
        """
        msgs = self.read_messages(limit=30)
        target = msgs[: len(msgs) - skip_last] if skip_last > 0 else msgs
        self.mark_seen(target, scope)
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

    def editor_contains(self, text: str) -> bool:
        """
        输入框里现在是不是就是这句话。

        这是「回复已经写进去、只差按发送」的判据 —— 也就是**可恢复的进度**。
        VM 场景下它决定了两件事：重试时要不要重新粘贴、以及这条回复还算不算数。
        """
        want = (text or "").strip()
        if not want:
            return False
        got = self.editor_text().strip()
        return bool(got) and want in got

    # ---------------------------------------------------- 会话身份 / 发送前复核
    def message_ids(self, limit: int = 8) -> list[str]:
        """
        消息区最后 N 条消息的 AutomationId。

        实测（probe-tree.txt:187 起）：`ml-item` 的 AutomationId 是 18~19 位的消息 ID，
        例如 `7684140137907339382` —— **全局唯一**，所以「一组消息 ID」天然是
        「当前打开的是哪个会话」的强指纹：两个不同会话不可能有相同的可见消息集合。
        """
        if self.ml_list is None:
            self.refresh_layout(force=True)
        if self.ml_list is None:
            return []
        items = [c for c in _kids(self.ml_list) if _has_cls(c, CLS_ML_ITEM)]
        if not items:
            return []
        return [_aid(c) for c in items][-limit:]

    def title_now(self) -> str:
        """重新扫树只为拿会话标题（比全量 refresh_layout 便宜，且不受 5s 缓存影响）。"""
        if self.win is None:
            return ""
        for ctrl, _d in iter_bfs(self.win, SCAN_MAX_DEPTH, limit=4000):
            if CLS_CHAT_TITLE in _cls(ctrl) and _visible(ctrl):
                return _name(ctrl).strip()
        return ""

    def chat_signature(self, force: bool = True) -> tuple:
        """
        当前打开会话的签名 = (标题, 是否群聊, 最后 6 条消息 ID)。

        发送前复核用它。为什么必须带消息 ID：
        - 标题只等于「对方给我的备注名」，重名/改备注都可能骗过它；
        - 消息 ID 是全局唯一的，切换会话 → 消息集合必然改变，骗不过；
        - 它还顺带解决了「切了但没渲染完」——渲染没完成时 ID 列表也对不上。

        `force=True` 会强制重扫锚点（≈103ms）。这一步不能省：
        切换会话后 `self.ml_list` 可能还指向旧容器，用陈旧引用读出来的 ID
        恰好等于切换前的值，复核就会假通过 —— 那正是「发错人」。
        """
        if force:
            self.refresh_layout(force=True)
        return (self.title_now(), bool(self.is_group), tuple(self.message_ids(6)))

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
            report("E-FG-003", "归还前台失败，QQ 会继续留在最上层",
                   ctx={"目标窗口": prev_fg, "当前前台": _foreground_title()})

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
            report("E-UIA-004", "找不到输入框（ExEditor-qq-msg-editor）",
                   ctx={"消息列表": self.ml_list is not None,
                        "窗口可见": _visible(self.win) if self.win else False,
                        "标题": self.dialog_title or "(无)"})
            return False

        # 归还目标优先用「切会话时记下的那个用户窗口」。
        # 如果我们是被 serve_queue 叫起来的，此刻的前台**已经是 QQ 自己**
        # （切会话是坐标点击，必须先顶上去），直接读 _fg_hwnd() 会把前台
        # 「还」给 QQ，用户原来的窗口就再也回不来了 —— 方案 C 会静默失效。
        prev_fg = self.fg_before_switch or _fg_hwnd()
        if not force_foreground(self.hwnd):
            report("E-FG-001", "无法把 QQ 窗口切到前台，已放弃输入（防止按键漏到其它窗口）",
                   ctx={"锁屏": _desktop_locked(),
                        "当前前台": _foreground_title(),
                        "窗口最小化": _u32.IsIconic(self.hwnd) if self.hwnd else None})
            return False

        try:
            try:
                self.editor.SetFocus()      # 只设焦点，不发鼠标点击
            except Exception:
                pass
            time.sleep(0.05)
            if not self.is_foreground():
                report("E-FG-002", "聚焦后 QQ 仍不在前台，放弃输入",
                       ctx={"当前前台": _foreground_title()})
                return False

            # ⚠️ 顺序很重要：**先把剪贴板写好，再清空输入框**。
            #
            # 原来的顺序是反的（先 clear_editor 再 copy_to_clipboard），代价是真实的：
            # `clear_editor()` 靠 Ctrl+A / Delete，会**立刻毁掉输入框里原有的内容**；
            # 如果紧接着剪贴板这一步失败，我们就把上一轮（甚至用户正在打的）草稿白毁了。
            # 写剪贴板是纯内存操作、没有任何副作用，所以能提前就提前 ——
            # 「先做没有副作用的检查，再做有副作用的动作」。
            if not copy_to_clipboard(text):
                report("E-SEND-001", f"写入剪贴板失败，无法输入中文（{len(text)} 字）",
                       ctx={"pyperclip": "已装" if pyperclip else "未安装"})
                return False
            self.clear_editor()
            time.sleep(0.05)
            auto.SendKeys("{Ctrl}v", waitTime=0.05)
            return True
        finally:
            self._release_foreground(prev_fg)   # 无论成功失败，都把焦点还回去
            self.fg_before_switch = 0           # 用掉了，下次由新的切会话重新记

    def wait_send_enabled(self, timeout: float | None = None) -> bool:
        """
        等发送按钮从禁用态恢复。

        这同时是「文本是否真的进了输入框」的一个信号 —— 但**不是唯一信号**，
        下面 `send_text` 里的回读校验更直接。

        ## 超时为什么不能写死 1 秒

        粘贴之后，QQ 需要把输入框的状态同步回发送按钮的禁用态。
        在**慢速虚拟机**上这一步经常超过 1 秒（CPU 被抢、渲染被节流），
        于是会出现：粘贴成功 → 按钮尚未恢复 → 判定失败 → 中止 + 清理。
        用户看到的就是「它粘了一段字，又回来把它删了」这种莫名其妙的动作。

        所以改成可配（`chat.send_button_wait_seconds`，默认 3 秒）。
        """
        if timeout is None:
            timeout = float(self.cfg["chat"].get("send_button_wait_seconds") or 3.0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._send_disabled():
                return True
            time.sleep(0.05)
        return False

    # ---------------------------------------------------- 发送
    def send_text(self, text: str, guard=None, resume: bool = False) -> bool:
        """
        写 + 发。`guard` 是**发出去之前的最后一道闸**（可调用对象，返回 bool）。

        时序（三次校验，见 `并发能力评估与优化方向.md` D1）：
            type_text          ← 抢前台 385ms，写完后把前台还回去
            wait_send_enabled  ← 免前台
            回读输入框          ← 免前台，确认写进去的确实是我们的话
            guard()            ← 免前台，**复核「现在还是那个会话」**  ← 关键
            Invoke 发送按钮     ← 免前台

        ## `resume=True`：原地重发，不重新粘贴

        表示「上一轮已经把这句话粘进输入框了，只是没按发送」。此时若输入框里**仍然
        是那句话**，就跳过 `type_text`，直接走校验 + 发送。

        为什么要这条路径 —— VM 场景下「打扰」不是成本，**「丢消息」才是**：

        · 一次发送失败不该让我们重新调模型（内容会变，还要再花一次调用）；
        · 也不该重新写一遍输入框（多一次抢前台 + 剪贴板，多一次出错机会）；
        · 输入框里的那句话本身就是**可恢复的进度**，重试路径越短越可靠。

        如果草稿已经不在了（被清掉、被切走会话），就退回完整流程重新写入，
        并报一条 `E-SEND-014` —— 这种情况本身值得知道。
        """
        want = text.strip()
        if resume:
            if self.editor_contains(text):
                log("RESUME", f"输入框里仍是上一轮那句话（{len(want)} 字）"
                              f"→ 跳过重新写入，直接重试发送")
            else:
                resume = False
                report("E-SEND-014", "上一轮留下的草稿已不在输入框里，改为重新写入",
                       ctx={"期望": clip(want, 40), "实际": clip(self.editor_text(), 40) or "(空)"})

        if not resume and not self.type_text(text):
            return False

        if not self.wait_send_enabled():
            report("E-SEND-002", "发送按钮仍是禁用态，文本可能没进输入框，已中止发送",
                   ctx={"输入框回读": clip(self.editor_text(), 40) or "(空)",
                        "等待秒数": self.cfg["chat"].get("send_button_wait_seconds"),
                        "本轮是重试": bool(resume)})
            self._abort_cleanup("发送按钮未恢复")
            return False

        # 回读校验：确认输入框里真的是我们要发的内容，防止发出错误内容
        got = self.editor_text()
        if want and want not in got:
            report("E-SEND-003", "输入框回读不符，已中止发送",
                   ctx={"期望": clip(want, 40), "实际": clip(got, 40)})
            self._abort_cleanup("回读不符")
            return False

        # ---- 发送前最后一道闸：此刻会话还是不是原来那个？----
        if guard is not None:
            try:
                ok = bool(guard())
            except Exception as exc:
                ok = False
                report_exc(exc, "E-SEND-006", ctx={"阶段": "发送前复核抛异常"})
            if not ok:
                report("E-SEND-006", "发送前复核失败：会话已被切换或未渲染完成 —— 本条中止，避免发错人",
                       ctx={"目标": getattr(self, "_guard_target", "")})
                self._abort_cleanup("发送前会话复核失败")
                return False

        # 首选 InvokePattern 触发发送按钮：走 UIA 调用，**不需要前台窗口，也不产生任何按键**
        if self.send_btn is not None:
            try:
                self.send_btn.GetInvokePattern().Invoke()
                return True
            except Exception as exc:
                report_exc(exc, "E-SEND-008", ctx={"阶段": "InvokePattern.Invoke 发送按钮"})

        # 兜底才用回车，且必须确认前台 —— 否则宁可失败也不发（见文件上方踩坑说明）
        if not self.is_foreground():
            report("E-SEND-008", "Invoke 失败且 QQ 不在前台，拒绝发送回车（防止按键漏到其它窗口）",
                   ctx={"发送按钮": "已定位" if self.send_btn is not None else "未定位",
                        "当前前台": _foreground_title()})
            return False
        auto.SendKeys("{Enter}")
        return True

    def clear_editor_uia(self) -> bool:
        """
        免前台清空输入框：走 `ValuePattern.SetValue("")`。

        能成就绝不碰键盘。键盘方案（Ctrl+A / Delete）**必须抢前台**，
        而「为了擦掉自己留下的草稿再抢一次前台」正是用户最反感的那种打扰。

        返回是否真的清干净了（用回读确认，不信 SetValue 不抛异常就算成功）。
        """
        if self.editor is None:
            return False
        try:
            pat = self.editor.GetValuePattern()
        except Exception:
            return False                      # 不支持该 Pattern（uiautomation 这里是抛异常）
        if pat is None:
            return False
        try:
            pat.SetValue("")
            time.sleep(0.05)
            return not self.editor_text().strip()
        except Exception:
            # Chromium 的 contenteditable 有时不暴露可写 ValuePattern，退化为按键
            return False

    def _abort_cleanup(self, reason: str = "") -> None:
        """
        发送中止后的收尾。

        ## ⚠️ 默认**不删**草稿 —— 这是 VM 场景下的正确取舍

        原来这里一律把粘进输入框的回复擦掉（还为此抢前台），理由写在注释里：
        「留一段没发出去的草稿，人可能误发」。那是把「打扰/误发」当成主要成本时的判断。

        但 VM 场景下成本结构变了：**「这条消息永远没人回」才是不可接受的错误。**
        涂掉草稿意味着：

            已生成的回复没了 → 要重新调模型（内容会变、多花一次调用）
                            → 或者在重试上限用尽后被撤单 → **那批消息永远没有回应**

        所以现在把草稿当成**可恢复的进度**留着：队列会带着 `item.reply` 继续重试，
        下一轮走 `send_text(resume=True)` 直接原地重发，路径极短。

        想回到「删掉草稿」的行为，把 `chat.keep_draft_on_abort` 设为 false
        （那种情况下才启用下面的三级降级清理）。
        """
        try:
            draft = self.editor_text()
            if not draft.strip():
                return

            if self.cfg["chat"].get("keep_draft_on_abort", True):
                report("E-SEND-012",
                       "本条暂未发出，输入框里的草稿**已保留**（队列会继续重试发送）",
                       ctx={"中止原因": reason,
                            "草稿": clip(draft, 60),
                            "说明": "保留草稿是刻意的：删掉它等于丢掉一条已经生成好的回复，"
                                    "那批消息可能因此永远得不到回应"})
                return

            # ---- 下面是「不保留草稿」时的三级降级清理（按打扰程度从低到高）----
            # ① 免前台
            if self.clear_editor_uia():
                report("E-SEND-010", "本条回复已作废，粘进输入框的内容已清掉",
                       ctx={"作废原因": reason, "清理方式": "免前台（ValuePattern）",
                            "草稿": clip(draft, 40)})
                return

            # ② 已经在前台：顺手用键盘清掉，不额外抢前台
            if self.is_foreground():
                self.clear_editor()
                report("E-SEND-010", "本条回复已作废，粘进输入框的内容已清掉",
                       ctx={"作废原因": reason, "清理方式": "键盘（QQ 当时已在前台）",
                            "草稿": clip(draft, 40)})
                return

            # ③ 清不掉就算了 —— 不为了擦草稿再抢一次前台
            report("E-SEND-011",
                   "本条回复已作废，但输入框里留下了一段没发出去的草稿",
                   ctx={"作废原因": reason,
                        "草稿": clip(draft, 60),
                        "为什么不自动清": "清它必须抢前台，而抢前台会打断你正在做的事；"
                                          "程序已承诺「用完即还」，不再二次夺取。"
                                          "这条草稿是可见的，请你顺手删掉或直接发出去"})
        except Exception as exc:
            report_exc(exc, "E-SEND-003", ctx={"阶段": "中止后的清理"})
        except Exception as exc:
            report_exc(exc, "E-SEND-003", ctx={"阶段": "中止后清理残留草稿", "后果": "输入框里可能留有一段没发出去的草稿，请手动清空"})


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
            report_exc(exc, "E-UIA-004", ctx={"阶段": "OCR 兜底读取", "提示": "OCR 只是兜底，失败不影响 UIA 主链"})
            return []


# ============================================================ 主流程
class Agent:
    """
    主流程。与旧版最大的区别：**收消息**和**调模型**被拆成两件事。

        读新消息 → 过准入判定 → 进防抖缓冲（等待窗口内继续收）
                                            ↓ 到点
                                   合并成一条 → 调模型 → 发出去

    这样对方连发「在吗」「你在干嘛」「算了」三条，AI 只会看到合并后的一条、只回一次。
    """

    def __init__(self, cfg: dict, dry_run: bool = False, no_send: bool = False):
        self.cfg = cfg
        self.dry_run = dry_run
        self.no_send = no_send      # 彩排模式：走完全链路但不真的发出
        self.qq = QQWindow(cfg)
        self.llm = LLMClient(cfg)
        self.ocr = OcrReader(cfg)
        self.store = ConversationStore(cfg)
        self.deb = Debouncer(cfg)
        self.queue = ReplyQueue(cfg)
        self.scope = ""
        self._last_reply_at = 0.0
        self._last_msg_key = ""
        self._uid_store = None      # 懒加载：没开 QQ 时不该因为读缓存而报错
        self._identity = {}         # scope -> {"display_name":…, "uin":…}
        self._rotation = None       # 懒加载 RotationState（发现/轮转状态）
        self._last_scan = 0.0       # 上次扫会话列表的时刻（节流用）
        self._scan_stats = {"scans": 0, "hits": 0, "enqueued": 0}
        # 本轮「读到多少 / 新鲜多少 / 收下多少 / 各被什么原因挡掉」。
        # 存在的意义：把「读到了新消息但一条都没进上下文」这种**静默失败**变成一行可查的日志
        # （见 Agent._report_round）。这类故障原本和「对方根本没发消息」在日志上无法区分。
        self._round = {"read": 0, "fresh": 0, "handled": 0,
                       "skips": collections.Counter()}

    # ---------------------------------------------------- 会话身份
    def _uids(self):
        """QQ 号缓存（qqid 依赖 agent，所以这里延迟导入，避免循环 import）。"""
        if self._uid_store is None:
            import qqid
            path = (self.cfg.get("identity") or {}).get("store") or qqid.DEFAULT_STORE
            if not os.path.isabs(path):
                path = os.path.join(HERE, path)
            self._uid_store = qqid.UidStore(path)
        return self._uid_store

    def _rot(self) -> RotationState:
        """发现/轮转状态（同样懒加载，保证 --dry-run 不碰 QQ 也能跑起来）。"""
        if self._rotation is None:
            self._rotation = RotationState(_rot_path(self.cfg))
        return self._rotation

    def identity_of(self, display_name: str, is_group: bool) -> tuple[str, str]:
        """
        把「会话列表上显示的名字」解析成 (scope, uin)。

        scope **优先用 QQ 号**：昵称可以随时改、也可以重名，拿它当主键迟早串味。
        没取到号时降级回昵称，并把 uin 置空 —— 调用方据此知道这条不稳。
        """
        uin = self._uids().uin_of(display_name) if display_name else ""
        kind = "group" if is_group else "private"
        if uin:
            return f"{kind}:{uin}", uin
        return f"{kind}:{display_name or '(未命名会话)'}", ""

    def remember_identity(self, scope: str, display_name: str, uin: str) -> None:
        self._identity[scope] = {"display_name": display_name, "uin": uin}

    def current_scope(self) -> str:
        title = self.qq.dialog_title or "(未命名会话)"
        scope, uin = self.identity_of(title, self.qq.is_group)
        self.remember_identity(scope, title, uin)
        return scope

    def _sync_scope(self) -> None:
        """
        窗口标题变了 = 换了一个会话 → 结算旧会话、**按需**给新会话建基线、换上下文。

        ⚠️ 基线的建立必须**每个会话只做一次**，而且要落盘。

        老实现是无条件 `baseline()`：每切一次会话，就把新会话当前可见的消息全标成已读。
        单会话时代这样没问题（切走的会话本来就没人服务）；一旦要服务多个会话，
        它就会把「我不在的那段时间到达的消息」在切回去的瞬间**静默吃掉** —— 这就是 D5。
        """
        scope = self.current_scope()
        if scope == self.scope:
            return
        if self.scope:
            if self.deb.pending:
                log("BUF", f"会话切换，先把 {len(self.deb.pending)} 条待聚合消息结算掉")
                self.flush(force=True)
            self.deb.clear()
            self.qq.refresh_layout(force=True)
            rot = self._rot()
            if self.qq.has_seen(scope):
                log("INFO", f"会话切换 → {scope}（本进程建过基线，"
                            f"按它自己的已读集合切新消息，不吞消息）")
            else:
                kept = self.qq.baseline(scope=scope)
                rot.mark_baselined(scope)
                rot.save()
                log("INFO", f"会话切换 → {scope}（首见，建立基线，忽略已有 {kept} 条历史）")
        self.scope = scope

    def _cont_timeout(self) -> float:
        return float(self.cfg["continuous"].get("timeout_seconds") or 1800)

    def _continuous_now(self, scope: str) -> bool:
        if not self.cfg["continuous"].get("enabled"):
            return False
        return self.store.is_continuous(scope, time.time(), self._cont_timeout())

    # ---------------------------------------------------- 准入判定（两条路径共用）
    def _admit(self, m: Message, scope: str) -> tuple[list, str]:
        """
        单条消息的准入判定。返回 (写入指令, 跳过原因)。

        写入指令是 `(role, text, source)` 三元组列表，按顺序写进上下文即可；
        跳过原因是给人看的短句（空串表示「静默丢弃」，不刷日志）。

        为什么要抽出来共用：现在有两条路径会往上下文里写消息 ——

            A) 已打开会话的**实时路径**（`step` → `_ingest`）
            B) 未打开会话的**延迟总读取路径**（`_read_into_history`）

        不共用的话，同一条消息会因为「当时是不是凑巧开着这个会话」
        而时进时不进上下文 —— 这是最难查的一类不一致。
        """
        if not m.content:
            return [], ""
        if m.direction != "other":
            if m.direction == "me" and self.cfg["teach"].get("honor_own_outgoing"):
                body = parse_teach(m.content, self.cfg)
                if body:
                    return [("assistant", body, "teach-self")], ""
            # ⚠️ 这一条**必须给出原因**，不能静默。
            #
            # 原来的写法是 `return [], ""`（静默丢弃），后果很严重：
            # 一旦方向判反（对方的消息被认成自己发的），这条消息会被悄悄扔掉，
            # **日志里一个字都不出现** —— 表现就是「对方明明发了消息，程序一声不吭」。
            # 而且它和「对方根本没发消息」在日志上完全无法区分，只能靠猜。
            # 现在把原因写出来，并在里面直接给出验证方法。
            return [], (f"方向判定为『{m.direction}』（自己发的/系统消息）→ 跳过"
                        f"｜若这条其实是对方发的，说明方向判反了："
                        f"跑一次「只读诊断」核对【我方】/【对方】与气泡左右是否一致"
                        f"（key={m.key[:24]} 发送者={m.sender or '?'}）")

        # 白名单：只跟私聊说话时，群聊一律跳过
        if self.cfg["chat"].get("private_chat_only") and self.qq.is_group:
            return [], f"群聊『{self.qq.dialog_title}』已跳过"

        # 多媒体（图片/语音/文件/动画表情）：由 chat.nontext_policy 决定
        #   "skip"     —— 一律跳过（默认）
        #   "describe" —— 拿 QQ 气泡里的占位文本（如 `[动画表情]`）当正文继续走
        #
        # 为什么要有 describe：`read_messages` 解析非文本气泡时，拿到的 content 本身
        # 就是 QQ 渲染的占位串（`[动画表情]`/`[图片]`/`[语音]`），它天然就是一句描述。
        # 不利用它的话，「对方只发了个表情」会在总读取阶段被判成 0 条新消息 → 撤单，
        # 表现为「明明有红点却一声不吭」。陪伴场景下表情是社交信号，值得让 AI 自己判断。
        if m.kind != "text":
            policy = str(self.cfg["chat"].get("nontext_policy") or "skip").lower()
            if policy != "describe":
                return [], f"非文本消息已跳过（{m.kind}）"
            desc = (m.content or "").strip()
            if not desc:
                return [], f"非文本消息没有可用描述（{m.kind}）"
        else:
            desc = ""
        text_src = desc or m.content

        # 调教语句：不触发 AI，直接以 assistant 身份写入
        # （非文本的描述串不参与调教解析，免得表情占位串碰巧长得像调教语句）
        if not desc:
            body = parse_teach(m.content, self.cfg)
            if body is not None:
                return [("assistant", body, "teach")], ""

        # 触发词判定：
        # 连续对话激活期间免触发词（插件同款行为）；
        # 群聊默认额外要求触发词，想放开就把 chat.group_requires_trigger 设为 false
        need_trigger = (not self.cfg["chat"].get("always_reply")) or (
            self.qq.is_group and self.cfg["chat"].get("group_requires_trigger", True)
        )
        if need_trigger and not self._continuous_now(scope):
            if not looks_like_trigger(text_src, self.cfg):
                return [], "未命中触发词"
            text = strip_trigger(text_src, self.cfg)
        else:
            text = text_src
        if not text:
            return [], "触发词之后没有内容"
        return [("user", text, "incoming")], ""

    # ---------------------------------------------------- 一轮：读 → 入队
    def step(self) -> int:
        """读一轮新消息并入队，返回本轮收下的条数（真正调模型在 flush）。"""
        self.qq.refresh_layout()
        self._sync_scope()
        msgs = self.qq.read_messages()
        fresh = self.qq.split_new(msgs, self.scope)
        self._round = {"read": len(msgs), "fresh": len(fresh), "handled": 0,
                       "skips": collections.Counter()}
        if not fresh:
            return 0
        return self._ingest(fresh, self.scope)

    def _report_round(self) -> None:
        """
        把「读到了新消息，但一条都没进上下文」这种情况**强制报出来**。

        ## 为什么非要有这个方法

        实时路径（当前打开的那个会话）以前有一种完全静默的失败模式：
        `step()` 读到 N 条新消息 → 逐条走 `_admit` → 全被拒 → **日志里什么都没有**。
        因为 `_admit` 对拒收只返回一个短句，而调用方只在「短句非空」时才打印，
        方向判反那条更是连短句都没有。

        结果是：「对方发了消息程序不回」和「对方根本没发消息」在日志上**一模一样**，
        完全无法区分。用户只能来问「为什么没反应」，而没有任何线索可查。

        现在只要有「读到但全被跳过」，就打出一行带原因分布的汇总 ——
        一眼就能看出是方向判反、触发词没命中、还是非文本被策略挡了。
        """
        r = self._round
        if not r or not r["fresh"] or r["handled"]:
            return
        dist_items = r["skips"].most_common(5)
        dist = "；".join(f"{k.split('｜')[0]} ×{v}" for k, v in dist_items) or "（没有记录到原因）"
        top = dist_items[0][0] if dist_items else ""
        report("E-READ-001",
               f"本轮读到 {r['fresh']} 条新消息（共读 {r['read']} 条），"
               f"但一条都没进入上下文",
               ctx={"跳过原因分布": dist,
                    "会话": self.scope,
                    "方向可疑": "是 —— 见原因里的「方向判定」" if "方向判定" in top else "否"})

    def _ingest(self, fresh: list[Message], scope: str = "") -> int:
        scope = scope or self.scope
        hist = self.store.history(scope)
        handled = 0
        for m in fresh:
            writes, why = self._admit(m, scope)
            if not writes:
                if why:
                    log("SKIP", why)
                    self._round["skips"][why] += 1
                else:
                    # 连原因都没有的只剩「正文为空」这一种 —— 也要记账，
                    # 否则它就成了新的静默黑洞。
                    self._round["skips"]["正文为空（解析不出内容）"] += 1
                continue

            # ---- 调教语句：立即写入，不触发 AI ----
            # 立即写入是为了「调教即生效」；防抖缓冲里那批更早的用户消息，
            # 结算时会用 push_before_teach 回插到它之前，保证因果不倒置。
            if all(role == "assistant" for role, _t, _s in writes):
                for role, text, src in writes:
                    hist.push(role, text, source=src, teach=True)
                    log("TEACH", f"（{src}）写入上下文：{clip(text)}")
                    handled += 1
                continue

            user_text = next(t for r, t, _s in writes if r == "user")
            log("RECV", f"{m.sender or '对方'}: {clip(user_text)}")

            # ---- 冷却 / 重复 ----
            cooldown = float(self.cfg["chat"].get("reply_cooldown_seconds") or 0)
            if cooldown > 0 and time.time() - self._last_reply_at < cooldown:
                log("SKIP", "命中冷却窗口，本条丢弃")
                continue
            if m.key == self._last_msg_key:
                continue

            seq = hist.next_seq()      # 先占号，用于回插定位

            # ---- 排队期间的补充消息：5s 阈值判定（见 reply_queue.offer_followup）----
            # 该会话已经在队列里等着被回复，这时又发来一句：
            #   还要等 > 阈值 → 直接并进他待提交的上下文，不额外产生一次回复
            #   还要等 ≤ 阈值 → 不掺和，让他照常排队，这句之后单独回一次
            piece = f"【{m.sender or '对方'}】：{user_text}"
            verdict = self.queue.offer_followup(scope, piece, m.key, seq)
            if verdict == "deferred":
                # 该会话这一条**已经定稿**（回复生成好了/已粘进输入框）。
                # 新消息不能并进去（那句话是按旧内容写好的），也不能丢
                # —— 已经记进它的 late 桶，等它发完会结转成新的一条。
                it = self.queue.get(scope)
                log("QUE", f"该会话已定稿待发（{len(it.reply)} 字回复等风控放行）→ "
                           f"本条记入迟到队列，发完后单独回应（不丢）")
                handled += 1
                continue
            if verdict == "merged":
                it = self.queue.get(scope)
                log("QUE", f"排队中补充消息已并入 {it.display_name!r}"
                           f"（还需等 {self.queue.remaining_wait(scope):.1f}s > "
                           f"{self.queue.merge_threshold:.1f}s 阈值）"
                           f"→ 该批共 {it.count} 条，不单独回复")
                handled += 1
                continue
            if verdict == "close":
                log("QUE", f"该会话 {self.queue.remaining_wait(scope):.1f}s 内就会被轮到"
                           f"（≤ {self.queue.merge_threshold:.1f}s），本条不并入，另排一次回复")

            wait = self.deb.add(PendingMessage(m.sender or "对方", user_text, m.key, seq), scope)
            if self.deb.enabled:
                log("BUF", f"入队（缓冲 {len(self.deb.pending)} 条），{wait:.1f}s 后聚合结算"
                           f"｜基础等待 {self.deb.summary(scope)}")
            else:
                log("BUF", f"入队（缓冲 {len(self.deb.pending)} 条，防抖已关闭 → 立即结算）")
            handled += 1
        self._round["handled"] += handled
        return handled

    # ---------------------------------------------------- 结算：聚合 → 入队
    def flush(self, force: bool = False) -> bool:
        """
        缓冲到期（或 force）则把这一批合并成**一个待回复项**放进队列。

        注意这里**不再调模型、也不发送** —— 那是 `serve_queue()` 的活。
        拆开的原因：模型 1.3~1.8s 不占前台，UI 串行段只有 0.5s 左右；
        把模型挪出 UI 临界区，才能让「风控限速」成为唯一的节拍器。
        """
        if not self.deb.pending:
            return False
        if not force and not self.deb.ready:
            return False

        batch = self.deb.take()
        scope = self.scope
        self.deb.note_round(scope, len(batch))     # 记录本轮条数 → 两档自适应
        combined = "\n".join(f"【{p.sender}】：{p.content}" for p in batch)
        batch_seq = min((p.seq for p in batch if p.seq), default=0)

        ident = self._identity.get(scope) or {}
        display_name = ident.get("display_name") or self.qq.dialog_title or "(未命名会话)"
        uin = ident.get("uin") or self._uids().uin_of(display_name)

        log("AGG", f"聚合 {len(batch)} 条消息 → 1 个待回复项入队")

        if self.dry_run:
            # dry-run 的语义是「记录，但不调模型、不入队、不发送」。
            # 所以这里必须把聚合结果写进上下文 —— 否则下面 dump 出来的还是旧内容，
            # `--dry-run` 就验证不了「到底读到了什么、聚合成了什么」。
            self.store.history(scope).push_before_teach(
                batch_seq or None, "user", combined, source="incoming")
            log("DRY", f"dry-run：已把聚合后的 {len(batch)} 条写入上下文，"
                       f"不调用模型、不入队、不发送")
            print(self.store.history(scope).dump(6))
            self.store.save()
            return True

        item = self.queue.submit(scope, display_name, uin, combined,
                                 key=batch[-1].key, seq=batch_seq, wait_seconds=0.0)
        item.batch = batch
        self.remember_identity(scope, display_name, uin)
        log("QUE", self.queue.snapshot())
        self.store.save()
        return True

    # ==================================================== 发现（只扫列表，不碰前台）
    def _discovery_wait(self, scope: str) -> float:
        """
        发现路径的静默窗长度（秒）。

        默认直接复用防抖器的当前基础等待（配置里是 5s）—— 这样「5s 阈值」
        在整个系统里始终是同一个数，不会出现两处阈值互相打架。
        该会话若已被判成快档（连续几轮都是单条消息），这里也跟着缩短。
        上限受 `queue.max_hold_seconds` 约束，免得窗口比硬上限还长。
        """
        base = self.deb.current_base(scope) / 1000.0
        return max(0.0, min(base, self.queue.max_hold))

    def _scope_for_session(self, s) -> tuple[str, str]:
        """
        由会话项推出 (scope, uin)。

        群 / 私聊必须判准：它决定 scope 前缀（`private:` / `group:`），
        判错会把**同一个会话的历史劈成两份**，AI 从此记不住之前聊过什么。
        优先级：

            1) uid 缓存里的 `is_group` —— 最可靠，是 QQ 自己在资料卡上给的
            2) 预览首段带「：」→ 群聊 —— 会话列表的启发式，实测 6/6 命中
            3) 都判不了 → 按私聊走，同时记一条 WARN
        """
        info = self._uids().get(s.display_name)
        if info is not None and info.uin:
            return self.identity_of(s.display_name, info.is_group)
        guess = "群聊" if s.looks_group else "私聊"
        if info is None:
            log("WARN", f"{s.display_name!r} 没有 QQ 号缓存 → 按「预览首段带：」猜为{guess}。"
                        f"建议先跑 `python qqid.py --enroll-all`，否则 scope 可能判错")
        else:
            log("WARN", f"{s.display_name!r} 缓存里没有 QQ 号（当初资料卡没取到）→ "
                        f"按预览猜为{guess}")
        return self.identity_of(s.display_name, s.looks_group)

    def _refresh_fp(self, display_name: str) -> None:
        """
        重采某个会话的指纹快照。**必须在发完自己的消息之后立刻调。**

        我们的回复会让这个会话的预览变掉。不刷新的话，下一轮扫描就会把
        「我自己刚发的那条」当成对方的新消息 → 再占位 → 再切过去总读取
        （结果发现没有新消息）→ 撤单。白付一次切会话的成本，
        严重时还会形成「自己叫醒自己」的循环。
        """
        try:
            import qqid
            for s in qqid.list_sessions(self.qq.win):
                if s.display_name == display_name:
                    self._rot().set_fp(s.key, s.fingerprint())
                    return
        except Exception as exc:
            report_exc(exc, "E-UIA-002", ctx={"阶段": "发送后重采指纹", "后果": "可能把自己刚发的那条误判成新消息"})

    def discover(self) -> int:
        """
        「发现」阶段：扫一遍会话列表，看谁有新消息 —— 给它占一个待回复位。

        **这一步完全不碰前台、也不读正文。** 桌面 UI 上每个会话只保留
        「最新一条消息的节选」，既不足以构造上下文，也不是必须的：
        真正的总读取推迟到 `prepare()`（见那里的说明）。

        跳过当前已打开的那个会话 —— 它的正文由 `step()` 的实时路径负责，
        两边都入队会让同一个会话被回两次。
        """
        d = self.cfg.get("discovery") or {}
        if not d.get("enabled") or self.qq.win is None:
            return 0
        now = time.time()
        if now - self._last_scan < max(0.0, float(d.get("scan_interval_seconds") or 2.0)):
            return 0
        self._last_scan = now

        import qqid
        try:
            sessions = qqid.list_sessions(self.qq.win)
        except Exception as exc:
            report_exc(exc, "E-UIA-002", ctx={"阶段": "发现扫描", "处理": "本轮跳过，下一轮继续"})
            return 0
        if not sessions:
            return 0

        rot = self._rot()
        cur_title = self.qq.title_now()
        on_unread = bool(d.get("trigger_on_unread", True))
        on_preview = bool(d.get("trigger_on_preview_change", True))
        on_first = bool(d.get("trigger_on_first_sight_unread", True))
        cap = max(1, int(d.get("max_enqueue_per_scan") or 3))
        self._scan_stats["scans"] += 1
        enqueued = 0

        for s in sessions:
            if not s.display_name:
                continue
            if cur_title and s.display_name == cur_title:
                continue                    # 已打开的会话交给 step() 的实时路径

            fp = s.fingerprint()
            old = rot.fp_of(s.key)
            rot.set_fp(s.key, fp)           # 先记账，免得中途异常导致下轮误判

            if old is None:
                log("DISC", f"首见 {s.display_name!r}：记录指纹快照（未读={s.unread}）")
                # 未读徽标本身就等于「对方发来之后一直没人看」→ 是新鲜的，值得回。
                # 不想让它一上来就处理历史未读，把 trigger_on_first_sight_unread 关掉。
                if not (on_first and s.unread > 0):
                    continue
                trigger = f"首见但已有未读({s.unread})"
            elif old == fp:
                continue
            else:
                # 红点是硬证据；预览变化是兜底（有些会话不显未读数字）
                if not ((on_unread and s.unread > 0) or on_preview):
                    continue
                trigger = (f"红点(未读 {old[1]}→{fp[1]})" if s.unread > 0
                           else "摘要变化")

            wait = self._discovery_wait(self.scope or s.display_name)
            scope, uin = self._scope_for_session(s)
            item, created = self.queue.ensure_pending(
                scope, s.display_name, uin, now=now,
                wait_seconds=wait, unread_hint=s.unread)
            if not created:
                log("DISC", f"{s.display_name!r} 又有新动静（{trigger}），但已在队列里 "
                            f"→ 不重复占位")
                continue
            enqueued += 1
            self._scan_stats["enqueued"] += 1
            log("DISC", f"发现 {s.display_name!r} 有新消息：{trigger}"
                        f"｜摘要={s.summary[:20]!r} → 占位入队，"
                        f"{wait:.1f}s 后做上下文总读取")
            if enqueued >= cap:
                log("DISC", f"本轮已达上限 {cap} 个，剩下的下一轮再说")
                break

        if enqueued:
            self._scan_stats["hits"] += 1
            rot.save()
            log("QUE", self.queue.snapshot())
        return enqueued

    # ==================================================== 总读取（把正文读全）
    def _read_into_history(self, item) -> int:
        """
        「上下文总读取」—— 降级方案的第二步。

        调用前提：**已经切到目标会话**（由 `prepare` 负责切）。

        这里才把关这个会话的消息列表**读全**，按 `_admit` 的判定写进
        **它自己的**上下文。返回写进去的对方消息条数；0 表示没有新东西，
        调用方应当撤单（别再白回一条）。
        """
        scope = item.scope
        hist = self.store.history(scope)
        rot = self._rot()

        if not self.qq.has_seen(scope):
            # 本进程还没给这个会话建过已读集合（可能是刚启动、也可能是刚发现它）：
            # 建基线，但把「会话项上显示的未读条数」留出来本次处理 ——
            # 这样既不回灌 30 条历史，也不会把对方刚发的那几条当成历史吃掉。
            # 这个 skip_last 就是「降级方案」里唯一还能利用的未读线索。
            keep = max(0, min(int(item.unread_hint or 0), 8))
            kept = self.qq.baseline(skip_last=keep, scope=scope)
            rot.mark_baselined(scope)
            rot.save()
            log("INFO", f"{item.display_name!r} 本进程首次访问：建立基线"
                        f"（忽略 {kept} 条历史，保留最后 {keep} 条本次处理）")

        self.qq.refresh_layout(force=True)
        limit = max(5, int((self.cfg.get("discovery") or {}).get("read_limit") or 30))
        msgs = self.qq.read_messages(limit=limit)
        fresh = self.qq.split_new(msgs, scope)
        if not fresh:
            return 0

        written = 0
        for m in fresh:
            writes, why = self._admit(m, scope)
            if not writes:
                if why:
                    log("SKIP", f"[总读取] {why}")
                continue
            for role, text, src in writes:
                if role == "assistant":
                    hist.push(role, text, source=src, teach=True)
                    log("TEACH", f"[总读取] 用户替 AI 说：{clip(text)}")
                else:
                    hist.push("user", f"【{m.sender or '对方'}】：{text}", source=src)
                    written += 1
                    log("RECV", f"[总读取] {m.sender or '对方'}: {clip(text)}")
        return written

    # ==================================================== 出队：读+生成 → 发送
    def prepare(self, item) -> bool:
        """
        结算的第一阶段：**读 → 写上下文 → 调模型**，把回复算好放进 `item.reply`。

        拆两阶段的原因，正是「降级方案」的核心：

            桌面 UI 上每个会话只保留最新一条消息的**节选**，靠它构造不出上下文。
            所以「发现」阶段只排队、不读正文；真正的上下文总读取推迟到这里 ——
            此时静默窗（5s 阈值）已经关闭、对方这一轮说完了，一次读全。

        这一步**不发送**，所以不消耗风控额度，可以早于风控放行执行
        （用 `pick(ignore_budget=True)`）；真正排到之后再走 `deliver()` 发出去。
        """
        scope = item.scope
        hist = self.store.history(scope)

        if item.pending_read:
            set_phase("切会话", 15, target=item.display_name)
            if not self._ensure_session(item):
                self.queue.requeue(item, delay=2.0)
                log("WARN", f"{item.display_name!r} 切不过去，读不了正文 → 排回队尾重试")
                return False
            set_phase("总读取", 20, target=item.display_name)
            n = self._read_into_history(item)
            if n <= 0:
                if item.has_late:
                    # ⚠️ 不能撤单：已经有「迟到消息」的标记在，说明这个会话确实有过动静，
                    # 只是这一次总读取没读到（渲染没跟上 / 被别处读过 / 刚好卡在中间）。
                    # 排回去再读一遍，总比把这几条消息永久丢掉强。
                    log("QUE", f"{item.display_name!r} 总读取没读到新消息，"
                               f"但这一条记着有迟到消息 → 不撤单，稍后重读")
                    self.queue.requeue(item, delay=2.0)
                    return False
                self.queue.drop(scope, aborted=True)
                log("QUE", f"{item.display_name!r} 总读取没拿到新消息"
                           f"（已被别处读过 / 撤回 / 只是自己发的）→ 撤单，不回复")
                self.store.save()
                return False
        elif not item.pushed:
            # 实时路径：正文在 _ingest 时就拿到了，直接写进上下文
            seq0 = item.seqs[0] if item.seqs else 0
            hist.push_before_teach(seq0, "user", item.combined(), source="incoming")
            item.pushed = True

        if self._continuous_now(scope):
            self.store.refresh_continuous(scope, time.time())

        # 模型调用（不占前台；故意放在 UI 临界区之外）
        #
        # ⚠️ 但它是**同步**跑在主循环线程上的：这一步期间既不读消息、也不写心跳。
        # 上限就是 llm.timeout_seconds（默认 60s）。所以这里显式把阶段和上限报给心跳 ——
        # 否则界面会把「模型调用耗时 40s」误报成「进程可能卡住了」。
        set_phase("调模型", float(self.cfg["llm"].get("timeout_seconds") or 60) + 15,
                  target=scope)
        reply = self.generate_reply(scope)
        if not reply:
            self.queue.drop(scope, aborted=True)
            # 生成失败的**根因**在 generate_reply 里已经带码报过了（模型侧各状态码分类）。
            # 这里只说清后果，不重复报同一件事 —— 重复报错本身就是一种混淆。
            report("E-LLM-009", "本条回复没生成出来，已作废（消息留在上下文里，下一轮仍会带上）",
                   ctx={"会话": item.display_name})
            self.store.save()
            return False

        item.reply = reply
        item.prepared = True
        item.prepared_at = time.time()
        item.fail_count = 0                  # 新回复，失败计数重新起算
        item.last_fail = ""
        # 立刻落盘：从这一刻起「这条回复必须被送出去」。
        # 进程崩了/被重启，下次启动会把它捞回来继续发 —— 否则那批消息永远没人回。
        self._save_pending()
        log("QUE", f"{item.display_name!r} 回复已备好（{len(reply)} 字），等风控放行发送")
        return True

    def serve_queue(self) -> bool:
        """
        队列服务。分两阶段（见 `prepare`）：

            阶段一 prepare ：读 → 写上下文 → 调模型  （**可以早于风控放行**，不吃额度）
            阶段二 deliver ：带租约发送             （必须等风控放行）

        返回「这一轮有没有真的把消息发出去」。

        为什么一次只处理一项：UI 是唯一串行资源，多会话同时到点时连切好几次
        会话会把前台预算打穿。而两阶段之间我们**停在同一会话上**（单项队列天然
        把我们park在那儿），所以整条链路仍然只付一次「切会话」的成本。
        """
        item = self.queue.pick(ignore_budget=True)
        if item is None:
            return False

        # ---- 阶段一：读 + 生成（不发送，因此不需要风控放行）----
        if not item.prepared:
            self.prepare(item)          # 失败时 prepare 内部已经撤单或重排
            return False                # 本轮只做读+生成，下一轮 tick 再看能不能发

        # ---- 阶段二：发送（这一步才占风控额度）----
        if self.queue.budget.wait_seconds() > 0:
            return False                # 回复已备好，只是还没轮到

        ok = self.deliver(item, item.reply)
        if ok:
            sent = self.queue.drop(item.scope)
            self.queue.served()
            self._last_reply_at = time.time()
            self._last_msg_key = item.keys[-1] if item.keys else ""
            self._refresh_fp(item.display_name)     # 别把自己发的那条当成新消息
            self._carry_over(sent)                  # 迟到的消息结转成新的一条
        else:
            # ⚠️ **不丢弃已生成的回复**，也不撤单。
            #
            # 原实现这里做两件事：`item.reply = ""` + `item.prepared = False`（作废重生成），
            # 加上 `attempts >= max_attempts` 时直接 drop（撤单）。两者都指向同一个后果：
            # **这条消息可能永远没有人回。** 而在 VM 场景下那是最不可接受的错误。
            #
            # 现在的语义：回复保持不动，项留在队列里，退避之后**原地重试发送**。
            # 重试路径很短（校验 + Invoke），因为 `deliver` 已经把「草稿还在不在输入框里」
            # 记到了 `item.drafted` 上，下一轮直接走 `send_text(resume=True)`。
            self.queue.note_send_failure(item, reason=_LAST_CODE.get("code") or "E-SEND-008")
            self.queue.requeue(item, delay=self._retry_delay(item))
            log("QUE", f"{item.display_name!r} 未发出，保持原回复原地重试"
                       f"（第 {item.fail_count} 次失败｜{item.last_fail}｜"
                       f"{'草稿仍在输入框' if item.drafted else '草稿已不在，将重新写入'}）")
            self._save_pending()
        self.store.save()
        return ok

    # ---------------------------------------------------- 待发回复的保全
    def _retry_delay(self, item) -> float:
        """
        发送失败后的退避。

        比原来的固定 1 秒更宽，但**不是放弃**：随失败次数递增，上限就是静默窗硬上限。
        回复已经生成好了，早一点晚一点发出去都行，但**必须发出去**。
        """
        base = float(self.cfg["chat"].get("poll_interval_seconds") or 0.8)
        return min(self.queue.max_hold, base * (1 + item.fail_count * 1.5))

    def _carry_over(self, item) -> None:
        """
        把「回复生成之后才到的消息」结转成新的一条队列项。

        ## 为什么必须有这一步

        回复一旦生成，它就**冻结**了（内容是按当时的上下文写好的）。之后到的消息：
          · 塞进这一条 → 发出去的那句话没涵盖它们，而它们又已经进了 `texts`，
            不会被单独处理 → **等于没被回应**；
          · 丢掉不管   → 更糟，那是彻底的消息丢失。
        所以只能单独记着，等老的发出去了，再为它们排一轮。

        结转时统一标记 `pending_read=True`：**让它做一次完整重读**。
        这样「迟到的 + 重读期间又新来的」会一起被读全，不依赖任何猜测。
        """
        if item is None or not item.has_late:
            return
        # 已经有新的在排队了（发现路径先占了位），不重复。
        # 判据必须用「同 scope 且不是自己」——
        # 用 `get(scope)` 是不行的：它返回列表里第一条，而那条很可能就是这个 item 自己，
        # 于是「有没有别人」这个判断会漏掉真正新加进来的那条。
        if any(x.scope == item.scope and x is not item for x in self.queue.items):
            return
        # 用 submit 而不是 ensure_pending：语义上这里就是**要新建一条**，
        # 而 ensure_pending 会「发现已存在就复用」—— 那正是上面那个 guard 的职责，
        # 两处都判断会让行为取决于调用顺序。
        new = self.queue.submit(item.scope, item.display_name, item.uin, text="",
                                wait_seconds=0.0)
        new.pending_read = True
        new.unread_hint = max(item.late_hint, item.late_count)
        self.queue.stats["carried_over"] = self.queue.stats.get("carried_over", 0) + 1
        log("QUE", f"{item.display_name!r}：上一批发出去之后还有 "
                   f"{item.late_count or item.late_hint} 条迟到消息 → "
                   f"已结转成新的一条，稍后会单独回应（不丢）")
        self._save_pending()

    def _pending_path(self) -> str:
        return os.path.join(HERE, "state", "pending-replies.json")

    def _save_pending(self) -> None:
        """
        把待发队列落盘。

        没有它，「重启」就是一条永久丢消息的路径：那批消息的正文已经写进上下文、
        也被标成已读，重启后不会再被认成新消息 —— 而回复只活在内存里。
        """
        try:
            path = self._pending_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.queue.dump_state(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            report_exc(exc, "E-PATH-003", ctx={"阶段": "落盘待发队列",
                                               "后果": "重启会丢掉已生成但没发出的回复"})

    def _load_pending(self) -> int:
        """启动时恢复待发队列。返回恢复了几条。"""
        try:
            path = self._pending_path()
            if not os.path.isfile(path):
                return 0
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            n = self.queue.load_state(data)
            if n:
                ready = sum(1 for it in self.queue.items if it.prepared)
                report("E-SEND-015",
                       f"从磁盘恢复了 {n} 条没做完的待回复（其中 {ready} 条已生成好回复）",
                       ctx={"文件": path,
                            "清单": "；".join(f"{it.display_name}"
                                              f"({'已生成' if it.prepared else '待读'}"
                                              f"{'/已粘进输入框' if it.drafted else ''})"
                                              for it in self.queue.items[:5])})
            return n
        except Exception as exc:
            report_exc(exc, "E-PATH-003", ctx={"阶段": "恢复待发队列"})
            return 0

    # ---------------------------------------------------- 发送租约
    def _prepare_qq_for_switch(self, display_name: str) -> None:
        """
        切换会话前的**最小**准备：窗口最小化就还原，**不抢前台**。

        ## 为什么不抢前台了（2026-09-11 实测，B5 的进一步结论）

        会话列表项支持 `InvokePattern`，而它是**免前台**的：QQ 完全不在前台时
        `Invoke()` 照样能把会话切过去。所以只要窗口**可见**（非最小化）就够了 ——
        不必再把 QQ 顶到最上面，也就不会打断你正在用的窗口、不会挪走你的光标。

        这比"抬前台 + 坐标点击"干净得多：坐标点击点到的是「该屏幕坐标上最顶层的
        窗口」，被盖住就静默打偏，而且**返回值还会骗你**（旧会话的标题也非空）。

        ## 为什么最小化必须还原

        Chromium 在窗口被最小化/完全遮挡时会**节流 renderer**，此时 `Invoke()`
        会静默失效（实测：最小化后 Invoke 无任何效果，还原后立刻可用）。
        注意**读取不受影响** —— 读的是 a11y 树缓存，最小化后节点数/会话数/消息数
        完全一致（见 `probe_minimized.py`）。
        """
        hwnd = getattr(self.qq, "hwnd", 0)
        if not hwnd:
            return                       # 没附着到窗口（或测试替身），没什么可做
        try:
            if _u32.IsIconic(hwnd):
                log("INFO", "QQ 处于最小化：先还原窗口（最小化时 Invoke 会静默失效）")
                _u32.ShowWindow(hwnd, 9)          # SW_RESTORE
                time.sleep(0.6)
        except Exception as exc:
            report_exc(exc, "E-QQ-003", ctx={"阶段": "还原最小化窗口"})

    def _ensure_session(self, item) -> bool:
        """确保 QQ 里当前打开的就是目标会话；不是就切过去。"""
        cur = self.qq.title_now()
        if cur and cur == item.display_name:
            return True

        try:
            import qqid
            sessions = qqid.list_sessions(self.qq.win)
        except Exception as exc:
            report_exc(exc, "E-UIA-002", ctx={"阶段": "读会话列表（准备切换）"})
            return False

        target = None
        for s in sessions:
            if s.display_name == item.display_name:
                target = s
                break
        if target is None:
            # 分两种：列表整体为空（渲染/无障碍问题）vs 列表有内容但没有这个会话（真的没了）
            if not sessions:
                report("E-QQ-007", "读不到任何会话（会话列表为空）",
                       ctx={"目标": item.display_name, "标题": cur or "(空)"})
            else:
                report("E-SEND-004", "会话列表里找不到目标会话（已删除 / 退群 / 未加载）",
                       ctx={"目标": item.display_name, "列表条数": len(sessions)})
            return False

        log("INFO", f"切换会话 → {item.display_name!r}")

        # 切会话会把 QQ 激活到最上层（Invoke 与点击都会）。这不是我们需要的前台，
        # 只是副作用 —— 记下你原来的窗口，切完就还回去（方案 C）。
        # ⚠️ 必须在 _prepare_qq_for_switch 之前读：那个函数在窗口最小化时会
        # `SW_RESTORE`，而还原一个最小化窗口本身就会改变前台。
        # hwnd 为 0 表示没附着到真窗口（或测试替身）→ 整套前台逻辑跳过。
        hwnd = getattr(self.qq, "hwnd", 0)
        prev_fg = 0
        if hwnd:
            prev_fg = _fg_hwnd()
            if prev_fg and prev_fg != hwnd:
                self.qq.fg_before_switch = prev_fg

        self._prepare_qq_for_switch(item.display_name)

        if not qqid.switch_session(target, self.qq.win):
            report("E-FG-004", "切换会话失败",
                   ctx={"目标": item.display_name,
                        "当前标题": self.qq.title_now() or "(空)",
                        "锁屏": _desktop_locked(),
                        "窗口最小化": _u32.IsIconic(hwnd) if hwnd else None})
            self._return_focus_after_switch(prev_fg)
            return False

        # 等渲染稳定：连续两次签名一致、且标题已对上才算稳。
        # 必须 force=True 重扫锚点 —— 切换会话后 ml_list 可能仍指向旧容器，
        # 用陈旧引用读出的消息 ID 恰好等于切换前的值，签名就会「稳定地错」。
        last = None
        settled = False
        for _ in range(10):
            time.sleep(0.2)
            sig = self.qq.chat_signature(force=True)
            if sig[0] == item.display_name and sig == last:
                settled = True
                break
            last = sig
        self._return_focus_after_switch(prev_fg)
        if not settled:
            got = self.qq.title_now()
            # 标题已经对了、只是签名没稳定下来：这不算失败，但要说清楚，
            # 因为它会直接导致下一道闸（签名复核）失败 —— 提前把因果挂上。
            code = "E-FG-004" if got != item.display_name else "E-SEND-006"
            report(code, "切换后渲染未稳定（下面可能紧跟一次签名复核失败）",
                   ctx={"当前标题": got or "(空)", "目标": item.display_name})
        return settled or (self.qq.title_now() == item.display_name)

    def _return_focus_after_switch(self, prev_fg: int) -> None:
        """
        切会话之后把前台还给你原来的窗口。

        `InvokePattern` 虽然**不需要**前台，但 Chromium 内部会对目标元素 `SetFocus`
        → QQ 被激活、顶到最上层。既然这个激活是我们引起的、而且我们并不需要它
        （后面读消息、读签名全是 UIA，免前台），就该还回去（方案 C）。

        还成功后清掉 `qq.fg_before_switch`：那个值只在"归还失败、QQ 会继续占着前台"时
        才有用 —— 交给输入路径，免得它把前台「还」给 QQ 自己。
        """
        if not self.cfg["uia"].get("restore_foreground", True):
            return
        if not prev_fg or prev_fg == self.qq.hwnd:
            return
        if restore_foreground(prev_fg):
            log("INFO", "切换会话后已把前台还给你原来的窗口")
            self.qq.fg_before_switch = 0
        else:
            report("E-FG-003", "切换后前台归还失败（QQ 会留在最上层，发送完成后会再试一次）",
                   ctx={"目标窗口": prev_fg, "当前前台": _foreground_title()})

    def _enroll_now(self, display_name: str) -> str:
        """现场取号（打开资料卡，会抢前台一次）。取到就写缓存。"""
        try:
            import qqid
            sessions = qqid.list_sessions(self.qq.win)
            target = next((s for s in sessions if s.display_name == display_name), None)
            if target is None:
                return ""
            log("INFO", f"缓存里没有 {display_name!r} 的 QQ 号，现场取一次（会抢前台）")
            info = qqid.enroll(self.qq.win, self._uids(), target)
            return info.uin if info else ""
        except Exception as exc:
            report_exc(exc, "E-SEND-007",
                       ctx={"阶段": "现场取号（资料卡通路）", "后果": "本次发送只能按昵称复核"})
            return ""

    def _verify_target(self, item) -> bool:
        """
        身份复核 —— 这是「绝不发错人」的第二道闸（D1 的核心）。

            标题对得上  AND  QQ 号对得上

        标题只是备注名，重名/改备注都可能骗过它；QQ 号是稳定标识，骗不过。
        目标没取到号时（降级模式）只校验标题，并明确记一条 WARN。
        """
        title = self.qq.title_now()
        if title != item.display_name:
            report("E-SEND-004", "身份复核失败：当前打开的会话不是目标",
                   ctx={"当前打开": title or "(空)", "目标": item.display_name,
                        "QQ号": item.uin or "未知"})
            return False

        uin = self._uids().uin_of(title) or self._enroll_now(title)
        if not uin:
            if item.uin:
                report("E-SEND-005", "当前会话取不到 QQ 号，而目标要求了 QQ 号 —— 拒绝发送",
                       ctx={"当前会话": title, "目标QQ": item.uin})
                return False
            report("E-SEND-007", "该会话没有 QQ 号，退化为「仅按昵称复核」（有重名风险）",
                   ctx={"会话": title, "建议": "跑一次「给所有会话取号」"})
            return True

        if item.uin and uin != item.uin:
            report("E-SEND-005", "身份不符：QQ 号对不上 —— 拒绝发送（最严重的一类）",
                   ctx={"当前会话": title, "当前QQ": uin, "目标QQ": item.uin})
            return False

        if not item.uin:
            # 入队时没号、发之前取到了 → 补上，下次就不必再现场取
            item.uin = uin
            self.remember_identity(item.scope, title, uin)
        return True

    def deliver(self, item, reply: str) -> bool:
        """带租约的发送事务：切会话 → 身份复核 → 捕获签名 → 写入 → 复核签名 → 发送。"""
        scope = item.scope
        # 这一整段都要抢前台、写剪贴板、发按键，还带若干固定 sleep，天生是秒级的。
        # 上限给 30s，让心跳的 stale 判定知道「这里慢是正常的」。
        set_phase("发送", 30, target=item.display_name)

        if not self._ensure_session(item):
            return False
        if not self._verify_target(item):
            return False

        # 第三道闸：写入前后签名必须完全一致。
        # 它挡的是「模型生成/写入期间，人手动切了会话」——那时 InvokePattern 会
        # 把这段文字发到另一个会话的输入框里，是并发化最凶险的一种错发。
        sig = self.qq.chat_signature(force=True)
        log("SEND", f"→ {item.display_name!r}(QQ {item.uin or '未知'}) {clip(reply, 70)}")

        if self.no_send:
            log("DRY", f"彩排模式：已通过①②闸，签名={sig[0]!r}/{len(sig[2])}条，"
                       f"本条不会真的发出")
            self.store.history(scope).push("assistant", reply, source="auto")
            return True

        def guard() -> bool:
            now_sig = self.qq.chat_signature(force=True)
            if now_sig != sig:
                log("WARN", f"会话签名已变化：{sig[0]!r}/{len(sig[2])}条 "
                            f"→ {now_sig[0]!r}/{len(now_sig[2])}条")
                return False
            return True

        if not self.qq.send_text(reply, guard=guard, resume=bool(item.drafted)):
            # 记下「草稿还在不在输入框里」——下一轮据此决定是原地重发还是重新写入。
            # 这是「不丢消息」的关键状态：输入框里的那句话本身就是**可恢复的进度**。
            item.drafted = self.qq.editor_contains(reply)
            if item.drafted:
                item.draft_at = time.time()
            return False
        item.drafted = False
        self.store.history(scope).push("assistant", reply, source="auto")
        return True

    def generate_reply(self, scope: str) -> str:
        hist = self.store.history(scope)
        try:
            reply = self.llm.chat(hist.build_messages())
        except EC.AppError as exc:
            # 模型侧的错误已经按状态码分类好了（密钥/地址/限流/超时/服务端…），
            # 直接透传，不要再包一层「模型调用失败」把码盖掉。
            report(exc.code, exc.detail_text, ctx={**exc.context, "scope": scope})
            return ""
        except Exception as exc:
            report_exc(exc, "E-LLM-008", ctx={"scope": scope})
            # 刚推进去的 user 消息留在历史里没关系，下一轮还会带上
            return ""

        reply = (reply or "").strip()
        limit = int(self.cfg["chat"].get("max_reply_chars") or 500)
        if len(reply) > limit:
            report("E-LLM-010", f"回复 {len(reply)} 字，超过上限 {limit}，已截断",
                   ctx={"scope": scope, "上限": limit})
            reply = reply[:limit]
        if not reply:
            report("E-LLM-009", "模型返回空内容，本条不再重试",
                   ctx={"scope": scope, "max_tokens": self.cfg["llm"].get("max_tokens")})
            return ""

        # 开口即激活连续对话（插件的 activateContinuous）
        if self.cfg["continuous"].get("enabled"):
            self.store.activate_continuous(scope, time.time())
            log("CONT", f"连续对话已激活（{self._cont_timeout():.0f}s 内该会话免触发词）")
        return reply

    # ---------------------------------------------------- 常驻循环
    def run_forever(self) -> int:
        # ---- 入口三道检查，每一道都对应**一个**确定的原因 ----
        # 这三件事以前挤在同一句报错里，导致修复动作经常是错的（详见 diagnose_attach）。
        if not self.qq.attach():
            code, ctx = self.qq.diagnose_attach()
            report(code, "无法附着到 QQ 窗口", ctx=ctx)
            return 2
        if not self.qq.dom_exposed():
            report("E-QQ-004",
                   "窗口找到了，但里面读不到输入框和消息列表 —— 无障碍树是空壳",
                   ctx={"窗口": self.qq.dialog_title or "(无标题)",
                        "类名": _cls(self.qq.win),
                        "消息列表": self.qq.ml_list is not None,
                        "输入框": self.qq.editor is not None,
                        "窗口可见": _visible(self.qq.win)})
            return 2

        log("INFO", f"已附着 QQ 窗口，标题={self.qq.dialog_title!r}，群聊={self.qq.is_group}")
        if _desktop_locked():
            report("E-ENV-006", "桌面处于锁屏状态，只能读不能发", ctx={"前台": _foreground_title()})
        elif not _is_foreground(self.qq.hwnd):
            log("WARN", f"QQ 当前不是前台窗口（前台={_foreground_title()!r}）："
                        f"切会话走的是坐标点击，必须先把它抬到最上层抢一次焦点；"
                        f"发完会还给你原来的窗口（uia.restore_foreground=true 时）。")
        if self.qq.ml_list is None:
            report("E-QQ-007", "没定位到消息列表控件，读取可能拿不到内容",
                   ctx={"考虑": "启用 OCR 兜底（uia.read_chain）"})

        self.scope = self.current_scope()
        # ---- 恢复上次没做完的待回复（重启不丢消息）----
        # 必须在建基线**之前**：基线会把当前可见消息全标成已读，
        # 而恢复出来的待发项要靠自己记着的上下文/回复继续兑现。
        self._load_pending()
        n = self.qq.baseline(scope=self.scope)
        self._rot().mark_baselined(self.scope)      # 当前会话的基线建立过了，记下来
        self._rot().save()
        log("INFO", f"基线建立完成，忽略已有 {n} 条历史消息")
        log("INFO", f"当前会话 {self.scope}（上下文 {len(self.store.history(self.scope))} 条，"
                    f"连续对话={self._continuous_now(self.scope)}）")

        ident = self._identity.get(self.scope) or {}
        if ident.get("uin"):
            log("INFO", f"会话身份已锁定：{ident.get('display_name')!r} → QQ {ident['uin']}"
                        f"（发送前按 QQ 号复核）")
        else:
            report("E-SEND-007", "当前会话还没有 QQ 号，发送前只能按昵称复核",
                   ctx={"会话": self.scope, "建议": "跑一次「给所有会话取号」"})

        b = self.queue.budget.snapshot()
        log("INFO", f"风控预算：{b['per_minute']} 条/分，硬间隔 {b['min_interval']:.1f}s "
                    f"｜排队合并阈值 {self.queue.merge_threshold:.1f}s"
                    f"｜静默窗硬上限 {self.queue.max_hold:.0f}s")
        disc = self.cfg.get("discovery") or {}
        if disc.get("enabled"):
            log("INFO", f"发现已开启：每 {disc.get('scan_interval_seconds', 2.0)}s 扫一次会话列表，"
                        f"重{disc.get('trigger_on_unread', True) and '点' or '-'}"
                        f"/{'摘要' if disc.get('trigger_on_preview_change', True) else '-'}触发"
                        f"→ 占位排队 → 出队时做一次上下文总读取")
        else:
            log("WARN", "发现已关闭：只会服务「当前打开的那一个会话」")
        log("INFO", f"进入循环（poll={self.cfg['chat'].get('poll_interval_seconds')}s，"
                    f"防抖={'开' if self.deb.enabled else '关'}，"
                    f"基础等待={self.deb.summary(self.scope)}）")
        if STOP_PATH:
            log("INFO", f"停止方式：Ctrl+C，或由界面写入停止哨兵 {STOP_PATH}")
        else:
            log("INFO", "按 Ctrl+C 退出")

        interval = float(self.cfg["chat"].get("poll_interval_seconds") or 0.8)
        started = time.time()
        round_start = time.time()
        while True:
            try:
                set_phase("读消息", 20, round_started=round_start)
                self.step()
                self._report_round()    # 「读到但全被跳过」必须留痕，不能静默
                set_phase("结算缓冲", 15)
                self.flush()
                set_phase("扫会话列表", 30)
                self.discover()         # 扫会话列表：谁有新消息就占位排队（不碰前台）
                set_phase("队列服务", 30)
                self.serve_queue()      # 读+生成 → 排到就发
                set_phase("落盘", 15)
                self.store.save()
                round_cost = round(time.time() - round_start, 2)
                round_start = time.time()
                set_phase("空闲", interval + 15)
                heartbeat(              # 供 exe 壳的 WebUI 展示实时状态
                    uptime=round(time.time() - started, 1),
                    scope=self.scope,
                    title=self.qq.dialog_title,
                    is_group=bool(self.qq.is_group),
                    queue_len=len(self.queue),
                    pending=len(self.deb.pending),
                    scans=self._scan_stats["scans"],
                    hits=self._scan_stats["hits"],
                    enqueued=self._scan_stats["enqueued"],
                    read=self._round.get("read", 0),
                    fresh=self._round.get("fresh", 0),
                    skipped=sum(self._round.get("skips", {}).values()),
                    last_round_seconds=round_cost,
                    # 「已生成但没发出去」的条数 —— VM 场景下这是最该盯的一个数：
                    # 它不为 0 就说明有已经写好的回复卡在发送环节，越久越可能丢。
                    unsent=len(self.queue.unsent()),
                    unsent_detail="；".join(
                        f"{it.display_name}×{it.fail_count}"
                        + (f"({it.last_fail})" if it.last_fail else "")
                        for it in self.queue.unsent()[:3]),
                    late_pending=sum(it.late_count for it in self.queue.items),
                )
                # 单轮耗时明显超过轮询间隔时，要能直接说清「慢在哪一步」——
                # 只说「心跳停更」会把人误导向「进程卡死了」。
                if round_cost > max(3.0, interval * 4):
                    report("E-PROC-007",
                           f"本轮耗时 {round_cost:.2f}s，远超轮询间隔 {interval:.2f}s",
                           ctx={"上一阶段": _HB.get("prev_phase"),
                                "该阶段耗时": f"{_HB.get('prev_phase_seconds')}s",
                                "模型超时上限": self.cfg["llm"].get("timeout_seconds"),
                                "本机CPU核数": os.cpu_count()})
                if stop_requested():
                    return self.shutdown("收到界面下发的停止哨兵")
                time.sleep(interval)
            except KeyboardInterrupt:
                return self.shutdown("收到退出信号")
            except Exception as exc:
                # 循环里的异常是**唯一**会被无限重复的一类：这里必须靠抑制器兜住，
                # 否则每 0.8 秒一条堆栈，几分钟就把日志刷爆、把真正的第一现场埋掉。
                report_exc(exc, "E-UIA-002", ctx={"阶段": "主循环 tick"})
                time.sleep(interval)

    def shutdown(self, reason: str) -> int:
        """
        优雅退出：把已经攒下的东西尽量处理完，再落盘返回。

        **不能直接 return** —— 退出瞬间正在防抖缓冲里的消息、以及队列里已经生成好的回复，
        丢了就意味着「对方明明发了消息，我们却永远不回」。所以这里给它们各留一个时限。
        """
        log("INFO", f"{reason}，开始收尾")
        if self.deb.pending:
            log("BUF", f"退出前把剩下的 {len(self.deb.pending)} 条结算掉")
            try:
                self.flush(force=True)
            except Exception as exc:
                report_exc(exc, "E-PROC-005", ctx={"阶段": "退出收尾：结算防抖缓冲"})
        try:
            self._drain_queue(limit_seconds=10.0)
        except Exception as exc:
            report_exc(exc, "E-PROC-005", ctx={"阶段": "退出收尾：发送剩余队列"})
        try:
            self._rot().save()
            self.store.save(force=True)
            # 待发队列也要落盘：退出时还没发出去的回复，下次启动继续发。
            # 不落这一步，「重启」就成了一条静默的丢消息路径。
            self._save_pending()
        except Exception as exc:
            report_exc(exc, "E-PATH-003", ctx={"阶段": "退出收尾：落盘上下文与待发队列"})
        if self.queue.unsent():
            report("E-SEND-016",
                   f"退出时仍有 {len(self.queue.unsent())} 条已生成但没发出去的回复",
                   ctx={"清单": "；".join(f"{it.display_name}（失败 {it.fail_count} 次"
                                          f"{'，' + it.last_fail if it.last_fail else ''}）"
                                          for it in self.queue.unsent()[:5]),
                        "已落盘": self._pending_path(),
                        "下次启动会自动继续发送": "是"})
        log("INFO", "已退出")
        return 0

    def _drain_queue(self, limit_seconds: float = 10.0) -> int:
        """退出前的收尾：在时限内把队列尽量发完（仍然遵守风控，不破例）。"""
        if not self.queue:
            return 0
        log("QUE", f"收尾：队列还剩 {len(self.queue)} 项，最多再等 {limit_seconds:.0f}s")
        deadline = time.time() + limit_seconds
        sent = 0
        while self.queue and time.time() < deadline:
            if self.serve_queue():
                sent += 1
                continue
            if self.queue.pick() is None and not any(
                    it.ready_at <= time.time() for it in self.queue.items):
                break          # 剩下的都还没到点，等也没用
            time.sleep(0.4)
        if self.queue:
            log("WARN", f"队列仍有 {len(self.queue)} 项未发出：{self.queue.snapshot()}")
        return sent


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
        print(f"    [X] {diag_line('E-LLM-001')}")
        for f in EC.get("E-LLM-001")["fixes"]:
            print(f"        → {f}")
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
        except EC.AppError as exc:
            # 把错误码、判据、动作一起打出来 —— 这三样缺任何一样，
            # 用户都会退回到「把整段日志发给我」的循环里。
            print(f"    [X] {diag_line(exc.code)}")
            print(f"        详情　：{exc.detail_text}")
            for c in EC.get(exc.code)["causes"]:
                print(f"        可能　：{c}")
            for f in EC.get(exc.code)["fixes"]:
                print(f"        怎么办：{f}")
        except Exception as exc:
            code = EC.wrap(exc, "E-LLM-008").code
            print(f"    [X] {diag_line(code)}")
            print(f"        {type(exc).__name__}: {exc}")

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

    print("\n[6] 已移植的文本能力（小清澈3.0.js → PC 端）")
    a, ct, ps = cfg["aggregate"], cfg["continuous"], cfg["persist"]
    print(f"    防抖聚合   : {'开' if a.get('enabled') else '关'}"
          f"（基础等待 {float(a.get('base_wait_ms') or 0) / 1000:.1f}s"
          f"，连续 {int(a.get('fast_after_single_rounds') or 0)} 轮单条后降到 "
          f"{float(a.get('fast_wait_ms') or 0) / 1000:.1f}s"
          f"，冷启动惩罚 {float(a.get('cold_start_penalty_ms') or 0) / 1000:.1f}s）")
    print(f"    连续对话   : {'开' if ct.get('enabled') else '关'}"
          f"（超时自动退出 {float(ct.get('timeout_seconds') or 0):.0f}s）")
    print(f"    上下文持久 : {'开' if ps.get('enabled') else '关'} → {abs_here(ps.get('file') or '')}")
    store = ConversationStore(cfg)
    if store.enabled:
        names = store.scopes()
        print(f"    已存会话   : {len(names)} 个" + (f"（{', '.join(names[:5])}）" if names else ""))
    print(f"    多媒体     : 图片识别 / URL 读取 → 继续悬置（按当前要求不移植）")

    print("\n[7] 会话身份与排队风控")
    q = cfg.get("queue") or {}
    ident = cfg.get("identity") or {}
    print(f"    会话主键   : QQ 号（identity.store = {ident.get('store')}）")
    print(f"    排队       : {'开' if q.get('enabled', True) else '关'}"
          f"｜风控 {q.get('max_replies_per_minute')} 条/分"
          f"｜硬间隔 {q.get('min_interval_seconds')}s")
    print(f"    补充消息   : 剩余等待 > {q.get('merge_if_wait_over_seconds')}s 则并入待发上下文，"
          f"否则另排一次回复")
    print(f"    静默窗上限 : 顺延不超过 {q.get('max_hold_seconds')}s"
          f"｜到期抖动 ±{q.get('jitter_seconds')}s")
    try:
        import qqid
        store_path = ident.get("store") or qqid.DEFAULT_STORE
        if not os.path.isabs(store_path):
            store_path = os.path.join(HERE, store_path)
        st = qqid.UidStore(store_path)
        print(f"    已取号缓存 : {len(st.map)} 个会话 → {store_path}")
        conf = st.conflicts()
        if conf:
            print(f"    [!] 重号告警 : {conf}")
    except Exception as exc:
        print(f"    [i] 取号缓存读取跳过：{exc}")
    print(f"    [i] 取号/查看：python qqid.py --list ｜ --enroll-all ｜ --check")
    print(f"    [i] 排队单测：python reply_queue.py --selftest")

    print("\n[8] 错误码目录")
    problems = EC.audit()
    n_codes = len(getattr(EC, "CATALOG", {}))
    if problems:
        print(f"    [!] 目录有 {len(problems)} 个问题（报错本身可能失真）：")
        for p in problems[:8]:
            print(f"        - {p}")
    else:
        print(f"    [✓] {n_codes} 条错误码，目录自检通过")
    print(f"    [i] 遇到带码的报错时，用「诊断报告」导出完整现场，别只截图一行")

    print("\n自检结束。")
    return 0


def show_state(cfg: dict) -> int:
    """不开 QQ，把持久化下来的会话上下文列出来。"""
    print("=" * 72)
    print("会话上下文（持久化文件内容）")
    print("=" * 72)
    store = ConversationStore(cfg)
    print(f"\n文件：{store.path}")
    print(f"开关：{'开' if store.enabled else '关'}")
    if not store.enabled:
        print("\n[!] persist.enabled=false，不会有任何持久化。")
        return 0
    names = store.scopes()
    if not names:
        print("\n(空) 还没有任何会话被记录。")
        return 0
    now = time.time()
    for scope in names:
        row = store._book[scope]
        cont = row["cont"]
        alive = cont["active"] and (now - cont["last_at"] <= float(cfg["continuous"].get("timeout_seconds") or 1800))
        print(f"\n── {scope}")
        print(f"   消息 {len(row['history'])} 条（seq={row['history'].seq}）"
              f"，连续对话={'激活' if alive else '未激活'}"
              f"，最近使用 {time.strftime('%m-%d %H:%M', time.localtime(row['used_at'])) if row['used_at'] else '—'}")
        print(row["history"].dump(6))
    return 0


def forget(cfg: dict, target: str) -> int:
    """清空某个会话的上下文。target 支持模糊匹配（命中多个时全部列出，不做删除）。"""
    store = ConversationStore(cfg)
    if not store.enabled:
        print("[X] persist.enabled=false，没有持久化内容可清。")
        return 2
    names = store.scopes()
    if not names:
        print("(空) 还没有任何会话被记录。")
        return 0

    exact = target in names
    hits = [s for s in names if exact or target in s]
    if not hits:
        print(f"[X] 没有匹配『{target}』的会话。现有会话：")
        for s in names:
            print(f"    {s}")
        return 2
    if len(hits) > 1:
        print(f"『{target}』匹配到多个会话，请写全一点：")
        for s in hits:
            print(f"    {s}")
        return 2

    scope = hits[0]
    n = len(store._book[scope]["history"])
    store.forget(scope)
    print(f"[✓] 已清空 {scope} 的上下文（{n} 条消息），连续对话状态一并重置。")
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
        diag("E-LLM-001", "没有可用的 API Key")
        return 2
    try:
        t0 = time.time()
        reply = client.chat(history.build_messages())
        print(f"\n模型回复（{time.time() - t0:.2f}s）：\n    {reply}")
    except EC.AppError as exc:
        diag(exc.code, exc.detail_text, exc.context)
        return 2
    except Exception as exc:
        diag(EC.wrap(exc, "E-LLM-008").code, f"{type(exc).__name__}: {exc}")
        return 2
    return 0


def diag(code: str, detail: str = "", ctx: dict | None = None) -> None:
    """
    把错误码的完整诊断块**打印到 stdout**。

    与 `report()` 的分工：`report()` 走日志（带抑制、给常驻循环用），
    `diag()` 直接打印（给一次性的命令行诊断用 —— 那种场合用户就是在等这一句话，
    抑制反而会让它不出现）。`qqid.py` 与几个测试脚本也复用它。
    """
    print()
    for line in EC.describe(code, detail, ctx).splitlines():
        print("  " + line)


def diag_attach(cfg: dict | None = None) -> tuple[str, dict]:
    """
    一次性回答「为什么附着不上 QQ」，返回 (错误码, 现场数据)。

    给命令行脚本用：它们通常先自己找一遍窗口，失败时想知道具体原因。
    这样 `test_pipeline.py` / `test_send_guard.py` 这类脚本不需要各自复制一份判定逻辑
    —— 判定只允许有一处实现，否则界面说一套、脚本说另一套，用户会被搞糊涂。
    """
    try:
        qq = QQWindow(cfg or load_config())
        return qq.diagnose_attach()
    except Exception as exc:
        return EC.wrap(exc, "E-UIA-002").code, {"异常": f"{type(exc).__name__}: {exc}"}


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
        code, ctx = qq.diagnose_attach()
        diag(code, "无法附着到 QQ 窗口", ctx)
        return 2
    print(f"\n[窗口] title={qq.dialog_title!r}  群聊={qq.is_group}  群人数={qq.member_count}")
    if qq.editor is None:
        diag("E-UIA-004", "找不到输入框（ExEditor-qq-msg-editor）",
                    {"消息列表": qq.ml_list is not None,
                     "窗口可见": _visible(qq.win) if qq.win else False})
        return 2
    print(f"[输入框] class={_cls(qq.editor)!r}")
    print(f"[前台]   QQ 在前台 = {qq.is_foreground()}  (QQ hwnd={qq.hwnd}, 当前前台 hwnd={_fg_hwnd()})")
    print(f"[发送按钮] 存在={qq.send_btn is not None}  写入前禁用={qq._send_disabled()}")

    print(f"\n[1] 写入文本：{text!r}")
    if not qq.type_text(text):
        # type_text 内部已经打过带码的诊断了（前台/剪贴板/输入框各自独立），
        # 这里只补一句「怎么把上面的码用起来」，不重复报同一件事。
        print("\n  ↑ 上面那条带错误码的报错就是根因；按它的「怎么办」处理。")
        return 2
    time.sleep(0.4)

    got = qq.editor_text()
    print(f"[2] 回读输入框：{got!r}")
    print(f"    与写入一致 = {got == text}")

    print(f"[3] 写入后 发送按钮禁用 = {qq._send_disabled()}")
    if not qq.wait_send_enabled(1.0):
        print(f"    [!] {diag_line('E-SEND-002')}")

    # type_text 已经把焦点还给你了，这里要清空就得再借一次前台，清完立刻归还
    prev_fg = _fg_hwnd()
    cleared = False
    if force_foreground(qq.hwnd):
        cleared = qq.clear_editor()
        time.sleep(0.4)
        restore_foreground(prev_fg)
    print(f"[4] 清空输入框 = {cleared}，发送按钮禁用 = {qq._send_disabled()}")

    ok = got == text
    if ok:
        print("\n结论：输入链路正常 ✅（以上全程未发送任何消息）")
    else:
        print("\n结论：输入链路异常 ❌")
        diag("E-SEND-003",
                    "写入的内容没能原样出现在输入框里",
                    {"期望": clip(text, 40), "实际": clip(got, 40), "清空成功": cleared})
    return 0 if ok else 1


# ============================================================ entry
def peek(cfg: dict, limit: int = 12) -> int:
    """只读诊断：把 QQWindow 解析出来的消息原样打印，用来验证读取层是否正确。"""
    print("=" * 72)
    print("只读诊断：附着 QQ 并解析最近消息（不发送任何内容）")
    print("=" * 72)

    qq = QQWindow(cfg)
    if not qq.attach():
        code, ctx = qq.diagnose_attach()
        diag(code, "无法附着到 QQ 窗口", ctx)
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
        # 「没读到消息」有三种完全不同的原因，这里按**已有的现场数据**分流：
        # 锚点缺失说明无障碍树整体没暴露；锚点齐全说明只是当前会话是空的。
        if qq.ml_list is None or qq.editor is None:
            diag("E-QQ-004", "锚点定位失败，无障碍树像是空壳",
                        {"消息列表": qq.ml_list is not None,
                         "输入框": qq.editor is not None,
                         "窗口": _cls(qq.win) if qq.win else "(无)"})
        else:
            diag("E-QQ-007", "锚点都在，但消息区里没有消息条目",
                        {"标题": qq.dialog_title or "(空)",
                         "建议": "确认 QQ 停在某个**有聊天记录**的会话上，而不是空的会话或设置页"})
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


def watch(cfg: dict, seconds: float = 60.0, interval: float = 1.0,
          limit: int = 12) -> int:
    """
    实时读取监视器：**专门回答「新消息到底有没有被读到」**。只读，绝不发送。

    ## 为什么需要这个模式

    排「当前会话收不到新消息」时，靠看常驻日志是没用的：日志里只有
    「读到并收下」的痕迹，而**读不到**和**读到了但被跳过**在日志上几乎一样。
    这个模式把读取这一层单独拎出来，每一轮都报：
    读到几条、其中哪几条是新增的、每条的方向判定是什么、key 长什么样。

    这样「读不到」和「读到了但没送进上下文」就能被彻底分开 ——
    前者是本模式的输出里根本没有新条目，后者是本模式能看到、而常驻日志里被 SKIP 掉了。

    ## 怎么用

    启动后照着提示，**去 QQ 里给这个会话发一条消息**。1 秒内应该看到它被报出来。
    """

    print("=" * 74)
    print("实时读取监视器（只读，绝不发送任何内容）")
    print("=" * 74)
    print(f"时长 {seconds:.0f}s　轮询间隔 {interval:.1f}s　每轮读 {limit} 条")

    qq = QQWindow(cfg)
    if not qq.attach():
        code, ctx = qq.diagnose_attach()
        diag(code, "无法附着到 QQ 窗口", ctx)
        return 2

    print(f"\n[窗口] title={qq.dialog_title!r}  群聊={qq.is_group}  群人数={qq.member_count}")
    print(f"[锚点] 消息列表={'OK' if qq.ml_list is not None else '缺失'}"
          f"  输入框={'OK' if qq.editor is not None else '缺失'}"
          f"  发送按钮={'OK' if qq.send_btn is not None else '缺失'}")
    print(f"[账号] 自动识别的自己昵称 = {qq.self_nickname!r}"
          f"　（方向判定的第二依据）")
    print(f"[方向] direction_mode = {cfg['uia'].get('direction_mode')!r}"
          f"　read_chain = {cfg['uia'].get('read_chain')!r}")
    if qq.ml_list is None:
        diag("E-QQ-004", "消息列表锚点都找不到，读取不可能有结果",
             {"窗口": _cls(qq.win) if qq.win else "(无)"})
        return 2

    try:
        import qqid
        sessions = qqid.list_sessions(qq.win)
        cur = qq.title_now()
        row = next((s for s in sessions if s.display_name == cur), None)
        print(f"[会话] 当前标题 {cur!r}　会话列表共 {len(sessions)} 条"
              f"　该项未读数 = {row.unread if row else '未匹配到'}")
    except Exception as exc:
        print(f"[会话] 会话列表读取失败：{type(exc).__name__}: {exc}")
        qqid = None
        row = None

    def _preview() -> tuple:
        """
        会话列表里「当前会话」这一行的（摘要, 未读数）。

        这是**另一条独立的读取通路**：消息区读的是 `ml-list` 的子树，
        这里读的是左栏会话列表项。两者都来自同一棵 UIA 树，但由 Chromium
        在不同时机更新 —— 正是这一点让它们能互为对照（见文件末尾的判定）。
        """
        if qqid is None:
            return ("", -1)
        try:
            cur = qq.title_now()
            for s in qqid.list_sessions(qq.win):
                if s.display_name == cur:
                    return (s.summary, s.unread)
        except Exception:
            pass
        return ("", -1)

    preview0 = _preview()

    print("\n" + "-" * 74)
    print("正在建立读取基线（记录当前可见消息的 key）…")
    qq.refresh_layout(force=True)
    base = qq.read_messages(limit=limit)
    seen = {m.key for m in base}
    print(f"已记录 {len(base)} 条现有消息。")
    for m in base:
        print(f"  【{'我方' if m.direction == 'me' else '对方' if m.direction == 'other' else '未知'}】"
              f"{(m.sender or '(无昵称)')[:16]:<16} key={m.key[:34]:<34} {clip(m.content, 34)}")
    if not base:
        diag("E-QQ-007", "锚点在，但一条消息条目都没解析出来",
             {"当前标题": qq.title_now() or "(空)",
              "建议": "确认 QQ 停在**有聊天记录**的会话上，而不是空会话或设置页"})
    print("-" * 74)
    print(">>> 现在请去 QQ 里给**这个会话**发一条消息（脚本不会回复、不会发送任何东西）")
    print(">>> 如果 5 秒内没看到它被报出来，就说明读取这一层有问题。\n")

    deadline = time.time() + seconds
    rounds = 0
    new_total = 0
    anchor_lost = 0
    key_changed = []
    preview_changed = 0
    last_keys: dict = {m.content: m.key for m in base}
    silent_rounds = 0
    while time.time() < deadline:
        time.sleep(interval)
        rounds += 1
        try:
            qq.refresh_layout()
        except Exception as exc:
            report_exc(exc, "E-UIA-002", ctx={"阶段": "watch: refresh_layout"})
        if qq.ml_list is None:
            anchor_lost += 1
            qq.refresh_layout(force=True)      # 容器可能被整体替换了，强制重扫
            if qq.ml_list is None:
                print(f"  [第{rounds}轮] 消息列表锚点丢了，强制重扫也没找回来")
                continue
        try:
            msgs = qq.read_messages(limit=limit)
        except Exception as exc:
            report_exc(exc, "E-UIA-002", ctx={"阶段": "watch: read_messages"})
            continue

        new = [m for m in msgs if m.key not in seen]

        # ---- 交叉验证：会话列表那一行有没有变 ----
        # 每 5 轮查一次（读整棵列表有点贵）。摘要/未读变了但消息区没有新条目，
        # 说明**消息区的无障碍树是陈旧的**，而左栏还在更新 —— 这能直接把
        # 「读不到」和「对方没发」分开，单看消息列表永远分不出来。
        if rounds % 5 == 0:
            pv = _preview()
            if pv != preview0 and pv[1] >= 0:
                preview_changed += 1
                if not new:
                    print(f"  [第{rounds}轮] ⚠ 会话列表已更新（摘要 {preview0[0][:14]!r}→{pv[0][:14]!r}，"
                          f"未读 {preview0[1]}→{pv[1]}），但消息区**没有**读到新条目")
                preview0 = pv

        # key 稳定性检查：同一段正文如果换出了不同的 key，
        # 说明 key 落到了「发送者+正文+序号」的指纹兜底上（AutomationId 缺失），
        # 那会让「重复发同样的话」被误判成同一条 —— 静默丢消息的经典成因。
        for m in msgs:
            prev = last_keys.get(m.content)
            if prev and prev != m.key and m.content:
                key_changed.append((clip(m.content, 20), prev[:22], m.key[:22]))
            if m.content:
                last_keys[m.content] = m.key

        if new:
            new_total += len(new)
            silent_rounds = 0
            print(f"  [第{rounds}轮] ★ 新增 {len(new)} 条"
                  f"（本轮共读 {len(msgs)} 条）")
            for m in new:
                mark = {"me": "我方", "other": "对方"}.get(m.direction, "未知")
                print(f"      【{mark}】{(m.sender or '(无昵称)')[:16]:<16} "
                      f"key={m.key[:34]:<34} {clip(m.content, 40)}")
                # 只做方向与正文的静态提示，不写上下文、不改任何状态
                if m.direction != "other":
                    print(f"      ⚠ 方向是『{mark}』→ 常驻会**跳过**这一条。"
                          f"如果它其实是对方发的，就是方向判反了。")
                elif not m.content:
                    print("      ⚠ 解析不出正文 → 常驻会跳过这一条")
            seen.update(m.key for m in msgs)
        else:
            silent_rounds += 1
            # 每 10 轮打一个点，证明脚本还活着（否则用户分不清"没消息"和"卡死了"）
            if silent_rounds % 10 == 0:
                print(f"  [第{rounds}轮] 无新增（本轮读 {len(msgs)} 条，"
                      f"与基线一致）… 已运行 {rounds * interval:.0f}s")

    print("\n" + "=" * 74)
    print("结论")
    print("=" * 74)
    print(f"  轮询 {rounds} 轮（约 {rounds * interval:.0f}s）")
    print(f"  每轮读到的条数：约 {len(base)} 条（与基线{'一致' if not anchor_lost else '不一致'}）")
    print(f"  期间检测到的新增消息：{new_total} 条")
    if anchor_lost:
        print(f"  ⚠ 有 {anchor_lost} 轮消息列表锚点丢失（已自动重扫）")
    if preview_changed and new_total == 0:
        print(f"\n  ⚠ **关键证据**：会话列表那一行变了 {preview_changed} 次，"
              f"但消息区一次都没读到新条目。")
        print("      说明左栏（会话列表）的无障碍树在更新，而消息区的没更新 ——")
        print("      典型成因：QQ 窗口被最小化/被完全遮挡/虚拟机控制台不在前台，")
        print("      Chromium 节流了渲染，消息区的 a11y 树被冻结。")
        print("      处理：让 QQ 窗口**真的可见**（别最小化、别被别的窗口盖住），")
        print("      在虚拟机里保持控制台窗口处于活动状态；再重跑一次本监视器。")
    if key_changed:
        print(f"\n  ⚠ 检测到 {len(key_changed)} 处 **key 不稳定**：同一段正文换出了不同的 key")
        for content, k1, k2 in key_changed[:5]:
            print(f"      {content!r}: {k1} → {k2}")
        print("      这说明消息条目的 AutomationId 读不到，key 退化成「发送者+正文+序号」指纹。")
        print("      后果：重复发同一句话会被当成同一条而**静默丢弃**。")
        print("      处理：把「消息去重依据」改成基于 AutomationId 的方案，或避免重复发完全相同的内容。")
    if new_total == 0:
        print("\n  期间没有检测到任何新增消息。两种情况：")
        print("    a) 你确实没有在 QQ 里发消息 → 这次测试没有结论，重跑一次并在这 60 秒内发一条")
        print("    b) 你发了，但这里什么都没显示 → **读取这一层有问题**，接着往下看：")
        print("       · 方向是否判反：上面新增条目要是显示【我方】，常驻就会跳过它")
        print("       · 窗口是否被最小化：读取一般还行，但某些操作会失效")
        print("       · 换一个会话试试：如果换个会话就能读到，说明与「当前会话」这一状态有关")
    else:
        print("\n  ✅ 读取这一层是通的：新消息能被读到。")
        print("     那么「没回应」的原因在后面几层 —— 按常驻日志里的错误码看：")
        print("       E-READ-001 读到了但全被跳过 ／ E-LLM-* 模型侧 ／ E-SEND-* 发送侧")
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
        code, ctx = qq.diagnose_attach()
        diag(code, "无法附着到 QQ 窗口", ctx)
        return 2

    print(f"\n[窗口] title={qq.dialog_title!r}  群聊={qq.is_group}  群人数={qq.member_count}")
    if qq.is_group and cfg["chat"].get("private_chat_only") and not force:
        print("\n[!] config 里 private_chat_only=true，已拒绝在群聊里发送。")
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
    else:
        # send_text 内部每一处失败都已经带了独立的码（前台/剪贴板/按钮/回读），
        # 这里只说清「返回 False ≠ 发了又失败」，避免用户以为消息已经发出去一半了。
        print("\n  ↑ 上面那条带错误码的报错就是根因。")
        print("    本条**没有发出任何内容**（失败都发生在按下发送之前，输入框已被清空）。")
    return 0 if ok else 1


def _rot_path(cfg: dict) -> str:
    """轮转状态文件路径（相对路径按脚本所在目录解析）。"""
    path = (cfg.get("discovery") or {}).get("state_file") or "state/rotation.json"
    return path if os.path.isabs(path) else os.path.join(HERE, path)


def scan_sessions(cfg: dict) -> int:
    """
    只读诊断：打印「发现」视图 —— 会话列表 / 未读徽标 / 指纹与快照的差异。

    **完全不点击、不切换会话、不发送。** 想看切换行为请用 `--no-send` 彩排。
    """
    import qqid
    desc, hwnd = qqid.find_qq_main_window()
    if not hwnd:
        # 走与常驻完全相同的分流逻辑：QQ 没启动 / 窗口不可见 / 无障碍没生效
        probe = QQWindow(cfg)
        code, ctx = probe.diagnose_attach()
        diag(code, "没找到可用的 QQ 主窗口", ctx)
        return 2
    win = control_from_hwnd(hwnd)
    try:
        cards = qqid.list_sessions(win)
    except Exception as exc:
        diag(EC.wrap(exc, "E-UIA-002").code, "读会话列表失败",
                    {"异常": f"{type(exc).__name__}: {exc}"})
        return 1
    if not cards:
        diag("E-QQ-007", "会话列表为空",
                    {"建议": "确认 QQ 当前停在「消息」标签页（不是联系人/设置页）",
                     "无障碍": "若刚重启过 QQ，稍等几秒让列表渲染完"})
        return 1

    cur = qqid.header_title(win)
    rot = RotationState(_rot_path(cfg))
    d = cfg.get("discovery") or {}
    print("=" * 90)
    print("发现视图（只读：不点击、不切会话、不发送）")
    print("=" * 90)
    print(f"发现开关={d.get('enabled')}  扫描间隔={d.get('scan_interval_seconds')}s  "
          f"单轮上限={d.get('max_enqueue_per_scan')}  "
          f"红点触发={d.get('trigger_on_unread')}  摘要触发={d.get('trigger_on_preview_change')}")
    print(f"当前打开 = {cur!r}（发现会跳过它，交给实时路径）")
    print(f"状态文件 = {_rot_path(cfg)}")
    print(f"  已建基线 {len(rot.baselined)} 个：{sorted(rot.baselined)[:6]}"
          f"{' …' if len(rot.baselined) > 6 else ''}")
    print("-" * 90)
    print(f"{'#':>2}  {'会话':<28}{'未读':>4} {'类型':>4}  {'判断'}")
    for s in cards:
        old = rot.fp_of(s.key)
        fp = s.fingerprint()
        if cur and s.display_name == cur:
            verdict = "当前打开 → 跳过（实时路径负责）"
        elif old is None:
            verdict = "首见 → 只记快照，本轮不动作"
        elif old == fp:
            verdict = "无变化"
        elif s.unread > 0:
            verdict = f"★红点触发（未读 {old[1]} → {fp[1]}）"
        else:
            verdict = "摘要变化触发（兜底）"
        print(f"{s.index:>2}  {s.display_name[:28]:<28}{s.unread:>4} "
              f"{'群聊' if s.looks_group else '私聊':>4}  {verdict}")
    print("-" * 90)
    print("提示：首见会话只记快照不动作，所以**第一次跑起来时不会立刻回历史未读**；")
    print("     之后再来的新消息才会触发。这也正是重启后要保留指纹快照的原因。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="PC 端 QQ 私聊 AI 代理（路线A 最小 demo）")
    ap.add_argument("--selftest", action="store_true", help="不开 QQ，自检配置与模型连通性")
    ap.add_argument("--replay", metavar="TEXT", help="不开 QQ，回放一条消息看模型会怎么回")
    ap.add_argument("--peek", type=int, nargs="?", const=12, default=None, metavar="N",
                    help="只读诊断：解析并打印最近 N 条消息（默认 12），不发送任何内容")
    ap.add_argument("--sessions", action="store_true",
                    help="只读诊断：打印「发现」视图（会话列表/未读徽标/指纹变化），不点击不发送")
    ap.add_argument("--input-test", nargs="?", const="输入框测试文本 ABC123，不会发送",
                    default=None, metavar="TEXT",
                    help="只验证能不能把文字写进输入框：写入→回读→清空，绝不发送")
    ap.add_argument("--watch", type=float, nargs="?", const=60.0, default=None, metavar="SEC",
                    help="实时读取监视器（只读）：盯着当前会话，报出读到的每条新消息"
                         "及其方向判定。用来判定「新消息到底有没有被读到」。默认 60 秒")
    ap.add_argument("--send", metavar="TEXT", help="向当前会话发一条文本（会真的发出去！）")
    ap.add_argument("--state", action="store_true", help="不开 QQ，查看持久化下来的会话上下文")
    ap.add_argument("--forget", metavar="SCOPE", help="不开 QQ，清空某个会话的上下文（支持模糊匹配）")
    ap.add_argument("--force", action="store_true", help="与 --send 配合，允许在群聊里发送")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出（调试用；会跳过防抖直接结算）")
    ap.add_argument("--dry-run", action="store_true", help="只读不发，确认读取是否准确")
    ap.add_argument("--no-send", action="store_true",
                    help="彩排：走完整链路（切会话 → 三重复核 → 生成回复），但最后一步不真的发出")
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
    if args.sessions:
        return scan_sessions(cfg)
    if args.input_test is not None:
        return input_test(cfg, args.input_test)
    if args.watch is not None:
        return watch(cfg, args.watch)
    if args.send is not None:
        return send_once(cfg, args.send, force=args.force)
    if args.state:
        return show_state(cfg)
    if args.forget is not None:
        return forget(cfg, args.forget)

    agent = Agent(cfg, dry_run=args.dry_run, no_send=args.no_send)
    if args.once:
        if not agent.qq.attach():
            code, ctx = agent.qq.diagnose_attach()
            report(code, "无法附着到 QQ 窗口", ctx=ctx)
            return 2
        agent.scope = agent.current_scope()
        kept = agent.qq.baseline(skip_last=1)
        log("INFO", f"已附着（{agent.qq.dialog_title!r}，群聊={agent.qq.is_group}）")
        log("INFO", f"忽略 {kept} 条历史，只把最新 1 条当作新消息（调试模式：跳过防抖，立即结算）")
        log("INFO", f"当前会话 {agent.scope}，上下文 {len(agent.store.history(agent.scope))} 条")
        if not agent.dry_run and not agent.no_send:
            log("WARN", "未加 --dry-run / --no-send，会真的回复并发送！建议先加 --no-send 彩排")
        handled = agent.step()
        log("INFO", f"本轮入队 {handled} 条")
        agent.flush(force=True)
        agent.serve_queue()
        agent.store.save(force=True)
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
