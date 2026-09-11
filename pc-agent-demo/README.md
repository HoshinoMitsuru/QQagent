# PC 端 QQ AI 代理（路线 A 最小 demo）

用「操作本机 QQ 客户端」代替协议接入，绕开 QQ 协议风控。
不碰协议、不碰硬件模拟，程序做的只是**读控件、贴剪贴板、触发发送按钮**。

## 范围

| 项目 | 状态 |
| --- | --- |
| 纯文本 | ✅ |
| 私聊 | ✅ 实测通过 |
| 群聊 | ✅ 已放开（`private_chat_only: false`）；类型也能正确识别 |
| 防抖聚合（连发多条合并成一次调用） | ✅ 已移植（`aggregate`） |
| 连续对话模式（活跃期内免触发词 + 超时退出） | ✅ 已移植（`continuous`） |
| 上下文持久化（按会话隔离、重启不丢） | ✅ 已移植（`persist`） |
| 图片 / 语音 / 文件 | ⛔ 识别为「非文本」并跳过 |
| 图片识别（视觉模型） / URL 网页读取 | ⏸ **继续悬置**，本轮不移植 |
| 富文本格式 | ⛔ 只取纯文本 |

## 文件

| 文件 | 用途 |
| --- | --- |
| `probe.py` | 第 0 步：探明 QQNT 把界面暴露成了哪些 UIA 控件 |
| `agent.py` | 主程序：读消息 → **防抖聚合** → 调模型 → 发消息 |
| `config.json` | 配置（模型、提示词、聚合/连续对话/持久化开关、调教） |
| `secrets.local.json` | **本地敏感信息文件**（密钥），已被 `.gitignore` 排除，不进仓库 |
| `secrets.example.json` | 上面那个文件的**模板**（不含真密钥），可以入库 |
| `state/conversations.json` | 运行期自动生成：按会话隔离的上下文（同样被 ignore） |
| `test_text_port.py` | 离线冒烟测试：不碰 QQ、不联网，验证移植的三项能力 |
| `requirements.txt` | 依赖清单 |
| `下一步操作清单.md` | **分步操作手册**（含预期输出与排错对照表） |
| `probe-tree.txt` | 控件树快照（当前是**私聊**会话的快照） |

### 密钥怎么放（重要）

`config.json` 里**不要**填 `llm.api_key` —— 本仓库会推到公开 Gitee。
密钥单独放在 `pc-agent-demo/secrets.local.json`：

```json
{ "llm": { "api_key": "sk-xxxxxxxx" } }
```

`config.json` 只用 `llm.api_key_file: "secrets.local.json"` 指向它（相对路径按脚本目录解析）。
`.gitignore` 已排除该文件与 `state/` 目录，`git status` 里不会出现它们。

这个文件定位是**所有敏感信息的统一存放处**：以后要接视觉模型（图片识别 / URL 读取），
对应的 key 也写进同一个文件（例如 `vision.api_key`），不要散落到别处。
结构照抄可入库的 `secrets.example.json`。

## 命令速查

> 详细分步操作见 [`下一步操作清单.md`](下一步操作清单.md)。

先在 PowerShell 里执行一次，之后本窗口内所有命令直接写 `python`、不需要引号：

```powershell
cd D:\Psyche\Sealdice-AIChat\pc-agent-demo
$env:Path = "C:\Users\Psyche\.workbuddy\binaries\python\envs\default\Scripts;$env:Path"
```

> PowerShell 里用带引号的绝对路径调用程序**必须加 `&`**，否则报 `意外的标记"-X"`。

```powershell
python -X utf8 agent.py --selftest             # 不开 QQ：配置/密钥/模型/调教解析/新能力开关
python -X utf8 agent.py --replay "在吗"         # 不开 QQ：跑一遍生成回复的链路
python -X utf8 agent.py --state                # 不开 QQ：看持久化下来的各会话上下文
python -X utf8 agent.py --forget "小明"         # 不开 QQ：清空某个会话的上下文（可模糊匹配）
python -X utf8 test_text_port.py               # 不开 QQ：离线冒烟测试（聚合/回插/持久化/连续对话）
python -X utf8 agent.py --peek 12              # 开 QQ：只读解析最近 12 条，不截断
python -X utf8 agent.py --input-test           # 开 QQ：写入输入框→回读→清空，绝不发送
python -X utf8 agent.py --input-test "自定义文本"
python -X utf8 agent.py --once --dry-run       # 单轮：只把最新 1 条当新消息，跳过防抖，不发送
python -X utf8 agent.py --send "测试"           # 真发一条
python -X utf8 agent.py --dry-run              # 常驻循环，只读不发
python -X utf8 agent.py                        # 常驻循环，正式运行（Ctrl+C 退出）
python -X utf8 probe.py --depth 30 --dump      # 重新导出控件树
```

