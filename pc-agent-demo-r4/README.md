# PC 端 QQ AI 代理（路线 A · R4 独立桌面托管版）

用「操作本机 QQ 客户端」代替协议接入，绕开 QQ 协议风控。
不碰协议、不碰硬件模拟，程序做的只是**读控件、写文字、触发发送按钮**。

## 版本定位：这是 V2（R4 独立桌面托管版）

本目录是**两棵并列目录树里的 V2**。另一半在 `../pc-agent-demo-vm/`（V1），
指针钉在 git tag `v1.0-vm-attach`。

**本目录是从 V1 复制出来的**，所以里面**还有大量 V1 的代码**（附着式那一套）。
下面把「已经属于 V2 的部分」和「还是 V1 的部分」分开列清楚 ——
看这份 README 时请以这张表为准，不要拿文件的数量去猜进度。

| 能力 | 状态 |
| --- | --- |
| `app/desktop.py` —— 桌面生命周期 + 跨桌面启动进程 | ✅ 已落地 |
| `app/winmsg.py` —— 窗口消息投递 + 画面抓取（无 Pillow 也出图） | ✅ 已落地 |
| `test_desktop.py` —— 跨桌面落地 + 非活动桌面抓图的自动化验证（31 项） | ✅ 全通过 |
| `error_codes.py` 的 `E-DESK-*` 域（6 条） | ✅ 已落地 |
| `app/host.py` —— 独立 profile 的启动 / 认领 / 停止（可指定桌面） | ✅ 已落地，实机验证 |
| `app/hostagent.py` —— **宿主进程**（跑在隐藏桌面上、替壳去碰 QQ） | ✅ 已落地，实机验证 |
| **登录**（隐藏桌面上免扫码登录） | ✅ 实测通过（比截图扫码省事得多，见下） |
| **控制台里的「隐藏桌面」面板** | ⏸ 进行中 |
| **端到端真机收发**（小号在隐藏桌面上跑 agent 收发消息） | ⏸ 未做 |
| V1 的附着逻辑（`agent.py` 里的前台/剪贴板链路） | ⚠️ 原样保留，**尚未替换** |
| V1 的 VM 专用文档（`跨终端部署与操作手册.md` 等） | ⚠️ 原样保留，V2 不适用 |

### V2 与 V1 的分工

| | V1（`../pc-agent-demo-vm/`） | **V2（本目录）** |
| --- | --- | --- |
| 运行前提 | 附着到**已经登录好的那个桌面**上的 QQ | 自己建一张**用户看不见的桌面**，把 QQ 启动上去 |
| 写文本怎么走 | 抢前台 → 剪贴板 → 按键 → **归还前台** | 一次窗口消息（那张桌面上没有前台可抢） |
| 打扰用户吗 | 会：写入期间 QQ 被置顶、键盘输入被接管 | 不会：QQ 不在用户的桌面上，屏幕上什么都没有 |
| 适用场景 | **虚拟机 / 专用机** | **用户自己的机器** |
| 状态 | 可用，已实机验证 | 机制层已落地，宿主与登录流程进行中 |

## 机制要点（这三条是 V2 成立的全部理由）

1. **`EnumWindows` 只枚举调用者所在桌面**。壳活在用户桌面上，所以它**永远看不到**
   隐藏桌面上 QQ 的窗口 —— 这就是必须有「宿主进程」的原因，
   也是「壳直接去读 QQ 窗口」这类写法必然失败的原因。见 `app/winmsg.py`。
2. **「前台」是 per-desktop 的概念**。在那张桌面上我们的窗口就是前台，
   写文本不再需要抢用户的前台，连「按键漏进别的窗口」这个老坑一起消掉。
3. **Chromium 的遮挡判定只在本桌面内算**（z 序枚举减法）。别的桌面上的窗口
   不在它的枚举结果里，于是不会触发「被盖住 → 停止渲染 + JS 节流」那条自保逻辑。

实测结论（见 `archive/host-result.json`）：探针最后一次运行用的是**「零激活」档** ——
不 `AttachThreadInput`、不 `SetFocus`、不抢前台，直接向顶层窗口投递，6/6 全部落地，
页面自报 `fps` 从 0 升到 175、输入框读回 `R4中文测试`，UIA 也独立读到了同一个值。
所以 `winmsg.send_text()` 默认走最简单的一条路，`key` 模式作为备查保留。

### 身份怎么定：不靠读界面，靠启动参数

实测（2026-09-24，证据在 `archive/two-accounts.json`）：本机同时登录的两个 QQ，
**主进程命令行逐字相同**，且所有子进程的 `--user-data-dir` 都指向同一个 `%APPDATA%\QQ`。
只读路径下连「自己这个号是多少」都读不到 —— 主界面左侧只有昵称/头像/签名/天气，
扫完 894 个节点 × 8 类 UIA 属性，命中 0。

所以 V2 不去事后猜身份，而是**让身份由启动参数保证**：
小号由我们用独立 `--user-data-dir` 启动，于是

> 命令行里带这份 profile 路径的 QQ 进程 == 我们托管的小号。

这一条同时解决三个问题：壳看不到隐藏桌面的窗口但**看得到进程**（进程枚举不受桌面限制），
所以凭命令行就能认领自己的实例；停止时按 pid 精确结束，绝不误伤用户自己的 QQ；
独立 profile 也是 Electron 单实例锁的作用域，不会被用户桌面上那个实例接管。

### 登录这一关怎么过的：不截图，直接点

原方案是「`PrintWindow` 把二维码抓出来给用户扫」。实测发现有一条**省事得多**的路：

QQ 的登录票据**不在** `--user-data-dir` 里，而在全局的
`%APPDATA%\Tencent\TXSSO\SSOConfig\{AppDB,GlobleDB}`。所以只要这台机器之前登录过这个号，
新实例（哪怕 profile 是全新的）登录页会直接列出账号，并给一个「登录」按钮：

```
[CheckBoxControl] name='自动登录'  class='q-checkbox'
[ButtonControl]   name='登录'      class='q-button q-button--primary ... login-btn'
```

宿主进程读 `IsInvokePatternAvailable`（**不能**用 `GetCurrentPattern` 判断 —— Chromium 会谎报）
拿到 `invoke=True`，`Invoke()` 一下，再勾上「自动登录」，就完成了：

| 时刻 | 窗口尺寸 | UIA 读到的 |
| --- | --- | --- |
| 登录前 | 320×460 | 38 节点，只有账号名 |
| 登录后 | 963×643 | 137 节点，昵称 + 会话列表 |

**全程用户不碰键盘鼠标，也没有抢前台。** 「截图扫码」降级为「机器从没登录过这个号」时的兜底。

⚠️ 代价要说清楚：`--user-data-dir` 隔离的是 **Chromium 侧**（缓存 / cookie / localStorage），
**登录票据是全局共享的**。所以它不等于账号隔离 —— 换个 profile 并不会换号，
同一个号在两处登录仍然会互相挤下线（点登录前应先把桌面上那个同号实例退出）。

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

## 一体化控制台（exe + WebUI）

手动开终端敲一串命令的过程已经收进一个窗口程序里：**双击 `qq-agent.exe` → 浏览器自动
打开控制台 → 所有操作在界面里点**。用户侧的准备工作只剩两件：**打开 QQ 并登录**、
**填一次 API Key**。

详见 **[`EXE使用说明.md`](EXE使用说明.md)**（面向使用者）。这里是设计与实现上的要点。

### 一个 exe 两种身份

```
qq-agent.exe                  壳模式：HTTP 服务 + 托盘 + 代管子进程
qq-agent.exe --run-agent ...  把自己当 agent.py 跑（壳以子进程方式调用）
qq-agent.exe --run-qqid  ...  把自己当 qqid.py 跑
```

**为什么要共用同一个 exe**：冻结之后只有一个可执行文件。壳若去调用「系统里的 python」，
那台机器就必须先装 Python 和一整套依赖，「拷过去双击就能用」的前提立刻消失。
共用的附带好处是——界面里跑的和命令行里跑的**必然是同一份代码**，不会漂移。

**为什么 agent 必须是子进程**：UIA 的 COM 对象绑定在创建它的线程公寓上；和 Web 服务共用
一个进程既会被信号处理干扰，也没法「停了再起」。另外任务要能串行排队（见下）。

