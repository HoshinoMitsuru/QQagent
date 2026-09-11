# 小清澈 2.3 与 qqai_server / qqai_direct 对比评审

评审日期：2026-09-07
被评对象：

| 文件 | 版本 | EXT_NAME | 架构 | 指令 | @timestamp |
|------|------|----------|------|------|-----------|
| `小清澈2.3.js` | **2.3.0** | `AiChat` | 直连 LLM API | `.ai` / `.aichat` | 2026-09-07 |
| `qqai_direct.js` | 2.2.0 | `Mitsuru-AiChat` | 直连 LLM API | `.ai` / `.aichat` | 2026-07-28 |
| `qqai_server.js` | 3.2.0 | `Mitsuru-AiChat-Server` | 连自建后端 `/api/qq/message` | `.ai2` / `.aichat2` | **2026-07-19** |

## 结论速览

1. **`qqai_direct` 是 2.3 的「前一代基线」**，2.3 是其**功能超集**——2.3 已吸收 direct 的全部修复，并修掉了 direct 自身的一个致命回归。direct 版无独有价值，可归档。
2. **`qqai_server` 是「并行分支」而非「更高版本」**。它版本号 3.2.0 数字最大，但时间戳（07-19）比 direct（07-28）和 2.3（09-07）都**更早**，因此**缺失 2.2/2.3 时代的绝大部分修复与新特性**，且自身带有 2 个致命 bug。
3. **server 版有 2 个必须立即修的 bug**（识图 100% 失效、连续对话关不掉），详见 §3.1。

---

## 1. 三者关系

```
                 ┌── 2.2.0 主分支（小清澈2.2.js，人格文案新，3 个 bug）
                 │
  共同祖先 ──────┤
                 │
                 ├── Mitsuru 直连分支（qqai_direct.js 2.2.0）
                 │      └─ 修了主分支 3 bug，但引入「漏传 timeoutMs」回归
                 │
                 └── 2.3.0（小清澈2.3.js）
                        = 2.2 主分支人格文案
                        + direct 的 4 项修复
                        + 修掉 direct 的 timeoutMs 回归
                        + 新增：图片白名单 / needPrefix 语义 / 私聊去 @

  另一条线（时间更早、架构不同）：
                 └── 服务端分支（qqai_server.js 3.2.0）
                        = 连 /api/qq/message + HMAC 签名
                        ── 未吸收 07-28 与 09-07 的任何改进
```

---

## 2. `qqai_direct.js`（2.2.0） vs `小清澈2.3.js`

差异规模：337 行。**2.3 = direct 的超集**，逐项如下。

### 2.1 2.3 相对 direct 的改进

| # | 项目 | direct（2.2.0） | 2.3 | 说明 |
|---|------|----------------|-----|------|
| 1 | **识图/读网页超时** | `imageToText`(L479)、`fetchUrlText`(L584) 漏传第 3 参 `timeoutMs` → `setTimeout(fn, undefined)` 退化 **0ms 立即 reject** → **100% 超时失败** | 显式传 `VISION_FETCH_TIMEOUT_MS` / `URL_FETCH_TIMEOUT_MS`，且函数内加兜底 | **direct 的致命回归，2.3 已修** |
| 2 | `fetchWithTimeout` 兜底 | 无，`timeoutMs` 非法值直接传给 `setTimeout` | `Number(timeoutMs)>0 ? … : CHAT_FETCH_TIMEOUT_MS`（2.3:67） | 防御性 |
| 3 | 超时常量化 | 仅 chat 硬编码 `30000` | 常量：`CHAT 30s / VISION 60s / URL 15s`（2.3:43-45） | 可维护 |
| 4 | **图片白名单** | 无。任何人甩图都无条件唤起（`some(m=>m.hasImages)`，L905） | buffer 增 `triggeredUsers`、`userId` 字段；`imageAllowed` 判定；未获准走 `stripImages()` 剔图（2.3:876-889、970-972） | **2.3 新增，防白烧视觉 token** |
| 5 | 群聊开关语义 | `autoReply` = **是否回应**，群聊默认**沉默**（需 `.ai on` 才答） | `needPrefix` = **是否要喊名字**，群聊**恒应答**，默认需触发词（2.3:233） | 产品语义变更 |
| 6 | 存储键/配置 | `AUTOREPLY_PREFIX`、`loadAutoReply/saveAutoReply`、`autoReplyEnabled` 注册 true | `FREETALK_PREFIX`、`loadFreeTalk/saveFreeTalk`；`autoReplyEnabled` **彻底删除**（仅留注释，2.3:1025） | 键名变更→**历史不互通** |
| 7 | 私聊 @ 前缀 | 恒拼 `@${senders[0]} `（L925） | 私聊置空 `groupId ? … : ""`（2.3:994） | 去冗余 |
| 8 | `.ai img` 门槛 | `!sw.image \|\| !imgConfig.imageRecognitionEnabled` → 私聊也被全局开关挡 | `!sw.image \|\| (!sw.isPrivate && !…)`（2.3:766）→ 私聊不受限 | 沉浸式一致性 |
| 9 | 人格提示词 | 旧版，含「你是海豹骰子的群聊助手」 | 2.2 主分支版，去掉助手身份 + 加「不要输出思维链」 | 以 2.2 为准 |
| 10 | `.ai off` 别名 | 仅 `"off"` | `"off"` + `"close"`（2.3:738） | 小改进 |
| 11 | 死代码 | 保留未使用的 `stripPrefix`(L282) | 已删除 | 清理 |

