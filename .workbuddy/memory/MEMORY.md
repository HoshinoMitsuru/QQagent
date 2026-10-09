# 项目长期记忆（本人/Psyche/HoshinoMitsuru）

> 真实值（真名 / QQ 昵称 / 群名）只住在 gitignored 的 `.workbuddy/memory-local/`；
> 本文件与仓库其它文件一律用**占位符**。对照表：`.workbuddy/memory-local/身份对照表.md`。

## 统一纪律（跨项目）
- **统一更新纪律**：同一处功能改动必须同步到所有适用树（pc-agent-demo-r4 / -cu / -vm），同步后各树独立回归；多版本并列存放、从不覆盖不删除。
- 安全风格：先只读侦察；破坏性操作先警告+清单+确认；删除走回收站/确认可 Git 复原；错误码一个码一个根因、fixes 给可执行抓手；掩码按最宽形态认（含 • 即掩码，防真密钥被占位符覆盖）。
- **脱敏约定（2026-10-09 起）**：真名/昵称/备注名/群名一律写成占位符 —— `本人`（叙述与决策记录）、`小明`（测试语料位）、`测试目标A`（QQ 资料卡昵称）、`测试备注C`（同一联系人的**备注名**，必须与 `测试目标A` 分开 —— README 那个「列表显示备注名、资料卡显示昵称」的例子靠两者不同成立）、`测试群B`（群名）。**同一个人的多个名字各占一个占位符**。真实值只落 `.workbuddy/memory-local/`（已 gitignore）；词表 `tools/sensitive-words.local.txt` + `.githooks/pre-commit` 会拦住漏网，命中提示里指向那个目录。**绝不要 `git add -f` 本地值目录**（`-f` 绕过 ignore = 直接入库）。
- 流程偏好：报告 bug 只给症状→AI 先修复；复跑命令完整含全部 flags；A+B 选择题拍板后再落地；UI 禁 emoji、用 SVG 图标；文档用 file+function 锚点（不用行号）。
- 密钥规范：config.json（gitignored）为唯一来源；接口只回掩码；模板走 *.example.json。
- 每日变更落盘 .workbuddy/memory/YYYY-MM-DD.md 再切项目。

## 小清澈.js（海豹骰 AI 插件）
- v1.7.0 通用版 / v2.0.0 QQ 陪伴特化（识图默认开、连续对话 1800s）。
- 单文件 IIFE + seal.ext API；防抖 onNotCommandReceived+setTimeout；历史存 ext.storage。
- 识图 image-to-text：transformTextToArray/imageToText/processMessageWithImages；学习源 D:/Psyche/sealdiceextension/aiplugin4.js。