### 界面上的五步

| 步骤 | 做什么 | 关键点 |
|---|---|---|
| ① 定位 QQ | 自动查找 | 顺序：手动指定 → 注册表 → 常见目录 → **盘根**（本机 `D:\QQ.exe` 靠这条） |
| ② 启动 QQ | 带 `--force-renderer-accessibility` 拉起 | 只在 QQ 完全没在跑时可用；登录由用户自己做 |
| ③ 体检 | 判「无障碍树读不读得到」 | **不看命令行猜**，见下 |
| ④ 填参数 | 6 组配置 + 运行环境 | API Key 只写 `secrets.local.json` |
| ⑤ 开始常驻 | 先只读、后真发 | 停止走「哨兵文件」而不是 Ctrl+C，见下 |

### 为什么体检不看命令行参数

启动参数**只在 QQ 主进程首次启动时生效**。QQ 已经在跑的时候再执行一次带参命令，
它只会把已有窗口唤到前台，**参数不应用** —— 查命令行会告诉你「参数都在」，
而实际什么都读不到。所以判据只有一个：**读得到会话列表 / 消息列表吗**。

### 停止常驻为什么用「哨兵文件」

子进程是用 `CREATE_NO_WINDOW` 起的，**它没有控制台**，所以 `CTRL_C` / `CTRL_BREAK`
送不进去，信号这条路走不通。改成：supervisor 写 `state/STOP`，agent 的循环每轮看一眼，
看到就走收尾（把队列里已生成的回复发完、落盘、返回 0）。跨进程协议只有「文件在不在」，
不挑环境。

### 任务串行与互斥

除「纯读取」的任务（自检、离线回放、只读诊断、会话列表体检、看上下文）之外，
所有会碰 QQ 的任务都要求：**常驻进程先停下来 + 同一时刻只有一个**。
理由很直白：两个进程同时切会话、同时写输入框，轻则文字打进错误的会话，
重则把 A 的回复发进 B 的窗口。约束用一个 `concurrent` 标志表达，
而不是散落在一堆 `if` 里 —— 这种安全约束最怕漏判一处。

### 密钥边界

| 文件 | 内容 | 能不能公开 |
|---|---|---|
| `config.json` | 全部行为参数 | 可以（注释键都会被保留） |
| `secrets.local.json` | API Key | **不可以**，已被 `.gitignore` 排除 |
| `app-settings.json` | 壳自己的设置（QQ 路径、端口） | 可以 |

界面回显 Key 永远是掩码，接口也不会把明文吐回来 —— `test_ui.py` 里有专门的用例守着。

### 安全模型

控制台只监听 `127.0.0.1`，且**每次启动生成随机令牌，首屏 HTML 也必须带令牌才能打开**。
这不是走过场：这个界面能读聊天记录、能冒充你发消息、还能结束你的 QQ 进程 ——
没有令牌的话，同一台机器上任何一个网页里的一行 `fetch` 就能把这些全做完。

### 虚拟机路线

选择 VM 是为了彻底绕开「抢用户屏幕」和「锁屏不可用」。界面里有两个配套动作：

- **禁用自动锁屏**：`powercfg` 关显示器/硬盘/睡眠/休眠超时 + 关屏保 + 关锁屏界面
  （组策略项仅专业版生效）。**必须做** —— 锁屏时 UIA 读得到但点击、按键、Invoke 全部失败，
  表现是「发现 → 入队 → 复核失败 → 重排」无限循环，很容易被误判成程序坏了。
- **开机自启（计划任务 + 最高权限）**：`/RL HIGHEST` 的登录任务**不弹 UAC**，
  这对无人值守的虚拟机是决定性的。

> 宿主机锁屏不影响 guest；但**宿主机睡眠/休眠会挂起虚拟机**。

### 这一层踩到的坑（都不是逻辑问题，是 Windows 的脾气）

| 现象 | 根因 | 修法 |
|---|---|---|
| `尚未调用 CoInitialize` / `Can not load UIAutomationCore.dll` | `uiautomation` 内部的 `CUIAutomation` 是**进程级单例**，在首次被使用时把 COM 对象建在「当时那个线程」的公寓里；从 HTTP 请求线程顺手 import 一下就会把它钉死在错误的公寓 | 全进程**只在一条 UIA 工作线程上**碰 UIA，且该线程启动时显式 `InitializeUIAutomationInCurrentThread()`。只 import 是不够的——import 不创建单例 |
| `ctypes.ArgumentError: int too long to convert`（窗口过程里） | 没声明函数原型时 ctypes 按 32 位 int 传参；而 `HWND/HANDLE/LPARAM` 是指针宽度 | 显式写 `argtypes` / `restype`。同理 `CreateToolhelp32Snapshot` 不声明 restype 会**静默截断句柄** |
| 打包后状态写到重启就丢 | 冻结后 `__file__` 指向 `_MEIPASS` 临时解压目录 | `HERE` 改成「`QQ_AGENT_HOME` → exe 所在目录 → 源码目录」三级解析；`qqid.py` 直接引用 `agent.HERE` 而不是自己再算一遍 |
| 退出时最后几行日志丢失 / 常驻子进程被丢下 | 后台线程收尾、主线程同时往下走直接退出进程 | 谁在等退出信号谁负责收尾；托盘模式下由看门线程收尾、主线程等它做完 |
| 界面里任务卡在「运行中」不动 | Python 输出到管道是块缓冲 | 子进程强制 `PYTHONUNBUFFERED=1` + `PYTHONIOENCODING=utf-8`；日志泵读**二进制**再按 UTF-8 解码，避免被中文 Windows 的 GBK 弄成乱码 |

### 怎么验证这一层

```powershell
python test_ui.py      # 101 条：端到端跑真 HTTP、真子进程，只隔离数据目录
python test_exe.py     # 38 条：把真 exe 拷到干净目录里跑，验证冻结适配
build.bat              # 构建 dist\qq-agent.exe
build.bat console      # 构建带控制台的诊断版（排错用）
build.bat dir          # 构建目录版（启动快）
```

`test_exe.py` 这一步不能省：冻结后最容易出的两类问题（状态写进临时目录、
隐式导入漏掉导致运行到某个功能才 ImportError）**只有跑真 exe 才能发现**。

## 错误体系：一个码只对应一个根因

### 先说它解决的问题

项目早期所有失败都挤在一句话里。最典型的是这句：

```
找不到 QQ 窗口。请确认 QQ 已启动，且带 --force-renderer-accessibility 参数。
```

它同时压在**三个根因**上，而三者的处理方式完全不同：

| 真实根因 | 正确动作 | 按那句话去做的后果 |
| --- | --- | --- |
| QQ 根本没启动 | 启动 QQ | 没损失 |
| QQ 在跑，但窗口缩在托盘 / 被最小化 | 把窗口显示出来（不必重启） | **白重启一次，正在输入的文字会丢** |
| 窗口可见，但没带无障碍参数 | **必须完全退出后带参重启** | （另一种情况会去白重启） |

报错混淆的代价不是"不够好看"，而是**把人引向错误的修复动作**。
所以规矩是：**一个错误码只对应一个根因，并给出确定性判据与对应动作。**

### 结构

```
error_codes.py        错误码目录（71 条 / 十一个域，agent.py 与 app/ 共用同一份）
  ├─ CATALOG          码 → {title, severity, causes, fixes, domain}
  ├─ describe(code)   多行诊断块（人看的主输出）
  ├─ wrap(exc)        原生异常 → 码（只做能确定的映射，不瞎猜）
  ├─ Throttle         同一码在窗口内只输出一次完整诊断
  └─ audit()          目录自检（写错了域/少了 fixes 会被抓出来）

app/errors.py         统一 JSON 信封（接口层）
app/diagnose.py       一键体检 → 结构化清单 + 可复制报告
agent.py: report()    带抑制的带码报错（常驻循环用）
agent.py: diag()      直接打印诊断块（命令行诊断用）
```

十个域，覆盖每个环节：

