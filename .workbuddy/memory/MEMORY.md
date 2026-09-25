# 小清澈.js 项目记忆

## 项目概述
海豹骰子 JS 插件，接入 DeepSeek/OpenAI 兼容接口，支持 AI 聊天、多人格切换、连续对话、图片识别。

## 当前版本
- `小清澈.js` v1.7.0 (通用版，识图需手动开启)
- `小清澈2.0.js` v2.0.0 (QQ陪伴特化版，对话与识图默认全开)

## 关键架构
- 单文件 IIFE 架构 `(() => { ... })()`
- 使用 `seal.ext` API 注册插件
- 配置通过 `seal.ext.registerXxxConfig` 注册，通过 `cfg(ext)` 函数统一读取
- 消息缓冲防抖机制（`onNotCommandReceived` + setTimeout）
- 历史记录持久化到 ext.storage

## 图片识别功能 (v1.7.0 新增)
- 移植自 `D:/Psyche/sealdiceextension/aiplugin4.js` 第 14400 行附近
- 采用 image-to-text 模式：先识别图片为文字，再融入对话
- 核心函数：`transformTextToArray`、`imageToText`、`processMessageWithImages`
- 配置项：`imageRecognitionEnabled`（默认关闭）、`visionApiUrl/Key/Model`（留空回退到主API）
- 指令：`.ai img [提示词] + 图片`

## 文件位置
- 通用版：`小清澈.js`（v1.7.0）
- 陪伴版：`小清澈2.0.js`（v2.0.0）
- 学习源：`D:/Psyche/sealdiceextension/aiplugin4.js`
- 类型定义：`seal.d.ts`

## v2.0.0 与 v1.7.0 关键差异
- EXT_NAME: `Mitsuru-AiChat-Companion`（可独立安装）
- imageRecognitionEnabled: `false` → `true`
- continuousConversationTimeoutSeconds: `600` → `1800`

---

# pc-agent-demo（PC 端 QQ AI 代理 · 路线 A）

目标：附着到本机已登录的 QQ 客户端窗口，用 **UIAutomation** 读消息、调模型、
再用剪贴板+按键把回复打进去发出。不碰协议、不碰群控。

## 已定路线
- **部署走 Windows 虚拟机**（guest 里跑 QQ + 代理），宿主机不受打扰，且绕开锁屏限制。
- **交付形态：单个 exe + WebUI 控制台**（`qq-agent.exe`），用户只需预先打开并登录 QQ。
- 前台依赖的真实范围（实测）：**读取完全免前台**（最小化也能读），切会话走 `InvokePattern` 也免前台。
  ⚠️ 原写「只有写文本必须抢前台」—— **已证伪**，见下面「写文本」条目。

## 两棵并列目录树（V1 / V2）与 V2 现状

- **V1** `pc-agent-demo-vm/`（附着式，服务 VM），代码零改动，tag `v1.0-vm-attach`。
- **V2** `pc-agent-demo-r4/`（R4 独立桌面托管），tag `v2.0-r4-mechanism`。
  V2 是从 V1 复制出来的，**里面仍有大量 V1 的附着逻辑**（尚未替换，待拍板）。
- **V3** `pc-agent-demo-cu/`（LLM Computer Use，2026-09-25 立项）。路线 C 混合：
  语义工具集（6 个 tool calls）为主路径 + 视觉 verifier 兜底；Executor 双执行面抽象
  （attach 主号人工确认 / hosted 小号全自主）；第一版手动下发任务。
  默认模型 `deepseek-flash`（V4.1-Flash，自带图像理解+Tool Calls+1M 上下文，
  已核实官方文档，verifier 免第二路视觉 Key）。
  分期 F1(基建+基线✅)→F2(Executor双执行面+安全锁✅)→F3(工具化 dispatch✅)
  →F4(Brain tool calling 循环+实测授权：hosted 小号全自主、attach 主号仅「我，我们/苏霖韵」
  白名单+逐条确认✅)→F5(Verifier 视觉校验：截图+deepseek-flash 图像理解三态结论✅)
  →F6 WebUI→F7 exe 验收。详见 2026-09-25 日志。
