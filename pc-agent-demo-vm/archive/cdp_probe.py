# -*- coding: utf-8 -*-
"""
cdp_probe.py — 方案 A 可行性探针（CDP = Chrome DevTools Protocol）

要解决什么问题
--------------
UIA 路线里「把文字写进输入框」这一步必须抢占前台，副作用是把 QQ 窗口顶到最上层、
抢走你的焦点、还可能把按键漏进别的窗口。CDP 路线绕过整个问题：
让 QQNT 以 `--remote-debugging-port=9222` 启动，我们直接连到它的**渲染层**，
用 DevTools 协议读写 DOM —— 不碰窗口、不碰焦点、不产生任何按键。

走通之后，QQ 窗口最小化、被完全遮住，照样能收发消息。

怎么用
------
第 0 步：重启 QQ，带上调试参数（原来的无障碍参数可以保留）
    "D:\\QQ.exe" --remote-debugging-port=9222 --remote-allow-origins=* --force-renderer-accessibility

第 1 步：看端口通不通、有哪些可注入的目标
    python -X utf8 cdp_probe.py

第 2 步：连上去，找 QQ 的输入框和消息列表
    python -X utf8 cdp_probe.py --dom

第 3 步：实测能不能把文字写进输入框（写完会尝试清空，**绝不发送**）
    python -X utf8 cdp_probe.py --type "CDP 写入测试 ABC123"

安全承诺
--------
本脚本**永远不会**触发发送按钮、不会按回车、不会提交任何消息。
--type 模式只做「写入 → 回读 → 尽力清空」，最坏情况是输入框里留一段测试文字。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_PORT = 9222

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from websockets.sync.client import connect as ws_connect
except ImportError:
    print("[X] 缺少 websockets。请先执行：pip install websockets")
    raise SystemExit(2)


# ---------------------------------------------------------------- HTTP 侧
def http_json(port: int, path: str, timeout: float = 3.0):
    """读 Chromium 的调试 HTTP 接口（/json/version、/json/list）。"""
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, headers={"Host": f"127.0.0.1:{port}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)


def probe_port(port: int) -> dict | None:
    """探测端口是不是 CDP，是就返回 /json/version 的内容。"""
    try:
        ver = http_json(port, "/json/version")
    except urllib.error.URLError as exc:
        print(f"[X] 连不上 127.0.0.1:{port} —— {exc.reason}")
        return None
    except Exception as exc:                       # noqa: BLE001
        print(f"[X] 127.0.0.1:{port} 有响应，但不是 CDP：{exc}")
        return None
    if not isinstance(ver, dict) or "Browser" not in ver:
        print(f"[X] 127.0.0.1:{port} 返回的不是 CDP 握手信息：{str(ver)[:200]}")
        return None
    return ver


# ---------------------------------------------------------------- CDP 会话
class Session:
    """极简 CDP 会话：发一条命令，等它对应的回包，事件一律丢弃。"""

    def __init__(self, ws_url: str, timeout: float = 6.0):
        self.timeout = timeout
        self._id = 0
        # origin=None：不带 Origin 头，避免被 --remote-allow-origins 卡住
        self.ws = ws_connect(ws_url, open_timeout=5, close_timeout=2, origin=None)

    def call(self, method: str, params: dict | None = None):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}},
                                ensure_ascii=False))
        while True:
            raw = self.ws.recv(timeout=self.timeout)
            if raw is None:
                raise RuntimeError("WebSocket 已关闭")
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method} 失败：{msg['error']}")
                return msg.get("result", {})

    def evaluate(self, expr: str):
        res = self.call("Runtime.evaluate", {
            "expression": expr,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if "exceptionDetails" in res:
            raise RuntimeError(f"页面里执行 JS 报错：{res['exceptionDetails'].get('text')}")
        return res.get("result", {}).get("value")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


# 在页面里跑：找 QQ 的输入框 / 消息列表
JS_INSPECT = r"""
(function () {
  function pick(sel) { return document.querySelector(sel); }
  var wrap = pick('.ExEditor-qq-msg-editor');
  var ce = wrap ? (wrap.querySelector('[contenteditable="true"]') || wrap)
                : pick('[contenteditable="true"]');
  var list = pick('.ml-list');
  return JSON.stringify({
    href: location.href,
    title: document.title,
    editorFound: !!ce,
    editorClass: ce ? String(ce.className || '') : null,
    editorEditable: ce ? (ce.getAttribute('contenteditable') || '') : '',
    msgListFound: !!list,
    msgItemCount: document.querySelectorAll('.ml-item').length,
    sendBtnFound: !!pick('.send-msg')
  });
})()
"""

# 把文字插进已聚焦的可编辑元素（走浏览器输入管线，ProseMirror 能收到）
JS_FOCUS_EDITOR = r"""
(function () {
  var wrap = document.querySelector('.ExEditor-qq-msg-editor');
  var ce = wrap ? (wrap.querySelector('[contenteditable="true"]') || wrap)
                : document.querySelector('[contenteditable="true"]');
  if (!ce) { return null; }
  ce.focus();
  var r = ce.getBoundingClientRect();
  return JSON.stringify({ tag: ce.tagName, cls: String(ce.className || ''),
                          x: r.left + r.width / 2, y: r.top + r.height / 2 });
})()
"""

JS_READ_EDITOR = r"""
(function () {
  var wrap = document.querySelector('.ExEditor-qq-msg-editor');
  var ce = wrap ? (wrap.querySelector('[contenteditable="true"]') || wrap)
                : document.querySelector('[contenteditable="true"]');
  return ce ? (ce.innerText || ce.textContent || '') : null;
})()
"""

JS_CLEAR_EDITOR = r"""
(function () {
  var wrap = document.querySelector('.ExEditor-qq-msg-editor');
  var ce = wrap ? (wrap.querySelector('[contenteditable="true"]') || wrap)
                : document.querySelector('[contenteditable="true"]');
  if (!ce) { return 'no-editor'; }
  ce.focus();
  document.execCommand('selectAll', false, null);
  document.execCommand('delete', false, null);
  return 'ok';
})()
"""


def list_targets(port: int) -> list[dict]:
    try:
        raw = http_json(port, "/json/list")
    except Exception as exc:                       # noqa: BLE001
        print(f"[X] 读 /json/list 失败：{exc}")
        return []
    return [t for t in raw if t.get("webSocketDebuggerUrl")]


def print_targets(targets: list[dict]) -> None:
    print(f"\n共 {len(targets)} 个可连接目标：")
    for i, t in enumerate(targets):
        print(f"  [{i}] type={t.get('type'):<14} title={t.get('title')!r}")
        print(f"      url={str(t.get('url'))[:110]}")


def step1(port: int) -> list[dict]:
    print("=" * 72)
    print(f"第 1 步：探测 127.0.0.1:{port} 是否是 CDP 调试端口")
    print("=" * 72)
    ver = probe_port(port)
    if ver is None:
        print("\n结论：CDP 不可用 ❌")
        print("  最可能的原因：QQ 启动时没带 --remote-debugging-port 参数。")
        print("  请完全退出 QQ 后重新启动：")
        print(f'    "D:\\QQ.exe" --remote-debugging-port={port} '
              f"--remote-allow-origins=* --force-renderer-accessibility")
        return []
    print(f"[✓] CDP 可用")
    for k in ("Browser", "Protocol-Version", "User-Agent", "webSocketDebuggerUrl"):
        if k in ver:
            print(f"    {k:<18} = {str(ver[k])[:96]}")
    targets = list_targets(port)
    print_targets(targets)
    print("\n接下来跑：python -X utf8 cdp_probe.py --dom")
    return targets


def find_qq_target(port: int, targets: list[dict]) -> tuple[dict, dict] | None:
    """逐个目标跑探测脚本，返回第一个能找到 QQ 输入框的目标。"""
    print("\n逐个目标探测 QQ 界面结构……")
    for t in targets:
        name = f"{t.get('type')}/{str(t.get('title'))[:28]}"
        try:
            sess = Session(t["webSocketDebuggerUrl"])
        except Exception as exc:                   # noqa: BLE001
            print(f"  [跳过] {name} —— 连不上：{exc}")
            continue
        try:
            raw = sess.evaluate(JS_INSPECT)
            info = json.loads(raw) if raw else {}
            hit = bool(info.get("editorFound")) or bool(info.get("msgListFound"))
            mark = "命中" if hit else "无关"
            print(f"  [{mark}] {name}  editor={info.get('editorFound')} "
                  f"msgList={info.get('msgListFound')} items={info.get('msgItemCount')}")
            if hit:
                return t, info
        except Exception as exc:                   # noqa: BLE001
            print(f"  [跳过] {name} —— 探测失败：{exc}")
        finally:
            sess.close()
    return None


def step2(port: int, targets: list[dict]) -> None:
    print("\n" + "=" * 72)
    print("第 2 步：在渲染层里定位 QQ 的输入框与消息列表")
    print("=" * 72)
    if not targets:
        print("[X] 没有可连接目标。请先跑第 1 步。")
        return
    found = find_qq_target(port, targets)
    if not found:
        print("\n结论：没找到挂着 QQ 聊天的渲染层 ❌")
        print("  可能原因：①当前没打开任何聊天窗口；②QQ 把界面拆在别的进程里。")
        print("  请先在 QQ 里点开一个会话，再重跑。")
        return
    t, info = found
    print(f"\n[✓] 找到目标：{t.get('title')!r}")
    print(f"    url            = {info.get('href')}")
    print(f"    editorFound    = {info.get('editorFound')}")
    print(f"    editorClass    = {info.get('editorClass')}")
    print(f"    contenteditable= {info.get('editorEditable')!r}")
    print(f"    msgListFound   = {info.get('msgListFound')}")
    print(f"    msgItemCount   = {info.get('msgItemCount')}")
    print(f"    sendBtnFound   = {info.get('sendBtnFound')}")
    print("\n接下来跑：python -X utf8 cdp_probe.py --type \"CDP 写入测试\"")
    print("（该模式只写入 + 回读 + 清空，绝不发送）")


def step3(port: int, targets: list[dict], text: str) -> int:
    print("\n" + "=" * 72)
    print("第 3 步：实测 Input.insertText 能不能写进 QQ 输入框（绝不发送）")
    print("=" * 72)
    found = find_qq_target(port, targets)
    if not found:
        print("[X] 没找到可注入的 QQ 渲染层，先跑第 2 步排查。")
        return 2
    t, _ = found

    sess = Session(t["webSocketDebuggerUrl"], timeout=8.0)
    try:
        before = sess.evaluate(JS_READ_EDITOR)
        print(f"\n[1] 写入前输入框内容：{before!r}")
        if before:
            print("    ⚠️ 输入框不是空的 —— 为避免搅乱你的草稿，本次只做只读探测，不写入。")
            print("       请先手动清空输入框再重跑。")
            return 1

        focus_info = sess.evaluate(JS_FOCUS_EDITOR)
        print(f"[2] 聚焦可编辑元素：{focus_info}")
        if not focus_info:
            print("[X] 页面上找不到 contenteditable 输入框。")
            return 2

        sess.call("Input.insertText", {"text": text})
        time.sleep(0.35)
        got = sess.evaluate(JS_READ_EDITOR)
        print(f"[3] insertText 之后回读：{got!r}")

        ok = (got is not None) and (text.strip() in (got or ""))
        print(f"    写入生效 = {ok}")

        print("[4] 尝试清空（selectAll + delete）……")
        clr = sess.evaluate(JS_CLEAR_EDITOR)
        time.sleep(0.35)
        after = sess.evaluate(JS_READ_EDITOR)
        print(f"    清空动作 = {clr}，清空后内容 = {after!r}")

        print("\n结论：CDP 写入链路 " + ("可用 ✅（全程未发送任何消息）" if ok else "不可用 ❌"))
        if ok and (after or "").strip():
            print("  注意：清空没成功，输入框里可能残留测试文字，请手动删掉。")
        if not ok:
            print("  下一步：把 --type 的输出发我，我改用 Input.dispatchKeyEvent 合成按键重试。")
        return 0 if ok else 1
    finally:
        sess.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="方案 A：CDP 远程调试可行性探针")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="调试端口（默认 9222）")
    ap.add_argument("--dom", action="store_true", help="只做第 2 步：定位输入框与消息列表")
    ap.add_argument("--type", metavar="TEXT", help="第 3 步：实测写入输入框（绝不发送）")
    args = ap.parse_args()

    targets = step1(args.port)
    if args.dom or args.type:
        if not targets:
            return 2
        step2(args.port, targets)
    if args.type:
        return step3(args.port, targets, args.type)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
