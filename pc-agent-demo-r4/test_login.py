# -*- coding: utf-8 -*-
"""
test_login.py —— 「自动登录」与「连续对话常开」的离线回归

## 为什么要有这个测试

这里钉住的是 2026-10-10 真机实测踩出来的**两个真事故**
（「连续对话常开」那部分在三树共有的 test_text_port.py 里，见 §4b）：

  1. **登录按钮定位陷阱**：QQ 登录页上真正的按钮类名是 `login-btn`，
     外面包了一层 `login-btn-wrap` —— 它**含有 `login-btn` 这个子串**。
     老代码 `_find_by(cls_has="login-btn")` 是「DFS 先访问父节点、命中即返回」，
     于是永远拿到那个容器（GroupControl）。容器没有默认动作，
     `DoDefaultAction` 必抛 `COMError(-2146233079)` ——
     表现出来就是「程序不会自己去按登录键、卡在登录界面」。
     判据：必须在「容器 + 真按钮」同时在场的候选里**挑出真按钮**。

  2. **DPI 感知的假承诺**：本机 150% 缩放，进程若 DPI-unaware，
     win32 坐标会被虚拟化成逻辑像素（1707x1067 基准），而 UIA 的
     `BoundingRectangle` 永远是物理像素（2560x1600 基准）—— 差 1.5 倍。
     后果之一是 `PrintWindow` 按逻辑尺寸建位图 → **抓图全黑**。
     老写法的 `enable_dpi_awareness()` 把「设过了」直接写进返回值，
     而 `SetProcessDpiAwareness` 失败时 ctypes **不会抛异常**（返回 HRESULT），
     于是它会谎报成功。判据：成功与否必须**回读**，返回值里必须有 `now`。

  3. **连续对话常开**：`timeout_seconds = 0`（或负数）= 永不超时。
     这不是可有可无的细节 —— 历史上好几处取值点用 `or 1800` 兜底，
     而 `or` 会把合法的 **0** 当假值丢掉，"常开"于是永远配不出来。
     判据：0 与负数都能真的落进常开分支，且 `cont_window()` 不吞 0。

## 不需要什么

不需要 QQ、不需要隐藏桌面、不需要网络、不发送任何东西。
UIA 那几处依赖被替换成替身，只让**排序/判定逻辑**在真实代码里跑。

运行：
    python test_login.py
"""

from __future__ import annotations

import inspect
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# ⚠️ 必须先把 QQ_AGENT_HOME 指到临时目录再 import agent ——
# agent 在**它自己的导入期**就把数据目录定死，导入后再改已经晚了。
TMP = tempfile.mkdtemp(prefix="qqagent-login-")
os.environ["QQ_AGENT_HOME"] = TMP

from app import hostagent as H          # noqa: E402
from app import winmsg as W             # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}  {detail}")


def section(title: str) -> None:
    print(f"\n{'-' * 72}\n{title}")


# ============================================================ 替身
class _Rect:
    """`_describe` 只读 left/top/right/bottom 四个属性。"""

    def __init__(self, l: int, t: int, r: int, b: int) -> None:
        self.left, self.top, self.right, self.bottom = l, t, r, b


class FakeCtrl:
    """最小可用的 UIA 控件替身。属性名与 uiautomation 一致。"""

    def __init__(self, cls: str, name: str = "", ctype: str = "Button",
                 enabled: bool = True, rect=(0, 0, 200, 60)) -> None:
        self.ClassName = cls
        self.Name = name
        self.ControlTypeName = ctype
        self.IsEnabled = enabled
        self.BoundingRectangle = _Rect(*rect)

    def __repr__(self) -> str:                       # 失败信息里要能看出是哪个
        return f"<FakeCtrl {self.ClassName!r}/{self.ControlTypeName}>"