> `--once` 会**跳过防抖直接结算**，方便调试；常驻模式才走完整等待窗口。

## QQ 的启动方式（关键）

QQ 主窗口必须**可见**（不能缩托盘、不能最小化），并带无障碍开关启动：

```
"D:\QQ.exe" --force-renderer-accessibility
```

右键 QQ 快捷方式 → 属性 →「目标」末尾加一个空格 + `--force-renderer-accessibility`。

## 实测的 QQNT 控件结构

私聊与群聊**共用同一套结构**，只有少数地方不同（下表标出）。

```
WindowControl 'QQ' class='Chrome_WidgetWin_1'
  ... DocumentControl '' id='RootWebArea'
    GroupControl class='chat-header panel-header ...'
      ButtonControl '光みつる' class='chat-header__contact-name'        ← 会话标题
        └ 群聊时：同级还有一个 TextControl '(9)'    ← 人数，【群聊判据】
        └ 私聊时：同级是 ButtonControl '在线状态 在线'
    GroupControl class='group-chat'              ← ⚠️ 私聊也用这个名字，不能当群聊标志
      GroupControl class='chat-msg-area'
        PaneControl '消息列表' class='ml-area ...'
          GroupControl class='... ml-root ...' id='ml-root'      ← 消息区根
            GroupControl class='ml-list list'
              GroupControl class='ml-item' id='<消息ID>'          ← 消息条目
                GroupControl class='message__timestamp no-copy' → TextControl 时间
                GroupControl class='message-container message-container--self message-container--align-right ...'
                                                                      ← 我方：带 --self / --align-right
                GroupControl class='message-container message-container--mix-or-markdown'
                                                                      ← 对方：无 --self
                  GroupControl '昵称' class='avatar-span'              ← 【昵称在这里】
                  GroupControl class='user-name ...' → TextControl 昵称 ← 群聊有，私聊没有
                  GroupControl class='message-content__wrapper'
                    GroupControl class='msg-content-container container--self|--others ...'
                      GroupControl class='message-content mix-message__inner' → 正文
    GroupControl class='qq-msg-editor__root'
      GroupControl class='ProseMirror ExEditor-qq-msg-editor is-empty'  ← 输入框
    GroupControl class='send send--disabled'
      ButtonControl '发送' class='send-msg'                        ← 发送按钮
```

### 五个反直觉的坑（全部实测踩过）

| # | 直觉 | 实际 |
| --- | --- | --- |
| 1 | 消息列表是 `ListControl`/`ListItemControl` | **没有**。条目是 `GroupControl`，靠 `class='ml-item'` + `AutomationId` 识别 |
| 2 | 输入框是 `EditControl`/`DocumentControl` | **不是**。是 `GroupControl`（ProseMirror 富文本） |
| 3 | 方向靠坐标猜 | 不用。class 里直接带：`container--self/--others`、`message-container--self/--align-right` |
| 4 | 容器 class 叫 `group-chat` 就是群聊 | **错！私聊容器也叫 `group-chat`**。群聊只能靠「标题旁的 `(人数)`」或「可见的群资料面板」判定 |
| 5 | 昵称在 `user-name` 元素里 | 私聊**根本没有** `user-name`，昵称挂在 `avatar-span` 的 `Name` 上 |

### 意外收获

- **`AutomationId` 就是消息 ID**（如 `7684140137907339375`）。天然唯一、跨轮询稳定 → 去重问题彻底解决
- **`send--disabled` class 是免费的「粘贴成功」检测器**：输入框空时 `True`，粘贴后翻 `False`
- **顶栏 `user-profile-card__nickname` 就是登录账号昵称** → 自动识别我方，不用手填配置
- **发送按钮支持 `InvokePattern`** —— 走 UIA 调用，**不需要前台窗口、不产生按键**（实测：QQ 不在前台时用 Invoke 点「表情」按钮，面板照样弹出来了）
- 输入框支持 `LegacyIAccessiblePattern`，但 `SetValue` 被 Chromium 拒绝（`UIA_E_ELEMENTNOTENABLED`），所以输入只能走剪贴板