- V2 已实机验证：独立 profile 启动 → 免扫码登录 → **打开一个会话** →
  常驻宿主 `app/hostd.py` 跑 `agent.run_forever()` → **真的发出回复** → 优雅停止；
  控制台「隐藏桌面」面板（`GET /api/host/status|shot.png`、`POST /api/host/start|stop|grab`）
  + 日志通道（`app/logtail.py` tail `logs/hostd.log`）。
- **V2 的三条新增硬约束**（都是踩出来的）：
  ① 宿主进程里**不要提前碰 UIA** —— 一次失败的 `Invoke` 会把本进程的 uiautomation
     弄成永久坏状态（`-2147220991 事件无法调用任何订户`），登录/探测一律派**一次性子进程**；
  ② 常驻前**必须有一个打开着的会话**（`ml-list`/`ExEditor-qq-msg-editor` 只在聊天页）
     → `E-QQ-008`；且「点第一条」不够（第一条可能是公众号 webview），要按客观判据迭代候选；
  ③ 读窗口标题必须走 `SendMessageTimeoutW` + 两阶段枚举（`EnumWindows` 回调里发消息会挂）。

**理论依据**（解释为什么"藏起来"会导致停摆）：Chromium 在 Windows 上**自算遮挡**
（z 序枚举减法），判定 occluded 后**停止渲染 + JS 节流**。会被判的情形：
最小化 / 完全遮挡 / 另一虚拟桌面 / 锁屏 / **移到屏幕外**。

**参考项目 sealdice-core 走的是协议端路线**（`lagrange/` `milky/`），它的"无窗口"
只是 `ShowWindow(GetConsoleWindow(), SW_HIDE)` 藏控制台 ——
**给不了"后台渲染 UI"的答案，只有进程形态可借鉴**。

## 目录约定（`pc-agent-demo/`）
- `agent.py` 主流程（读→防抖聚合→入队→生成→三重复核→发送）；
  `qqid.py` 取 QQ 号；`reply_queue.py` 排队与风控。
- `error_codes.py` **错误码目录**（73 条，十一个域），agent 与 app 共用同一份；
  `app/errors.py` 统一 JSON 信封；`app/diagnose.py` 一键体检 + 可复制报告。
- `app/` 控制台壳：`main`（双模式入口）`server`（REST+SSE）`supervisor`（子进程托管）
  `qqctl`（QQ 发现/体检/重启 + UiaWorker）`settings`（三文件配置）`tray` `platform_win`
  `logbus` `paths` `runtime` `errors` `diagnose` `web/index.html`。
- **`archive/` = 开发期证据（归档不删）**：11 个探针/基准脚本 + 9 个原始 dump +
  `archive/README.md`（逐个写明「当时要回答什么问题 → 结论」，含复现命令）。
  它们是 README 里「实测证明……」说法的依据，**不要删**。
  文档里的命令已同步成 `archive/` 前缀。
  新增（R4 调研）：`probe_hidden_desktop.py`、`probe_no_foreground_write.py`、
  `r4_bed.html`（被测目标页）、`r4_shot_hidden.png`、`host-result.json`。
  ⚠️ 其中 `cdp_probe.py` 的「已探通」在当前 QQ 版本**不再成立**（QQ 丢弃了调试端口）。
- 构建：`build.bat` → `qq-agent.spec` → `dist/qq-agent.exe`（onefile + windowed + uac_admin）。
  三个变体：admin / `qq-agent-noadmin.exe`（免 UAC）/ `qq-agent-console.exe`（诊断版）。
  **开关名 `nadmin`（也接受 `noadmin`）**，产物名由 build.bat 自动决定，不再互相覆盖。
- **打包的两条铁律**（都是踩出来的）：
  ① **冻结后 `sys.executable` 是 exe 自己，不接受 `-m`** —— 任何「让程序再跑一个自己」
     的地方都要走子命令（`--run-agent/--run-qqid/--run-hostagent/--run-hostd`），
     V2 里统一收在 `host.child_args()`；只被动态拉起的模块必须写进 spec 的 hiddenimports。
  ② **打包版没有 Pillow**（spec 刻意排除）→ `winmsg.grab_any` 落的是 `.bmp` 不是 `.png`。
     服务端不能假定后缀，要问抓图结果要文件名（`server._shot_path()`）。
