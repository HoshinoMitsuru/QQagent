# PC 端 QQ AI 代理（路线 A 最小 demo）

用「操作本机 QQ 客户端」代替协议接入，绕开 QQ 协议风控。
不碰协议、不碰硬件模拟，程序做的只是**读控件、贴剪贴板、触发发送按钮**。

## 范围

| 项目 | 状态 |
| --- | --- |
| 纯文本 | ✅ |
| 私聊 | ✅ 实测通过 |
| 群聊 | ✅ 已放开（`private_chat_only: false`）；类型也能正确识别 |
| 图片 / 语音 / 文件 | ⛔ 识别为「非文本」并跳过 |
| 富文本格式 | ⛔ 只取纯文本 |

## 文件

| 文件 | 用途 |
| --- | --- |
| `probe.py` | 第 0 步：探明 QQNT 把界面暴露成了哪些 UIA 控件 |
| `agent.py` | 主程序：读消息 → 调模型 → 发消息 |
| `config.json` | 配置（密钥、模型、提示词、调教开关） |
| `requirements.txt` | 依赖清单 |
| `下一步操作清单.md` | **分步操作手册**（含预期输出与排错对照表） |
| `probe-tree.txt` | 控件树快照（当前是**私聊**会话的快照） |

## 命令速查

> 详细分步操作见 [`下一步操作清单.md`](下一步操作清单.md)。

先在 PowerShell 里执行一次，之后本窗口内所有命令直接写 `python`、不需要引号：

```powershell
cd D:\Psyche\Sealdice-AIChat\pc-agent-demo
$env:Path = "C:\Users\Psyche\.workbuddy\binaries\python\envs\default\Scripts;$env:Path"
```

> PowerShell 里用带引号的绝对路径调用程序**必须加 `&`**，否则报 `意外的标记"-X"`。

```powershell
python -X utf8 agent.py --selftest             # 不开 QQ：配置/密钥/模型/调教解析
python -X utf8 agent.py --replay "在吗"         # 不开 QQ：跑一遍生成回复的链路
python -X utf8 agent.py --peek 12              # 开 QQ：只读解析最近 12 条，不截断
python -X utf8 agent.py --input-test           # 开 QQ：写入输入框→回读→清空，绝不发送
python -X utf8 agent.py --input-test "自定义文本"
python -X utf8 agent.py --once --dry-run       # 单轮：只把最新 1 条当新消息，不发送
python -X utf8 agent.py --send "测试"           # 真发一条
python -X utf8 agent.py --dry-run              # 常驻循环，只读不发
python -X utf8 agent.py                        # 常驻循环，正式运行（Ctrl+C 退出）
python -X utf8 probe.py --depth 30 --dump      # 重新导出控件树
```

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
| 会抢前台 | 发送时 QQ 会被提到前台，模拟按键的固有限制 |
| 剪贴板被覆盖 | 输入靠剪贴板粘贴，你复制的内容会被替换 |
| 需要前台权限 | 从终端启动才有；抢不到会明确报错并放弃，不会乱打键 |
| 视窗内约 20 条 | QQ 只渲染可视区域附近的消息，更早的要滚动才出现 |
| 运行期间别操作 QQ | 会互相抢焦点干扰 |
| 窗口不能最小化 | 最小化会让无障碍树失效 |
| QQ 升级可能变结构 | 重跑 `probe.py` 对比 `probe-tree.txt` |
| 同名联系人切换 | 靠标题变化触发重扫，标题相同可能漏判 |