## ⚠️ 前台窗口：一个必须知道的安全约束

Windows **默认禁止后台进程抢前台窗口**。单纯调 `SetForegroundWindow` 会**静默失败**
（实测 `last error 5 = ACCESS_DENIED`）。

失败之后危险的地方在于：`SendKeys` 的按键**不会消失，而是打进当时真正的那个前台窗口**。
实测后果是 `{Enter}` 打进了终端，把半截命令行当命令提交了 —— 表现为「程序莫名卡死」。

因此代码里有两条硬规矩：

1. **发按键之前必须验证前台**（`force_foreground()` 以 `GetForegroundWindow()` 的结果为准，
   带 5 次重试 + `AttachThreadInput` 打通输入队列）。抢不到就**一个键都不发**，明确报错退出。
2. **发送优先用 `InvokePattern`**（不需要前台、无按键），只有它失败才回退到回车键，
   且回退前必须再次确认前台。

> 实操影响：**从你自己的终端启动**（子进程才被允许抢前台），别用后台/服务方式启动。

### 打扰控制（方案 C）：用完即还焦点

「QQ 被顶到最上层」是模拟按键的固有限制，但**可以把它压到最短**：

| 手段 | 说明 |
| --- | --- |
| 前台存档 / 还原 | `type_text()` 动手前记下原前台窗口，写完在 `finally` 里调 `restore_foreground()` 还回去 |
| 去掉 `editor.Click()` | 真实鼠标点击会**二次提升**窗口、还会挪走你的光标位置；改用纯 UIA `SetFocus()` |
| `clear_editor()` 自检前台 | 「用完即还焦点」之后，任何后续按键都可能漏进你的窗口，所以清空动作在**入口重验前台** |
| 开关 | `config.json` → `uia.restore_foreground`（默认 `true`） |

> ⚠️ **改代码时注意**：`type_text()` 返回时焦点**已经还给用户了**。
> 任何「在它之后再发按键」的代码都必须**重新抢一次前台** —— `clear_editor()` 的前台自检就是为此加的护栏，别删。

### 彻底解法：方案 A（CDP）

上面只是缓解。**根治**是让 QQ 以 `--remote-debugging-port=9222` 启动，
改用 DevTools 协议直接读写渲染层 —— 不碰窗口、不碰焦点、不产生按键，窗口最小化也能干活。

```powershell
"D:\QQ.exe" --remote-debugging-port=9222 --remote-allow-origins=* --force-renderer-accessibility
python -X utf8 cdp_probe.py                # 1) 验证端口与可注入目标
python -X utf8 cdp_probe.py --dom          # 2) 定位输入框 / 消息列表
python -X utf8 cdp_probe.py --type "测试"   # 3) 实测写入（绝不发送）
```

> `cdp_probe.py` 的 HTTP 探测、错误分支、WebSocket 会话层（握手 / 事件交错跳过 / 错误透传）**均已自测通过**；
> `Input.insertText` 能否被 QQ 的 ProseMirror 输入框接受，是**留给你的最后一步实测**。

## 群聊 / 私聊判定

只认两个**正向且可见**的信号：

1. 标题旁有形如 `(9)` 的人数标记
2. 界面上真实存在群资料面板（`群公告` / `群聊成员 N`，要求 `rect` 面积 > 0）

> Chromium 会把换出去的界面留在 DOM 里（`rect` 全 0），所以判定特征必须过滤可见性，
> 否则会命中残留节点。

## 方向判定（三级降级）

1. **class**：`container--self/--others`、`message-container--self/--align-right`
2. **昵称**：发送者昵称 == 自己的昵称（配置优先，为空则自动识别）
3. **头像水平位置**：对方头像在左，自己头像在右，用消息区中线切分

昵称来源优先级：`user-name`（群聊）→ `avatar-span` 的 Name（私聊）→ **私聊里对端昵称缺失时用会话标题兜底**。

## 性能

实测：完整遍历 UIA 树 **0.40s**（586 节点），按锚点做有界 BFS 只要 **0.04s**。
所以锚点一次扫描后缓存，每 5 秒或失效时才重扫，每轮轮询只读缓存的 `ml-list` 子节点。