| 域 | 条数 | 管什么 |
| --- | --- | --- |
| ENV | 8 | 启动、权限、端口、锁屏、位宽 |
| PATH | 3 | 路径解析、文件缺失、状态文件损坏 |
| QQ | 7 | 定位 / 进程 / 可见窗口 / 无障碍 / 重启 / CDP |
| UIA | 4 | COM 线程、调用超时、Pattern、锚点 |
| READ | 2 | 读到了但全被跳过、会话读取覆盖不全 |
| FG | 4 | 抢前台、聚焦被抢、归还失败、切会话失败 |
| SEND | 9 | 剪贴板 / 按钮 / 回读 / 三道闸 / 重试耗尽 |
| LLM | 10 | 密钥 / 连通 / 超时 / 401 / 404 / 429 / 5xx / 结构 / 空回复 |
| CFG | 7 | 语法错（带行列号）/ 越界 / 矛盾 / 保存失败 |
| PROC | 6 | 任务串行、常驻互斥、参数缺失、退出 |
| WEB | 3 | 令牌、服务不可达、接口内部异常 |

### 输出的样子

**日志里**（常驻循环，逐行输出以便控制台按级别着色）：

```
[00:11:40] ERR  E-QQ-004 QQ 未以无障碍模式启动（UIA 树是空的）
[00:11:40] ERR    可能原因：启动时没带 --force-renderer-accessibility
[00:11:40] ERR    可能原因：启动时带了参数，但 QQ 之前已经在跑，参数没被应用
[00:11:40] ERR    怎么办　：点界面上的「重启 QQ 到可读状态」（会完全退出后带参拉起，登录态保留）
[00:11:40] ERR    怎么办　：判据是「读不读得到会话列表」，不看命令行 —— 命令行会骗人
[00:11:40] ERR    现场数据：窗口=QQ  消息列表=False  输入框=False  窗口可见=True
```

**接口里**（所有失败响应同一个信封，界面据此显示码 + 原因 + 下一步）：

```json
{ "ok": false, "code": "E-QQ-004", "severity": "error",
  "error": "QQ 未以无障碍模式启动（UIA 树是空的）",
  "hint": "点界面上的「重启 QQ 到可读状态」…",
  "causes": ["…"], "fixes": ["…"],
  "detail": "窗口找到了，但里面读不到输入框和消息列表",
  "context": {"消息列表": "False", "输入框": "False"} }
```

界面上的错误码是**可点击的**：点一下就弹出这个码的判据与动作，不用去翻文档。

### 错误抑制：为什么必须有

常驻循环每 ~0.8 秒一轮。持续性故障（QQ 被最小化、锁屏、切不过去）会以**每秒一条**
的速度重复同一句话，几分钟就是几万行 —— 真正有价值的信息（第一现场）被埋掉，
界面上的日志面板也没法看了。

`Throttle` 的行为：窗口内第一条输出完整诊断，后续静默计数，窗口过期后输出
「同一问题已被抑制 N 次」+ 完整块。窗口默认 60 秒。

### 一键诊断报告

界面右上角「诊断报告」，或 `GET /api/diagnose`。跑 17 项检查，每项给
**结论 + 错误码 + 现场数据 + 下一步动作**，最后拼成一段可复制纯文本：

```
[X] 无障碍可读性  E-QQ-004
    现状：窗口找到了，但读不到输入框和消息列表
    建议：点界面上的「重启 QQ 到可读状态」（会完全退出后带参拉起，登录态保留）
```

这一步的意义：出问题时最花时间的从来不是修，而是**确认到底哪一环坏了**。
报告一复制出去，就不用再来回问「你的数据目录在哪」「Windows 版本多少」
「QQ 是最小化的吗」。

> 注意 `ok` 与 `healthy` 是两个独立字段：`ok` 表示"这次体检调用成功了"，
> `healthy` 才是"环境是否健康"。混成一个会让界面把「体检发现 3 个问题」
> 显示成「接口调用失败」——恰好丢掉最有价值的信息。

### 读取路径的可观测性：「对方发了消息但没回应」怎么查

这是现场反馈里最难判的一类问题，因为**「读不到」和「读到了但被跳过」在日志上几乎一样**。
两条路径本来就不同：

```
当前打开的会话  → 实时路径  step() → read_messages() → split_new() → _ingest() → _admit()
其它会话        → 发现路径  discover()（扫会话列表）→ 占位 → prepare() → _read_into_history()
```

`discover()` 里明确跳过当前会话（`if cur_title and s.display_name == cur_title: continue`），
所以「**只是当前会话收不到**」这种症状，指向的一定是实时路径。

实时路径上原本有一个**完全静默**的失败模式：`_admit` 拒收时只返回一个短句，
而「方向判反」那一支连短句都没有（`return [], ""`）—— 于是
「对方发了消息但被判成自己发的」会被悄悄扔掉，**日志里一个字都不出现**，
和「对方根本没发消息」完全无法区分。已经修掉：

| 改动 | 效果 |
|---|---|
| `_admit` 拒收一律给出原因，且原因里**直接写验证方法** | 方向判反会在日志里留下 `SKIP 方向判定为『me』…跑一次「只读诊断」核对【我方】/【对方】` |
| 新增 `_report_round()`：本轮读到 N 条但一条都没收下时，报 `E-READ-001` 并列出**原因分布** | 「静默无回应」变成一行可查的日志 |
| 心跳加 `read / fresh / skipped` 三个计数 | 界面「常驻运行」卡片直接显示这三个数，不相等就要留意 |
| 新增 `--watch` 实时读取监视器（只读） | **专门回答「新消息到底有没有被读到」**，见下 |

#### `--watch`：把这一层单独拎出来测

```powershell
qq-agent.exe --run-agent --watch 60      # 控制台里也有对应按钮
```

启动后照着提示**去 QQ 里给当前会话发一条消息**，1 秒内会报出来：

```
  [第7轮] ★ 新增 1 条（本轮共读 12 条）
      【对方】Psyche-嗅尘紫蝶      key=aid:7684140137907339382        在吗
```

它同时做三件交叉验证：

1. **方向对照** —— 新条目标【我方】就说明方向判反了（常驻会跳过它）。
2. **key 稳定性** —— 同一段正文换出不同 key，说明消息 ID 读不到、退化成内容指纹，
   那会让「重复发同样的话」被当成同一条**静默丢弃**（现场反馈里的「重复发消息测试」正好撞这个）。
3. **与会话列表交叉验证** —— 会话列表那一行（摘要/未读）是**另一条独立的读取通路**。
   它变了而消息区没读到新条目，就证明**消息区的无障碍树被冻结**，而左栏还在更新：
   典型成因是 QQ 窗口被最小化/完全遮挡，Chromium 节流了渲染。
   单看消息列表永远分不出这两种情况，这条对照能直接分开。

### 场景切换：VM 之后，「绝不丢消息」成为最高目标

**部署场景确定为专用虚拟机，所以「打扰」不再是约束，正确性才是。**
据此，之前为「少打扰」做的三处取舍全部反转 —— 它们每一条都会导致
**「那批消息永远没有人回」**：

| 原来的行为（少打扰优先） | 现在（不丢优先） |
| --- | --- |
| 中止时**删掉**输入框里的草稿 | **保留**（`E-SEND-012`），队列拿着同一条继续重试 |
| 重排时 `item.reply = ""`（作废重生成） | **保留回复**，只重试**发送**，不重新生成 |
| `attempts >= max_attempts` → `drop(aborted=True)`（撤单） | **不撤单**，长退避重试（`E-SEND-013`） |
| 队列只在内存里，重启即丢 | **落盘** `state/pending-replies.json`，启动恢复（`E-SEND-015`） |

还有两处会**间接**丢消息的地方，一并修掉：

- `ensure_pending` 原来在「发现该会话又有新动静」时**作废已生成的回复**（`reply=""`）。
  现在改为记进 `late` 桶 —— 老的先发，新的结转成新的一条。
- `offer_followup` 原来允许把新消息 append 进 `texts`，**即使这条已经定稿**。
  那句回复是按旧内容写好的，所以并进去等于「新消息事实上没被回应」。
  现在已定稿的项返回 `deferred`，新消息进 `late`。

#### 三条保证

