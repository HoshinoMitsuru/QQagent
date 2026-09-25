# -*- coding: utf-8 -*-
"""
test_verifier.py —— 视觉校验层（cu/verifier.py）+ Brain 集成的离线自检

## 测什么

    - 图片处理：PNG 直传 / BMP→PNG 内存转换 / 文件缺失降级
    - 模型回复解析：纯净 JSON / ```json 围栏 / 废话包裹 / 完全不是 JSON
    - 四种结论：confirmed / mismatch / inconclusive（三种成因）
    - Brain 集成：send_text 成功后自动截图校验，verify 结论进回灌信封；
      verify_sends=false 时不产生视觉调用

全部离线：poster 替身替代 HTTP，临时 PNG/BMP 由 Pillow 现场生成。不碰网络、不碰 QQ。

用法：
    python test_verifier.py
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import error_codes as EC                                   # noqa: E402
from cu.base import (ChatMessage, Executor, Health,         # noqa: E402
                     SendReceipt, SessionInfo, Shot)
from cu.brain import CU_DEFAULTS, Brain                     # noqa: E402
from cu.tools import dispatch                               # noqa: E402
from cu.verifier import VerifyResult, Verifier, _extract_json  # noqa: E402

OK = FAIL = 0


def case(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def make_png(path: str, w: int = 40, h: int = 30) -> None:
    from PIL import Image
    Image.new("RGB", (w, h), (200, 30, 30)).save(path, "PNG")


def make_bmp(path: str) -> None:
    from PIL import Image
    Image.new("RGB", (40, 30), (30, 200, 30)).save(path, "BMP")


def ai_msg(content=None, tool_calls=None) -> dict:
    m = {"role": "assistant", "content": content if content is not None else ""}
    if tool_calls:
        m["tool_calls"] = tool_calls
    return m


def tc(tid, name, args) -> dict:
    return {"id": tid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


class StubExecutor(Executor):
    name = "stub"
    require_confirmation = False

    def __init__(self, shot: Shot | None = None):
        self.calls: list[tuple] = []
        self.shot = shot

    def list_sessions(self):
        return []

    def open_chat(self, name="", index=0):
        self.calls.append(("open_chat", name, index))
        return {"ok": True, "opened": name}

    def read_recent(self, limit=12):
        return []

    def send_text(self, text, *, armed=None):
        self.calls.append(("send_text", text, armed))
        return SendReceipt(True, route="wmchar", chat_title="苏霖韵")

    def screenshot(self, path=""):
        self.calls.append(("screenshot", path))
        return self.shot or Shot(False, error="无截图")

    def health(self):
        return Health(True, mode="stub", chat_open=True)


# ============================================================ 图片处理
print("§1 图片 → data URI")
tmpdir = tempfile.mkdtemp()
png_path = os.path.join(tmpdir, "shot.png")
bmp_path = os.path.join(tmpdir, "shot.bmp")
make_png(png_path)
make_bmp(bmp_path)
uri, fmt = Verifier._image_data_uri(png_path)
case("PNG 直传：data:image/png 前缀", uri.startswith("data:image/png;base64,") and fmt == "png", "")
case("PNG 内容可反向解码", base64.b64decode(uri.split(",", 1)[1])[:8] == b"\x89PNG\r\n\x1a\n", "")
uri, fmt = Verifier._image_data_uri(bmp_path)
case("BMP 有 Pillow → 内存转 PNG", uri is not None and fmt == "png"
     and uri.startswith("data:image/png;base64,"), f"fmt={fmt}")

# ============================================================ JSON 抠取
print("§2 模型回复的 JSON 抠取")
case("纯净 JSON",
     _extract_json('{"found": true, "evidence": "有"}') == {"found": True, "evidence": "有"}, "")
case("```json 围栏",
     _extract_json('```json\n{"found": false, "evidence": "没有"}\n```')["found"] is False, "")
case("前后废话包裹",
     _extract_json('好的。{"found": true, "evidence": "看到了"} 以上。')["found"] is True, "")
case("完全不是 JSON", _extract_json("我在截图里看到了你说的内容。") is None, "")

# ============================================================ verify_send 四结论
print("§3 verify_send：confirmed / mismatch / inconclusive")
stub_brain = types.SimpleNamespace(
    cfg={"verify_model": ""},
    model="deepseek-flash",
    post=lambda payload: {"choices": [{"message": {"content":
        '{"found": true, "evidence": "气泡显示「你好呀」"}}'}}]})
v = Verifier(stub_brain)
case("verify_model 留空 = 沿用主 model", v.model == "deepseek-flash", v.model)
r = v.verify_send("你好呀", png_path)
case("found=true → confirmed",
     r.ok and r.verdict == "confirmed" and "你好呀" in r.detail, r.to_dict().__str__())
r = v.verify_send("你好呀", "不存在的路径.png")
case("截图文件缺失 → inconclusive（ok=False，不抛异常）",
     (not r.ok) and r.verdict == "inconclusive" and "读不到" in r.detail, r.to_dict().__str__())

stub_brain.post = lambda payload: {"choices": [{"message": {"content":
    '{"found": false, "evidence": "最新气泡是别的消息"}}'}}]}
r = v.verify_send("你好呀", png_path)
case("found=false → mismatch（校验过程可信）",
     r.ok and r.verdict == "mismatch", r.to_dict().__str__())

stub_brain.post = lambda payload: {"choices": [{"message": {"content": "我看到了。"}}]}
r = v.verify_send("你好呀", png_path)
case("回复不是 JSON → inconclusive",
     (not r.ok) and r.verdict == "inconclusive" and "JSON" in r.detail, r.to_dict().__str__())


def boom(payload):
    raise EC.AppError("E-LLM-003", "超时")


stub_brain.post = boom
r = v.verify_send("你好呀", png_path)
case("视觉模型调用失败 → inconclusive 且带码",
     (not r.ok) and r.verdict == "inconclusive" and "E-LLM-003" in r.detail, r.to_dict().__str__())

case("空文本直接 inconclusive",
     v.verify_send("", png_path).verdict == "inconclusive", "")

# ============================================================ payload 形状
print("§4 视觉请求的 payload 形状")
captured: dict = {}


def capture_post(payload: dict) -> dict:
    captured.update(payload)
    return {"choices": [{"message": {"content": '{"found": true, "evidence": "有"}'}}]}


stub_brain.post = capture_post
v.verify_send("测试文本", png_path)
msg = captured["messages"][0]
case("content 是 text+image_url 两段",
     [c["type"] for c in msg["content"]] == ["text", "image_url"], str(captured)[:150])
case("image_url 是 data URI", captured and
     msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"), "")
case("提示词里带待校验文本", "测试文本" in msg["content"][0]["text"], "")
case("请求带 model 与 max_tokens",
     captured["model"] == "deepseek-flash" and captured["max_tokens"] == 200, "")

# ============================================================ Brain 集成
print("§5 Brain 集成：send_text 成功后自动校验")
ex = StubExecutor(shot=Shot(True, path=png_path))
replies = [
    # poster 返回的必须是**完整响应**（{"choices":[...]}），不是裸 message
    {"choices": [{"message": ai_msg(tool_calls=[tc("s1", "send_text", {"text": "你好呀"})])}]},
    {"choices": [{"message": {"content":                              # ② 视觉校验
        '{"found": true, "evidence": "气泡确认"}}'}}]},
    {"choices": [{"message": ai_msg(content="已发送并经视觉确认。")}]},  # ③ chat → 答复
]
payloads: list[dict] = []
seq = list(replies)
cu = dict(CU_DEFAULTS)          # verify_sends 默认 True
cu["api_key"] = "sk-test-12345678"
b = Brain(cu, {}, poster=lambda p: (payloads.append(p), seq.pop(0))[1])
r = b.run(ex, "发消息")
tool_payload = payloads[2]      # ①chat ②vision ③chat
tool_msg = [m for m in tool_payload["messages"]
            if m.get("role") == "tool" and m.get("tool_call_id") == "s1"][0]
content = json.loads(tool_msg["content"])
case("verify 结论进回灌信封",
     content["ok"] and content["verify"]["verdict"] == "confirmed", str(content)[:200])
case("视觉调用确实发生（payload ② 无 tools 键）",
     len(payloads) == 3 and "tools" not in payloads[1], f"payloads={len(payloads)}")
case("步骤事件里有 verified",
     any(s["type"] == "verified" for s in r["steps"]), str([s["type"] for s in r["steps"]]))

# verify_sends=false → 不产生视觉调用
ex2 = StubExecutor(shot=Shot(True, path=png_path))
payloads2: list[dict] = []
seq2 = [{"choices": [{"message": ai_msg(tool_calls=[tc("s2", "send_text", {"text": "你好"})])}]},
        {"choices": [{"message": ai_msg(content="完成。")}]}]
cu2 = dict(CU_DEFAULTS)
cu2["api_key"] = "sk-test-12345678"
cu2["verify_sends"] = False
b2 = Brain(cu2, {}, poster=lambda p: (payloads2.append(p), seq2.pop(0))[1])
r2 = b2.run(ex2, "发消息")
case("verify_sends=false → 只有 2 次 chat 调用",
     r2["ok"] and len(payloads2) == 2, f"payloads={len(payloads2)}")

# 截图失败 → 校验 inconclusive 但不推翻发送
ex3 = StubExecutor(shot=Shot(False, error="抓图失败"))
seq3 = [{"choices": [{"message": ai_msg(tool_calls=[tc("s3", "send_text", {"text": "你好"})])}]},
        {"choices": [{"message": ai_msg(content="完成。")}]}]
payloads3: list[dict] = []
cu3 = dict(CU_DEFAULTS)
cu3["api_key"] = "sk-test-12345678"
b3 = Brain(cu3, {}, poster=lambda p: (payloads3.append(p), seq3.pop(0))[1])
r3 = b3.run(ex3, "发消息")
# 截图失败 → 没有视觉调用（①chat ②chat，共 2 个 payload），verify 直接 inconclusive
tool_msg3 = [m for m in payloads3[1]["messages"]
             if m.get("role") == "tool" and m.get("tool_call_id") == "s3"][0]
content3 = json.loads(tool_msg3["content"])
case("截图失败 → verify.inconclusive，发送结果不受影响",
     content3["ok"] and content3["verify"]["verdict"] == "inconclusive"
     and "抓图失败" in content3["verify"]["detail"], str(content3)[:200])

# ============================================================
print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
