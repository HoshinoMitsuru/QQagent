# -*- coding: utf-8 -*-
"""
settings.py —— 配置的三文件模型

刻意拆成三个文件，各管一件事，谁都不越界：

    config.json        agent.py 的全部行为参数（结构必须和 agent.DEFAULTS 对齐）
    secrets.local.json API key。单独放，因为它不该进仓库、也不该出现在截图里
    app-settings.json  exe 壳自己的设置（QQ 路径、端口、自启…）。agent.py 根本不读它

为什么 key 不写进 config.json：那个文件的历史约定就是「可以公开」，
真把 key 写进去，迟早被谁复制粘贴出去。UI 里输入 key 只会落到 secrets.local.json。

保存时用「读原文件 → 只覆盖我们管的那些路径 → 写回」的策略，而不是整体重写：
config.json 里那些 `_说明` 注释是给人看的，整体重写会把它们全部抹掉。
"""

from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any

from . import errors as E
from . import paths

# ---------------------------------------------------------------- 字段声明
# type: text | password | number | bool | select | textarea | tags
# secret=True 的字段写入 secrets.local.json，其余写入 config.json


def _f(path: str, label: str, ftype: str, group: str, default: Any,
       hint: str = "", *, secret: bool = False, advanced: bool = False,
       options: list | None = None, step: float | None = None,
       minimum: float | None = None, maximum: float | None = None,
       placeholder: str | None = None) -> dict:
    return {"path": path, "label": label, "type": ftype, "group": group,
            "default": default, "hint": hint, "secret": secret, "advanced": advanced,
            "options": options, "step": step, "minimum": minimum, "maximum": maximum,
            "placeholder": placeholder}


GROUPS = [
    {"id": "model", "title": "模型接入", "desc": "OpenAI 兼容接口。绝大多数情况只需要填 API Key。"},
    {"id": "behavior", "title": "对话行为", "desc": "谁会被回、回多长、以及非文本消息怎么处理。"},
    {"id": "rhythm", "title": "节奏与聚合", "desc": "对方连发多条时合并成一次调用，避免刷屏。"},
    {"id": "commands", "title": "对话内指令", "desc": "在 QQ 里以 .ai 开头下指令。指令本身不会转发给模型，也不会写进上下文。"},
    {"id": "risk", "title": "风控与排队", "desc": "决定能同时服务几个会话。改大之前先读并发评估文档。"},
    {"id": "discovery", "title": "多会话发现", "desc": "扫会话列表找新消息。这是「降级方案」的核心开关。"},
    {"id": "foreground", "title": "前台与稳定性", "desc": "关于抢前台、OCR 兜底、方向判定的取舍。"},
]