def patch_uia(cls_cands, name_cands, pats):
    """
    替换掉 hostagent 里三处「要真 UIA 才能跑」的依赖，返回还原函数。

        伪 `_collect_by`      → 直接给出候选（按 cls_has / name_eq 分流）
        伪 `_patterns_available` → 从 pats 表里取（key = id(ctrl)）
        `_describe` 不替换：它只读属性，替身已经满足，让真实代码跑

    这样 `_find_login_button` 的**打分与排序**完全在真实实现里执行 ——
    那正是出 bug 的地方。
    """
    old_collect, old_pats = H._collect_by, H._patterns_available

    def fake_collect(hwnd, cls_has="", name_eq="", limit=40):
        return list(cls_cands) if cls_has else list(name_cands)

    H._collect_by = fake_collect
    H._patterns_available = lambda c: dict(pats.get(id(c), {}))

    def restore():
        H._collect_by, H._patterns_available = old_collect, old_pats
    return restore


# ============================================================ §1 DPI 感知
section("§1 DPI 感知：成功与否必须回读，不能靠设置调用的返回值")

_w_first = W.dpi_awareness_now()
check("dpi_awareness_now() 返回可读等级（不是崩溃）",
      isinstance(_w_first, str) and not _w_first.startswith("回读失败"),
      f"now={_w_first!r}")

_r1 = W.enable_dpi_awareness()
check("enable_dpi_awareness 返回 ok=True", _r1.get("ok") is True, f"{_r1!r}")
check("返回值里带 now（回读字段）—— 没有它就是旧版的「假承诺」",
      "now" in _r1, f"keys={list(_r1)}")
check("回读等级落在 per-monitor / system（不是 unaware）",
      _r1.get("now") in ("per-monitor", "system"), f"now={_r1.get('now')!r}")
check("how 是可读的尝试记录", isinstance(_r1.get("how"), str) and _r1["how"] != "",
      f"how={_r1.get('how')!r}")

_r2 = W.enable_dpi_awareness()
check("幂等：第二次调用标 already，不改结论",
      _r2.get("already") is True and _r2.get("now") == _r1.get("now"), f"{_r2!r}")

# ============================================================ §2 click_at 守卫
section("§2 click_at：取不到窗口位置时必须明确失败，而不是假装成功")

_cl = W.click_at(0, 100, 200)
check("hwnd=0 → ok=False", _cl.get("ok") is False, f"{_cl!r}")
check("hwnd=0 → 有可读的 error",
      isinstance(_cl.get("error"), str) and _cl["error"] != "", f"{_cl!r}")

# ============================================================ §3 登录按钮定位
section("§3 登录按钮定位：容器与真按钮同时在候选里时，必须挑出真按钮")

wrap = FakeCtrl("q-button q-button--primary q-button--middle login-btn-wrap",
                name="", ctype="GroupControl", rect=(1074, 939, 1486, 999))
real = FakeCtrl("q-button q-button--primary q-button--middle login-btn",
                name="登录", ctype="Button", rect=(1074, 939, 1486, 999))
pats_wrap = {"invoke": False, "legacy": True, "toggle": False}
pats_real = {"invoke": False, "legacy": True, "toggle": False}

# --- 3a 真机现场复现：两个都在，都是 legacy-only ---
restore = patch_uia([wrap, real], [], {id(wrap): pats_wrap, id(real): pats_real})
try:
    got, cands = H._find_login_button(0)
finally:
    restore()
check("3a 挑中的是真按钮 login-btn（不是 login-btn-wrap）",
      got is real, f"got={got!r}")
check("3a 候选现场里两个都在（事后可复盘）",
      len(cands) == 2, f"n={len(cands)}")
check("3a 候选现场带 desc.class / patterns",
      all("desc" in c and "patterns" in c for c in cands), f"{cands!r}")
check("3a 真按钮排在第一位（排序是真的按优先级）",
      cands[0]["desc"].get("class", "").endswith("login-btn"), f"{cands[0]!r}")