- **验收顺序**：`test_exe.py`（真 exe，**必须用 `-noadmin` 变体**，默认版带
  requireAdministrator 清单而 CreateProcess 不弹 UAC → `WinError 740`）
  → `archive/probe_exe_hidden_desktop.py`（打包版真能进隐藏桌面，14 项）。
- 测试：`test_errors.py`(252)、`test_ui.py`(104)、`test_text_port.py`(104)、
  `test_discovery.py`(43)、`reply_queue.py --selftest`(68)、`test_exe.py`(45)；
  另有 3 套真机测试 `test_live_chain` / `test_pipeline` / `test_send_guard`。
- **`技术核实文档.md`**：汇报底稿，主线 = 技术难点与创新。
  每个难点按「问题→约束→做法→证据→代价」写，含 §9 汇报要点速查（30 秒/3 分钟/10 分钟）。

## 硬性约定
1. **路径解析只允许一个事实来源**：数据目录走 `QQ_AGENT_HOME → exe 目录 → 源码目录`
   三级解析（`app/paths.py` / `agent._resolve_home()`）；其它模块引用它，不要各自重算 `__file__`。
   相对路径一律按数据目录解析，**绝不看当前工作目录**（CWD 相关的行为会导致「换个目录结果不同」）。
2. **密钥三文件分离**：`config.json`（可公开）/ `secrets.local.json`（Key，已 gitignore）/
   `app-settings.json`（壳设置）。API Key 绝不写进 config.json，接口只回掩码。
3. **全进程只有一条线程碰 UIA**（`CUIAutomation` 是进程级单例，跨线程会静默返空）；
   该线程启动时必须 `UIAutomationInitializerInThread()`。
4. **停止常驻用哨兵文件** `state/STOP`，不用 Ctrl+C（子进程 `CREATE_NO_WINDOW` 无控制台）。
5. **会碰 QQ 的任务必须串行**，且常驻运行时一律禁用（用一个 `concurrent` 标志表达）。
6. 纯 ctypes 调用一律显式声明 `argtypes`/`restype`（64 位句柄截断是静默的）。
7. 改完必须跑：`test_errors.py` + `test_ui.py` + 重新 `build.bat` + `test_exe.py`
   （冻结问题只在真 exe 上暴露）。
8. **报错规范：一个错误码只对应一个根因**，且每条 `fixes` 要给可执行的抓手
   （界面元素名/路径/命令/明确动词），不要写成解释。
   新增报错一律走 `report()`/`E.envelope()`，不允许裸 `log("ERR", "失败了")`
   —— `test_errors.py` 里有源码扫描会拦住它。
   `agent.py` 侧：`report()` 带抑制（常驻用）、`diag()` 直接打印（命令行用）。

## 已知使用约束（部署时必看）
1. **启动常驻前，QQ 里必须先有一个打开着的会话** —— 2026-09-25 已**修好并精确报码**：
   `ml-list` / `ExEditor-qq-msg-editor` / `send-msg` / `chat-header__contact-name`
   只存在于聊天页，起始页一个都没 → `dom_exposed()` 为 False。
   - `E-QQ-004` 与新增的 `E-QQ-008` 分工：判据是「`recent-contact-list` 在不在」。
     会话列表在 = `E-QQ-008`（点开会话即可）；连会话列表都没有 = `E-QQ-004`（树是空壳）。
     两个原误导点都改了：① 常驻不再拿「参数没生效」去解释「没打开会话」；
     ② 界面「体检」的 `ok` 判据改成与 `dom_exposed()` **完全一致**（`ml_list_found or editor_found`，
     不再把 `session_count` 算作可读），并单独给出 `chat_open` 字段让界面说清那一种情况。
   - 隐藏桌面形态下由 `hostd` 自动完成（迭代候选会话条目，直到 `ml-list` 出现）。