FIELDS: list[dict] = [
    # -------------------------------------------------- 模型接入
    _f("llm.api_base", "接口地址", "text", "model", "https://api.deepseek.com",
       "OpenAI 兼容的 base_url。DeepSeek 官方是 https://api.deepseek.com，不要带 /v1 后缀之外的路径。"),
    _f("llm.api_key", "API Key", "password", "model", "",
       "只保存在 secrets.local.json，不进仓库。", secret=True),
    _f("llm.model", "模型名", "text", "model", "deepseek-flash",
       "如 deepseek-flash / deepseek-chat / gpt-4o-mini / qwen-plus。"),
    _f("llm.temperature", "温度", "number", "model", 1.1,
       "0~2。陪伴场景 1.0~1.2 比较自然；越低越稳越像客服。", step=0.1, minimum=0, maximum=2),
    _f("llm.max_tokens", "单次最大长度", "number", "model", 800,
       "单位是 token，中文大约 1 字 ≈ 1 token。", step=50, minimum=64, maximum=8192),
    _f("llm.timeout_seconds", "超时（秒）", "number", "model", 60,
       "超过这个时间就放弃本次调用。", step=5, minimum=5, maximum=300),
    _f("llm.system_prompt", "人格设定", "textarea", "model",
       "你是『小清澈』，一个温和、真诚、有点黏人的 AI 陪伴者。说话口语化、简短，一次回复不超过三句话。"
       "不要用列表和标题，不要像客服一样客套。",
       "每次请求都会带上。写清楚「说话风格 + 长度约束」比写人设背景更有效。"),

    # -------------------------------------------------- 对话行为
    _f("chat.private_chat_only", "只回私聊", "bool", "behavior", False,
       "打开后群聊一律跳过。"),
    _f("chat.group_requires_trigger", "群聊需要触发词", "bool", "behavior", False,
       "打开后群聊只有喊到名字才回。建议开启，否则群聊会自由发言。"),
    _f("chat.trigger_prefixes", "触发词", "tags", "behavior", ["小清澈", "清澈"],
       "回车分隔。私聊里也用于「调教」：以 `小清澈：` 开头的内容会直接当 AI 发言写入上下文。"),
    _f("chat.always_reply", "私聊总是回应", "bool", "behavior", True,
       "关闭后私聊也只认触发词。"),
    _f("chat.nontext_policy", "非文本消息", "select", "behavior", "skip",
       "图片/语音/文件/动画表情怎么处理。skip 一律跳过（最省额度）；"
       "describe 把气泡占位文本（如 [动画表情]）当正文交给 AI。",
       options=[{"value": "skip", "label": "skip — 一律跳过"},
                {"value": "describe", "label": "describe — 交给 AI 判断"}]),
    _f("chat.max_reply_chars", "回复字数上限", "number", "behavior", 500,
       "超出会被硬截断。", step=50, minimum=50, maximum=4000),
    _f("chat.self_nickname", "我的昵称", "text", "behavior", "",
       "留空就自动从 QQ 顶栏个人卡片识别。识别错了才需要手填。", advanced=True),
    _f("chat.poll_interval_seconds", "轮询间隔（秒）", "number", "behavior", 0.8,
       "每轮读一次消息列表。调大省 CPU，调小更跟手。", step=0.1, minimum=0.2, maximum=5, advanced=True),
    _f("chat.keep_draft_on_abort", "中止时保留草稿", "bool", "behavior", True,
       "发送失败时**不删掉**已经粘进输入框的那段回复，队列会带着同一条继续重试。"
       "关掉它就恢复旧行为（删掉草稿），代价是丢掉一条已生成的回复、"
       "那批消息可能因此永远没有得到回应。"),
    _f("chat.send_button_wait_seconds", "发送按钮等待（秒）", "number", "behavior", 3.0,
       "粘贴之后等 QQ 把发送按钮恢复可用的时间。慢速虚拟机（vCPU 少、无 GPU 加速）上"
       "这个同步经常超过 1 秒，设太小会反复判成「文本没进输入框」→ 中止并清理输入框，"
       "表现就是「它粘了字又回来删掉」。", step=0.5, minimum=0.5, maximum=15, advanced=True),

    # -------------------------------------------------- 节奏与聚合
    _f("aggregate.enabled", "启用防抖聚合", "bool", "rhythm", True,
       "对方连发三条时合并成一次模型调用，只回一次。"),
    _f("aggregate.base_wait_ms", "基础等待（毫秒）", "number", "rhythm", 5000,
       "收到消息后先等这么久，看对方还发不发。", step=500, minimum=500, maximum=30000),
    _f("aggregate.fast_wait_ms", "快速档等待（毫秒）", "number", "rhythm", 2000,
       "连续多轮都只发一条时会自动降到这个值，反应更快。", step=500, minimum=200, maximum=30000),
    _f("aggregate.fast_after_single_rounds", "降档所需轮数", "number", "rhythm", 3,
       "连续这么多轮只收到单条就切快速档。", step=1, minimum=1, maximum=20, advanced=True),
    _f("aggregate.cold_start_penalty_ms", "冷启动惩罚（毫秒）", "number", "rhythm", 6000,
       "隔很久没聊过天的会话，第一轮多等一会儿。", step=500, minimum=0, maximum=60000, advanced=True),
    _f("continuous.enabled", "启用连续对话", "bool", "rhythm", True,
       "AI 在某个会话开口后，一段时间内该会话免触发词。"),
    _f("continuous.timeout_seconds", "连续对话时长（秒）", "number", "rhythm", 1800,
       "30 分钟没有来回就退出连续状态。", step=60, minimum=30, maximum=86400),

    # -------------------------------------------------- 对话内指令
    _f("commands.enabled", "启用对话内指令", "bool", "commands", True,
       "关掉之后 `.ai reset` 之类会被当成普通消息交给模型。"),
    _f("commands.accept_from", "谁可以下指令", "tags", "commands", ["other", "self"],
       "other = 对方（聊天对象）；self = 你自己在 QQ 里手输的。"
       "机器人自己的发言会被自动排除，不用担心自己触发自己。",
       placeholder="other"),
    _f("commands.reply_ack", "指令回执", "bool", "commands", True,
       "执行完要不要回一句确认话。关掉就静默执行。"),
    _f("commands.ack_reset", "reset 回执文案", "text", "commands",
       "小清澈读懂了你的意思，回到了房间，脱衣睡下。",
       "`.ai reset` 成功后发出去的那句话。"),
    _f("commands.reset_clears_pending", "reset 一并丢掉未结算消息", "bool", "commands", True,
       "打开后，`.ai reset` 会把还没轮到 AI 的那几条一起丢掉。"
       "关掉的话它们几秒后照常被回复，看起来就像「说了 reset 却没生效」。"),
    _f("commands.prefixes", "指令前缀", "tags", "commands",
       [".ai", "。ai", "/ai", ".aichat"],
       "回车分隔。前缀后必须跟空格或冒号，所以 `.aichat` 不会被 `.ai` 抢走。", advanced=True),

    # -------------------------------------------------- 风控与排队
    _f("queue.max_replies_per_minute", "每分钟最多回复", "number", "risk", 12,
       "个人号的真实瓶颈。默认 12 已经是安全区间中段，别轻易上调。",
       step=1, minimum=1, maximum=60),
    _f("queue.min_interval_seconds", "两次回复最小间隔（秒）", "number", "risk", 5.0,
       "与上一项同时生效，取更严格的那个。", step=0.5, minimum=0.5, maximum=120),
    _f("queue.merge_if_wait_over_seconds", "超时合并阈值（秒）", "number", "risk", 5.0,
       "等太久的会话，用户又补了一条就并进同一批，不额外回一次。",
       step=0.5, minimum=0, maximum=120, advanced=True),
    _f("queue.max_hold_seconds", "静默窗硬上限（秒）", "number", "risk", 30.0,
       "最多让用户等这么久。不设会导致「每 20 秒来一句」的用户永远结算不掉。",
       step=5, minimum=5, maximum=600, advanced=True),
    _f("queue.jitter_seconds", "抖动（秒）", "number", "risk", 3.0,
       "给到期时间加随机量，避免多个会话同时涌入发送队列。",
       step=0.5, minimum=0, maximum=30, advanced=True),
    _f("queue.max_attempts", "发送重试上限", "number", "risk", 3,
       "发送前复核对不上就重排，连续失败这么多次后放弃并撤单。",
       step=1, minimum=1, maximum=10, advanced=True),

    # -------------------------------------------------- 多会话发现
    _f("discovery.enabled", "启用多会话发现", "bool", "discovery", True,
       "关闭后只会服务「当前打开的那一个会话」。"),
    _f("discovery.scan_interval_seconds", "扫描间隔（秒）", "number", "discovery", 2.0,
       "每次扫描要走一遍控件树，几十到一两百毫秒。", step=0.5, minimum=0.5, maximum=30),
    _f("discovery.trigger_on_unread", "未读红点触发", "bool", "discovery", True,
       "最硬的证据，优先依赖它。"),
    _f("discovery.trigger_on_preview_change", "摘要变化触发", "bool", "discovery", True,
       "兜住「不显未读数字」的会话。会被自己发出的回复误触发，代码里已做抑制。"),
    _f("discovery.trigger_on_first_sight_unread", "首见未读也触发", "bool", "discovery", True,
       "第一次见到某个会话时，即使它已经带着未读徽标也照样触发。",
       advanced=True),
    _f("discovery.max_enqueue_per_scan", "单轮最多入队", "number", "discovery", 3,
       "一次扫描最多占位几个会话，防止把队列和前台预算打穿。",
       step=1, minimum=1, maximum=20),
    _f("discovery.read_limit", "总读取条数", "number", "discovery", 30,
       "切换到某个会话后往前读多少条消息作为上下文。", step=5, minimum=5, maximum=200),

    # -------------------------------------------------- 前台与稳定性
    _f("uia.restore_foreground", "用完后归还前台", "bool", "foreground", False,
       "默认**关闭**（VM 场景）：不归还就没有「抢不到前台」这类失败，发送链路更可靠。"
       "在你自己每天用的电脑上跑，打开它以免 QQ 一直霸占最上层。"),
    _f("uia.direction_mode", "消息方向判定", "select", "foreground", "auto",
       "auto 自动推断左右归属；读到的「我方/对方」反了才需要手动指定。",
       options=[{"value": "auto", "label": "auto — 自动"},
                {"value": "left", "label": "left — 左侧是对方"},
                {"value": "right", "label": "right — 右侧是对方"}]),
    _f("uia.read_chain", "读取链路", "tags", "foreground", ["uia", "ocr"],
       "按顺序尝试。uia 快但依赖无障碍树；ocr 需要额外安装 tesseract 并勾选中文包。",
       advanced=True),
]