```python
# ① 已生成的回复一定会被送出去：一旦生成就冻结
item.prepared = True; item.reply = reply
self._save_pending()          # 立刻落盘：从这一刻起它「必须」被送出去
# 失败时不再清空：
self.queue.note_send_failure(item, reason=_LAST_CODE.get("code") or "E-SEND-008")
self.queue.requeue(item, delay=self._retry_delay(item))   # 退避递增，永不放弃

# ② 输入框里的草稿 = 可恢复的进度 → 重试走最短路径，不重新粘贴
if self.editor_contains(text):
    log("RESUME", "输入框里仍是上一轮那句话 → 跳过重新写入，直接重试发送")
else:
    reply = f"E-SEND-014 草稿已不在，改为重新写入"

# ③ 挤压时先发老的：队列把「已定稿待发」的项排在前面
cands.sort(key=lambda x: (2 if x.fail_count >= 3 else (0 if x.frozen else 1),
                          x.first_at, ...))
```

第 ③ 条的 `fail_count >= 3` 那一档是刻意的：**降权但不丢弃** ——
一条永远发不出去的项不能霸占循环把其它会话全饿死，但它必须留在队列里。

#### 关于「宁可重复，不可丢失」

落盘是在**发送之前**做的。如果进程恰好在 `Invoke` 之后、标记成功之前被杀，
重启后会**重发一次** —— 也就是说极端情况下可能出现一条重复消息。
这是刻意选的：**重复一条，比永久少一条好**。VM 场景下这个取舍没有争议。

#### 现在该盯哪个数

界面上「常驻运行」卡片多了一个 **待发出回复**：

- `0` = 正常
- `> 0` = 有已经写好的回复卡在发送环节，卡片会列出会话、失败次数与原因码。
  **不会丢**，但如果一直不降，就按那个码去修。

> **千万别删 `state/pending-replies.json`** —— 那才是真正会丢消息的操作。

#### 顺带改掉的两个默认值

| 配置 | 原默认 | 现默认 | 原因 |
| --- | --- | --- | --- |
| `uia.restore_foreground` | true | **false** | 不归还就没有「抢不到前台」这类失败（`E-FG-001/002`），下次操作也不用重新抢。专用虚拟机上让 QQ 一直留在前台**更可靠** |
| `chat.keep_draft_on_abort` | —（原来是删） | **true** | 草稿 = 可恢复的进度 |

> 在**你自己每天用的电脑**上跑，把「用完后归还前台」打开，否则 QQ 会一直霸占最上层。

### 输入框是**用户的地盘**：三条硬约束

现场观察到的现象促成了这一节：

```
agent 呼出 QQ → 粘贴一段文字 → 归还前台 → 又呼出 QQ → 把输入框里的字删掉
```

这是中止发送后的清理（`_abort_cleanup`）。原来它「一律抢回前台来清」，等于把
「用完即还焦点」的承诺撕了：用户被打扰两次，第二次纯破坏性；**万一用户此刻正在输入，
被删掉的就是他自己的字**。固化成三条约束（`test_errors.py §14` 逐条守着）：

**① 先做无副作用的检查，再做有副作用的动作。**

```python
# 改前：先 clear_editor() 再 copy_to_clipboard()
#   → 剪贴板若失败，输入框里原有的内容（可能是用户打了一半的话）已经被毁掉了
self.clear_editor()
if not copy_to_clipboard(text): return False
# 改后：写剪贴板是纯内存操作、零副作用，所以提前
if not copy_to_clipboard(text): return False
self.clear_editor()
```

**② 清理按「打扰程度」三级降级，能用低就不用高。**

| 级别 | 手段 | 抢前台？ |
| --- | --- | --- |
| ① | `ValuePattern.SetValue("")` —— 免前台、不动键盘 | 否 |
| ② | QQ **恰好还在**前台 → Ctrl+A/Delete 顺手清掉 | 否（本来就在前台） |
| ③ | 都不可用 → **保留草稿** + 报 `E-SEND-011`（带草稿内容） | **绝不**为了擦草稿再抢一次 |

第 ③ 种是刻意的取舍：**留一段看得见的草稿（人就坐在电脑前，一眼能看到、顺手删掉）
远好过偷偷抢走焦点再删一次** —— 前者是可见的不便，后者是不可预期的手部动作。

**③ 「慢」要当成一等公民，而不是当成失败。**

`wait_send_enabled` 原来硬编码等 1 秒。慢速虚拟机上 QQ 同步按钮状态经常超过 1 秒，
于是「粘贴成功 → 按钮还没恢复 → 判成失败 → 中止 + 清理」——
用户看到的就是「它粘了字又回来删掉」这种毫无意义的动作。
现在改成可配的 `chat.send_button_wait_seconds`，默认 **3 秒**。

> 这三个问题在源码里看着都"合理"，只有盯着界面观察才会暴露 ——
> 所以它们都进了 `EXE使用说明.md §8.3`，让下一个人不必再看一遍才明白。

### 心跳：为什么它必须带上「阶段」

心跳是常驻主循环每转一圈写一次的时间戳，它要回答的是「循环还在转吗」。
但主循环是**串行**的，而其中有几步天生就慢：

| 阶段 | 上限 |
| --- | --- |
| 调模型（**同步跑在主循环上**） | `llm.timeout_seconds`，默认 **60s** |
| 切会话 + 发送 | ~30s（抢前台、剪贴板、按键、若干 sleep） |
| 扫会话列表 | ~30s（随会话数线性增长） |
| 读消息 / 重扫锚点 | ~20s（跨进程 COM 遍历控件树） |

**原来的实现有个必然误报**：心跳只带 `ts`，且 stale 阈值在 `supervisor` 里
硬编码 `age > 30`。于是**每一次稍慢的模型调用都会弹出「心跳已停更，进程可能卡住了」**——
不是故障征兆，而是设计与实际耗时脱钩。它还会把人引向错误方向（去重启一个健康的进程）。

现在 `agent.set_phase(name, budget)` 在**每个慢阶段的入口**把三样东西写进心跳：

```
phase            现在在哪一步          → 界面显示「调模型　已 42s」
phase_since      这一步从何时开始      → 算出「该阶段已持续多久」
stale_after      这一步的合理上限      → 取代硬编码的 30s
+ prev_phase / prev_phase_seconds / phase_stats   → 界面上的「各阶段耗时排行」
```

`supervisor.heartbeat()` 只负责用 `stale_after` 判定，不再自己拍一个数。
副作用是**可诊断性大幅提升**：以前只知道「卡了」，现在直接知道「卡在调模型，已 42s，上限 75s」。
单轮耗时超过轮询间隔 4 倍时，还会报一条 `E-PROC-007`（带阶段、耗时、CPU 核数）。

> **这张「各阶段耗时排行」也是回答「是不是虚拟机太慢」的直接证据** ——
> 慢在「调模型」是接口问题，慢在「读消息/扫会话列表」才是虚拟机 CPU 或渲染问题。

### 两条防回归的检查`test_errors.py` 里有两条会随代码增长自动生效的检查：

1. **扫源码**：不允许再出现「说了失败但没给码」的报错
   （匹配 `log("ERR"|"WARN", "…失败/找不到/无法…")` 且同处没有 `E-XXX-000`）。
   加新功能时很容易又写回一句 `log("ERR", "失败了")`，那正是这次要消灭的东西。
2. **目录自检**：编号格式、域前缀、必填字段、以及每条 `fixes` 是否给了
   **可执行的抓手**（界面元素名 / 路径 / 命令 / 明确动词）。
   写成「这是正常现象」那样的解释会被判不合格 —— 用户要的是「我现在该点哪儿」。

顺带说一句：这两条检查在本轮真的抓到了东西 —— 8 处漏网的降级报错、
3 条过于笼统的 `fixes`、以及 `resolve_api_key` 一个**依赖当前工作目录**的隐蔽 bug：

```python
# 改前：CWD 下刚好存在同名文件时会优先用它 —— 从项目目录启动就读到项目里的密钥，
#       从别处启动就读到别的（或读不到）。表现为「同样的配置，换个目录结果不一样」。
if path and not os.path.isabs(path) and not os.path.isfile(path):
    path = abs_here(path)
# 改后：相对路径一律按数据目录解析，绝不看 CWD
if path and not os.path.isabs(path):
    path = abs_here(path)
```

### 怎么验证这一层

```powershell
python error_codes.py     # 目录自检 + 按域统计（71 条）
python test_errors.py     # 179 条：目录完整性 / 异常映射 / LLM 分类 / 信封 / 诊断报告 / 读取可观测性 / 心跳阶段 / 输入框安全 / **不丢回复** / 防回归扫描
```

