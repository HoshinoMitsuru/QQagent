# 代码评审：小清澈2.2.js  ⇄  qqai_direct.js

- 评审对象
  - A：`D:\Psyche\Sealdice-AIChat\小清澈2.2.js`（v2.2.0，mtime 2026-08-29，较新）
  - B：`D:\Psyche\aichataligera\ai-web-page\wechat_et_qq_client\qq_client\qqai_direct.js`（v2.2.0，mtime 2026-08-12）
- 结论一句话：**两者是同一版本的两个分支，不是前后迭代**。B 修掉了 A 的 3 个逻辑 bug，但引入了 1 个更严重的回归（识图/读网页必失败），并且人格提示词停在了旧版。

---

## 一、差异总表

| # | 维度 | A 小清澈2.2.js | B qqai_direct.js | 判定 |
|---|---|---|---|---|
| 1 | `EXT_NAME` | `AiChat` | `Mitsuru-AiChat` | 行为差异，见 P1 |
| 2 | Storage 键前缀 | `AiChat:history:` 等 4 个 | `Mitsuru-AiChat:history:` 等 4 个 | 历史/人格/自动回复开关**互不互通** |
| 3 | fetch 超时保护 | 无，裸 `fetch` | 新增 `fetchWithTimeout()` | 方向正确，实现有坑 |
| 4 | `callAi` 超时 | 无 | `30000ms` | ✅ 正确 |
| 5 | `imageToText` 超时 | 无 | **漏传第三参** | 🔴 回归，见 B1 |
| 6 | `fetchUrlText` 超时 | 无 | **漏传第三参** | 🔴 回归，见 B1 |
| 7 | `.ai off` 语义 | 被 `stop` 分支吞掉，群聊关不掉陪伴 | 独立分支，私聊提示 / 群聊真关 | ✅ B 修复了 A 的 bug |
| 8 | @ 判定正则 | `/^(…小清澈)\b/` | `/^(…小清澈)$/` | ✅ B 修复了 A 的 bug |
| 9 | 连续对话超时传参 | 把 boolean 当秒数传入 | 正确短路判断 | ✅ B 修复了 A 的 bug |
| 10 | 默认 systemPrompt / 4 个预设人格 | 新版：去掉"海豹骰子助手"，新增"只返回角色扮演所说的话，不要提供思维链、草稿内容" | 旧版：保留"你是海豹骰子的群聊助手。" | ⚠️ B 落后于 A，见 P2 |
| 11 | 交互文案（`.ai on/off`） | "Ciallo～(∠・ω< )⌒★…" / "向你露出朦胧的微笑" | "私聊里小清澈始终陪伴着你…" / "揉了揉惺忪的睡眼，起身" | 风格差异，无功能影响 |

---

## 二、🔴 Blocker（必须修）

### B1 — `qqai_direct.js:479` 与 `:584`：视觉/网页请求 100% 超时失败

```js
// qqai_direct.js:40
function fetchWithTimeout(url, options, timeoutMs) {
  const timer = setTimeout(() => reject(new Error(...)), timeoutMs);  // timeoutMs === undefined
  ...
}

// qqai_direct.js:479  ← 只传了 2 个参数
var response = await fetchWithTimeout(visionUrl, { method: "POST", ... });
// qqai_direct.js:584  ← 同样只传 2 个参数
const response = await fetchWithTimeout(url, { method: "GET", ... });
```

**Why：** `timeoutMs` 为 `undefined`，`setTimeout(fn, undefined)` 等价于 0ms 定时器，会在当前 tick 之后的第一个宏任务立刻触发；而 `fetch` 是网络 IO，必然晚于它。因此 Promise 先被 `reject`，`imageToText` / `fetchUrlText` 的 `catch` 吞掉错误后 `return ""`。
后果：
- 所有图片消息被替换成 `"[图片: 识别失败]"`（`:544`）；
- 私聊里 `sw.image` 被强制为 `true`，所以**私聊发图必然识别失败**；
- `.ai img` 走 `:730` 的 `then`，永远落到"图片识别失败"分支；
- URL 读取永远不生效，`urlReadingEnabled` 形同虚设。