# exe 壳自己的设置（写 app-settings.json，agent.py 不读）
APP_FIELDS: list[dict] = [
    _f("qq_exe_path", "QQ 可执行文件", "text", "app", "",
       "留空自动查找（注册表 → 常见安装目录 → 盘根）。本机是 D:\\QQ.exe 这类非标准路径时请手填。"),
    _f("qq_extra_args", "QQ 额外启动参数", "text", "app", "",
       "默认会自动带上 --force-renderer-accessibility，这里填的会追加在后面。"),
    _f("cdp_enabled", "同时开启 CDP 调试端口", "bool", "app", False,
       "给 QQ 加 --remote-debugging-port。仅监听 127.0.0.1，是将来彻底摆脱前台依赖的路。"),
    _f("cdp_port", "CDP 端口", "number", "app", 9222, "", step=1, minimum=1024, maximum=65535),
    _f("auto_launch_qq", "启动时自动拉起 QQ", "bool", "app", True,
       "只在「QQ 完全没在跑」时生效。已经登录的 QQ 不会被重启。"),
    _f("auto_start_agent", "UI 起来后自动开始常驻", "bool", "app", False,
       "适合无人值守的虚拟机：开机即接管。"),
    _f("web_host", "WebUI 监听地址", "select", "app", "127.0.0.1",
       "VM 场景想从宿主机浏览器打开，选 0.0.0.0（有令牌保护，但仍请只在可信网络使用）。",
       options=[{"value": "127.0.0.1", "label": "127.0.0.1 — 仅本机"},
                {"value": "0.0.0.0", "label": "0.0.0.0 — 允许局域网访问"}]),
    _f("web_port", "WebUI 端口", "number", "app", 8765,
       "被占用时会自动往后找可用端口。", step=1, minimum=1024, maximum=65535),
]

