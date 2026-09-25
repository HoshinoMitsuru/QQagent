# -*- coding: utf-8 -*-
"""
brain.py —— CU 的规划层（LLM Computer Use 的「大脑」）

## 它是什么

一个 OpenAI 兼容的 tool calling 循环：

    任务（人下发的）
        ↓ ① 带 tools 调 /chat/completions
    模型返回 tool_calls
        ↓ ② 逐个 dispatch()（cu/tools，确认锁在这层生效）
    结果以 role=tool 消息回灌
        ↓ ③ 重复 ①②，直到模型给出纯文本答复
    最终答复 + 完整 trace

默认 `https://api.deepseek.com` + `deepseek-flash`；base_url / model / Key
全部可配。Key 解析顺序：`cu.api_key` → `cu.api_key_file` → 复用主 llm 密钥
（`agent.resolve_api_key`，即 secrets.local.json 里那份）——同一个 DeepSeek
Key 三处通用。

## 安全闸（本层的三道）

1. **确认锁不归模型**：dispatch 返回 needs_confirmation（E-CU-004）时，
   Brain 调用注入的 `confirm` 回调问人。人拒了就以「用户拒绝发送」回灌，
   模型重发也会被拒。`confirm` 不传 = 视为拒绝（fail-closed）。
2. **目标白名单**：`open_chat_allow` 非空时，open_chat 的目标（按名字，
   index 会先查最近一次 list_sessions 的缓存翻译成名字）不在名单内就拒绝，
   原语不被调用。这是给**实测**用的：附着主号时只许「我，我们」和「苏霖韵」。
3. **步数上限**：max_steps 跑满还没答复就停（E-CU-008），防止模型打转烧 token。

## 状态码分类

与 agent.LLMClient 同一套口径：401/403→E-LLM-004、404→E-LLM-005、
429→E-LLM-006、5xx→E-LLM-007、超时→E-LLM-003、连不上→E-LLM-002。
一个错误码一个根因，Brain 的 trace 里直接可读。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import requests

import agent
import error_codes as EC
from cu.base import Executor, ExecutorError
from cu.tools import dispatch, tool_schemas

__all__ = ["Brain", "load_cu_config", "CU_DEFAULTS"]

#: CU 配置默认值。刻意**不进** agent.DEFAULTS（那有 settings 逐键闸门），
#: cu 是独立配置节，这里就是它的事实来源。
CU_DEFAULTS: dict = {
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-flash",
    "api_key": "",                  # 留空 = 复用主 llm 密钥
    "api_key_file": "",
    "timeout_seconds": 120.0,
    "max_steps": 8,
    "max_tokens": 2048,
    # 思考模式（DeepSeek V4.1 默认开启，effort=high）：默认**关闭**——
    # 规划层的每一步都要快；开着时带 tools 的请求还必须回传 reasoning_content
    # （不回传会 400）。true=开启（实现回传）；"auto"=不发送该参数（第三方
    # 中转不认 thinking 字段时用它避免 400）。
    "thinking": False,
    "system_prompt": "",            # 追加到内置系统提示之后
    # 发送后视觉校验（F5）：send_text 成功后自动截图 + 视觉模型确认气泡出现。
    # 校验是旁证，不推翻 UIA 读回结论；inconclusive 不算失败。
    "verify_sends": True,
    "verify_model": "",             # 留空 = 用主 model（deepseek-flash 自带图像理解）
    # 实测白名单：附着主号时只允许切换/发送到这些会话（空 = 不限制）。
    # 2026-09-25 苏霖韵拍板：attach 实测仅限群「我，我们」与小号「苏霖韵」。
    "open_chat_allow": [],
}

_SYSTEM_PROMPT = """你是一个 QQ 客户端的执行规划器（Computer Use Agent）。
你只能通过提供的工具操作 QQ，不能编造工具，不能编造界面内容。

工作规则：
1. 动手前先想清楚最少需要哪几步；不确定界面状态时先用只读工具
   （list_sessions / read_recent / health）确认，再执行有副作用的动作。
2. 发消息前必须先 open_chat 明确目标会话——宁可多确认一次，不可发错人。
3. 工具返回 {"ok": false} 时，读 error.code / error.detail / error.ctx 里的
   线索再决定下一步；同一个动作不要原样重试超过一次。
4. send_text 可能返回 needs_confirmation=true（主号执行面必须人工确认）：
   此时停下等待，不要尝试绕过确认机制。
