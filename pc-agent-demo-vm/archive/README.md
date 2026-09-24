# archive —— 开发期的探查脚本与原始数据

这里的东西**不要删**。它们不是代码，是**结论的证据**。

`README.md` 与 `跨终端部署与操作手册.md` 里很多「实测证明……」的说法，
背后都是这些脚本跑出来的。汇报或复查时被问到「你怎么知道 UIA 能读到消息」，
答案就在这个目录里。

> 2026-09-24 项目审查时从根目录收进来的。文档里的命令已同步改成 `archive/` 前缀。

## 探查脚本（可重跑）

| 脚本 | 当时要回答的问题 | 结论 |
|---|---|---|
| `probe.py` | QQNT 把界面暴露成了哪些 UIA 控件？ | 拿到 `ml-list` / `ExEditor-qq-msg-editor` / `send-msg` / `chat-header__contact-name` 这几个锚点 |
| `probe_minimized.py` | 窗口最小化之后 UIA 还能读到什么？ | **读取不受影响**（读的是 a11y 缓存）；**最小化**时 `Invoke` 会静默失效。⚠️ 注意区分：**「完全被别的窗口盖住」时 `Invoke` 是可用的**（实测 0.63 s 切会话成功），两者常被混为一谈 —— 见 `虚拟后台方案调研.md §6.7` |
| `probe_switch_nofg.py` | 切会话能否免前台？ | **可以** —— 会话列表项支持 `InvokePattern` |
| `probe_uid.py` | 会话列表项里能不能直接读到 QQ 号？ | **读不到**，必须走资料卡（`probe_profile.py`） |
| `probe_profile.py` | 资料卡取号这条路可行吗？ | 可行，但会**抢一次前台**，所以取一次就缓存 |
| `cdp_probe.py` | CDP 路线（`Input.insertText`）能不能走通？ | HTTP 探测 / DOM 定位 / 写入均已自测通过，但**未落地成主路径**。⚠️ **在当前 QQ 版本（9.9.35-52892）已作废** —— 实测 QQ 主动丢弃 `--remote-debugging-port`（隐藏桌面与用户桌面两组对照都不监听、`DevToolsActivePort` 不存在），见 `虚拟后台方案调研.md §6.8` |
| `bench.py` / `bench_llm.py` | 一轮读取与一次模型调用各花多久？ | 读取 ~103ms、模型 1.3~1.8s —— 这个数字决定了「模型调用不占前台」的设计 |
| `probe_hidden_desktop.py` | **能把 QQ 启动到一个用户看不见的桌面上、且照常渲染/读/写吗？** | ✅ **全部可以**。R4 的四项判据全通过（§6.4）：不节流（`raf` 持续涨、`fps=180`）、a11y 树完整（199 节点）、免前台写入成功（含中文）、**用户前台全程不变**。消融还发现**连激活调用都不需要**（`--focus-plan none` 就成功） |
| `probe_no_foreground_write.py` | **对用户桌面上处于后台的真实 QQ，能不能不抢前台就写进去？** | ✅ **能**，条件是 `AttachThreadInput` + `SetFocus(Chrome_RenderWidgetHostHWND)`。⚠️ 这**推翻了**项目里「写文本必须抢前台」的前提（§0.1-① / §6.5） |
| `r4_bed.html` | （被测目标，非探针）隐藏桌面上到底发生了什么？ | 自报状态页：`rAF 帧数 / fps / visibilityState / activeElement / input 事件数 / 两个编辑框内容` 全写进 `document.title`，于是"那张桌面上发生了什么"可以跨桌面直读 |

### 为什么要用测试页而不是直接测 QQ

`probe_hidden_desktop.py` 的被测目标是 `r4_bed.html`（一个裸 Chromium 页面），不是 QQ。原因：

- 隐藏桌面上的 QQ 只能用**独立 profile** 启动（用同一份 `%APPDATA%\QQ` 会和用户自己的 QQ 抢同一份数据）
  → 它是**未登录**实例 → 没有聊天页、没有输入框 → `WM_CHAR` 无处可落，测了也白测
- 而 `WM_CHAR → 焦点 renderer 宿主` 是**引擎级代码**，QQ（Electron）走同一段，结论可以平移
- 换来三个好处：不需要登录、不动用户 profile、**状态可以三方读回**（标题 / a11y 树 / 截图）
  —— 比直接测 QQ 更严格，因为 QQ 的 ProseMirror 只给只读 `TextPattern`

### 复现命令

```bash
PY="<带 uiautomation 的 python>"

# R4 主实验（隐藏桌面 + 写入 + 抓图）
"$PY" -X utf8 archive/probe_hidden_desktop.py \
  --exe "C:\Program Files\Google\Chrome\Application\chrome.exe" \
  --args '--user-data-dir="%TEMP%\r4bed-profile" --no-first-run --no-default-browser-check
          --app="file:///<项目根>/archive/r4_bed.html"' \
  --type-text "R4probe" --send-modes char --focus-plan none --probe-secs 2 --wait 20 \
  --shot "<项目根>/archive/r4_shot_hidden.png"

# 激活方案消融（每档必须独立进程、全新窗口 —— 焦点状态有粘性，连跑会失效）
for P in none renderer_only no_attach no_renderer_focus full; do
  "$PY" -X utf8 archive/probe_hidden_desktop.py ... --focus-plan "$P" --type-text "R4$P"
done

# 用户桌面上的后台 QQ：免前台写入（会写进 QQ 输入框并自动抹掉，不发消息）
"$PY" -X utf8 archive/probe_no_foreground_write.py
```

## 原始数据（体积大，且可重新导出）

| 文件 | 内容 |
|---|---|
| `probe-tree.txt` | 一次 `--depth 30` 的全量控件树导出（50 KB）。`agent.py` 里「`ml-item` 的 AutomationId 是 18~19 位消息 ID」这类判断就是从它里面读出来的 |
| `probe-report.json` / `probe-stdout.txt` | 同一次探查的结构化结果与原始输出 |
| `profile-probe.json` | 资料卡控件结构（取号通路） |
| `uid-probe.json` / `uid-tree.txt` | 会话列表项的控件结构（「读不到 QQ 号」这个结论的依据） |
| `card-tree.txt` / `peek-fixed.txt` | 局部结构摘录，调试时用的中间产物 |
| `host-result.json` | R4 实验里 host 进程的完整结构化结果（本桌面枚举到的窗口、a11y 统计、消融每步的 前台/活动/焦点、投递返回值、两路读回、截图统计） |
| `r4_shot_hidden.png` | **隐藏桌面上抓到的窗口截图**。肉眼可见刚用 `WM_CHAR` 写进去的「R4中文测试」—— 三方信源（标题 / a11y / 截图）同时对上 |
| `@AutomationLog.txt` | **uiautomation 库自己写的错误日志**。内容是 `尚未调用 CoInitialize` + `Can not load UIAutomationCore.dll` —— 正是「全进程只能有一条线程碰 UIA，且启动时必须 `UIAutomationInitializerInThread()`」这条硬性约定的来源 |

## 想重新导出控件树

```powershell
cd <项目根>
python -X utf8 archive/probe.py --depth 30 --dump     # 会重新生成 probe-tree.txt
```

> ⚠️ 这些都是**会操作真实 QQ 窗口**的脚本，不是纯读取的单元测试。
> 跑之前确认 QQ 已登录、且当前没有别的自动化在跑（会碰 QQ 的任务必须串行）。