# --- 3b 容器有 Invoke、真按钮没有：exact class 仍然是第一优先级 ---
restore = patch_uia([wrap, real], [],
                    {id(wrap): {"invoke": True, "legacy": True},
                     id(real): {"invoke": False, "legacy": True}})
try:
    got_b, _ = H._find_login_button(0)
finally:
    restore()
check("3b 精确类名压过 invoke（避免点到容器上）", got_b is real, f"got={got_b!r}")

# --- 3c 名字叫「登录」但不是真按钮（只有名字命中） ---
tab = FakeCtrl("q-tab", name="登录", ctype="TabItem", rect=(0, 0, 80, 30))
restore = patch_uia([], [tab], {id(tab): {"invoke": True, "legacy": True}})
try:
    got_c, cands_c = H._find_login_button(0)
finally:
    restore()
check("3c 只有名字命中时仍会返回它（有总比没有好）", got_c is tab, f"got={got_c!r}")
check("3c 候选现场非空", len(cands_c) == 1, f"n={len(cands_c)}")

# --- 3d 界面结构变了：一个候选都没有 → (None, []) ---
restore = patch_uia([], [], {})
try:
    got_d, cands_d = H._find_login_button(0)
finally:
    restore()
check("3d 无候选 → (None, [])", got_d is None and cands_d == [],
      f"got={got_d!r} cands={cands_d!r}")

# --- 3e 源码级守门：定位必须走 _find_login_button，不能再退回 _find_by ---
_src = open(os.path.join(HERE, "app", "hostagent.py"), encoding="utf-8").read()
check("3e login_state 走 _find_login_button",
      _src.count("btn, cands = _find_login_button(hwnd)") >= 1,
      f"count={_src.count('btn, cands = _find_login_button(hwnd)')}")
check("3e 源码里不再有旧的 _find_by(cls_has=LOGIN_BTN_CLASS) 写法",
      "_find_by(hwnd, cls_has=LOGIN_BTN_CLASS)" not in _src)

# ============================================================ §4 重名守门
section("§4 hostagent 里两个「收集候选」的函数不能同名（同名会静默覆盖）")

_p_collect = list(inspect.signature(H._collect_by).parameters)
_p_old = list(inspect.signature(H._find_all_by).parameters)
check("_collect_by 支持 name_eq", "name_eq" in _p_collect, f"{_p_collect}")
check("_find_all_by 只吃 cls_has（旧签名的既有调用依赖它）",
      "cls_has" in _p_old and "name_eq" not in _p_old, f"{_p_old}")
check("两者是两个不同的函数对象", H._collect_by is not H._find_all_by)
# 从源码扫顶层 def 名 —— 比 dir() 靠得住：dir() 只能看到「最后生效」的那个，
# 而被覆盖的定义在 dir() 里是**看不见**的（这正是本次事故的隐蔽之处）。
_defs = re.findall(r"^def (\w+)", _src, re.M)
_dups = sorted({n for n in _defs if _defs.count(n) > 1})
check("hostagent 顶层函数无重名（同名会静默覆盖，且不报错）",
      _dups == [], f"dups={_dups}")

# ============================================================ §5 措辞契约
section("§5 do_login 的措辞契约：hostd 靠「没找到」四个字分流排查方向")

_old_flb, _old_pats2 = H._find_login_button, H._patterns_available
try:
    # --- 5a 根本没找到按钮 ---
    H._find_login_button = lambda hwnd: (None, [])
    o_a = H.do_login(0, check_auto=False)
    check("5a error 含「没找到」", "没找到" in o_a.get("error", ""), f"{o_a.get('error')!r}")

    # --- 5b 找到了、但三条点击路径都断了 ---
    H._find_login_button = lambda hwnd: (real, [{"desc": H._describe(real),
                                                 "patterns": pats_real}])
    H._patterns_available = lambda c: {"invoke": False, "legacy": False, "toggle": False}
    o_b = H.do_login(0, check_auto=False)