### 2.2 direct 已修、被 2.3 吸收的部分（说明 2.3 确实已合并）

- `@` 判定用 `$` 锚定 `^(Claritas-小清澈|小清澈)$`（`\b` 对 CJK 失效）—— direct 先修，2.3 沿用。
- 连续对话布尔短路 `sw.continuous ? isContinuousActive(…, 配置秒数) : false`—— direct 先修，2.3 沿用（2.3:951-953）。
- `.ai off` 独立分支（不被 `stop` 提前 return 吞掉）—— direct 先修，2.3 沿用并加注释警告（2.3:721）。

### 2.3 结论

**`qqai_direct.js` 无独有价值，建议归档。** 若骰上仍装着 `Mitsuru-AiChat`，其 `cmdMap["ai"]` 会与 2.3 的 `AiChat` **互相覆盖**，必须卸载其中一个。

---

## 3. `qqai_server.js`（3.2.0） vs `小清澈2.3.js`

差异规模：1243 行。**架构根本不同**（服务端 vs 直连），因此按「server 独有」与「server 缺失」分别列出。

### 3.1 ⚠️ server 版的 2 个致命 bug（必须修）

| # | 问题 | 位置 | 后果 | 修法 |
|---|------|------|------|------|
| **A** | **`imageToText` 漏传 `timeoutMs`** | `qqai_server.js:496-500` | `fetchWithTimeout(visionUrl, {...})` 第 3 参缺失 → `setTimeout(fn, undefined)` = **0ms 立即 reject** → **识图 100% 失败**（`.ai2 img` 与自动识图全废）。且该版 `fetchWithTimeout`(L32-37) **连兜底都没有**，比 direct 更糟 | ① 传 `VISION_FETCH_TIMEOUT_MS`；② `fetchWithTimeout` 内加 `Number(timeoutMs)>0 ? … : 30000` 兜底（照抄 2.3:66-72） |
| **B** | **连续对话缺布尔短路** | `qqai_server.js:752` | `isContinuousActive(ext, scope, now, c.…)` **未经 `sw.continuous ?` 短路** → 即使 `continuousConversationEnabled=false`，只要 storage 残留 `active:true` 仍判定激活 → **连续对话关不掉** | 改为 `sw.continuous ? isContinuousActive(…) : false`（照抄 2.3:951-953） |

### 3.2 server 版完全缺失的 2.3 特性（需同步）

| # | 2.3 特性 | server 版现状 | 影响 | 建议 |
|---|---------|--------------|------|------|
| 1 | **图片白名单** | 无 `triggeredUsers` / `imageAllowed` / `stripImages` / `userId` 字段；`hasImages = some(m=>m.hasImages)`（L761） | 群聊触发词模式下，没喊名字的人甩图也能唤起 → 白烧视觉 API | 高优先同步（照抄 2.3:462-477、687-701、874-889、967-973） |
| 2 | **连续对话冷却续期** `updateContinuousCooldown` | 无（2.3:190-195） | 固定 1800s 窗口，聊到一半可能被强制断开 | 建议同步 |
| 3 | **私聊去 @ 前缀** | `atString` 恒拼 `@${senders[0]} `（L781） | 私聊回复带冗余 `@昵称 ` | 改为 `groupId ? … : ""` |
| 4 | `preprocessMessage` 统一预处理 | `if (sw.image) {...} else {...}` 两个分支重复推 buffer（L662-690） | 代码重复、后续加 URL 处理会更乱 | 建议重构为 2.3 的单入口 |
| 5 | 死代码 `stripPrefix` | 保留未使用（L697-699） | 无害但冗余 | 删除 |