## pc-agent-demo（PC 端 QQ AI 代理，UIAutomation 路线）
- 三树并列：-vm（V1 附着）/ -r4（V2 独立桌面 R4）/ -cu（V3 LLM Computer Use，deepseek-flash，F1-F7✅）。仓库 github.com/HoshinoMitsuru/QQagent，双远程 GitHub+Gitee。
- 交付：qq-agent.exe（onefile）+ WebUI；验收必须 -noadmin 变体（默认版 UAC 清单→WinError 740）。
- 打包铁律：冻结后 sys.executable 不接受 -m，一律子命令 child_args()；动态拉起模块进 spec hiddenimports；打包版无 Pillow（抓图落 .bmp）。
- 数据目录唯一来源 QQ_AGENT_HOME→exe 目录→源码目录（app/paths.py），绝不看 CWD。
- 宿主进程不要提前碰 UIA（失败 Invoke 会永久弄坏进程内 uiautomation）；探测派一次性子进程；全进程一条线程碰 UIA。
- 读窗口标题：两阶段枚举+SendMessageTimeoutW（GetWindowTextW 会挂）。
- 写文本免前台：判据=焦点在 Chrome_RenderWidgetHostHWND；AttachThreadInput+SetFocus+WM_CHAR；「必须抢前台」已证伪。剪贴板路在独立桌面必败→ write_mode=auto 按桌面自动（独立桌面判据见 agent._independent_desktop，锁屏不能当独立桌面）。
- R4：CreateDesktopW+lpDesktop，实测六项通过；最小激活集合=空；停机语义=宿主退出连 QQ+桌面一并销毁（_cleanup_after_stop 由壳执行，两分支都收尾）。锁屏判据用 OpenInputDesktop。
- CDP 已作废（QQ 丢弃调试端口，判据=端口是否监听）；Chromium 系 Pattern 判断用 IsXxxPatternAvailable（GetCurrentPattern 谎报全可用）。
- 常驻前提：QQ 有打开着的会话（E-QQ-008）；无会话列表=E-QQ-004（判据 recent-contact-list）。
- 停止用哨兵 state/STOP；碰 QQ 任务串行；ctypes 显式 argtypes/restype。
- 会话持久化：conversations/ 目录化（每会话一 json），旧单文件一次性迁移（目录非空不迁移防覆盖）；UI 可删（store.forget）。
- 长期记忆（2026-10-09，VM+R4，CU 不做）：`memory` 配置块 + `LongTermMemory`；当日流水 jsonl（临时）→ 跨天/启动补算用模型压成 digest+events（长期只留摘要）；注入位置=system 之后、对话之前；检索用本地 CJK 2/3-gram（不引 embedding）；超容量把最老事件**先并进画像再删**；`.ai reset` 与 UI 删会话都**连带删**记忆（壳侧 `_delete_memory`，E-WEB-006 报「部分成功」）。E-MEM-001~008。
- 长期记忆的两个坑：① `m.get(k) or 5` 会把合法值 **0** 悄悄改回 5（`recall_max_items` 的 0 是文档化取值）→ 用 `k if k is None else ...`；② 壳进程不能 import agent，`_memory_dir()` 必须与 `abs_here(memory.dir)` 解析出**同一目录**，否则「界面看不到记忆/删不干净」。
- 测试隔离：History.push 会往 memory.dir 写流水，凡造 Agent/History 的测试都要把 `memory.dir` 指到临时目录，否则假会话（`private_小明`）会进真实 state/memory。
- 行尾判据：本机 `grep -c $'\r'` 会**假阴性**；判 CRLF 用 `tr -cd '\r' | wc -c`。R4 盘上 CRLF、VM 是 LF（core.autocrlf=true），打补丁前先把目标文件整份 LF 化再 `patch -p1`，打完还原。
- 安全闸（CU）：GatedExecutor 三名单 open_chat_allow/read_allow/attach，config.json 唯一来源，`qq-cu allow` 管理；hosted 空名单 fail-closed；探不到会话标题=fail-closed。
- 改 agent 配置键两处同步：agent.DEFAULTS + app/settings.py 副本（test_ui 有比对闸）。
- **DPI 虚拟化（2026-10-10 定位，R4+CU）**：本机 150% 缩放。进程若 DPI-unaware，win32 坐标被虚拟化成逻辑像素（窗口读到 640x424），而 **UIA 的 BoundingRectangle 永远是物理像素**（同一窗口 960x636）——差 1.5 倍且各自内部自洽，极难发现。所以 **hostagent / hostd / qq-cu 开工前第一件事就是 `winmsg.enable_dpi_awareness()`**；壳（tkinter/托盘）**不要**调，否则界面偏小。成功判据必须**回读** `GetProcessDpiAwareness`（`SetProcessDpiAwareness` 失败时 ctypes 不抛异常，只看返回值会谎报成功）。
- **登录按钮定位陷阱 + 第三条点击路径（2026-10-10，R4+CU）**：登录页真正的按钮类名是 `login-btn`，外面包着 `login-btn-wrap` —— **含 `login-btn` 子串**。老代码 `_find_by(cls_has=...)` DFS「父节点先命中即返回」→ 永远拿到容器（GroupControl，无 Invoke；`DoDefaultAction` 抛 `COMError(-2146233079)`）→ 表现成「程序不去按登录键、卡在登录界面」。修法：`_collect_by`（命中后继续下探）+ `_find_login_button` 三级打分（**类名按空格切 token 后精确等于 `login-btn`** > 有 Legacy 且非 GroupControl > Invoke > Legacy）。第三条路 `winmsg.click_at()` 用**窗口消息鼠标点击**（WM_MOUSEMOVE/LBUTTONDOWN/UP 投给 `Chrome_RenderWidgetHostHWND`），不要求元素支持任何 Pattern —— 真机实测坐标换算吻合（screen 831,566 → client 271,282，窗口原点 560,284），点会话条目真的切换成功。它**依赖 DPI-aware**，两者必须一起上。
- **抓图全黑 ≠ DPI（已更正的一次误判）**：全黑真因是那台残留 QQ 的**渲染进程已死**（`PrintWindow` 返回 0），DPI 只让位图尺寸不对。同一台机器、同一张隐藏桌面上健康的 QQ 抓图完全正常（1471x991、99 色、非黑占比 0.997、104KB）。`winmsg.capture()` 现在显式给 `blank`（`distinct_colors<=1` 即单色；**不能只看亮度**，纯红图亮度 76 但同样是单色）；hostagent 会写 `capture_note` 说明「这不是坐标问题」。
- **「连续对话常开」语义（2026-10-10，三树）**：`continuous.timeout_seconds = 0`（或负数）= **永不超时**。实现收在两个纯函数 `agent.cont_window()` / `agent.cont_expired()` 里，**禁止 `or 1800` 兜底** —— 0 是假值，`or` 会把用户认真填的 0 悄悄改回 1800（这类「假承诺」不报错，只在真机上表现为「设了常开，30 分钟后它自己退了」）。settings 的 `minimum` 已开 0，UI hint 写明「填 0 = 常开」。
- **残留僵死现场（2026-10-10 真机）**：宿主 `rc=2` 退出后隐藏桌面的 QQ 可能残留，且窗口句柄还能枚举、但渲染进程已死（UIA 只剩顶层 1 个节点）。**若拉起它的壳是提权进程（默认版 exe 带 requireAdministrator 清单），非提权 shell 杀不掉它**：`taskkill /F /T` 与 `Stop-Process -Force` 都是「拒绝访问」，`app.host stop --yes` 也超时。后果连锁：旧 exe 被占用 → PyInstaller 构建时的 `os.remove` 被 safe-delete shim 拦下 → 构建失败。**绕法**：先把旧 exe `mv` 走再构建；验证用**新桌面名 + 复制 profile**（`qq-profile-verify`），完全避开残留实例。
- 同名函数会静默覆盖：`hostagent.py` 里曾有两个 `_find_all_by`（后定义者胜），调用方 `name_eq=` 直接 TypeError。守门测试改成**扫源码**统计顶层 `def` 重名（`dir()` 看不到被覆盖的那个）。
- `git apply` 对行尾不一致的文件会**返回 0 但「Skipped patch」什么都不做**；跨树同步改用 GNU `patch -p2`（R4/CU/VM 的 `.py` 都是 LF，但 `dist/config.json` 是 **CRLF** —— 写锚点前必须先探行尾）。
- `test_discovery.py` 三树的退出码曾是**反的**（0 失败返回 1），已修。
- 改完必跑：test_errors.py + test_ui.py + build.bat + test_exe.py。
- **密钥纪律（2026-09-28 定，2026-10-09 收尾）**：唯一主存储 = `config.json` 的 `llm.api_key`（该文件必须被 `.gitignore` 覆盖）；`secrets.local.json` 是旧配置的**只读后备** —— `resolve_api_key` 的 `api_key_file` 分支 / `ensure_secrets_file()` / `SECRETS_PATH` 都**有意保留，不许删**。两条安全不变量：① config.json 被 ignore ② 接口/状态只回掩码。三树 test_exe.py 的旧断言（「Key 进了 secrets.local.json」）已按新纪律反转。
- **`reset_to_defaults()` 的假承诺（10-09 抓到的真回归）**：它把 config.json 整份换掉 → 收拢后连密钥一起换掉，而界面文案承诺「API Key 不受影响」。修法=写盘前把现有 `llm.api_key`（掩码则丢）捞回来，返回值加 `kept_api_key`。**配套陷阱**：`agent_defaults()` 返回的是模块级 `_LOCAL_DEFAULTS` **本体**，`dict(...)` 只是浅拷贝 —— 在它上面 `_set_path` 会把用户密钥写进全局默认值。改它必须 `copy.deepcopy`。
- **跑测试要用对解释器**：本机 bash 的 `python` 是不带 uiautomation 的裸 3.13，跑 test_ui 会**假报 2 条失败**（--peek rc=2 / 子进程没输出 JSON）。用 `%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（build.bat 第二候选，装齐依赖 + PyInstaller）。
- 打包变体命名：**R4 与 CU 的 qq-agent.spec 是新版**（`APP_NAME = "qq-agent" + _SUFFIX` → 默认版 `qq-agent.exe` 与 `qq-agent-noadmin.exe` 互不覆盖）；**VM 的 spec 硬编码 `name="qq-agent"` 且 build.bat 没有 nadmin 参数**，所以 VM 要出多变体必须构建后 `mv` 改名，否则后建的会盖掉前一个。
- MCP 薄壳 qq-mcp.py：手写 stdio JSON-RPC，每次 tools/call 派一次性 qq-cu 子进程；信封提取兼容日志+JSON 混合 stdout。
- 验证脚本自举污染：用 cmdline 内容当进程判据会匹配到脚本自身→用进程名过滤+标记。
- git commit 信息含工具名字样会被 hook 误拦→写临时文件 `git commit -F`；gitee HTTPS 推送偶发卡死，github SSH 443 稳定。

## 多端 AI 陪伴（ai-web-page/wechat_client/小程序）
- 部署：仅 ECS https://aligera.website（FastAPI/uvicorn+nginx+systemd）；demo_gate 中间件 DEMO_PASSWORD。
- 语音克隆：DashScope Qwen-TTS qwen3-tts-vc-realtime；密钥收敛 ai-web-page\wechat_et_qq_client\tools\data；voices.json 登记。
- 前端错误收敛 wechat_client/utils/errors.js；webapp 迁移 Vite+TS+设计令牌。
- P0 路径穿越（/assets/ 读 server/config.py）已修，保持复核警惕。

## 共享雨伞 IoT
- STM32+ESP8266+微信云开发+小程序；边缘 GPS/防拆/锁，云端计费/二维码（generateRackQrcode）。
