# qq-cu CLI 用法（F6 · Agent 可调用接口）

> 定位（2026-09-25 拍板）：调用方是 **Agent 应用**（Codex / Claude Code / WorkBuddy），
> 不是人。CU 没有操作面板，只有可被 agent 稳定调用的命令行接口。

## 两种调用形态

| 形态 | 命令 | 适合 |
| --- | --- | --- |
| 委托式（内置 Brain） | `qq-cu.py run --task "..."` | 一步下发整个任务，CU 自己走工具循环 |
| 工具式（调用方编排） | `qq-cu.py health / sessions / open / read / shot / send` | 调用方 agent 自己决定每一步 |

## 输出契约

- stdout 输出 **JSON 信封**（缺省即是；`--human` 切人话）：
  - 成功 `{"ok": true, "tool": "...", "result"/"answer": ...}`
  - 失败 `{"ok": false, "error": {"code", "detail", "ctx"}}`
  - `run` 额外带 `"usage_steps"` 与 `"steps"`（完整 trace）
- 退出码：**0** 成功 / **1** 动作失败（细节看 JSON）/ **2** 配置参数错
- `--json` / `--human` 放在子命令前后都可以（`qq-cu health --json` ✅）

## 命令示例

```powershell
# 委托式：一步完成任务（hosted 全自主发送）
python qq-cu.py run --task "给「Psyche-嗅尘紫蝶」发一句『在吗』" --json

# 工具式：调用方 agent 自己编排
python qq-cu.py health --json
python qq-cu.py sessions --json
python qq-cu.py open --name "Psyche-嗅尘紫蝶" --json
python qq-cu.py read --limit 10 --json
python qq-cu.py send --text "你好" --json
```

## 安全模型（不随调用方变化）

| 执行面 | 发送策略 |
| --- | --- |
| `--mode hosted`（默认） | 独立桌面小号，**全自主发送**（用户已授权） |
| `--mode attach` | 附着主号，**永远 fail-closed**：非交互终端直接拒绝（E-CU-004 信封），交互终端逐条 y/n。**不提供预授权通道** |

- hosted 面有目标闸（`open_chat_allow`，测试期仅「Psyche-嗅尘紫蝶」「我，我们」）
- `run --mode attach` 在 agent shell（非交互 stdin）里被确认回调拒绝 → 任务失败而不是误发

## 前置条件

- `config.json` 里有 DeepSeek Key（`run` 需要；六原语只读不需要 Key）
- hosted：小号 QQ 已在隐藏桌面登录（`python host_setup.py start` / `status`）
- attach：本机 QQ 已登录主号且窗口未收进托盘

## 相关工具

| 工具 | 用途 |
| --- | --- |
| `host_setup.py` | hosted 小号的 start / qr / status / stop |
| `test_live_brain.py` | 真机联调 harness（attach 逐条确认、trace 打印更详细） |
| `qq-cu.py` | 本文：正式 CLI 入口（F7 打包 exe 的目标形态） |