## 文件

| 文件 | 用途 |
| --- | --- |
| `probe.py` | 第 0 步：探明 QQNT 把界面暴露成了哪些 UIA 控件 |
| `agent.py` | 主程序：读消息 → **防抖聚合** → **入队** → 生成 → **三重复核后发送**；另含**多会话发现**（`discover`）与**延迟总读取**（`prepare`） |
| `qqid.py` | **取 QQ 号**（资料卡通路）+ **解析会话列表**（`list_sessions`，发现链路的输入） |
| `reply_queue.py` | **排队 + 风控限速 + 补充消息合并**（纯逻辑，含离线单测与容量仿真） |
| `probe_uid.py` | 探查会话列表项能否读到 QQ 号（结论：**读不到**，见下） |
| `probe_profile.py` | 探查资料卡通路的取号可行性 |
| `test_send_guard.py` | **实测**防错发的三道闸（切走会话后必须能检出） |
| `test_pipeline.py` | **端到端彩排**：收到消息 → 入队 → 生成 → 复核 → 止步（不发送） |
| `test_discovery.py` | **离线单测**：发现/占位/总读取/撤单/非文本策略（43 条，QQ 与模型全 fake） |
| `test_live_chain.py` | **真机联调**：伪造一条未读 → 走完整真实链路（默认彩排，加 `--live` 才跑） |
| `probe_minimized.py` | 测「最小化之后 UIA 还能读到什么」（结论：读取不受影响） |
| `probe_switch_nofg.py` | 测「切会话能否免前台」（结论：`InvokePattern` 可以） |
| **`qq-agent.py`** | **控制台入口**（源码模式）：起 WebUI + 托盘，也是 exe 的入口文件 |
| **`app/`** | **控制台壳**：`main` 双模式入口、`server` 接口与 SSE、`supervisor` 进程托管、`qqctl` QQ 发现与体检、`settings` 配置读写、`tray` 托盘、`platform_win` Win32 底座、`logbus` 日志总线、`paths` 数据目录、`runtime` 退出信号、`web/index.html` 界面 |
| **`build_icon.py`** | 生成 exe 图标（自己写 ICO 编码，不引 Pillow） |
| **`qq-agent.spec`** | PyInstaller 打包配置（单文件/windowed/UAC 清单/隐式导入） |
| **`build.bat`** | 一键构建；自动挑一个装齐依赖的解释器 |
| **`run_ui.bat`** | 源码模式启动控制台 |
| **`test_ui.py`** | **控制台端到端测试**（101 条：令牌、配置往返、密钥隔离、进程托管、互斥、SSE、UIA 线程、托盘） |
| **`test_exe.py`** | **打包产物验收**（38 条：真 exe、冻结后的数据目录、exe 自调用、退出） |
| **`EXE使用说明.md`** | **给最终用户的手册**（五步流程、VM 加固、排错表） |
| `config.json` | 配置（模型、提示词、防抖/连续对话/持久化、identity、queue、discovery） |
| `secrets.local.json` | **本地敏感信息文件**（密钥），已被 `.gitignore` 排除，不进仓库 |
| `secrets.example.json` | 上面那个文件的**模板**（不含真密钥），可以入库 |
| `state/conversations.json` | 运行期自动生成：按会话隔离的上下文（同样被 ignore） |
| `state/uid-map.json` | 运行期自动生成：`显示名 → QQ号` 映射（同样被 ignore） |
| `test_text_port.py` | 离线冒烟测试：不碰 QQ、不联网，验证移植的三项能力 |
| `requirements.txt` | 依赖清单 |
| `下一步操作清单.md` | **分步操作手册**（含预期输出与排错对照表） |
| `并发能力评估与优化方向.md` | 并发量估算、5 个正确性缺陷、排队与风控设计 |
| `probe-tree.txt` | 控件树快照（当前是**私聊**会话的快照） |
| `card-tree.txt` | 资料卡窗口的控件树快照（QQ 号就在这里面） |

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

## 会话身份：怎么拿到 QQ 号（2026-09-11 实测）

原来的会话主键是**昵称**，昵称可以随时改、可以重名 —— 一旦重名，A 的上下文会喂给 B。
要根治必须换成 QQ 号。**UIA 侧能拿到吗？逐条试过了：**

| 通路 | 能否拿到 QQ 号 | 实测证据 |
| --- | --- | --- |
| 会话列表项 `recent-contact-item` 及**全部后代** | ❌ | `AutomationId` 全空；Name/HelpText/ItemStatus/FullDescription 全空 |
| UIA 通用属性（AriaRole / PositionInSet / SizeOfSet / Level） | ❌ | Chromium 对 group 角色一律填 `group` 与 `0`，无业务信息 |
| 全 UIA 树扫 `AutomationId` | ❌ | 192 个节点里只有 5 个非空：RenderWidget / RootWebArea / drag-area / app / loading |
| 全树扫「5~12 位数字」文本 | ❌ | 0 个 |
| **资料卡窗口** | ✅ | `buddy-profile__header-uid` → TextControl `'QQ 3302676083'` |
| CDP（`--remote-debugging-port=9222`） | ⏳ 未启用 | 端口未监听；需重启 QQ 加参数 |

### 关键：资料卡是**独立顶层窗口**

这是第一版探查踩的坑 —— 只在 QQ 主窗口的 UIA 子树里找，**永远找不到资料卡**。
它长这样：

```
WindowControl '资料卡' class='Chrome_WidgetWin_1'          ← 顶层窗口，不是主窗口的子节点
  └ … 'Chrome Legacy Window' / RootWebArea
      └ GroupControl 'buddy-profile' aid='mini-buddy-profile'
          ├ ButtonControl  'Psyche-嗅尘紫蝶的头像'
          ├ GroupControl   'buddy-profile__header-name-wrap' → '查看XXX的个人主页'
          ├ GroupControl   'buddy-profile__header-uid'       ← QQ 号在这里
          │   └ TextControl 'QQ 3302676083'                  ← 私聊带前缀
          └ GroupControl   'buddy-profile__details-item' …   → 备注 / 签名 / 所在地
```

**群聊卡片**结构略有不同，QQ 号是**裸数字**且被拆成两个节点：

```
GroupControl 'buddy-profile__header-sub-wrap'
  └ GroupControl 'buddy-profile__header-uid'
      ├ TextControl '1098451742'    ← 群号，没有 "QQ" 前缀
      └ TextControl ' (7人)'
```

所以解析必须两步走：先找带前缀的 `QQ xxx`，找不到再逐个文本节点找纯数字。

### 怎么唤出资料卡

对会话标题按钮 `chat-header__contact-name` 调 **`InvokePattern.Invoke()`** 即可
（不用鼠标点击，不移动光标）。但要清楚代价：**它会把这个弹窗激活，也就把 QQ 顶到前台**。

→ 所以取号是**一次性**操作：第一次见到某个会话时取一次，之后只读缓存
（`state/uid-map.json`）。缓存的键是**会话列表上显示的名字**（通常是备注名）。

> 实测：备注名和资料卡上的真实昵称**可以完全不同**。
> 例：会话列表显示 `光みつる`，资料卡上却是 `Psyche-嗅尘紫蝶`。

### 取号命令

```bash
python -X utf8 qqid.py --list          # 列出会话 + 已缓存的 QQ 号
python -X utf8 qqid.py --enroll-all    # 给所有还没号的会话取号（会依次抢前台）
python -X utf8 qqid.py --enroll 0 3    # 只给第 0、3 个取号
python -X utf8 qqid.py --check         # 重号诊断：不同会话是否映射到同一个号
```

## 排队与风控限速

单通道 UI + 账号风控决定了：**不能"越快越好"，只能"按安全频率回"**。

| 概念 | 配置项 | 说明 |
| --- | --- | --- |
| 硬间隔 | `queue.min_interval_seconds` | 相邻两次发送至少隔这么久 |
| 频率上限 | `queue.max_replies_per_minute` | 60 秒滑动窗口内最多发几条 |
| 补充消息合并阈值 | `queue.merge_if_wait_over_seconds` | 见下，默认 5s |
| 静默窗硬上限 | `queue.max_hold_seconds` | 防抖顺延的上限，默认 30s |
| 到期抖动 | `queue.jitter_seconds` | 避免多个会话的窗口同时到期 |

