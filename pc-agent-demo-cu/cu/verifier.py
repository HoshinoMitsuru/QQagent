# -*- coding: utf-8 -*-
"""
verifier.py —— 发送后的视觉校验（C 路线的 verifier 兜底）

## 它解决什么问题

`QQWindow.send_text` 的读回验证读的是 **UIA 树**——它可能说谎：
消息进了输入框但发送按钮没点中、气泡渲染了半截、或者读到的是旧气泡。
视觉校验是**另一条独立证据链**：抓窗口画面，让有图像理解的模型回答
「你能在截图里看到这条刚发出的消息吗」。

两条证据链的口径：
    UIA 读回（send_text 内部）  → ok 才会有 SendReceipt
    视觉校验（本模块）          → 只做旁证，**不推翻**读回结论，
                                  mismatch 时在信封里标注供上层/模型判断

## 模型

用 deepseek-flash 自带的图像理解（已核实官方文档支持），**不需要第二路
视觉 Key**——Verifier 复用 Brain 的 `_post`（HTTP 通道 + 状态码错误映射），
Key / base_url 与 Brain 完全一致。可用 `cu.verify_model` 换别的视觉模型。

## 降级纪律（诚实清单）

    截图失败          → inconclusive（原样报告 error）
    BMP 且无 Pillow    → inconclusive（打包版刻意排除 Pillow，转不了 PNG）
    模型输出不是 JSON  → inconclusive
    AppError（网络等） → inconclusive（带错误码）

inconclusive **不是失败**：发送本身已经过了 UIA 读回，校验只是旁证。
它绝不抛异常，也绝不阻塞 Brain 主循环。
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass

import error_codes as EC

__all__ = ["Verifier", "VerifyResult"]


@dataclass
class VerifyResult:
    ok: bool            # 「校验过程本身可信」（气泡没找到时 ok=True、verdict=mismatch）
    verdict: str        # "confirmed" | "mismatch" | "inconclusive"
    detail: str = ""
    shot_path: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "verdict": self.verdict,
                "detail": self.detail, "shot_path": self.shot_path}


def _extract_json(text: str) -> dict | None:
    """从模型回复里抠 JSON。容忍 ```json 围栏和前后废话。"""
    m = re.search(r"\{[^{}]*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


class Verifier:
    def __init__(self, brain):
        """复用 Brain 的 HTTP 通道与配置。"""
        self.brain = brain
        self.model = (brain.cfg.get("verify_model") or "").strip() or brain.model
        self.max_tokens = 200

    PROMPT = (
        "这是一张 QQ 聊天窗口的截图。请判断：聊天区域里最新的一条"
        "**由本人发出**的消息气泡，内容是否就是下面这段文本：\n"
        "<<<\n{text}\n>>>\n"
        "注意：只看消息气泡，不要把输入框里还没发出去的草稿当证据。\n"
        "只输出一个 JSON 对象，不要输出任何其他文字：\n"
        '{{"found": true/false, "evidence": "一句话说明你在截图里看到了什么"}}'
    )

    def verify_send(self, text: str, shot_path: str) -> VerifyResult:
        """校验「text 是否真的出现在了截图的已发送气泡里」。永不抛异常。"""
        if not text:
            return VerifyResult(False, "inconclusive", "没有待校验的文本", shot_path)

        # ---- 图片 → data URI（BMP 无 Pillow 时诚实降级）----
        try:
            uri, fmt = self._image_data_uri(shot_path)
        except OSError as exc:
            return VerifyResult(False, "inconclusive",
                                f"截图读不到：{type(exc).__name__}: {exc}", shot_path)
        if uri is None:
            return VerifyResult(
                False, "inconclusive",
                "截图是 BMP 且环境没有 Pillow（打包版刻意排除），转不了视觉模型能吃的格式",
                shot_path)

        # ---- 调视觉模型（复用 Brain 的 HTTP 通道与错误映射）----
        # ⚠️ thinking 必须显式关：deepseek-flash 思考模式默认开（effort=high），
        # 开着时结论可能整个落进 reasoning_content 而 content 为空 —— 2026-09-25
        # 真机实测 verify 拿到空回复就是这个原因。
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": self.PROMPT.format(text=text)},
                {"type": "image_url", "image_url": {"url": uri}},
            ]}],
            "max_tokens": self.max_tokens,
            "stream": False,
            "thinking": {"type": "disabled"},
        }
        try:
            data = self.brain.post(payload)   # 走 Brain 的统一通道（可被测试替身注入）
            message = (data.get("choices") or [{}])[0].get("message") or {}
            content = message.get("content") or ""
            if not content.strip():
                # 兜底：万一思考模式没被关掉（第三方中转可能忽略该参数），
                # 结论可能落在 reasoning_content 里
                content = message.get("reasoning_content") or ""
        except EC.AppError as exc:
            return VerifyResult(False, "inconclusive",
                                f"视觉模型调用失败 {exc.code}：{exc.detail_text}", shot_path)
        except Exception as exc:  # noqa: BLE001 —— 校验绝不上抛
            return VerifyResult(False, "inconclusive",
                                f"视觉模型调用出现未分类异常：{type(exc).__name__}: {exc}",
                                shot_path)

        # ---- 解析结论 ----
        obj = _extract_json(content)
        if obj is None or not isinstance(obj.get("found"), bool):
            return VerifyResult(False, "inconclusive",
                                f"模型回复不是预期 JSON：{content[:120]}", shot_path)
        evidence = str(obj.get("evidence", ""))[:200]
        if obj["found"]:
            return VerifyResult(True, "confirmed", evidence, shot_path)
        return VerifyResult(True, "mismatch",
                            evidence or "模型在截图里没有找到匹配的已发送气泡", shot_path)

    # ---------------------------------------------------- 内部
    @staticmethod
    def _image_data_uri(path: str) -> tuple[str | None, str]:
        """把截图文件转成 data URI。返回 (uri, 格式)；BMP 无 Pillow 时 (None, 'bmp')。"""
        with open(path, "rb") as f:
            raw = f.read()
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext == "png":
            return "data:image/png;base64," + base64.b64encode(raw).decode(), "png"
        if ext in ("jpg", "jpeg"):
            return "data:image/jpeg;base64," + base64.b64encode(raw).decode(), "jpeg"
        # BMP：视觉模型普遍不吃，用 Pillow 转 PNG（内存里转，不落第二个文件）
        try:
            from PIL import Image
            import io
            img = Image.open(path)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            return ("data:image/png;base64,"
                    + base64.b64encode(buf.getvalue()).decode()), "png"
        except ImportError:
            return None, "bmp"