**Suggestion：** 补默认超时，别让调用方有机会漏传：

```js
function fetchWithTimeout(url, options, timeoutMs) {
  const ms = Number(timeoutMs) > 0 ? Number(timeoutMs) : 30000;   // 兜底
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('请求超时(' + ms + 'ms)')), ms);
    fetch(url, options).then(resolve, reject).finally(() => clearTimeout(timer));
  });
}
```
再在 `:479` / `:584` 显式传入（视觉建议 60000，网页 15000）。

---

### B2 — `小清澈2.2.js:665` + `:681`：群聊 `.ai off` 是死代码，关不掉自动陪伴

```js
// 2.2.js:665
if (first === "stop" || first === "end" || first === "off") {   // ← off 在这里被吃掉
  stopContinuous(ext, ctx, msg);
  return seal.ext.newCmdExecuteResult(true);                     // ← 直接 return
}
...
// 2.2.js:681  ← 永远执行不到
if (first === "off" && msg.messageType === "group") { saveAutoReply(ext, scope, false); ... }
```

**Why：** 一旦 master 在群里 `.ai on` 开启陪伴，就**没有任何指令能关掉它**，`resolveSwitches` 里 `loadAutoReply` 恒为 `true`，机器人会一直自动回话。这是运维侧不可控。

**Suggestion：** 直接采用 B 的写法（`:674` / `:690` 两个独立分支），或至少把 `off` 从 `stop` 分支里摘出来。

---

### B3 — `小清澈2.2.js:877`：连续对话窗口被压缩成 1 秒

```js
const isContinuous = isContinuousActive(
  ext, scope, now,
  sw.continuous ? sw.continuous : c.continuousConversationTimeoutSeconds
);  // sw.continuous 是 boolean true → timeoutSeconds = true → true*1000 = 1000ms
```

**Why：** `isContinuousActive` 内部是 `elapsed <= timeoutSeconds * 1000`。传 `true` 进去等于把 1800 秒的窗口压成 **1 秒**。防抖基础等待就是 10 秒，所以 `isContinuous` 恒为 `false`：连续对话基本立刻失效，群聊里每条消息都得重新用触发词唤醒，私聊的"沉浸式"名存实亡。

**Suggestion：** 采用 B 的写法（`:891`）：
```js
const isContinuous = sw.continuous
  ? isContinuousActive(ext, scope, now, c.continuousConversationTimeoutSeconds)
  : false;
```

---

## 三、🟡 Suggestion（应该修）

### P1 — 两个插件同名指令冲突 + 数据双份

`EXT_NAME` 不同 → `seal.ext.find(EXT_NAME)` 都能通过 → 两个插件会**同时加载**，但都注册 `ext.cmdMap["ai"]` / `["aichat"]`（A `:746`、B `:759`），后注册者覆盖前者，实际只有一个生效，而 `.ai` 的 storage 键又分属两套前缀，切换/删除插件会导致历史记录"凭空消失"。

**Suggestion：** 明确二选一。若要并存，把指令名也区分开（如 `ai` / `aidirect`）；若只留一个，统一 `EXT_NAME` 与 storage 前缀。

### P2 — B 的人格提示词落后于 A

A 的 4 个预设人格与默认 `systemPrompt` 都换成了"去掉工具人身份 + 明确禁止输出思维链/草稿"的版本；B 仍是带"你是海豹骰子的群聊助手。"的旧版。在 DeepSeek-R1 类推理模型上，缺少"不要提供思维链"这句，很容易出现模型把 `<think>` 内容直接吐到群里。

**Suggestion：** 把 A 的 `DEFAULT_PERSONAS` 与两处 `systemPrompt`（cfg 兜底 + `registerStringConfig` 默认值）整体同步到 B。

### P3 — 两版共有：`autoReplyEnabled` 配置项是死配置