### 5s 阈值：排队期间用户又发来一句怎么办

| 情况 | 判定 | 结果 |
| --- | --- | --- |
| 他**不在**队列里 | `absent` | 走正常防抖路径 |
| 他还要等 **> 5s** | `merged` | **并进**他待提交给 AI 的那批上下文，**不额外回一次** |
| 他 **≤ 5s** 就能被轮到 | `close` | 不掺和，这句**照常另排一次回复** |

后者也可以接受，是因为真人聊天里「话音未落对方又接一句」本来就常见，
两条回复分别对应两段话，撞车符合现实。

### 离线验证

```bash
python -X utf8 reply_queue.py --selftest   # 45 条单测，不碰 QQ 不联网
python -X utf8 reply_queue.py --report     # 容量仿真：扫 5~40 并发，看哪里开始积压
python -X utf8 test_pipeline.py            # 端到端彩排（真实 QQ，但绝不发送）
```

`--report` 默认「12 条/分 + 5s 硬间隔」下的实测结论：

| 并发用户 | 实际频率 | 队列残留 | 单次回复吸收的消息数 |
| --- | --- | --- | --- |
| 5 | 3.9/分 | 1 | 1.56 |
| 10 | 7.5/分 | 1 | 1.64 |
| 15 | 11.1/分 | 3 | 1.63 |
| 20 | 11.9/分（顶到上限） | 14 | 1.73 |
| 30 | 11.9/分 | 22 | 2.55 |
| 40 | 11.9/分 | 36 | 3.21 |

**安全区约 15 个会话，20 个开始饱和。** 注意最右列：负载越高，合并越激进
（一条回复吸收 3 条消息），这是排队机制在自发省额度 —— 也是它比"每条都回"更抗压的原因。

## 多会话「发现」与延迟总读取（降级方案）

上面那套排队解决的是「**什么时候发**」。这一节解决「**怎么知道谁在等我**」。

### 问题：桌面 UI 只给你一条节选

QQ 的会话列表项**只保留最新一条消息的节选**（而且会被截断），而且它是虚拟列表：
实测 7 项 × 96px = 672px、列表视口 821px，`ScrollPattern` 又不受支持 ——
**折叠线以下的会话在 UIA 树里根本不存在**。

所以两个结论：

| 结论 | 含义 |
| --- | --- |
| 节选**不能**当 AI 的上下文 | 靠它构造不出对话，硬凑只会让 AI 胡说 |
| 稳定可服务 **7~8 个**会话 | 这是虚拟化造成的**可见性上限**，不是性能上限；轮转读只能在这个范围内 |

### 方案：发现与读取彻底分开

既然列表只能告诉我们「**有人找你了**」，那就只让它干这件事，正文推迟到必须切过去的时候再读。

```
阶段 1 · 免前台（每 2s 一次，实测约 84ms）
  扫描会话列表 → 指纹比对 → 占位排队（只记未读条数，不读正文、不碰前台）
      ↓  静默窗（5s 阈值）关闭
阶段 2 · 需要前台（排到时只付一次）
  切到目标会话 → 上下文总读取（读最近 30 条）→ 按准入判定写进**它自己的**上下文
  → 调模型 → 风控放行 → 带租约发送（三道闸）→ 刷新指纹
```

**双阶段**是关键：`prepare()`（读+生成）**不发送**，所以不吃风控额度，可以早于风控放行执行；
`deliver()`（发送）才占额度。模型调用的 1.3~1.8s 因此被移出了 UI 临界区。

### 指纹：怎么判「有新东西」

指纹 = `(预览文本元组, 未读条数)`。两个要点：

| 要点 | 为什么 |
| --- | --- |
| **时间 token 必须切掉** | 预览里混着 `13:40`/`昨天`，跨零点会让所有会话一起"变化"，误触发一整轮 |
| **未读优先、摘要兜底** | 红点是硬证据；有些会话不显未读数字，只能靠摘要变化兜住 |

副作用是**我方自己发出的回复也会改预览** → 所以 `deliver()` 成功后立刻 `_refresh_fp()` 重采指纹，
否则下一轮扫描会把「自己刚发的那条」当成对方的新消息，形成**自己叫醒自己**的循环。

### 首次红点即排队，但基线不能吞掉它

首次见到某个会话时，如果它**已经带未读徽标**，说明对方发来后一直没人看 —— 是新鲜的，照样触发。
建基线时用未读条数做 `skip_last`：

```python
keep = max(0, min(int(item.unread_hint or 0), 8))   # 就是降级方案唯一还能利用的未读线索
self.qq.baseline(skip_last=keep, scope=scope)
```

这样既不回灌 30 条历史，也不会把对方刚发的那几条当成历史吃掉。

### 总读取没拿到新消息 → 撤单

`_read_into_history()` 返回对方消息条数，**0 就撤单，不回复**。这挡的是三种情况：
已被用户自己打开读过、消息被撤回、以及那条"新消息"其实是我方自己发的。
看似浪费一次切会话，但比"为一条不存在的新消息回一句"安全得多。

> ⚠️ 这也意味着**非文本未读（对方只发了个表情）默认会被整条撤掉**，
> 表现为「有红点却一声不吭」。想让它回就把 `chat.nontext_policy` 改成 `describe`
> —— 见下面「已知限制」。

### 配置

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `discovery.enabled` | `true` | 关掉就只服务「当前打开的那一个会话」 |
| `discovery.scan_interval_seconds` | `2.0` | 扫一次实测 76~90ms（7 个会话），约 4% 时间占用 |
| `discovery.trigger_on_unread` | `true` | 红点触发（硬证据） |
| `discovery.trigger_on_preview_change` | `true` | 摘要变化触发（兜底） |
| `discovery.trigger_on_first_sight_unread` | `true` | 首见就带未读是否触发（关掉则不回历史未读） |
| `discovery.max_enqueue_per_scan` | `3` | 单轮最多占位几个，避免一次灌满队列打穿前台预算 |
| `discovery.read_limit` | `30` | 总读取读多深 |
| `discovery.state_file` | `state/rotation.json` | 已建基线集合 + 上次指纹快照（**丢了会重复建基线、吞掉重启瞬间的未读**） |

### 离线验证

```bash
python -X utf8 test_discovery.py               # 43 条：发现/占位/总读取/撤单/非文本策略，不碰 QQ
python -X utf8 test_live_chain.py              # 真机联调（默认不跑，加 --live）
python -X utf8 agent.py --sessions             # 只读发现视图：列出会话+未读+会怎么判
```

`test_discovery.py` 把 QQ 和模型全 fake 掉，覆盖三个最容易回归的点：
首见的 `skip_last` 用没用上未读条数、「读不到新消息」有没有撤单、发送后有没有重采指纹。

`test_live_chain.py` 是真机联调（**唯一被替换的输入是"谁发了新消息"**）：
它伪造会话列表里某一条的未读/预览，其余全走真实 UIA + 真实模型，默认彩排不发送。

## 防错发的三道闸

`deliver()` 是一个**带租约的发送事务**，任何一闸不过就中止并重新排队：

| # | 闸 | 挡什么 | 判据 |
| --- | --- | --- | --- |
| ① | **标题复核** | 根本没切过去 | `chat-header__contact-name` == 目标显示名 |
| ② | **QQ 号复核** | 标题重名/改备注骗人 | 缓存里该会话的 QQ 号 == 目标 QQ 号 |
| ③ | **会话签名复核** | **生成/写入期间被切走** | 写入前后 `(标题, 群标记, 最后 6 条消息 ID)` 完全一致 |

第 ③ 闸的判据里最关键的是**消息 ID**：`ml-item` 的 `AutomationId` 是 18~19 位的
**全局唯一消息 ID**（如 `7684140137907339382`）。两个不同会话的可见消息集合
不可能相同，所以它比标题可靠得多，还顺带解决了"切了但没渲染完"。

> ⚠️ 签名必须 `force=True` 重扫锚点。切换会话后 `self.ml_list` 可能还指向旧容器，
> 用陈旧引用读出来的 ID 恰好等于切换前的值 —— 复核会**假通过**。

```bash
python -X utf8 test_send_guard.py   # 实测：切走会话后 ③ 必须判否，切回来必须可复现
```

