# 小清澈.js 项目概览

## 当前版本

| 文件 | 版本 | 说明 |
|------|------|------|
| `小清澈.js` | v1.7.0 | 通用版，图片识别需手动开启 |
| `小清澈2.0.js` | v2.0.0 | **QQ陪伴特化版**，对话与识图全部默认开启 |

两个版本可独立安装，不冲突（EXT_NAME 不同）。

---

## v2.0.0 QQ陪伴特化版 — 概览

### 与 v1.7.0 的差异

| 项目 | v1.7.0 | v2.0.0 |
|------|--------|--------|
| EXT_NAME | `Mitsuru-AiChat` | `Mitsuru-AiChat-Companion` |
| imageRecognitionEnabled | `false` | **`true`** |
| continuousConversationTimeoutSeconds | `600` (10分钟) | **`1800`** (30分钟) |
| autoReplyEnabled | `true` | `true`（无变化） |
| continuousConversationEnabled | `true` | `true`（无变化） |

### 设计理念

v2.0.0 是面向 QQ 群聊/私聊 AI 陪伴场景的特化版本：
- **开箱即用**：安装后无需任何配置，直接 @ 小清澈或使用 `.ai` 指令即可开始对话
- **识图默认开启**：群友发的图片自动识别，融入对话上下文
- **更长陪伴时长**：连续对话超时从 10 分钟延长到 30 分钟，减少断联
- **独立安装**：使用不同 EXT_NAME，可与 v1.7.0 共存

### 仍需手动配置

- `apiUrl` / `apiKey` — API 地址和密钥（必须）
- `visionApiUrl` / `visionApiKey` / `visionModel` — 视觉 API（留空则复用聊天 API）
