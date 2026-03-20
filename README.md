# AstrBot Plugin: MaiBot Agent Runner

将 [MaiBot](https://github.com/MaiM-with-u/MaiBot) 作为 AstrBot 的 Agent 执行器，通过 WebSocket 连接 MaiBot API-Server，用 MaiBot 的思维链和回复逻辑替代 AstrBot 内置的 LLM Agent。

## 功能

- 🔌 **Provider 注册** — 启动时自动向 AstrBot 注册 `maibot` 类型的 Provider，WebUI 可直接选择
- 🔗 **持久 WebSocket 连接** — 与 MaiBot API-Server 保持长连接，支持心跳保活和自动重连
- 🛠️ **双向工具注入** — 将 AstrBot 的 LLM 工具同步到 MaiBot，MaiBot 可远程调用 AstrBot 侧的工具
- 📨 **maim_message 协议** — 完整实现 `sys_std` 消息信封协议，支持文本 / 图片 / 表情等消息段

## 配置

在 AstrBot WebUI 中：

1. 进入 **提供商管理** → **添加提供商** → 选择 **MaiBot**
2. 填写以下配置项：

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| `maibot_ws_url` | MaiBot API-Server 的 WebSocket 地址 | `ws://127.0.0.1:18040/ws` |
| `maibot_api_key` | API Key（需在 MaiBot 配置的 `api_server_allowed_api_keys` 中添加） | — |
| `maibot_platform` | 发送给 MaiBot 的平台标识 | `astrbot` |
| `maibot_bot_qq` | Bot 的 QQ 号（用于 MaiBot 识别发送者） | — |
| `maibot_bot_nickname` | Bot 的昵称 | `AstrBot` |

3. 进入 **AI 设置** → **模型提供商** → 选择 `MaiBot` 类型的提供商

## 前置依赖

- AstrBot `>= 4.0.0`（需要新版 Provider 注册 API）
- MaiBot 需开启 API Server（`enable_api_server: true`）
- MaiBot 侧需安装 [maibot_astrbot_bridge_plugin](https://github.com/EterUltimate/maibot_astrbot_bridge_plugin) 以支持回复路由和工具桥接

## 版本变更

### v1.1.0
- **Breaking**: 完全适配 AstrBot v4.0+ 新架构
  - 移除已废弃的 `AgentRunnerEntry` / `register_agent_runner` API
  - 改用 `register_provider_adapter` 注册为 Provider
  - 修复 `BaseAgentRunner.reset()` 签名兼容性
  - 移除对 `core_lifecycle` 的直接依赖
  - 仓库地址迁移至 `EterUltimate/astrbot_plugin_maibot`

### v1.0.0
- 初始版本，使用 Agent Runner 架构

## 文件结构

```
astrbot_plugin_maibot/
├── main.py                  # 插件入口，注册 Provider + 消息透传
├── maibot_agent_runner.py   # MaiBotAgentRunner 实现
├── maibot_ws_client.py      # WebSocket 客户端（持久连接 + 工具注入协议）
├── metadata.yaml            # 插件元数据
└── README.md                # 本文件
```