5. send_text 回执里的 verify 字段是发送后的视觉旁证（confirmed=截图确认 /
   mismatch=截图里没找到 / inconclusive=无法校验）。mismatch 时如实告知用户
   「视觉校验未确认」，不要声称发送已被视觉确认。
6. 任务完成或确认无法完成时，用一段简短的中文总结交代：做了什么、结果如何、
   遇到什么问题。不要虚构你没做到的事。
"""


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_cu_config(cfg: dict) -> dict:
    """从整体配置里取出 cu 节并与默认值合并。缺节 = 全默认（不会炸）。"""
    return _deep_merge(CU_DEFAULTS, (cfg or {}).get("cu") or {})


def _resolve_key(cu: dict, cfg: dict) -> tuple[str, str]:
    """Key 三级解析：cu.api_key → cu.api_key_file → 主 llm 密钥。
    返回 (key, 来源说明)。"""
    if (cu.get("api_key") or "").strip():
        return cu["api_key"].strip(), "cu.api_key"
    f = (cu.get("api_key_file") or "").strip()
    if f:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                v = fh.read().strip()
            if v:
                return v, f"cu.api_key_file({f})"
        except OSError:
            pass
    return agent.resolve_api_key(cfg), "主 llm 密钥（agent.resolve_api_key）"


def _raise_for_status(resp) -> None:
    """按状态码分类抛 AppError —— 与 agent.LLMClient 同一口径。"""
    code = resp.status_code
    if code == 200:
        return
    text = (resp.text or "")[:400]
    if code in (401, 403):
        raise EC.AppError("E-LLM-004", f"HTTP {code}：密钥无效或无权", {"响应": text})
    if code == 404:
        raise EC.AppError("E-LLM-005", f"HTTP 404：接口地址或模型名不对",
                          {"响应": text})
    if code == 429:
        raise EC.AppError("E-LLM-006", "HTTP 429：被限流或额度用尽", {"响应": text})
    if code >= 500:
        raise EC.AppError("E-LLM-007", f"HTTP {code}：模型服务端错误", {"响应": text})
    raise EC.AppError("E-LLM-012", f"HTTP {code}：未预期的响应状态", {"响应": text})


class Brain:
    """tool calling 循环。线程约束与执行面相同（attach 面须在 UIA 线程跑）。"""

    def __init__(self, cu_cfg: dict, full_cfg: dict | None = None,
                 *, poster: Callable[[dict], dict] | None = None):
        self.cfg = cu_cfg
        key, key_src = _resolve_key(cu_cfg, full_cfg or {})
        self.api_key = key
        self.key_src = key_src
        agent.check_ascii_for_header(key, "API Key")
        self.url = (cu_cfg.get("base_url") or "").rstrip("/") + "/chat/completions"
        agent.check_ascii_for_header(self.url, "接口地址")
        self.model = cu_cfg.get("model") or "deepseek-flash"
        self.timeout = float(cu_cfg.get("timeout_seconds") or 120)
        self.max_steps = max(1, int(cu_cfg.get("max_steps") or 8))
        self.max_tokens = int(cu_cfg.get("max_tokens") or 2048)
        self.allow = [s for s in (cu_cfg.get("open_chat_allow") or []) if s]
        self.system_prompt = _SYSTEM_PROMPT + (
            f"\n\n补充说明：\n{cu_cfg['system_prompt']}"
            if cu_cfg.get("system_prompt") else "")
        if self.allow:
            self.system_prompt += (
                "\n当前处于受限实测模式：open_chat 只允许这些目标："
                + "、".join(self.allow) + "。名单之外的会话会被拒绝，不要重试。")
        self._poster = poster or self._post
        self._last_sessions: list[dict] = []   # open_chat index → 名字翻译用
        # 思考模式参数：False→disabled（默认，快且省）；True→enabled；
        # "auto"→不带该字段（第三方中转不认 thinking 时用）
        t = cu_cfg.get("thinking")
        self._thinking_param = (None if t == "auto"
                                else {"type": "enabled" if t else "disabled"})
        # F5 视觉校验：send_text 成功后自动截图问模型「气泡真的出现了吗」
        self._verifier = None
        if cu_cfg.get("verify_sends"):
            from cu.verifier import Verifier
            self._verifier = Verifier(self)

    # ---------------------------------------------------- HTTP
    def post(self, payload: dict) -> dict:
        """HTTP 通道的**唯一入口**：chat 与 verifier 都走这里。
        测试通过构造参数注入 poster 替身，不碰网络。"""
        return self._poster(payload)

    def _post(self, payload: dict) -> dict:
        """默认 HTTP 通道（真实 requests）。测试不要直接调本方法。"""
        try:
            resp = requests.post(
                self.url,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=self.timeout)
        except requests.exceptions.Timeout as exc:
            raise EC.AppError("E-LLM-003", f"请求超时（{self.timeout}s）",
                              {"url": self.url}) from exc
        except requests.exceptions.RequestException as exc:
            # 能确定是连接问题的才给 E-LLM-002；其余按未分类兜底
            raise EC.wrap(exc, "E-LLM-012") from exc
        _raise_for_status(resp)
        try:
            return resp.json()
        except ValueError as exc:
            raise EC.AppError("E-LLM-008", "响应不是合法 JSON",
                              {"正文": resp.text[:200]}) from exc

    def _chat(self, messages: list[dict]) -> dict:
        """调一次模型，返回 choices[0].message。失败抛 AppError（带 E-LLM 码）。"""
        if not self.api_key:
            raise EC.AppError("E-LLM-001",
                              "没有可用 API Key（cu.api_key / api_key_file / 主 llm 密钥都为空）",
                              {"url": self.url, "model": self.model})
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tool_schemas(),
            "tool_choice": "auto",
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        if self._thinking_param is not None:
            payload["thinking"] = self._thinking_param
        data = self._poster(payload)
        try:
            return data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise EC.AppError("E-LLM-008", "响应缺少 choices[0].message",
                              {"响应键": list(data) if isinstance(data, dict) else "?"}
                              ) from exc

    # ---------------------------------------------------- 白名单闸
    def _allow_check(self, args: dict) -> str:
        """open_chat 的白名单检查。返回拒绝理由；空串 = 放行。"""
        if not self.allow:
            return ""
        name = args.get("name") or ""
        if not name:
            if "index" not in args:
                # name 与 index 都缺：这不是白名单问题，让执行层报 E-CU-005
                return ""
            idx = int(args.get("index") or 0)
            found = [s for s in self._last_sessions if s.get("index") == idx]
            name = found[0].get("name", "") if found else ""
            if not name:
                return (f"open_chat 用 index={idx} 但本步没有会话名字可核对白名单，"
                        f"请先用 list_sessions 再按 name 指定（允许：{'、'.join(self.allow)}）")
        if any(a in name or name in a for a in self.allow):
            return ""
        return (f"目标会话「{name}」不在允许名单（{'、'.join(self.allow)}）内，已拦截")

    # ---------------------------------------------------- 主循环
    def run(self, ex: Executor, task: str, *,
            confirm: Callable[[dict], bool] | None = None,
            on_event: Callable[[dict], None] | None = None) -> dict:
        """执行一个任务。返回：
            {"ok": True,  "answer": 最终答复, "steps": [...], "usage_steps": n}
            {"ok": False, "error": {...}, "steps": [...]}
        confirm(info) 在 send_text 被锁拦截时被调用；不传 = 一律拒绝。
        on_event(evt) 每步回调一次（WebUI/日志用），evt 带 step/type/payload。
        """
        steps: list[dict] = []
        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": task},
        ]

        def emit(evt_type: str, payload: dict, step: int) -> None:
            evt = {"step": step, "type": evt_type, **payload}
            steps.append(evt)
            if on_event:
                try:
                    on_event(evt)
                except Exception:
                    pass  # 观察者不许打断主循环

        try:
            for step in range(1, self.max_steps + 1):
                msg = self._chat(messages)
                tool_calls = msg.get("tool_calls") or []

                if not tool_calls:
                    answer = (msg.get("content") or "").strip()
                    emit("answer", {"answer": answer}, step)
                    if not answer:
                        return {"ok": False, "steps": steps,
                                "error": {"code": "E-LLM-009",
                                          "detail": "模型返回了空内容（既无 tool_calls 也无答复）",
                                          "ctx": {"step": step}}}
                    return {"ok": True, "answer": answer, "steps": steps,
                            "usage_steps": step}

                # assistant 消息原样回存（含 tool_calls），协议要求；
                # 思考模式开启时还必须回传 reasoning_content（否则 DeepSeek 400）
                assistant_msg = {"role": "assistant",
                                 "content": msg.get("content") or "",
                                 "tool_calls": tool_calls}
                if self._thinking_param == {"type": "enabled"} \
                        and msg.get("reasoning_content"):
                    assistant_msg["reasoning_content"] = msg["reasoning_content"]
                messages.append(assistant_msg)
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name") or ""
                    raw_args = fn.get("arguments")
                    emit("tool_call", {"tool": name, "arguments": raw_args}, step)

                    # 白名单闸：在原语被调用之前拦
                    if name == "open_chat" and self.allow:
                        try:
                            args0 = json.loads(raw_args) if isinstance(raw_args, str) \
                                else (raw_args or {})
                        except json.JSONDecodeError:
                            args0 = {}
                        reason = self._allow_check(args0)
                        if reason:
                            result = {"ok": False, "tool": name,
                                      "error": {"code": "E-CU-004", "detail": reason,
                                                "ctx": {"闸": "open_chat 白名单"}}}
                            emit("tool_blocked", {"tool": name, "reason": reason}, step)
                            messages.append({"role": "tool",
                                             "tool_call_id": tc.get("id") or "",
                                             "content": json.dumps(result, ensure_ascii=False)})
                            continue

                    result = dispatch(ex, name, raw_args)

                    # 确认锁：问人；人点头才以 armed=True 重发
                    if result.get("needs_confirmation"):
                        info = {"text": result.get("text", ""), "tool": name,
                                "chat_title": self._current_chat_hint(ex)}
                        ok = bool(confirm) and confirm(info)
                        if ok:
                            result = dispatch(ex, name, raw_args, armed=True)
                            emit("confirmed", {"tool": name, "text": info["text"]}, step)
                        else:
                            result = {"ok": False, "tool": name,
                                      "error": {"code": "E-CU-004",
                                                "detail": "用户拒绝发送（这不是故障，不要重试同样的内容）",
                                                "ctx": {"闸": "人工确认"}}}
                            emit("rejected", {"tool": name, "text": info["text"]}, step)

                    if name == "send_text" and result.get("ok") and self._verifier:
                        result = self._verify_send(ex, raw_args, result, step, emit)

                    if name == "list_sessions" and result.get("ok"):
                        self._last_sessions = result.get("sessions") or []
                    emit("tool_result", {"tool": name, "result": result}, step)
                    messages.append({"role": "tool",
                                     "tool_call_id": tc.get("id") or "",
                                     "content": json.dumps(result, ensure_ascii=False)})

            return {"ok": False, "steps": steps,
                    "error": {"code": "E-CU-008",
                              "detail": f"跑满 max_steps={self.max_steps} 仍未给出最终答复",
                              "ctx": {"建议": "看 steps 里最后几步是否在重复同一动作"}}}
        except EC.AppError as exc:
            return {"ok": False, "steps": steps,
                    "error": {"code": exc.code, "detail": exc.detail_text,
                              "ctx": exc.context}}

    def _verify_send(self, ex: Executor, raw_args, result: dict,
                     step: int, emit) -> dict:
        """send_text 成功后的视觉旁证：截图 → 视觉模型 → 结论并进回灌信封。

        校验失败/inconclusive **不推翻**发送结果（UIA 读回已通过），
        只把结论附在 verify 字段里，让模型与上层自己权衡。
        """
        from cu.verifier import VerifyResult
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError:
            args = {}
        text = str(args.get("text") or "")

        shot_path, shot_err = "", ""
        try:
            shot = ex.screenshot()
            shot_path = shot.path if shot.ok else ""
            shot_err = "" if shot.ok else (shot.error or "抓图失败")
        except Exception as exc:  # noqa: BLE001
            shot_err = f"{type(exc).__name__}: {exc}"

        if shot_path:
            vr = self._verifier.verify_send(text, shot_path)
        else:
            vr = VerifyResult(False, "inconclusive", shot_err or "截图不可用", "")
        result["verify"] = vr.to_dict()
        emit("verified", {"verdict": vr.verdict, "detail": vr.detail}, step)
        return result

    def _current_chat_hint(self, ex: Executor) -> str:
        """确认弹层里给人看的「现在要发给谁」。失败不阻塞确认流程。

        两面 extra 形态不同（2026-09-25 真机实测：弹层一直显示「（未知）」）：
        - attach：diagnose_dom 的中文平铺键，「窗口」的值就是标题字符串；
        - hosted：extra["window"] 是 {"title": ...} 形态。
        取不到就空串，由上层显示「（未知）」。"""
        try:
            h = ex.health()
            e = h.extra if isinstance(h.extra, dict) else {}
            title = e.get("窗口")
            if not title:
                w = e.get("window") or {}
                title = w.get("title") or "" if isinstance(w, dict) else ""
            return title or ""
        except Exception:
            return ""