## 从 小清澈3.0.js 移植的文本能力

主流程因此被拆成两段：**收消息（入队）** 和 **调模型（结算）**。

```
读新消息 → 过准入判定（调教 / 触发词 / 冷却 / 非文本）
                              ↓ 通过
                    进防抖缓冲（窗口内继续收）
                              ↓ 到点
              合并成一条 user 消息 → 调模型 → 发出去
```

### 1. 防抖聚合 `aggregate`

对方连发「在吗」「你在干嘛」「算了」→ AI 只看到合并后的一条、只回一次。
等待时长在插件 `setupDebounceTimer` 的自适应算法上做了**两档改造**：

```
等待 = 基础等待 + 该用户的「习惯性额外停顿」EMA(0.7/0.3) + 冷启动惩罚
冷启动惩罚：距上次发言 > 60s 时，按「已连续轮数」递减，最多 4 轮衰减到 0

基础等待（两档自适应，按会话独立计数）：
    常规档 5s  ←→  快档 2s
    连续 3 轮都只有「单条消息」提交给 AI  → 切快档 2s
    之后某轮出现 ≥2 条（对方又在连发）    → 立刻回到 5s 并重新计数
```

判定用的「单条轮次」计数只存内存：重启后回到 5s，最多再观察 3 轮就重新判定。

两点容易看错的行为：

- **每轮的第一条消息是 9.5s 而不是 5s** —— 插件把习惯初值设成 `now-120s`，一轮里的
  首条必然被判为冷启动，惩罚 `6000×(1−1/4)=4500ms`；紧接着的第二条就恢复成 5s。
  这是插件的既有行为，不是 bug。想连首条也 5s，把 `cold_start_penalty_ms` 设 0。
- **等待是从「最后一条消息」重新起算的**，不是每条叠加。所以连发 3 条 ≈ 距最后一条 5s。

> 想调快/调慢：`aggregate.base_wait_ms`（常规档）、`fast_wait_ms`（快档）、
> `fast_after_single_rounds`（几轮单条后降档）；想回到逐条即回就设 `"enabled": false`。

### 2. 连续对话模式 `continuous`

对齐插件的 `activateContinuous / isContinuousActive / updateContinuousCooldown`：

- AI 在某个会话开口 → 该会话进入「连续对话激活」
- 活跃期内**免触发词**（群聊场景有用；配 `group_requires_trigger: true` 时才体现）
- 每次持续互动会**续期**（刷新 `lastAt`），不会聊到一半被固定窗口强制断开
- 超过 `timeout_seconds`（默认 1800s）没动静 → **自动退出**，回到需要触发词的状态

### 3. 上下文持久化 `persist`

按会话隔离落盘，重启不丢。scope 命名与插件一致：

| 场景 | scope |
| --- | --- |
| 私聊 | `private:<对方昵称>` |
| 群聊 | `group:<群名>` |

- 写入做**节流**（`save_interval_seconds`，默认 3s 最多一次）+ **原子替换**（先写 `.tmp` 再 rename）
- 文件损坏 → 自动改名成 `*.json.bad` 备份，然后从空开始，**不会把程序带崩**
- 会话数超 `max_scopes` → 淘汰最久未使用的
- 命令行可查可清：`--state` / `--forget "对方昵称"`

> ⚠️ PC 端拿不到 QQ 号，**只能拿窗口标题当身份**：对方改昵称 / 群改名，等于换了一个 scope，
> 之前的上下文就读不到了（旧 scope 仍在文件里，不会丢）。

### 调教条目的因果回插

插件的 `pushHistoryEntry` 有个细节这里也照搬了：调教是**立即写入**的，而防抖缓冲里可能还压着
更早的用户消息。若不处理，就会出现「AI 先说了这句话、用户才来提问」的因果颠倒。
所以结算时会用 `push_before_teach` 把那批用户消息**回插到调教条目之前**（每条记录带单调递增的
`seq`，同一毫秒内也不会有平局）。

## 调教机制（从 小清澈3.0.js 移植）

对方发以 `小清澈：` 开头的消息 → **不触发模型调用**，直接以 `assistant` 角色写入上下文
（等于替 AI 说出它该说的话）。兼容 `小清澈：` / `小清澈:` / `Claritas-小清澈：`，全角半角冒号都认。