彩排模式（走完整链路但不真的发出）：

```bash
python -X utf8 agent.py --no-send        # 常驻循环彩排
python -X utf8 agent.py --once --no-send # 单轮彩排
```

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
python -X utf8 agent.py --once --no-send       # 单轮彩排：走完整链路（切会话/复核/生成），不发出
python -X utf8 agent.py --no-send              # 常驻循环彩排：完整链路但不发出
python -X utf8 agent.py --send "测试"           # 真发一条
python -X utf8 agent.py --dry-run              # 常驻循环，只读不发
python -X utf8 agent.py                        # 常驻循环，正式运行（Ctrl+C 退出）
python -X utf8 archive/probe.py --depth 30 --dump      # 重新导出控件树

# 会话身份（取 QQ 号）
python -X utf8 qqid.py --list                  # 列会话 + 已缓存 QQ 号
python -X utf8 qqid.py --enroll-all            # 给所有会话取号（会依次抢前台）
python -X utf8 qqid.py --check                 # 重号诊断

# 排队 / 风控 / 防错发（都不发送消息）
python -X utf8 reply_queue.py --selftest       # 45 条离线单测，不碰 QQ 不联网
python -X utf8 reply_queue.py --report         # 容量仿真：5~40 并发扫一遍
python -X utf8 reply_queue.py --simulate 20    # 单点仿真，输出 JSON
python -X utf8 test_send_guard.py              # 实测三道闸（切走会话必须能检出）
python -X utf8 test_pipeline.py                # 端到端彩排：入队→合并→生成→复核→止步
python -X utf8 test_discovery.py               # 43 条：发现/总读取/撤单/非文本策略，不碰 QQ

# 多会话发现（降级方案）
python -X utf8 agent.py --sessions             # 只读发现视图：列会话+未读+会怎么判，不切前台
python -X utf8 test_live_chain.py              # 打印说明（真机脚本，必须显式加 --live）
python -X utf8 test_live_chain.py --live --target "某会话"           # 真机联调，彩排不发送
python -X utf8 test_live_chain.py --live --target "某会话" --send    # 真机联调并真的发出去

# 前台依赖探查（决定能不能放到虚拟机/后台跑，只读诊断）
python -X utf8 archive/probe_minimized.py              # 最小化后 UIA 还能读到什么（会最小化再还原）
python -X utf8 archive/probe_switch_nofg.py            # 切会话能否免前台（会真切会话并切回）
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

### 会话列表的结构（左栏）

会话项上**没有任何可用 ID**（`AutomationId`/`Name` 全空），唯一能拼出来的标识是
「显示名 + 时间 + 摘要」。结构上有个**反直觉**的地方（第一版画错了）：

```
GroupControl class='recent-contact-item ...'
  GroupControl class='item__content'
    GroupControl class='item__avatar' → GroupControl class='avatar'   ← Name 空、aid 空
    GroupControl class='item__info'
      TextControl  '光みつる'                        ← 显示名（= 备注名）
      GroupControl class='secondary-info'           ← 空
      GroupControl class='summary-main' → TextControl 最后一条摘要
      GroupControl class='3条未读'                  ← ⚠️ 未读徽标是 item__info 的【直接子节点】
                                                    ← 它把时间/发送者/预览**一并包住**
```

⚠️ **有未读时结构会变形**：`summary-main` 变空，时间/发送者/预览整块挪到徽标块下面。
所以解析不能"按位置取第 N 个孩子"，只能**收集 `item__info` 下所有文本节点、去重、
再用时间正则把时间 token 切出去**，剩下的才是预览。

⚠️ 收集文本必须用**手写 DFS**：`uiautomation` 的 `iter_bfs` 不会剪枝，
会把 `q-badge` 里的未读数字混进预览（实测踩过，预览变成 `('骑羊驼的后羿：', '[动画表情]', '1')`）。

⚠️ 另外两个行为坑：
- **点击「当前已选中」的会话项会把它关掉**（toggle），不是重新打开。切换逻辑必须先判断"是不是已经在这个会话上"。
- **列表顺序随新消息置顶变化**，不要用序号当身份。

### 六个反直觉的坑（全部实测踩过）

| # | 直觉 | 实际 |
| --- | --- | --- |
| 1 | 消息列表是 `ListControl`/`ListItemControl` | **没有**。条目是 `GroupControl`，靠 `class='ml-item'` + `AutomationId` 识别 |
| 2 | 输入框是 `EditControl`/`DocumentControl` | **不是**。是 `GroupControl`（ProseMirror 富文本） |
| 3 | 方向靠坐标猜 | 不用。class 里直接带：`container--self/--others`、`message-container--self/--align-right` |
| 4 | 容器 class 叫 `group-chat` 就是群聊 | **错！私聊容器也叫 `group-chat`**。群聊只能靠「标题旁的 `(人数)`」或「可见的群资料面板」判定 |
| 5 | 昵称在 `user-name` 元素里 | 私聊**根本没有** `user-name`，昵称挂在 `avatar-span` 的 `Name` 上 |
| 6 | `element.Click()` 是"点这个控件"，跟窗口层级无关 | **错！** 它是「把光标移到控件中心、在那个屏幕坐标上按一下」，点到的是**该坐标上最顶层的窗口**。QQ 被别的窗口盖住时点击会静默打偏（见下） |

### 坑 6 的实测后果：切换会话会**静默失败**

这是真机联调（`test_live_chain.py`）才暴露的 bug，表现极其隐蔽：

| 观察到的 | 真相 |
| --- | --- |
| `click_session()` 返回 `True` | 它内部「点两次」兜底，且只要 `header_title()` 非空就算成功 —— 旧会话的标题也是非空 |
| 日志只有 `切换后渲染未稳定（当前标题 X，目标 Y）` | 点击**打在了盖住 QQ 的那个窗口上**（实测前台是编辑器），标题从头到尾没变 |
| 输入路径一直正常 | `type_text()` 里有 `force_foreground()`，**切换路径以前漏了这一步** |

修法：**别再依赖坐标点击**。会话列表项支持 `InvokePattern`，它是免前台的
（实测：QQ 完全不在前台时 `Invoke()` 照样切过去）。所以现在的实现是
**Invoke 优先、坐标点击兜底**，并且成功判据从"标题非空"改成了"**标题 == 目标名**"。

顺带一个**容易连带的坑**：`Invoke` 虽然是免前台，但 Chromium 内部会对目标元素
`SetFocus` → QQ 会被激活、顶到最上层。既然这个激活是我们引起的、我们也不需要它，
就该**切完归还前台**。而归还本身还有坑：必须和**当前前台窗口**的线程
`AttachThreadInput`（`force_foreground` 的做法），跟**目标窗口**握手是没用的 ——
后者对 `Invoke` 造成的激活连等 4 秒都还不回去。

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
python -X utf8 archive/cdp_probe.py                # 1) 验证端口与可注入目标
python -X utf8 archive/cdp_probe.py --dom          # 2) 定位输入框 / 消息列表
python -X utf8 archive/cdp_probe.py --type "测试"   # 3) 实测写入（绝不发送）
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

## 对话内指令（`.ai`，从 小清澈3.0.js 移植）

在 QQ 里发一条以 `.ai` 开头的消息就能控制代理。守住两条铁律：
**指令本身既不转发给模型，也不写进上下文。**

```
.ai reset        清空当前会话的上下文（并让它退出连续对话、丢掉未结算的缓冲）
.ai help         列出可用指令
.ai <任意内容>    剥掉前缀后照常交给模型（≈ 直接聊天，且跳过触发词过滤）
```

- 前缀：`.ai` `。ai` `/ai` `.aichat`（大小写不敏感，长前缀优先）。
  前缀后**必须**跟空格或冒号，所以 `.aichat` 不会被 `.ai` 抢成命令 `chat`，`.airest` 也不误判。
- **自触发防护**：机器人自己刚发出去的回复会被排除，不会「自己说了句 .ai 开头的话 → 触发自己」。
  谁有权下指令由 `commands.accept_from` 决定（默认 `other` + `self`：对方发的和你手输的都算）。
- 回执走**正常发送队列**（照吃风控限速、身份复核、失败重试），不是旁路 `send_text`。
  `commands.reply_ack` 可关成静默执行；`commands.ack_reset` 是 reset 的回执文案。