### 3.3 server 版与 2.3 的语义分歧（需你决策，非 bug）

| # | 项目 | server 版 | 2.3 | 决策点 |
|---|------|----------|-----|--------|
| 1 | **群聊 on/off 语义** | `autoReply`=**是否回应**；群聊默认**完全沉默**（`if (!sw.autoReply) return;` L608），需 `.ai2 on` 才应答 | `needPrefix`=**是否喊名字**；群聊**恒应答**，默认需喊「小清澈，」 | 装完默认沉默 vs 默认要喊名字才答，要统一成哪种？ |
| 2 | `autoReplyEnabled` 配置 | 注册默认 `false`（L890）且**确实被读取**（L197） | 已彻底删除 | 若统一到 2.3 语义，server 版也应删 |
| 3 | **人格切换** | 无 `.ai2 persona` / `.ai2 list`（人格在服务端/小程序设定，QQ 端只读） | 有 `.ai persona` / `.ai list`（4 个预设人格） | 架构取舍：QQ 端要不要保留本地人格切换？ |
| 4 | `imageRecognitionEnabled` 默认 | **true**（L895） | **false**（2.3:1029） | 默认值不一致，建议统一 |
| 5 | `historyTurns` 默认值 | 注册 **10**（L887）但 `cfg()` 兜底 **6**（L138）——**自相矛盾** | 统一 10 | server 版应统一为 10 |
| 6 | URL 读取 | 由**服务端**解析（L5/L200 `url: true`），客户端无 `extractUrls`/`fetchUrlText` | 客户端本地抓取 | 需确认后端 `/api/qq/message` 是否真的做了 URL 解析 |

### 3.4 server 版独有（2.3 没有，架构使然）

| 项目 | 位置 | 说明 |
|------|------|------|
| 纯 JS HMAC-SHA256 | L43-128（`_utf8Bytes`/`_sha256_bytes`/`hmacSha256`） | 不依赖运行环境 crypto，任意沙箱可跑 |
| 服务端签名 `X-QQ-Signature` | L313-317 | `HMAC-SHA256(secret, rawBody)`，对齐后端 `verify_qq_signature` |
| 后门令牌 `X-Backdoor-Token` | L318-322 | 绕过演示门禁，配置默认已填 `oVTHY…H64` |
| `callServer()` 调 `/api/qq/message` | L284-351 | 带 `qq_number`/`group_id`/`scope`/`nickname` → **与微信小程序共享上下文** |
| 「思考中…」占位回复 | L308 | 服务端慢时给用户即时反馈 |
| 指令名 `ai2`/`aichat2` | L801/L871-872 | 避免与直连版 `cmdMap["ai"]` 冲突 |

---

## 4. 关键 bug 清单（按优先级）

| 优先级 | 文件:行 | 问题 | 影响 |
|--------|---------|------|------|
| **P0** | `qqai_server.js:496` | `imageToText` 漏传 `timeoutMs` | 识图 100% 失效 |
| **P0** | `qqai_server.js:32` | `fetchWithTimeout` 无兜底 | 任何漏传都必超时 |
| **P1** | `qqai_server.js:752` | 连续对话缺布尔短路 | 连续对话关不掉 |
| **P1** | `qqai_server.js:761` | 图片无白名单 | 白烧视觉 token |
| **P2** | `qqai_server.js:781` | 私聊恒拼 @ | 体验噪声 |
| **P2** | `qqai_server.js:887` vs `:138` | `historyTurns` 注册 10 / 兜底 6 | 配置自相矛盾 |
| **P2** | `qqai_server.js:697` | `stripPrefix` 死代码 | 冗余 |
| — | `qqai_direct.js:479/584` | 同上「漏传 timeoutMs」 | 已由 2.3 修复，direct 可归档 |

---

## 5. 建议

### 5.1 直连版（已收敛）
- **`小清澈2.3.js` 作为唯一主线**，`qqai_direct.js` 归档不再维护。
- 部署前确认骰上已卸载 `Mitsuru-AiChat`（旧 direct 版），否则 `cmdMap["ai"]` 互相覆盖。