两个文件都 `registerBoolConfig(ext, "autoReplyEnabled", true)`，但 `resolveSwitches` 从不读它——私聊硬编码 `true`，群聊只看 `loadAutoReply`。改这个开关没有任何效果，属于误导。

**Suggestion：** 要么在群聊分支里纳入（`ar === null ? c.autoReplyEnabled : ar`），要么从配置面板移除。

### P4 — 两版共有：`stripPrefix()` 定义后从未使用

A `:274`、B `:282`，死代码，删掉即可。

### P5 — 两版共有：`.ai img` 在私聊下自相矛盾

A `:704` / B `:717`：`if (!sw.image || !imgConfig.imageRecognitionEnabled)`。私聊 `resolveSwitches` 强制 `sw.image = true`，但 `imageRecognitionEnabled` 全局默认 `false`，于是私聊用 `.ai img` 仍会收到"图片识别功能未开启"——与"私聊强制全开"的设计冲突。

**Suggestion：** 私聊场景只判 `sw.image`；全局配置只约束群聊。

---

## 四、💭 Nit

- B 的 `@author` 标了"功能整合/直连移植：WorkBuddy"，A 没有——若 B 是要合并回主线的分支，署名建议在合并时统一。
- A 的文案更"人设化"（Ciallo），B 更"说明性"。合并时挑一套，别让用户在不同群看到两种语气。

---

## 五、建议的合并方向

以 **A（小清澈2.2.js）为准**（人格提示词最新），把 B 的 4 处修复搬过去，并顺手补上 B1 的超时兜底：

| 动作 | 来源 | 落到 A 的位置 |
|---|---|---|
| `.ai off` 独立分支 + 私聊提示 | B `:690-699` | 替换 A `:665-686` |
| @ 判定 `$` 锚定 | B `:811` | A `:797` |
| 连续对话超时短路写法 | B `:891` | A `:877` |
| `fetchWithTimeout` + 三处调用 | B `:40`、`:333`、`:479`、`:584` | 新增到 A，并**务必给 479/584 补超时参数** |
| （可选）P3/P4/P5 清理 | — | A `:274`、`:704`、`:956` |

---

## 六、合并产物：`小清澈2.3.js`（v2.3.0）

按"功能取完善版、文案与提示词取 2.2"执行，已通过 `node --check` 语法校验，原两个文件均未改动。

| 项 | 取值来源 | 说明 |
|---|---|---|
| 人格提示词（4 个预设 + 默认 `systemPrompt` ×2 处） | **2.2** | 含"不要提供思维链、草稿内容"，去掉"海豹骰子助手"身份 |
| 对话文案（`.ai on` / `.ai off` / `reset` / `stop` / `persona` / `list`） | **2.2** | 私聊 `.ai off` 是新分支，补了一句同风格文案 |
| `EXT_NAME` / storage 前缀 | **2.2** = `AiChat` | 保持与现有历史记录、人格、群开关兼容 |
| `fetchWithTimeout` | qqai_direct | **并修正了它漏传 `timeoutMs` 的回归** |
| `.ai off` 独立分支 | qqai_direct | 修复群聊关不掉陪伴 |
| @ 判定 `$` 锚定 | qqai_direct | 修复 `\b` 对中文失效 |
| 连续对话布尔短路 | qqai_direct | 修复 1 秒窗口 |
| `.ai img` 私聊判定 | 新增 | 私聊只判 `sw.image`，不再被全局配置挡住 |
| `stripPrefix` | 删除 | 死代码 |

> 后续调整：`autoReplyEnabled` 已彻底移除，改为「群聊恒应答 + `.ai on/off` 只切换是否需要触发词」，详见下一节。

### 超时参数分配

| 调用点 | 超时 | 理由 |
|---|---|---|
| `callAi` | 30s | LLM 常规往返 |
| `imageToText` | 60s | 视觉模型要下载并编码图片，明显更慢 |
| `fetchUrlText` | 15s | 第三方网页，失败要尽快放弃，否则拖慢防抖 |