- `.ai reset` 默认连带丢掉**还没结算的防抖缓冲**（`reset_clears_pending`）——否则那几条过几秒
  照常被回复，看起来就像「说了 reset 却没生效」。
- 指令判定放在**准入层**（`_admit`），所以「实时路径」和「发现 → 总读取」路径行为一致；
  而且在总读取途中吃到的指令，回执已定稿，**不会因为『没读到新消息』被撤单**。

**尚未移植的指令**（在指令表里但未实现）：
`stop` `on` `off` `open` `close` `end` `list` `persona` `teach` `jiaoxue` `img` `image` `itt`。
对这些会**明确回绝并提示未移植**，不会当成普通消息丢给模型；
不在指令表里的词（如 `.ai 今天天气怎么样`）才走「剥前缀 → 直通模型」。
人格列表切换（`persona`）本轮按要求**不做**。

> 配置在控制台的「对话内指令」组：`commands.enabled`（总开关）、`accept_from`、`reply_ack`、
> `ack_reset`、`reset_clears_pending`、`prefixes`（高级）。

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
| **未读徽标可读** | ✅ 会话列表项能读到 `3条未读` / `99+`，并解析出数字（发现链路的输入） |
| **扫列表开销** | ✅ 7 个会话中位数 **84ms**（min 76 / max 90）；2s 间隔下约 4% 时间占用 |
| **列表虚拟化上限** | ⚠️ 7 项 × 96px = 672px，视口 821px，`ScrollPattern` 不支持 → 折叠以下的会话**不在 UIA 树里** |
| **发现链路离线单测**（2026-09-11 第三轮） | ✅ `test_discovery.py` **43/43 通过** |
| **发现链路真机联调** | ✅ `test_live_chain.py` **15/15 通过**：发现→占位→5s→切会话→总读取→生成(4.2s)→三道闸→彩排发送→回位 |
| **非文本策略** | ✅ `chat.nontext_policy`（`skip`/`describe`）两种行为均已单测固定 |
| **最小化后读取是否可用**（2026-09-11 第四轮） | ✅ **可用**：最小化后节点数 417、会话 7、消息 9、输入框/发送钮定位**全部不变**（`probe_minimized.py`）。旧文档"最小化会让无障碍树失效"**已更正** |
| **最小化后 Invoke/点击是否可用** | ❌ **都失效**（Chromium 节流 renderer）。`force_foreground()` 能自动还原窗口救回来 |
| **切会话能否免前台** | ✅ **可以**：会话列表项支持 `InvokePattern`，QQ 完全不在前台时 `Invoke()` 照样切换成功（`probe_switch_nofg.py`） |
| **Invoke 的副作用** | ⚠️ 会被 QQ 激活顶到最上层（Chromium 内部 `SetFocus`）→ 已实现切换后**归还前台**，`test_live_chain` 断言"前台已归还"通过 |
| **前台归还的线程真相** | ✅ 必须 `AttachThreadInput` 到**当前前台窗口**的线程（`force_foreground` 的做法）；跟目标窗口握手对 `Invoke` 造成的激活**连等 4 秒都失败**。两份实现已合并 |

## 唯一还没跑过的一步

**真的点「发送」把消息发出去** —— 因为不该替你的账号做这件事。

`test_live_chain.py --live` 已经把整条链路跑到**第三道闸通过、并捕获会话签名**为止
（切会话 → 身份复核 → 读全 → 生成 → 签名一致），差的就是最后那一下
`InvokePattern.Invoke()` 点「发送」按钮。这一步留给你自己确认目标会话后再跑：

```powershell
python -X utf8 agent.py --send "这是一条自动发送测试，忽略即可"
# 或者走完整的「发现 → 总读取 → 发送」，并指定目标：
python -X utf8 test_live_chain.py --live --target "某会话" --send
```

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
| **最多稳定服务 7~8 个会话** | 会话列表是虚拟列表，折叠线以下的会话**不在 UIA 树里**（`ScrollPattern` 也不支持）→ 「发现」看不到它们。这是**可见性**上限，不是性能上限 |
| **非文本未读默认不回** | `chat.nontext_policy = "skip"` 时，对方只发了个表情会让总读取拿到 0 条可读消息 → 撤单，表现为「有红点却一声不吭」。想回就改 `"describe"` |
| **锁屏下只能发现、不能发送** | UIA 读取（含发现扫描）在锁屏时照常可用，但切会话是**坐标点击**，锁屏必失败。`run_forever` 启动时会检测并 WARN |
| 会抢前台 | 切会话与发送都会把 QQ 激活到最上层；`uia.restore_foreground=true` 时用完会还给你原来的窗口 |
| 剪贴板被覆盖 | 输入靠剪贴板粘贴，你复制的内容会被替换 |
| 需要前台权限 | 从终端启动才有；抢不到会明确报错并放弃，不会乱打键 |
| 视窗内约 20 条 | QQ 只渲染可视区域附近的消息，更早的要滚动才出现（`discovery.read_limit` 默认 30） |
| 运行期间别操作 QQ | 会互相抢焦点干扰；你手动切会话会被第三道闸检出并重排 |
| **窗口不能最小化** | `Invoke` 与坐标点击在最小化时都会**静默失效**（Chromium 节流 renderer）。注意：**读取不受影响** —— 最小化后节点数/会话数/消息数完全一致，见下 |
| QQ 升级可能变结构 | 重跑 `probe.py` 对比 `probe-tree.txt` |
| 同名联系人切换 | 会话列表项没有可用 ID，稳定主键只能取显示名（备注名）；昵称重名时靠备注消歧 |
| `state/rotation.json` 丢了会怎样 | 重启后所有会话重新「首见」→ 只会记快照、不动作，重启瞬间的未读会被吞掉 |

### 前台依赖到底压在哪一步（2026-09-11 实测）

以前以为"整个方案都要抢前台"，实测逐条打表之后发现**不是**：

| 操作 | 机制 | 需要前台？ | 需要窗口非最小化？ | 抢焦点？ |
| --- | --- | --- | --- | --- |
| 扫会话列表 / 读消息 / 读签名 | UIA 属性读取 | ❌ | **❌**（最小化照样读） | ❌ |
| **切到目标会话** | **`InvokePattern.Invoke()`** | **❌** | ✅ | ✅（Chromium 内部 SetFocus） |
| 切到目标会话（兜底） | `item.Click()` 坐标点击 | ✅ | ✅ | ✅ |
| 清空输入框 / 写文本 | `SendKeys(Ctrl+A/Ctrl+V)` | ✅ | ✅ | ✅ |
| 点「发送」 | `InvokePattern.Invoke()` | ❌ | — | ❌ |

三个关键结论：

1. **读取完全不依赖前台，也不依赖窗口可见**。`probe_minimized.py` 实测：最小化后
   控件树 417 节点、7 个会话、9 条消息、输入框和发送钮的定位**全部不变**。
   （README 以前写的"最小化会让无障碍树失效"是**错的**，已更正。）

2. **切会话不需要前台**。会话列表项支持 `InvokePattern`，在 QQ 完全不是前台时
   `Invoke()` 照样能把会话切过去（`probe_switch_nofg.py` 实测）。
   所以现在的实现是 **Invoke 优先、坐标点击兜底**（`qqid.switch_session`）。

3. **但"免前台"≠"不抢焦点"**。`Invoke` 之后 QQ 会被激活顶到最上层 —— 这是 Chromium
   内部 `SetFocus` 的副作用。既然这个激活是我们引起的、而且我们并不需要它，
   就在切换完成后**显式归还前台**（`agent._return_focus_after_switch`）。

   归还这一步有个坑：`restore_foreground` 曾经是**另一份**实现，它 `AttachThreadInput`
   到**目标窗口**的线程，对 `Invoke` 造成的激活**连等 4 秒都还不回去**；
   而 `force_foreground`（附加到**当前前台窗口**的线程）一次就成功。
   原因是「前台归属权」握在当前前台窗口的那个线程手里。两份实现已合并成一个。

**所以真正的前台需求只剩「写文本」这一步**（剪贴板 + `SendKeys`）。
要彻底消掉它只有一条路：CDP 的 `Input.insertText`（见 §7 待验证实验）。
