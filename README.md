

# Sealdice AIChat

SealDice AIChat 是一个为 SealDice 骰子机器人设计的 AI 聊天扩展模块。它将大语言模型的能力与 TRPG（桌上角色扮演游戏）玩法相结合，为跑团玩家提供智能对话交互体验。

## 功能特性

- **AI 对话集成**：接入大语言模型 API，为跑团场景提供智能 NPC 对话
- **上下文记忆**：保持对话上下文，增强游戏沉浸感
- **骰子联动**：支持在 AI 对话中触发骰子掷骰功能
- **灵活配置**：可根据需求配置不同的 AI 模型和参数

## 使用前提

- 已安装并运行 SealDice 主程序
- 具备有效的 AI 模型 API Key（如 OpenAI、Claude 等）
- 了解基本的命令行操作

## 安装说明

从项目 Release 页面获取对应版本的插件文件，将其放置到 SealDice 的插件目录下，并在配置文件中启用本插件。

## 配置方法

1. 在 SealDice 配置文件中添加 AIChat 相关配置项
2. 设置 API 地址和密钥
3. 根据需要调整模型参数（温度、最大令牌数等）
4. 重启 SealDice 服务使配置生效

## 相关链接

- [SealDice 主项目](https://gitee.com/psychclaritas/sealdice)
- 问题反馈：请在项目 Issues 页面提交

## 开源协议

本项目遵循开源协议，详情请查看 LICENSE 文件。