## 已修的关键坑（防再犯）
1. **掩码判据过窄 → 真密钥被覆盖**（`settings._is_mask`）。
   判据必须是「出现 `•` 就当掩码」而不是「全是 `•`」：
   `_mask()` 会保留首尾各 4 位真字符（`sk-1••••••••ghij`），窄判据会把它当成真密钥
   写进 `secrets.local.json`，**静默覆盖真密钥**，之后报出的却是
   `UnicodeEncodeError: latin-1 position 11-18`（看起来像网络问题）。
   一般原则：**「是不是占位符」要按最宽形态认** —— 把真数据当占位符只是麻烦，
   把占位符当数据是拿它覆盖真数据。回归测试见 `test_errors.py §18`。
2. **兜底错误码不得断言具体根因**。`EC.wrap(exc, "E-LLM-002")` 这种写法会把
   任何未知异常打成「连不上接口」→ 假诊断。兜底一律用中性的 `E-LLM-012`。
   回归测试见 `test_errors.py §17`。
3. **失败之后不得再报一条互相矛盾的后续错误**（`_last_gen_fail` 机制）。
4. **「写文本必须抢前台」是错的**（原 `agent.py:3713`）。
   真正判据：**OS 焦点是否落在 `Chrome_RenderWidgetHostHWND` 上**（不是窗口是不是前台）。
   `AttachThreadInput` + `SetFocus(renderer 子窗口)` + `WM_CHAR` 即可写入后台窗口，
   **用户前台全程不动**（含中文，实测）。`SetForegroundWindow` / `BringWindowToTop` 在写文本路径上多余。
   ⚠️ 改之前必须先补测：`SetFocus` 到 QQ 后**用户的按键会不会漏进 QQ**（U3，尚未测）。
   另注：`SendMessage` 返回值非 0 **≠** 对方处理了 —— 必须读回验证。
5. **「被盖住」≠「被最小化」**。完全遮挡下 `Invoke` **可用**（实测 0.63 s 切会话成功）；
   只有最小化才静默失效。`agent.py:3713` 与 `qqid.py:698` 原口径互相矛盾，以后者为准。
6. **Chromium 系不能靠 `GetCurrentPattern` 判断 Pattern 存不存在** —— 它会**谎报"全可用"**。
   要读 `IsXxxPatternAvailable` 属性投影。实测 QQ 输入框：`IsValuePatternAvailable=False`、
   `IsTextPatternAvailable=True`（只读）→ **输入框没有任何可写 Pattern**，
   写入只能走窗口消息（或 CDP，但见下）。
7. **`GetWindowTextLengthW` / `GetWindowTextW` 会给对方线程发消息**，对方不处理就
   **无限期挂住**（无返回、无异常、无日志）。控制台的 `/api/state` 每 2s 被轮询，
   曾因此整个状态流永久冻住（界面显示"心跳停更/连不上"，而进程活得好好的）。
   正确做法：**两阶段**枚举（先收集不发消息的字段，枚举结束后再读标题）
   + `SendMessageTimeoutW(SMTO_ABORTIFHUNG)`。且在 `EnumWindows` 回调**里**发消息
   会静默拿到空标题（实测 45 个窗口全空）。
8. **`_desktop_locked()` 不能用「有没有前台窗口」当判据** —— 非活动桌面上
   `GetForegroundWindow()` 恒为 NULL，那是 R4 的正常态。判据应是
   `OpenInputDesktop` 能不能打开（锁屏的直接证据）。
9. **一个错误码只对应一个根因**这条也要用在「进程级状态」上：
   宿主进程的退出码、agent 的退出码、心跳里的阶段，三者不能混用一个字段
   （原名 `agent_rc` 会把「启动就失败」读成「正常返回 0」）。

## 后台化运行（R4）—— 已实测全部通过

- **R4 = `CreateDesktopW` + `STARTUPINFO.lpDesktop`**，把 QQ 启动到用户看不见的桌面。
- **实测六项全通过**：窗口可见 / **renderer 完全不节流**（`raf` 持续涨、`fps=180`、`vis=visible`）/
  a11y 树完整 / **免前台写入成功（含中文）** / **窗口画面可抓**（`PrintWindow`+`PW_RENDERFULLCONTENT`，
  二维码登录靠它）/ **用户前台完全不受影响**。
