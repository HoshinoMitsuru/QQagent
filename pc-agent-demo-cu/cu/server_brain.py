# -*- coding: utf-8 -*-
"""
server_brain.py —— ECS 大脑通道（P1）：把「生成回复」交给 ai-web-page 服务端。

## 它是什么

服务端大脑的**瘦客户端**。与 brain.py（本地 tool-calling 循环）的关系：
brain 是「本地大脑」（直连 DeepSeek、自己编排六原语）；本模块把生成一步
交给 ai-web-page 服务端（`POST /api/qq/agent`），客户端只负责 QQ 收发——
即双路径方案里的「大脑完全归属 ECS」。

服务端持有：LLM / 人格 / 记忆 / 用户映射（qq_number → user）/ 会话历史。
本模块不碰任何 UIA 原语。

## 鉴权（与 /api/qq/message 同口径）

    X-QQ-Signature = HMAC-SHA256(secret, raw_body).hexdigest()

secret 来源（按序取第一个非空）：
    1. config.json 的 qq_agent.secret
    2. secrets.local.json（路径取 config llm.api_key_file，缺省 secrets.local.json）
       里的 qq_agent.secret
服务端未配置 QQ_WEBHOOK_SECRET 时不强制校验，但客户端有 secret 就签名。

## 配置（config.json 新增节）

    "qq_agent": {
        "base_url": "https://aligera.website",
        "secret": "",
        "qq_number": "",            # hosted 小号 QQ 号（ask 命令的缺省发送者）
        "timeout_seconds": 90.0
    }

## 状态码分类（与 brain.py 同一口径）

401/403→E-LLM-004、404→E-LLM-005、429→E-LLM-006、5xx→E-LLM-007、
超时→E-LLM-003、连不上→E-LLM-002；本地配置缺失→E-SRV-001。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, List, Optional

import requests

import error_codes as EC  # noqa: F401  —— 保持与 brain 同一错误码目录口径

DEFAULT_BASE_URL = "https://aligera.website"
DEFAULT_TIMEOUT = 90.0


def _load_secrets(cfg: dict) -> dict:
    """读取 secrets.local.json（路径沿用 llm.api_key_file 约定），读不到返回 {}。

    相对路径一律按本项目根目录解析（cu/ 的上级 = agent.py 的 HERE），绝不看 CWD
    —— 与 agent.resolve_api_key 同一条纪律：从别的目录启动时读不到密钥文件，
    表现为「同样的配置换个目录结果就不一样」，这类依赖 CWD 的行为必须掐掉。
    """
    llm = cfg.get("llm") or {}
    here = os.path.dirname(os.path.abspath(__file__))
    path = (llm.get("api_key_file") or "").strip()
    if not path:
        path = os.path.join(here, "..", "secrets.local.json")
    elif not os.path.isabs(path):
        path = os.path.join(here, "..", path)
    path = os.path.normpath(path)
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _resolve(cfg: dict) -> Dict[str, Any]:
    """归并 config.json 的 qq_agent 节 + secrets.local.json 的 qq_agent 节。"""
    qa = dict(cfg.get("qq_agent") or {})
    qa.update(_load_secrets(cfg).get("qq_agent") or {})
    return qa


def get_reply(cfg: dict,
              texts: List[str],
              qq_number: str = "",
              scope: str = "private",
              group_id: str = "",
              channel: str = "r4",
              nickname: str = "") -> Dict[str, Any]:
    """把聚合后的消息批交给服务端大脑，返回信封：

    成功  {"ok": True, "reply": ..., "conversation_id": ..., "user_id": ..., "channel": ...}
    失败  {"ok": False, "error": {"code", "detail", "ctx"}}
    """
    qa = _resolve(cfg)
    base = (qa.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
    number = (qq_number or qa.get("qq_number") or "").strip()
    secret = (qa.get("secret") or "").strip()
    timeout = float(qa.get("timeout_seconds") or DEFAULT_TIMEOUT)

    if not number:
        return {"ok": False,
                "error": {"code": "E-SRV-001", "detail": "缺少发送者 QQ 号"
                          "（--qq 或 config.json 的 qq_agent.qq_number）", "ctx": {}}}
    clean = [t.strip() for t in (texts or []) if t and t.strip()]
    if not clean:
        return {"ok": False,
                "error": {"code": "E-SRV-001", "detail": "texts 为空", "ctx": {}}}

    payload = {"qq_number": number, "messages": clean, "scope": scope,
               "channel": channel, "nickname": nickname}
    if group_id:
        payload["group_id"] = group_id
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if secret:
        headers["X-QQ-Signature"] = hmac.new(
            secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    url = f"{base}/api/qq/agent"
    started = time.time()
    try:
        resp = requests.post(url, data=body, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        return {"ok": False, "error": {"code": "E-LLM-003",
                "detail": f"服务端响应超时（>{timeout}s）", "ctx": {"url": url}}}
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "error": {"code": "E-LLM-002",
                "detail": f"连不上服务端: {type(exc).__name__}", "ctx": {"url": url}}}

    if resp.status_code in (401, 403):
        return {"ok": False, "error": {"code": "E-LLM-004",
                "detail": f"签名/鉴权被拒（HTTP {resp.status_code}）", "ctx": {"url": url}}}
    if resp.status_code == 404:
        return {"ok": False, "error": {"code": "E-LLM-005",
                "detail": "服务端无 /api/qq/agent（版本过旧？）", "ctx": {"url": url}}}
    if resp.status_code == 429:
        return {"ok": False, "error": {"code": "E-LLM-006",
                "detail": "触发服务端保险丝（该号请求过于频繁）", "ctx": {"url": url}}}
    if resp.status_code >= 500:
        return {"ok": False, "error": {"code": "E-LLM-007",
                "detail": f"服务端错误（HTTP {resp.status_code}）", "ctx": {"url": url}}}
    if resp.status_code != 200:
        return {"ok": False, "error": {"code": "E-SRV-001",
                "detail": f"意外状态码 HTTP {resp.status_code}: {resp.text[:200]}",
                "ctx": {"url": url}}}

    try:
        data = resp.json()
    except ValueError:
        return {"ok": False, "error": {"code": "E-SRV-001",
                "detail": "响应不是 JSON", "ctx": {"url": url}}}

    reply = (data.get("reply") or "").strip()
    if not reply:
        return {"ok": False, "error": {"code": "E-SRV-001",
                "detail": "服务端返回空回复", "ctx": {"url": url}}}
    return {"ok": True, "reply": reply,
            "conversation_id": data.get("conversation_id", ""),
            "user_id": data.get("user_id"), "channel": data.get("channel", channel),
            "elapsed": round(time.time() - started, 2)}