`fetchWithTimeout` 内部另做 `Number(timeoutMs) > 0 ? … : 30000` 兜底，杜绝今后再出现"漏传参数 = 立即超时"。

### 部署提示

- 直接把 `小清澈2.3.js` 放进海豹的插件目录即可；由于 `EXT_NAME = "AiChat"` 与 2.2 一致，历史记录 / 人格会原样继承。
- 若骰子此前装过 `Mitsuru-AiChat`（qqai_direct）那一版，建议**先卸载再装 2.3**，避免两个插件同时注册 `cmdMap["ai"]` 互相覆盖。

---

## 七、触发词机制调整（v2.3.0 追加）

按新需求把 `.ai on/off` 的语义从「是否回应」改为「是否需要触发词」。

| 场景 | `.ai on` | `.ai off` | 默认（未设置） |
|---|---|---|---|
| 群聊 | 免触发词，直接对话 | 启用触发词机制，须以 `keywordPrefix` 开头 | **需要触发词** |
| 私聊 | 恒免触发词（提示"我一直都在"） | 恒免触发词（提示"这里不用喊我的名字"） | 恒免触发词 |

### 图片白名单

群聊处于触发词模式时，图片**不再无条件豁免**，改为白名单准入：只有本轮对话中"命中过触发词 / @了小清澈 / 连续对话进行中"的用户所发的图片才会被识别；其余用户的图片在**入队前**就被 `stripImages()` 剔除，不消耗视觉 API 调用。

准入判定发生在消息入队时（而非结算时），这一点很关键——否则未获准的图照样会调一次视觉接口，白烧 token 后才被丢弃。

**Why 这样改：** 原来 `.ai off` 是让机器人彻底闭嘴，但实际使用里更需要的是「别插话、但喊我就应」——触发词模式正好覆盖；而"完全不应答"会让群里的人以为机器人坏了。现在群聊一律应答，on/off 只决定要不要喊名字，同时也可以兼作降噪手段。

### 具体改动

| 位置 | 改动 |
|---|---|
| `AUTOREPLY_PREFIX` → `FREETALK_PREFIX` | 存储键改为 `AiChat:freeTalk:`，语义更准确 |
| `loadAutoReply/saveAutoReply` → `loadFreeTalk/saveFreeTalk` | 同上 |
| `resolveSwitches` | 移除 `autoReply` 字段，新增 `needPrefix`；群聊 `needPrefix = free === null ? false : !free` |
| `onNotCommandReceived` | 删除 `if (!sw.autoReply) return;`，群聊不再静音 |
| 防抖定时器 `:924` | `hasPrefix = !sw.needPrefix \|\| !sw.prefix \|\| startsWithPrefix(...)`，免触发词场景直接放行 |
| `atString` `:949` | 改为 `groupId ? \`@${...} \` : ""`，私聊不再拼接 @ |
| `.ai on/off` 分支 | 改写为存取 `freeTalk`，文案沿用 2.2 并补一句模式说明 |
| `cmdAi.help` / 头部 `@description` | 同步更新指令说明 |
| `autoReplyEnabled` | 配置注册与读取一并删除 |

### 验证

`test/plugin-smoke-test.js` 用 vm 沙箱桩掉 `seal` / `fetch` / 定时器后驱动插件，覆盖 5 个场景：

| 场景 | 期望 | 实测 |
|---|---|---|
| 1 群聊默认，A 带触发词发图 + B 无触发词发图 | 仅 A 的图被识别，B 的图被剔除 | 视觉调用 1 次，A=识别 / B=剔除 |
| 2 群聊默认，仅无触发词用户发图 | 整轮拦截，不调视觉、不发 AI 请求 | 视觉 0 次，无 AI 请求 |
| 3 `.ai on` 后任意用户发图 | 接受 | 视觉 1 次，识别成功 |
| 4 `.ai off` 后无触发词用户发图 | 拦截 | 视觉 0 次，无 AI 请求 |
| 5 连续对话进行中发图 | 接受（无需再喊名字） | 视觉 1 次，识别成功 |

运行：`node test/plugin-smoke-test.js`
