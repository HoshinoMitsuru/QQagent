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
- 前台依赖的真实范围（实测）：**只有「写文本」一步必须抢前台**；
  读取完全免前台（最小化也能读），切会话走 `InvokePattern` 也免前台。
  彻底解法是 CDP（`Input.insertText`），尚未落地。

## 目录约定（`pc-agent-demo/`）
- `agent.py` 主流程（读→防抖聚合→入队→生成→三重复核→发送）；
  `qqid.py` 取 QQ 号；`reply_queue.py` 排队与风控。
- `error_codes.py` **错误码目录**（61 条，十个域），agent 与 app 共用同一份；
  `app/errors.py` 统一 JSON 信封；`app/diagnose.py` 一键体检 + 可复制报告。
- `app/` 控制台壳：`main`（双模式入口）`server`（REST+SSE）`supervisor`（子进程托管）
  `qqctl`（QQ 发现/体检/重启 + UiaWorker）`settings`（三文件配置）`tray` `platform_win`
  `logbus` `paths` `runtime` `errors` `diagnose` `web/index.html`。
- 构建：`build.bat` → `qq-agent.spec` → `dist/qq-agent.exe`（onefile + windowed + uac_admin）。
- 测试：`test_errors.py`（错误体系 94 条）、`test_ui.py`（控制台 104 条）、
  `test_exe.py`（真 exe 45 条）、`test_discovery.py`(43)、`test_pipeline.py`(23)、
  `test_text_port.py`(57)、`reply_queue.py --selftest`(45)。

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


## 用户偏好（本项目内已确认）
- UI 禁 emoji，用内联 SVG 图标；支持明暗主题。
- 破坏性操作必须二次确认并说明后果（重启 QQ、真发消息、清空上下文）。
- 存疑的设计要给「为什么这样做」的解释，而不是只给结论。