- **激活方案消融：最小必需集合 = 空**。那张桌面上只有它一个窗口，Chromium 上来就认为自己是 active，
  连 `SetFocus` 都不必调。⚠️ 消融每档必须**独立进程、全新窗口**（焦点状态有粘性）。
- **非活动桌面上不存在前台窗口**：`SetForegroundWindow` 恒返 0、`GetForegroundWindow()` 恒 NULL。
  → 剪贴板 + `Ctrl+V` 那条路在隐藏桌面上**直接断掉**（但也**不需要**它）。
- **已排除**：窗口移到屏幕外（官方点名会节流）；R3 虚拟显示器（用户 D3=b 已否决）；
  **不能做成 Windows 服务 / Session 0**（UIA 看不到用户会话窗口）。
- **CDP 路线在本机 QQ 版本（9.9.35-52892）已作废**：双桌面对照证明 QQ 主动丢弃
  `--remote-debugging-port`（参数在命令行里、但端口不监听、`DevToolsActivePort` 不存在）。
  → 判据是「端口是否监听」，不是「命令行里有没有参数」。
- **新增产品级前提**：隐藏桌面上的 QQ 必须用**独立 profile**（否则抢用户自己的 `%APPDATA%\QQ`）
  → 未登录 → **首次必须扫码**，而二维码画在看不见的桌面上 → 靠 `PrintWindow` 取图给用户。
- 全文与未验证清单：`虚拟后台方案调研.md`（§6.4 实测 / §6.5 免前台写入 / §6.6 诚实清单）。

## V2 的一条血泪教训：两张卡操作的是**不同的号**
- 界面上「隐藏桌面（独立 profile）」操作**那张桌面上用独立 profile 的号**；
  「常驻运行」操作**你自己桌面上登录着的那个号**（= 主号）。两者互斥。
- 2026-09-25 01:37 真实误操作：有人在 V2 的 exe 上点了「常驻运行 → 开始常驻」，
  于是程序附着**主号**并**真的往一个群里发了一条 AI 回复**。
  三层叠加：① 那不是残留进程，V1 路线本来就操作本机 QQ；
  ② **exe 的数据目录是 `dist\`**（它自己那一层），读的是首次运行新生成的默认配置
  （默认 `private_chat_only=false`＝群聊也回）；③ 按下去没有任何二次确认。
- 已修：卡片换序并改名「常驻运行（操作你桌面上的 QQ）」、加二次确认
  （显示上次读到的会话名 / 回复范围 / 数据目录）、卡片内常驻警告行、
  `build_state` 暴露 `reply_policy` 供界面念出「这次会回复谁」。

## 写入路径必须是「按桌面自动」，不能靠人配（E-FG-001 的根因）
- `uia.write_mode` 三档：**`auto`（默认）**/ `wmchar` / `clipboard`。
  `auto` = 普通桌面走剪贴板（V1 老路）、独立桌面（R4）走窗口消息。
- 为什么不能让用户手配：**独立桌面上剪贴板路径 100% 不可能成功**
  （`SetForegroundWindow` 恒返 0）。原先这个键没有默认值、代码缺省当 clipboard，
  于是隐藏桌面路线上每次发送都报 `E-FG-001`，看着像偶发。
- 判定独立桌面：`本线程桌面名 != 输入桌面名` 且 `输入桌面 != 'Winlogon'`；
  输入桌面打不开时**不能**当成独立桌面（那是锁屏/权限，仍在自己的桌面上）。
  `agent._independent_desktop()`。
- 显式配 `clipboard` 但处于独立桌面 → 仍改用 wmchar + 一条 WARN（不静默改人配置）。
- 加 agent 配置键时**两处都要加**：`agent.DEFAULTS` 与 `app/settings.py` 里那份副本
  （`test_ui.py` 有逐键比对闸门）。

## 用户偏好（本项目内已确认）
- UI 禁 emoji，用内联 SVG 图标；支持明暗主题。
- 破坏性操作必须二次确认并说明后果（重启 QQ、真发消息、清空上下文）。
- 存疑的设计要给「为什么这样做」的解释，而不是只给结论。
- 有时会明确要求「只记录、先别改代码」——照做，把要点与将来方案写进日志即可。
