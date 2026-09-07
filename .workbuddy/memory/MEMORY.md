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