finally:
    H._find_login_button, H._patterns_available = _old_flb, _old_pats2

check("5b ok=False", o_b.get("ok") is False, f"{o_b!r}")
check("5b error **不含**「没找到」（否则排查方向会被带偏）",
      "没找到" not in o_b.get("error", ""), f"{o_b.get('error')!r}")
check("5b error 说清了「是点不动、不是没看到」",
      "三条点击路径" in o_b.get("error", ""), f"{o_b.get('error')!r}")
check("5b 带 click_attempts 现场",
      isinstance(o_b.get("click_attempts"), list) and len(o_b["click_attempts"]) >= 1,
      f"{o_b.get('click_attempts')!r}")
check("5b 带 click_at 现场（第三条路的返回）", "click_at" in o_b, f"keys={list(o_b)}")

# ============================================================ §6 纯色图识别
section("§6 抓图统计：纯色图必须能被认出来（别再假装「抓到了内容」）")

# 2026-10-10 踩过：一台渲染进程已死的残留 QQ，PrintWindow 返回 0、图纯黑，
# 而当时只看得见「颜色种类=1 平均亮度=0」，于是被误判成坐标系问题（查错方向）。
# 现在 capture() 会显式给出 blank 标记，这一节把它的前提钉住：
# blank 的判据就是 distinct_colors <= 1，而 distinct_colors 由 _blackness_stats 算。

_w, _h = 16, 8
_black = bytes(_w * _h * 4)                                   # 全黑 BGRX
_sb = W._blackness_stats(_black, _w, _h)
check("全黑 buffer → distinct_colors=1", _sb.get("distinct_colors") == 1, f"{_sb!r}")
check("全黑 buffer → nonblack_ratio=0", _sb.get("nonblack_ratio") == 0.0, f"{_sb!r}")
check("全黑 buffer → mean_luma=0", _sb.get("mean_luma") == 0.0, f"{_sb!r}")
check("全黑 ⇒ blank 判据成立（distinct_colors<=1）",
      bool(_sb.get("distinct_colors", 1) <= 1) is True)

_red = bytes([0, 0, 255, 0]) * (_w * _h)                      # BGRX 纯红
_sr = W._blackness_stats(_red, _w, _h)
# 「单色」= distinct_colors 只有 1 种。纯红和纯黑一样是单色，同样算空白 ——
# 真实界面不可能只有一种颜色。所以 blank 的判据是 distinct_colors<=1，
# **不是**「亮度是不是 0」（那会把纯色非黑图漏掉）。
check("纯红 buffer → distinct_colors=1（单色，同样算 blank）",
      _sr.get("distinct_colors") == 1, f"{_sr!r}")
check("但它的亮度不是 0 —— 说明 blank 不能只看亮度",
      _sr.get("mean_luma", 0) > 0 and _sr.get("nonblack_ratio") == 1.0, f"{_sr!r}")

_mix = bytes([0, 0, 0, 0]) * (_w * _h // 2) + bytes([255, 255, 255, 0]) * (_w * _h // 2)
_sm = W._blackness_stats(_mix, _w, _h)
check("黑白各半 → distinct_colors=2", _sm.get("distinct_colors") == 2, f"{_sm!r}")
check("黑白各半 → nonblack_ratio≈0.5", abs(_sm.get("nonblack_ratio", 0) - 0.5) < 0.2,
      f"{_sm!r}")
check("buffer 长度不足时不崩（返回全零统计）",
      W._blackness_stats(b"", _w, _h).get("distinct_colors") == 0)
check("capture() 的源码里有 blank 标记（不是只有统计量）",
      "info[\"blank\"]" in open(os.path.join(HERE, "app", "winmsg.py"),
                               encoding="utf-8").read())

# ---------------------------------------------------------------- 收尾
print(f"\n结果：{PASS} 过 / {FAIL} 挂")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