## 实测记录（2026-09-11）

| 项目 | 结果 |
| --- | --- |
| 依赖 / 语法 | ✅ `uiautomation` `comtypes` `pyperclip` `requests`；两脚本 `py_compile` 通过 |
| 密钥读取 | ✅ 自动取到 `secrets.json` 的嵌套字段 `llm.api_key` |
| 模型连通 | ✅ deepseek-chat，0.6~0.9s |
| 调教解析 | ✅ 6/6（全角/半角冒号、缺冒号不误判） |
| 窗口附着 | ✅ 自动挑最大的可见 QQ 窗口 |
| 控件树 | ✅ 586 节点完整读出 |
| **私聊识别** | ✅ `群聊=False  群人数=0`（修正前误判为 True） |
| **群聊识别** | ✅ `群聊=True  群人数=9` |
| 自己昵称 | ✅ 自动识别 `Claritas-小清澈` |
| 消息解析 | ✅ 9 条全对：昵称、正文、时间、`[图片]` 标记 |
| **正文完整性** | ✅ 实测 131 / 138 字完整读出，**无省略**（修正前的 `…` 是打印截断） |
| **方向判定** | ✅ 私聊双向都正确：`【我方】Claritas-小清澈` / `【对方】光みつる` |
| 剪贴板 | ✅ ctypes 写入与回读一致（不依赖第三方库） |
| **输入链路** | ✅ **可复现 3/3**：写入 → 回读完全一致 → `send--disabled` 翻转 → 清空 |
| **InvokePattern** | ✅ 代理测试（QQ 不在前台时用 Invoke 点「表情」，面板正常弹出并 Esc 关闭） |
| 未发送保证 | ✅ 以上验证全程未向任何人发出消息 |
| **文本能力移植**（2026-09-11 第二轮） | ✅ 防抖聚合 + 连续对话 + 上下文持久化，全部离线冒烟通过 |
| **基础等待两档自适应** | ✅ 连续 3 轮单条 → 5s 降 2s；出现连发 → 回 5s 并重新计数（端到端已验） |
| **离线测试** | ✅ `test_text_port.py` **57/57 通过**（不碰 QQ、不联网、不发送） |
| **敏感信息落位** | ✅ `secrets.local.json` 取到插件同款 key（35 位）；`.gitignore` 已排除，`git status` 不出现 |

## 唯一还没跑过的一步

**真的点「发送」把消息发出去** —— 因为不该替你的账号做这件事。

```powershell
python -X utf8 agent.py --send "这是一条自动发送测试，忽略即可"
```

输入侧、发送按钮侧、前台校验侧都已分别验证通过，这一步只是把三者串起来。

## 三个探索点的结论

| # | 原问题 | 结论 |
| --- | --- | --- |
| 1 | 读不到结构化文本怎么办 | UIA 已读出全部正文，**OCR 不需要**（代码保留兜底，需配 `direction_mode="last_only"`，默认不启用） |
| 2 | 分不出我方/对方气泡 | **已解决**，class 直接带方向 |
| 3 | 输入框要用固定坐标 | **不需要**，可结构化定位 |

## 已知限制

| 限制 | 说明 |
| --- | --- |
| **回复有延迟** | 防抖聚合导致「距对方最后一条消息 5s（连续单条聊过 3 轮后降为 2s）」才回；每轮首条因冷启动惩罚为 9.5s。可用 `aggregate.*` 调整 |
| **会话身份靠窗口标题** | PC 端拿不到 QQ 号，改昵称/改群名 = 换 scope（旧上下文不丢，但读不到） |
| 会抢前台 | 发送时 QQ 会被提到前台，模拟按键的固有限制 |
| 剪贴板被覆盖 | 输入靠剪贴板粘贴，你复制的内容会被替换 |
| 需要前台权限 | 从终端启动才有；抢不到会明确报错并放弃，不会乱打键 |
| 视窗内约 20 条 | QQ 只渲染可视区域附近的消息，更早的要滚动才出现 |
| 运行期间别操作 QQ | 会互相抢焦点干扰 |
| 窗口不能最小化 | 最小化会让无障碍树失效 |
| QQ 升级可能变结构 | 重跑 `probe.py` 对比 `probe-tree.txt` |
| 同名联系人切换 | 靠标题变化触发重扫，标题相同可能漏判 |