### 5.2 服务端版（需决策）
`qqai_server.js` 建议你明确它的定位，两个方向二选一：

- **方向 A：继续作为「跨端共享上下文」的主力**（推荐，如果 QQ 与小程序共享记忆是你的核心需求）
  → 需要把 §3.1 的 2 个 P0/P1 bug 修掉，并同步 §3.2 的图片白名单 + 冷却续期，对齐 §3.3 的语义。工作量中等。
- **方向 B：冻结/归档**
  → 若跨端共享暂时用不上，2.3 直连版功能更全、bug 更少，server 版可归档，避免两条线持续分叉。

### 5.3 版本号建议
当前「3.2.0（server，07-19）> 2.3.0（直连，09-07）」的编号**与新旧程度相反，容易误导**。建议 server 版改为与直连版对齐的语义（如 `2.3.0-server`），或在文件头注明「本分支时间戳早于 2.3，未吸收其改进」。

---

## 6. 修复记录（方向 A：以 2.3 为基线，修 server 版并对齐）

目标文件：`D:/Psyche/aichataligera/ai-web-page/wechat_et_qq_client/qq_client/qqai_server.js`（v3.2.0 → v3.3.0）

| # | 级别 | 问题（原 file:line） | 修复 |
|---|------|----------------------|------|
| 1 | P0 | `fetchWithTimeout` 无兜底（原 L32-37） | 加 `const ms = Number(timeoutMs) > 0 ? Number(timeoutMs) : CHAT_FETCH_TIMEOUT_MS`；新增 `CHAT/VISION/URL_FETCH_TIMEOUT_MS` = 30000/60000/15000 |
| 2 | P0 | `imageToText` 调 `fetchWithTimeout(visionUrl,{...})` 漏传第 3 参（原 L496）→ 识图 100% 失效 | 补 `VISION_FETCH_TIMEOUT_MS`；视觉 URL 走 `normalizeApiUrl` |
| 3 | P2 | `callServer` 硬编码 30000（原 L327） | 改用 `CHAT_FETCH_TIMEOUT_MS` |
| 4 | P1 | 定时器 `isContinuousActive(ext, scope, now, c.continuousConversationTimeoutSeconds)` 缺 `sw.continuous ?` 短路（原 L752）→ 连续对话开关关闭时失效 | 补 `sw.continuous ? ... : false`；新增 `updateContinuousCooldown` 并在结算时续期 |
| 5 | P1 | 群聊开关语义与 2.3 不一致（`autoReply`=是否应答） | 改为 `needPrefix`（存储 `Mitsuru-AiChat:freeTalk:`，默认需触发词），群聊恒应答、由触发词过滤 |
| 6 | P1 | 无图片白名单，任何人甩图都烧视觉 API | 新增 `triggeredUsers` + `stripImages()` + `imageAllowed`，消息附带 `userId`，统计图片时按白名单过滤 |
| 7 | P2 | 图片/URL 预处理分散在两处 if/else | 合并为 `preprocessMessage(text, sw, c, imageAllowed)` 单入口 |
| 8 | P2 | 私聊回复也拼 `@昵称` | 群聊才拼 `@`，私聊置空 |
| 9 | P2 | 死代码 `stripPrefix`、`autoReplyEnabled` | 删除（含 `registerBoolConfig`） |
| 10 | P2 | `historyTurns` 注册 10 / 兜底 6 矛盾 | 兜底统一 10 |
| 11 | P2 | `imageRecognitionEnabled` 默认 true | 对齐 2.3 改 false |

### 校验
- `node --check qqai_server.js` → 通过
- mock Sealdice 沙箱实跑 4 场景：
  1. 群聊无触发词 → 拦截，0 次上游请求 ✅
  2. 群聊带「小清澈，」→ 放行，调 `/api/qq/message` ✅
  3. 私聊 → 免触发词，直接调 `/api/qq/message` ✅
  4. 连续对话两轮（第 2 轮无触发词仍被应答）→ PASS ✅（修复前此项必失败）
- `setTimeout(fn, undefined)` 实测 5ms 即触发，佐证 #2 的严重性

### 迁移注意
旧群若执行过 `.ai2 on/off`，状态存在旧 key `Mitsuru-AiChat:autoReply:*`；语义变更后在群里重新执行一次 `.ai2 on` 才会免触发词。