ALL_FIELDS = FIELDS + APP_FIELDS
FIELD_BY_PATH = {f["path"]: f for f in ALL_FIELDS}

APP_DEFAULTS: dict = {f["path"]: f["default"] for f in APP_FIELDS}


# ---------------------------------------------------------------- 读写工具
def _read_json(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _atomic_write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _get_path(data: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_path(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _deep_merge(base: dict, override: dict) -> dict:
    """与 agent._deep_merge 保持一致的语义（忽略 `_` 前缀的注释键）。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(k, str) and k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def agent_defaults() -> dict:
    """
    agent 的内置默认值（本文件里的副本）。

    ## 为什么**刻意不 import agent**

    `agent` 会连带 import `uiautomation`。而 uiautomation 内部的 `CUIAutomation` 是
    **进程级单例**，它在第一次被用到时把 COM 对象创建在「当时的那个线程」的公寓里。
    UI 进程是多线程的（HTTP 请求线程 + UIA 工作线程），从请求线程里顺手 import 一下、
    再碰一下控件，就会把单例建在错误的公寓上 —— 之后 UIA 工作线程拿到的全是空数据。
    这类故障表现为「有时候读得到、有时候什么都读不到」，是本项目里最难查的一种。

    所以 UI 进程**只在 UIA 工作线程里**碰 uiautomation（见 qqctl.UiaWorker）。
    这里用副本，代价是可能和 agent.DEFAULTS 漂移 —— 由 test_ui.py 在**子进程**里
    比对（那里没有公寓问题）来兜住。

    另外即使副本过时也不会出错：agent 的 `load_config()` 是「内置默认值 ← 文件覆盖」的
    深合并，文件里缺的键会自动用它自己的默认值补上。
    """
    return _LOCAL_DEFAULTS


def diff_against_agent_defaults() -> dict:
    """
    起一个子进程读真实的 `agent.DEFAULTS`，和本地副本比对。

    返回 {"ok": bool, "missing": [...], "different": {path: [本地, agent]}, "error": str}。
    在子进程里做是因为本进程不能 import agent（理由见 agent_defaults）。
    """
    import subprocess
    import sys
    code = (
        "import json, sys; sys.path.insert(0, r'%s'); "
        "import agent; print(json.dumps(agent.DEFAULTS, ensure_ascii=False))"
        % paths.exe_dir()
    )
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=60,
                           cwd=paths.DATA_DIR,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        raw = r.stdout.decode("utf-8", "replace")
        start = raw.find("{")
        if start < 0:
            return {"ok": False, "error": "子进程没有输出 JSON：" + raw[-300:] +
                                           (r.stderr.decode("utf-8", "replace")[-300:])}
        real = json.loads(raw[start:])
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def flat(obj, prefix=""):
        out = {}
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(k, str) and k.startswith("_"):
                    continue
                out.update(flat(v, f"{prefix}{k}."))
        else:
            out[prefix[:-1]] = obj
        return out

    ours, theirs = flat(_LOCAL_DEFAULTS), flat(real)
    missing = sorted(k for k in theirs if k not in ours)
    different = {k: [ours[k], theirs[k]] for k in ours if k in theirs and ours[k] != theirs[k]}
    return {"ok": not missing and not different, "missing": missing,
            "different": different, "error": ""}


_LOCAL_DEFAULTS = {
    "llm": {"api_base": "https://api.deepseek.com", "api_key": "",
            "api_key_file": "secrets.local.json",
            "api_key_json_keys": ["llm.api_key", "deepseek.api_key", "openai.api_key",
                                  "api_key", "DEEPSEEK_API_KEY"],
            "model": "deepseek-flash", "temperature": 1.1, "max_tokens": 800,
            "timeout_seconds": 60,
            "system_prompt": "你是『小清澈』，一个温和真诚的 AI 陪伴者，回复口语化且简短。"},
    "chat": {"self_nickname": "", "private_chat_only": False, "always_reply": True,
             "group_requires_trigger": False, "trigger_prefixes": ["小清澈", "清澈"],
             "reply_cooldown_seconds": 1.5, "min_llm_interval_seconds": 2.0,
             "send_button_wait_seconds": 3.0, "keep_draft_on_abort": True,
             "max_history_entries": 40, "send_with_ctrl_enter": False,
             "poll_interval_seconds": 0.8, "max_reply_chars": 500,
             "nontext_policy": "skip"},
    "aggregate": {"enabled": True, "base_wait_ms": 5000, "fast_wait_ms": 2000,
                  "fast_after_single_rounds": 3, "cold_start_penalty_ms": 6000,
                  "decay_turns": 4, "cold_start_gap_ms": 60000,
                  "habit_min_interval_ms": 500, "habit_max_interval_ms": 15000,
                  "habit_ema_alpha": 0.3},
    "continuous": {"enabled": True, "timeout_seconds": 1800},
    "commands": {"enabled": True, "prefixes": [".ai", "。ai", "/ai", ".aichat"],
                 "accept_from": ["other", "self"], "reply_ack": True,
                 "ack_reset": "小清澈读懂了你的意思，回到了房间，脱衣睡下。",
                 "reset_clears_pending": True,
                 "unknown_hint": "这条指令还没移植到 PC 端，暂时只支持 .ai reset / .ai help。"},
    "persist": {"enabled": True, "file": "state/conversations.json",
                "save_interval_seconds": 3.0, "max_scopes": 50},
    "identity": {"enabled": True, "store": "state/uid-map.json",
                 "enroll_on_demand": True, "require_qq_uin": False},
    "queue": {"enabled": True, "max_replies_per_minute": 12, "min_interval_seconds": 5.0,
              "merge_if_wait_over_seconds": 5.0, "unit_cost_seconds": 3.0,
              "max_hold_seconds": 30.0, "jitter_seconds": 3.0, "max_attempts": 3},
    "discovery": {"enabled": True, "scan_interval_seconds": 2.0, "trigger_on_unread": True,
                  "trigger_on_preview_change": True, "trigger_on_first_sight_unread": True,
                  "max_enqueue_per_scan": 3, "read_limit": 30,
                  "state_file": "state/rotation.json"},
    "teach": {"enabled": True, "names": ["小清澈", "Claritas-小清澈"],
              "max_chars": 1000, "honor_own_outgoing": False},
    "uia": {"read_chain": ["uia", "ocr"], "direction_mode": "auto",
            "ocr_lang": "chi_sim+eng", "ocr_scale": 2.0, "restore_foreground": False},
}


def load_merged() -> dict:
    """agent 视角的完整配置：内置默认值 ← config.json 覆盖。"""
    return _deep_merge(agent_defaults(), _read_json(paths.CONFIG_PATH))


def load_app_settings() -> dict:
    data = _deep_merge(APP_DEFAULTS, _read_json(os.path.join(paths.DATA_DIR, "app-settings.json")))
    return data


def ensure_config_file() -> bool:
    """
    首次运行时把配置落盘（含注释键，方便用户直接编辑）。

    返回 True 表示这次是新建的 —— UI 可以据此提示「这是全新安装，请先填 API Key」。
    """
    if os.path.isfile(paths.CONFIG_PATH):
        return False
    paths.ensure_dirs()
    template = dict(agent_defaults())
    template["_说明"] = "由 QQAgent 界面生成。可以直接编辑，也可以在界面里改；界面保存时不会丢掉本文件的注释键。"
    template["_密钥说明"] = "api_key 留空时会去 api_key_file（secrets.local.json）里找。界面里填的 Key 只写那个文件。"
    _atomic_write_json(paths.CONFIG_PATH, template)
    return True


def ensure_secrets_file() -> None:
    if os.path.isfile(paths.SECRETS_PATH):
        return
    paths.ensure_dirs()
    _atomic_write_json(paths.SECRETS_PATH, {"llm": {"api_key": ""}})


def backup_config() -> str:
    """保存前留一份带时间戳的备份 —— 参数改崩了还能退回去。"""
    if not os.path.isfile(paths.CONFIG_PATH):
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(paths.DATA_DIR, f"config.backup-{stamp}.json")
    try:
        shutil.copy2(paths.CONFIG_PATH, dst)
        return dst
    except Exception:
        return ""


# ---------------------------------------------------------------- 对外接口
def as_ui_payload() -> dict:
    """给前端的：字段声明 + 当前值（key 只给掩码）。"""
    merged = load_merged()
    app = load_app_settings()
    # 这里原本还读过一次 config.json（raw_cfg），但从未被使用 ——
    # merged 已经包含它，所以那次读盘是纯浪费，已删除。
    secret = resolve_api_key_display()

    values: dict = {}
    for f in FIELDS:
        v = _get_path(merged, f["path"], f["default"])
        if f["secret"]:
            v = secret["masked"]
        values[f["path"]] = v
    for f in APP_FIELDS:
        values[f["path"]] = _get_path(app, f["path"], f["default"])

    return {
        "groups": GROUPS + [{"id": "app", "title": "运行环境", "desc": "QQ 定位、监听端口、自启策略。"}],
        "fields": ALL_FIELDS,
        "values": values,
        "api_key_present": secret["present"],
        "api_key_source": secret["source"],
    }


def resolve_api_key_display() -> dict:
    """只报「有没有 / 从哪来 / 掩码长什么样」，绝不把明文回给前端。"""
    cfg = load_merged()
    inline = (cfg.get("llm", {}).get("api_key") or "").strip()
    if inline:
        return {"present": True, "source": "config.json 内联 api_key", "masked": _mask(inline)}

    sec = _read_json(paths.SECRETS_PATH)
    key = ((sec.get("llm") or {}).get("api_key") or "").strip()
    if key:
        return {"present": True, "source": "secrets.local.json",
                "masked": _mask(key)}
    return {"present": False, "source": "", "masked": ""}


def _mask(key: str) -> str:
    if len(key) <= 10:
        return "•" * len(key)
    return f"{key[:4]}{'•' * 8}{key[-4:]}"


def _coerce(field: dict, value: Any) -> Any:
    t = field["type"]
    if t == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "是")
    if t == "number":
        try:
            num = float(value)
        except (TypeError, ValueError):
            return field["default"]
        if field.get("step") and float(field["step"]).is_integer():
            num = int(round(num))
        lo, hi = field.get("minimum"), field.get("maximum")
        if lo is not None:
            num = max(num, lo)
        if hi is not None:
            num = min(num, hi)
        return num
    if t == "tags":
        if isinstance(value, list):
            items = value
        else:
            items = str(value or "").replace("，", "\n").split("\n")
        return [str(x).strip() for x in items if str(x).strip()]
    if t == "textarea":
        return str(value or "")
    return str(value if value is not None else "").strip()


def _is_mask(value: str) -> bool:
    """
    判断前端回传的是不是**掩码**，而不是用户真的填了密钥。

    ## 这里曾经是一个会**覆盖真密钥**的 bug

    `_mask()` 生成的是 `{key[:4]}{'•' * 8}{key[-4:]}`（例如 `sk-1••••••••ghij`），
    首尾**各带着真密钥的 4 个字符**。而原来的判据是

        set(value) <= set("•")      # 只有「全是 •」才算掩码

    于是这个掩码被判成「用户真的填了 key」→ 被当成新密钥写进 `secrets.local.json`
    → **真实密钥被掩码覆盖掉**。之后所有模型调用都用这个假密钥，
    报出来的却是 `UnicodeEncodeError: latin-1 ... position 11-18`（`Bearer ` 共 7 字符，
    正好对上那 8 个 `•` 的位置）—— 一个看起来完全像网络问题的报错。

    判据放宽成「**出现了 • 就当掩码**」：真实的 API Key 不会包含 U+2022。
    """
    return bool(value) and "•" in value


def looks_like_mask(value: str) -> bool:
    """给诊断用的显式命名：这个字符串看起来是掩码（= 配置已经被污染）。"""
    return _is_mask(value)


def validate(payload: dict) -> dict:
    """
    返回 `{"errors": [...], "warnings": [...]}`，每项是 `{"code", "message"}`。

    `errors` 是**拦下来不让保存**的：语法错、必备项缺，这类问题存下去只会让人更迷惑。
    `warnings` 是**存得下去但结果会跟预期不一样**的：比如风控两项配出了互相压制的组合，
    或接口地址看起来不像常见形态。这类不拦 —— 有人就是要「无论多快都至少隔 30 秒」，
    只是他得知道实际速率不是他填的那个数。

    每一项都带错误码，是为了让界面能显示成可搜索、可对照的一句话，
    而不是弹一个「保存失败」让人再回来问。
    """
    errors: list[dict] = []
    warnings: list[dict] = []

    def add(bucket, code, message):
        bucket.append({"code": code, "message": message,
                       "hint": (E.EC.get(code)["fixes"] or [""])[0]})

    base = str(payload.get("llm.api_base") or "").strip()
    if base and not base.startswith(("http://", "https://")):
        add(errors, "E-CFG-004", f"接口地址必须以 http:// 或 https:// 开头（当前 {base!r}）")

    inline_key = (_get_path(load_merged(), "llm.api_key") or "").strip()
    sec_key = ((_read_json(paths.SECRETS_PATH).get("llm") or {}).get("api_key") or "").strip()
    typed = str(payload.get("llm.api_key") or "").strip()
    typed = "" if _is_mask(typed) else typed
    if not (inline_key or sec_key or typed):
        add(errors, "E-LLM-001", "API Key 还没填 —— 不填的话模型调不通")

    # 已配置的密钥如果本身是掩码，说明它被写坏过（历史 bug：掩码被当成真密钥存盘）。
    # 这种情况下模型**一定调不通**，而且报出来的是编码错误（看起来像网络问题）。
    # 在这里直接点破，比让用户去猜强得多。
    for label, stored in (("secrets.local.json", sec_key), ("config.json 内联 api_key", inline_key)):
        if stored and looks_like_mask(stored):
            add(errors, "E-LLM-011",
                f"{label} 里存的**不是真密钥，而是界面上的掩码**（{_mask(stored)}）—— "
                f"请到「模型接入 → API Key」里重新粘贴一次真实密钥")

    model = str(payload.get("llm.model") or "").strip()
    if not model:
        add(errors, "E-CFG-007", "模型名不能为空")

    # 密钥/地址必须是纯 ASCII：HTTP 头只能用 latin-1，混进一个中文就会让请求
    # 在**发出之前**失败，而且那个异常极易被误判成「网络不通」。
    # 在保存时就拦住，比等到调用模型时报错更好 —— 那时用户已经在看别的日志了。
    for path, label in (("llm.api_key", "API Key"), ("llm.api_base", "接口地址")):
        val = str(payload.get(path) or "")
        if _is_mask(val):
            continue
        bad = [(i, ch) for i, ch in enumerate(val) if ord(ch) > 127]
        if bad:
            where = "、".join(f"第 {i + 1} 个是 {ch!r}" for i, ch in bad[:4])
            add(errors, "E-LLM-011",
                f"{label}里有 {len(bad)} 个非 ASCII 字符：{where}"
                f"（只粘 `sk-` 开头那一串，不要带引号/空格/中文注释）")

    # 风控两项是「取更严格者」。配出矛盾组合时实际速率会远低于预期（甚至趋近 0），
    # 这是最容易被误当成「程序卡住」的一种配置错误 —— 所以必须提示，但不该拦死。
    try:
        rpm = payload.get("queue.max_replies_per_minute")
        miv = payload.get("queue.min_interval_seconds")
        if rpm is not None and miv is not None:
            rpm_f, miv_f = float(rpm), float(miv)
            if rpm_f > 0 and 60.0 / rpm_f < miv_f - 1e-9:
                add(warnings, "E-CFG-003",
                    f"风控两项会互相压制：每分钟 {rpm_f:g} 条意味着平均间隔 "
                    f"{60.0 / rpm_f:.1f}s，比最小间隔 {miv_f:g}s 还短 —— 实际速率会被"
                    f"最小间隔限制到约 {60.0 / miv_f:.1f} 条/分，你填的 {rpm_f:g} 不会生效。")
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    if base and not base.rstrip("/").lower().endswith("/v1") and "api.deepseek.com" not in base:
        add(warnings, "E-LLM-002",
            "接口地址看起来不是常见的 /v1 结尾 —— 如果模型调不通，先检查这里。")

    return {"errors": errors, "warnings": warnings}


def save(payload: dict) -> dict:
    """
    保存界面提交的值。

    返回 {ok, errors, warnings, backup, changed:[...]}。
    备份是在**写入之前**做的，所以改崩了永远能拿回来。
    """
    verdict = validate(payload)
    if verdict["errors"]:
        return {"ok": False, "errors": verdict["errors"],
                "warnings": [], "code": verdict["errors"][0]["code"],
                "error": verdict["errors"][0]["message"],
                "hint": verdict["errors"][0].get("hint", "")}

    ensure_config_file()
    backup = backup_config()

    # ---- config.json：从磁盘现读，只覆盖我们管的路径，注释键和被 agent 独享的键都留着
    cfg = _read_json(paths.CONFIG_PATH)
    changed: list[str] = []
    # secret=True 的字段（llm.api_key 等）2026-09-28 起也**写进 config.json**：
    # 该文件被 .gitignore 忽略（pc-agent-demo*/config.json），单文件收拢后
    # 不再有「config 与 secrets 两处都可能有 key」的索引混乱。
    # secrets.local.json 降级为**只读后备**（resolve_api_key 仍兼容旧配置）。
    for f in FIELDS:
        if f["path"] not in payload:
            continue
        new_val = _coerce(f, payload[f["path"]])
        # secret 字段掩码防线（2026-09-28 迁入统一循环时必须保留）：
        # 前端把掩码原样回传 = 用户没改这把 key，跳过，绝不把掩码写进 config.json
        if f.get("secret") and isinstance(new_val, str) and _is_mask(new_val):
            continue
        old_val = _get_path(load_merged(), f["path"], f["default"])
        if new_val != old_val:
            _set_path(cfg, f["path"], new_val)
            changed.append(f["path"])
    try:
        _atomic_write_json(paths.CONFIG_PATH, cfg)
    except Exception as exc:
        return E.from_exception(exc, "E-CFG-006", {"文件": paths.CONFIG_PATH})

    # ---- app-settings.json
    app_path = os.path.join(paths.DATA_DIR, "app-settings.json")
    app = _read_json(app_path)
    app_changed: list[str] = []
    for f in APP_FIELDS:
        if f["path"] not in payload:
            continue
        new_val = _coerce(f, payload[f["path"]])
        if new_val != _get_path(load_app_settings(), f["path"], f["default"]):
            app[f["path"]] = new_val
            app_changed.append(f["path"])
    if app_changed:
        try:
            _atomic_write_json(app_path, app)
        except Exception as exc:
            return E.from_exception(exc, "E-CFG-006", {"文件": app_path})

    warnings: list[str] = [w["message"] for w in verdict["warnings"]]
    warning_codes: list[str] = [w["code"] for w in verdict["warnings"]]
    if any(p.startswith("app.web_") for p in app_changed):
        warnings.append("监听地址/端口已改，需要重启本程序才能生效。")
        warning_codes.append("E-ENV-005")
    elif app_changed:
        warnings.append("运行环境设置已保存，其中 QQ 路径/端口类改动需要重启程序生效。")
    if {"llm.api_base", "llm.model", "llm.api_key", "llm.system_prompt"} & set(changed):
        warnings.append("模型接入信息已改，常驻进程需要重启才会用上新配置。")
    if {"uia.direction_mode", "uia.read_chain"} & set(changed):
        warnings.append("读取相关设置已改，下次启动常驻时生效。")

    return {"ok": True, "errors": [], "warnings": warnings,
            "warning_codes": warning_codes,
            "backup": backup, "changed": changed + app_changed}

def reset_to_defaults() -> dict:
    backup = backup_config()
    paths.ensure_dirs()
    template = dict(agent_defaults())
    template["_说明"] = "已恢复到内置默认值。"
    _atomic_write_json(paths.CONFIG_PATH, template)
    return {"ok": True, "backup": backup}
