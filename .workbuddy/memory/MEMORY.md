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

## 后台化运行（R4）—— 已实测全部通过

（详见文末「后台化运行（R4）」一节，此处不重复。）

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

## 已知使用约束（部署时必看，尚未修）
1. **启动常驻前，QQ 里必须先手动打开一个会话。**
   QQ 刚启动停在起始页，而 `ml-list` / `ExEditor-qq-msg-editor` / `send-msg` /
   `chat-header__contact-name` 这些锚点**只存在于聊天页**，起始页一个都没
   → `ml_list` 与 `editor` 均为 None → `QQWindow.dom_exposed()` 为 False
   → `run_forever()` 报 `E-QQ-004` 并 return 2，常驻起不来。
   当前处理：**手动点进任意一个会话**。
   两个待修的误导点（详见 `.workbuddy/memory/2026-09-12.md` 22:14 条）：
   ① `E-QQ-004` 的措辞只指向「无障碍参数没生效」，但「未打开会话」症状完全相同
      —— 按它去重启 QQ 是白折腾；
   ② 界面「体检」的判据是 `ml_list_found or session_count > 0`，而会话列表在左栏、
      起始页也有 → **体检显示通过、常驻却报错退出**，两边判据不一致。

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

## 用户偏好（本项目内已确认）
- UI 禁 emoji，用内联 SVG 图标；支持明暗主题。
- 破坏性操作必须二次确认并说明后果（重启 QQ、真发消息、清空上下文）。
- 存疑的设计要给「为什么这样做」的解释，而不是只给结论。
- 有时会明确要求「只记录、先别改代码」——照做，把要点与将来方案写进日志即可。
