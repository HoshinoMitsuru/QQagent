# -*- coding: utf-8 -*-
"""
errors.py —— WebUI 侧的错误适配层

错误码目录本体在项目根目录的 `error_codes.py`（`agent.py` 也在用它）。
这里只做三件事：

    1. 把目录导进来（顺手把项目根加进 sys.path，免得依赖调用方的启动方式）
    2. 提供统一的 JSON 信封 `envelope()` —— 所有失败响应长得一样
    3. 提供 `from_exception()` —— 把原生异常翻译成带码的信封

## 统一信封长什么样

```json
{
  "ok": false,
  "code": "E-QQ-004",
  "severity": "error",
  "error": "QQ 未以无障碍模式启动（UIA 树是空的）",
  "hint": "点界面上的「重启 QQ 到可读状态」（会完全退出后带参拉起，登录态保留）",
  "causes": ["启动时没带 --force-renderer-accessibility", "..."],
  "fixes": ["...", "..."],
  "detail": "原始异常或现场细节",
  "context": {"session_count": 0}
}
```

**为什么必须带 `code`**：界面上弹一句「执行失败」等于没说话 ——
用户下一步只能来问「为什么失败」。带了码就能自查：码 → 判据 → 动作，
而且这个码可以直接写进 issue / 聊天记录，双方说的是同一件事。
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import error_codes as EC          # noqa: E402

AppError = EC.AppError


def envelope(code: str, detail: str = "", context: dict | None = None) -> dict:
    """把错误码展开成完整信封。未知码也能安全展开（不会二次报错）。"""
    it = EC.get(code)
    return {
        "ok": False,
        "code": it["code"],
        "severity": it["severity"],
        "error": it["title"],
        "hint": (it["fixes"][0] if it["fixes"] else ""),
        "causes": list(it["causes"]),
        "fixes": list(it["fixes"]),
        "detail": str(detail)[:800] if detail else "",
        "context": {str(k): str(v) for k, v in (context or {}).items() if v is not None},
    }


def from_exception(exc: BaseException, default_code: str,
                   context: dict | None = None) -> dict:
    """原生异常 → 信封。映射不出来的话用 default_code，不瞎猜。"""
    if isinstance(exc, EC.AppError):
        env = envelope(exc.code, exc.detail_text, {**exc.context, **(context or {})})
        return env
    err = EC.wrap(exc, default_code)
    return envelope(err.code, err.detail_text, context)


def ok(payload: dict | None = None, **extra) -> dict:
    """成功信封。不用也行（很多接口直接回自己的结构），但失败一律用 envelope()。"""
    out = {"ok": True}
    if payload:
        out.update(payload)
    out.update(extra)
    return out


def severity_of(code: str) -> str:
    return EC.severity(code)


def catalog_summary() -> dict:
    """给「诊断报告」用：错误码目录的规模与自检结果。"""
    by_domain: dict[str, int] = {}
    for it in EC.CATALOG.values():
        by_domain[it["domain"]] = by_domain.get(it["domain"], 0) + 1
    return {
        "total": len(EC.CATALOG),
        "domains": {EC.DOMAINS.get(d, d): n for d, n in sorted(by_domain.items())},
        "problems": EC.audit(),
    }
