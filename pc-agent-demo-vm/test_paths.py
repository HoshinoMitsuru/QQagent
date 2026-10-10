# -*- coding: utf-8 -*-
"""
test_paths.py —— 数据目录解析（app/paths.py）+ 「agent 与壳同源」自检

## 为什么有这个文件（2026-10-10）

打包版 exe 的数据目录是**它自己所在的 dist 目录**，与项目根目录的 config.json
完全脱钩：人在项目根填好 API Key，双击 dist 里的 exe 却读到一份新建的空配置，
日志只有一句「没有可用的 API Key」——**没有一个字说明「你填的那份不是我在读的那份」**。
用户真实用法（把整个目录拷到另一台机器 → 双击 exe）正好踩这条，实测复现过。

本文件钉住两件事：

  A. 解析顺序（全部离线，替身环境，不碰真实目录）：

     1. `QQ_AGENT_HOME` 最高优先
     2. exe 目录里**已有我们的 config.json** → 用它（便携部署优先，不被上一级抢走）
     3. exe 目录没有、**上一级有我们的 config.json** → 用上一级 ← 本次新增的规则
     4. 都没有 → exe 目录（可写就地在旁边新建）
     5. exe 目录不可写 → `%LOCALAPPDATA%\\QQAgent` 兜底

     ⛔ 「别人的 config.json」不许被采用 —— 只看「文件在不在」太危险：
     用户把 exe 丢进任何恰好放着别人 config.json 的目录都会被悄悄接管，
     那种故障不报错、只是行为诡异，比现在这个更难查。

  B. 单一事实来源：`agent._resolve_home()` 必须与壳（`app.paths`）解析出**同一个**
     目录。这两处原来各写了一遍，而 `paths` 多两条规则；`qq-agent.exe --run-agent
     --selftest` 这类直接入口**不 import app.paths**，exe 落在不可写目录时两边就分叉
     —— 表现成「界面上填了密钥、命令行里说没有」。用子进程真造一个不可写的 exe
     目录实测（不是读源码猜）。

用法：python test_paths.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SANDBOX = tempfile.mkdtemp(prefix="qqagent-paths-")
#: 兜底目录（LOCALAPPDATA）也搬进沙箱：用例 [5] 会走到那里，不能污染真实用户目录。
_OLD_LOCALAPPDATA = os.environ.get("LOCALAPPDATA")
os.environ["LOCALAPPDATA"] = os.path.join(SANDBOX, "localappdata")
APP_DATA = os.path.join(SANDBOX, "localappdata")

#: ⚠️ 必须在 import app.paths **之前**设：它在导入期就把 DATA_DIR 定死并写进
#: 这个环境变量，晚一步就晚了。
os.environ["QQ_AGENT_HOME"] = os.path.join(SANDBOX, "home")
_OLD_HOME = os.environ["QQ_AGENT_HOME"]

from app import paths          # noqa: E402
import agent as A              # noqa: E402

OK = FAIL = 0


def case(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def write_cfg(dir_path: str, content) -> str:
    """写一份 config.json。content 是 dict 就 dump，是 str 就原样写（用来造坏文件）。"""
    os.makedirs(dir_path, exist_ok=True)
    path = os.path.join(dir_path, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(content, str):
            f.write(content)
        else:
            json.dump(content, f, ensure_ascii=False)
    return path


OURS = {"llm": {"api_key": "sk-test-0123456789", "api_base": "https://api.deepseek.com"}}


def make_tree(name: str, *, root_cfg=None, exe_cfg=None) -> tuple[str, str]:
    """造一棵迷你树：<base>/<name>/tree（项目根） + .../tree/dist（exe 所在）。"""
    root = os.path.join(SANDBOX, name, "tree")
    exe = os.path.join(root, "dist")
    os.makedirs(exe, exist_ok=True)
    if root_cfg is not None:
        write_cfg(root, root_cfg)
    if exe_cfg is not None:
        write_cfg(exe, exe_cfg)
    return root, exe


def resolve(exe_dir_path: str, *, writable=None, env=None) -> tuple[str, str]:
    """
    在替身环境下跑一次 `paths.resolve_data_dir()`，返回 `(路径, 原因)`。

    替身只换 `exe_dir` / `_writable` / `local_appdata_dir` 与 `QQ_AGENT_HOME`，
    跑完一律还原 —— 免得后面的用例在一个「假装」的环境里跑。
    `writable=None` 时用「目录存在即视作可写」当替身（用例 [5] 靠「不存在」模拟不可写）。
    """
    saved = {n: getattr(paths, n) for n in ("exe_dir", "_writable", "local_appdata_dir")}
    old_env = os.environ.get(paths.ENV_HOME)
    old_writable = getattr(paths, "_writable", None)
    try:
        paths.exe_dir = lambda: exe_dir_path
        paths.local_appdata_dir = lambda: APP_DATA
        paths._writable = (lambda p: os.path.isdir(p)) if writable is None else writable
        if env is None:
            os.environ.pop(paths.ENV_HOME, None)
        else:
            os.environ[paths.ENV_HOME] = env
        return paths.resolve_data_dir()
    finally:
        for n, v in saved.items():
            setattr(paths, n, v)
        if old_env is None:
            os.environ.pop(paths.ENV_HOME, None)
        else:
            os.environ[paths.ENV_HOME] = old_env
        _ = old_writable


try:
    # ------------------------------------------------------------------
    print("[1] QQ_AGENT_HOME 最高优先")
    home = os.path.join(SANDBOX, "envhome")
    root, exe = make_tree("env", root_cfg=OURS, exe_cfg=OURS)
    p, why = resolve(exe, env=home)
    case("QQ_AGENT_HOME 指定的目录胜出（哪怕 exe 旁边/上一级都有配置）",
         os.path.normcase(p) == os.path.normcase(home) and "环境变量" in why,
         f"{p} / {why}")
    case("环境变量分支创建了目录（不然第一次启动就写不进去）", os.path.isdir(home), p)

    # ------------------------------------------------------------------
    print("[2] exe 旁边已有我们的 config.json → 用 exe 目录（便携部署不被上一级抢走）")
    root, exe = make_tree("both", root_cfg=OURS, exe_cfg=OURS)
    p, why = resolve(exe)
    case("两处都有配置时选 exe 目录", os.path.normcase(p) == os.path.normcase(exe),
         f"{p} / {why}")
    case("原因里点明是「已有 config.json」", "已有 config.json" in why, why)

    # ------------------------------------------------------------------
    print("[3] 新增规则：exe 目录没有、上一级有 → 用上一级（本次修的就是这条）")
    root, exe = make_tree("parent", root_cfg=OURS)
    p, why = resolve(exe)
    case("采用上一级（项目根）里的那份配置", os.path.normcase(p) == os.path.normcase(root),
         f"{p} / {why}")
    case("原因里点明「上一级」，不是笼统的「便携模式」", "上一级" in why, why)
    case("并把上一级的目录名报出来", os.path.basename(root) in why, why)

    # ------------------------------------------------------------------
    print("[4] 防误采用：不是我们的 config.json 一律不认")
    root, exe = make_tree("foreign", root_cfg={"foo": 1, "bar": [1, 2]})
    p, why = resolve(exe)
    case("上一级的 config.json 是别人的 → 不采用，回到 exe 目录",
         os.path.normcase(p) == os.path.normcase(exe), f"{p} / {why}")

    root, exe = make_tree("broken", root_cfg="{ 这不是 JSON")
    p, why = resolve(exe)
    case("上一级的 config.json 是半截/坏 JSON → 不采用",
         os.path.normcase(p) == os.path.normcase(exe), f"{p} / {why}")

    root, exe = make_tree("listjson", root_cfg="[1, 2, 3]")
    p, why = resolve(exe)
    case("上一级是 JSON 但不是对象 → 不采用",
         os.path.normcase(p) == os.path.normcase(exe), f"{p} / {why}")

    # _has_our_config 本身
    d = os.path.join(SANDBOX, "hasour")
    case("有 llm 段 → 认", (write_cfg(d, OURS) or True) and paths._has_our_config(d))
    case("只有 _xxx说明 注释键 → 也认（我们写的空配置就长这样）",
         (write_cfg(d, {"_说明": "x", "_密钥说明": "y"}) or True)
         and paths._has_our_config(d))
    case("无关 JSON 对象 → 不认", (write_cfg(d, {"foo": 1}) or True)
         and not paths._has_our_config(d))
    case("根本不是 JSON → 不认", (write_cfg(d, "not json at all") or True)
         and not paths._has_our_config(d))
    case("目录里没有 config.json → 不认",
         not paths._has_our_config(os.path.join(SANDBOX, "empty-dir")))

    # ------------------------------------------------------------------
    print("[5] 都没有 → exe 目录新建；exe 目录不可写 → LOCALAPPDATA 兜底")
    root, exe = make_tree("fresh")
    p, why = resolve(exe)
    case("全新安装：落在 exe 目录", os.path.normcase(p) == os.path.normcase(exe),
         f"{p} / {why}")
    case("原因里说明「将在此新建」", "将在此新建" in why, why)

    root, exe = make_tree("readonly")
    p, why = resolve(exe, writable=lambda _p: False)
    case("exe 目录不可写 → 回退到 LOCALAPPDATA 那一支（由 local_appdata_dir 决定）",
         os.path.normcase(p) == os.path.normcase(APP_DATA), f"{p} / {why}")
    case("回退原因点明不可写", "不可写" in why, why)
    # 真实的兜底目录必须落在 %LOCALAPPDATA%\QQAgent（上面用的替身目录会掩盖这一点）
    real_fallback = paths.local_appdata_dir()
    case("真实兜底目录 = %LOCALAPPDATA%\\QQAgent",
         os.path.normcase(os.path.dirname(real_fallback))
         == os.path.normcase(os.environ["LOCALAPPDATA"])
         and os.path.basename(real_fallback) == paths.APP_DIR_NAME, real_fallback)
    case("APP_DIR_NAME 就是 QQAgent（界面上写的那个名字）",
         paths.APP_DIR_NAME == "QQAgent", paths.APP_DIR_NAME)

    # ------------------------------------------------------------------
    print("[6] 单一事实来源：agent 与壳必须解析出同一个目录（子进程实测）")
    #
    # 造一个**真的不可写**的 exe 目录：把它的父节点做成一个**文件**，
    # 于是 os.makedirs 抛错 → _writable 返回 False。这比替换 _writable 更硬
    # —— 走的是真实判据，不是替身。
    blocker = os.path.join(SANDBOX, "sub", "blocker.txt")
    os.makedirs(os.path.dirname(blocker), exist_ok=True)
    with open(blocker, "w", encoding="utf-8") as f:
        f.write("这里是一个文件，不是目录")
    frozen_home = os.path.join(SANDBOX, "sub")
    child_sandbox = os.path.join(SANDBOX, "sub", "sandbox")
    os.makedirs(child_sandbox, exist_ok=True)
    code = (
        "import os, sys, json\n"
        f"sys.path.insert(0, {HERE!r})\n"
        f"os.environ['LOCALAPPDATA'] = {child_sandbox!r}\n"
        "os.environ.pop('QQ_AGENT_HOME', None)\n"
        "sys.frozen = True\n"
        f"sys.executable = {os.path.join(blocker, 'qq-agent.exe')!r}\n"
        "from app import paths\n"
        "import agent\n"
        "print(json.dumps({'paths': paths.DATA_DIR, 'agent': agent.HERE,\n"
        "                  'reason': paths.DATA_DIR_REASON}, ensure_ascii=False))\n"
    )
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           cwd=HERE, timeout=180)
        line = [x for x in (r.stdout or "").splitlines() if x.strip().startswith("{")]
        got = json.loads(line[-1]) if line else None
    except Exception as exc:                       # noqa: BLE001
        got = None
        r = None
        print(f"  [i] 子进程没跑起来：{type(exc).__name__}: {exc}")

    if not got:
        case("冻结态子进程能跑出解析结果", False,
             f"rc={getattr(r, 'returncode', '?')} stderr={((getattr(r, 'stderr', '') or '')[-400:])}")
    else:
        case("冻结态 exe 目录不可写时，壳真的回退到了 LOCALAPPDATA（不是 exe 目录）",
             os.path.normcase(os.path.dirname(got["paths"])) == os.path.normcase(child_sandbox)
             and os.path.normcase(got["paths"]) != os.path.normcase(blocker),
             str(got))
        case("agent._resolve_home() 与 app.paths.DATA_DIR 是同一个目录（不再各算各的）",
             os.path.normcase(got["agent"]) == os.path.normcase(got["paths"]), str(got))
        case("这个目录确实落在沙箱里（没写进真实用户目录）",
             os.path.normcase(got["paths"]).startswith(os.path.normcase(child_sandbox)),
             str(got))

    # ------------------------------------------------------------------
    print("[7] 源码运行：agent 与壳也必须同源（本进程就是源码运行的活样本）")
    case("agent.HERE == paths.DATA_DIR（源码态）",
         os.path.normcase(A.HERE) == os.path.normcase(paths.DATA_DIR),
         f"{A.HERE} vs {paths.DATA_DIR}")
    case("agent.CONFIG_PATH 就落在那个目录里",
         os.path.normcase(os.path.dirname(A.CONFIG_PATH))
         == os.path.normcase(A.HERE), A.CONFIG_PATH)
    case("模块级 DATA_DIR_REASON 非空（界面/日志要能说明白数据目录是怎么定的）",
         bool(paths.DATA_DIR_REASON), paths.DATA_DIR_REASON)

    # 源码守门：agent 必须**问** paths，而不是自己再写一遍规则。
    # （dir() 看不出「写了两份逻辑」，只能扫源码。）
    _src = open(os.path.join(HERE, "agent.py"), encoding="utf-8").read()
    case("agent._resolve_home 里确实委托给了 app.paths",
         "from app.paths import DATA_DIR" in _src, "")
    case("内联规则还留着（app 包不可用时兜底，agent.py 能被单独拷走）",
         "os.path.dirname(os.path.abspath(sys.executable))" in _src, "")

finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)
    if _OLD_LOCALAPPDATA is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = _OLD_LOCALAPPDATA

print("=" * 70)
print(f"结果：{OK} 通过 / {FAIL} 失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
