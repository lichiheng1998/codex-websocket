# codex-websocket

Hermes 插件 — 通过 WebSocket 将编码任务委派给 OpenAI Codex。

插件维护一个持久的 `codex app-server` 子进程，通过 WebSocket + JSON-RPC 2.0 通信（wire 类型由 `codex-app-server-schema` 中的 pydantic 模型约束）。任务异步运行，进度、审批请求和结果实时推送到当前聊天平台。

## 架构

```
用户 (Telegram / Discord / Slack / ...)
  ↕  hermes 消息路由
hermes 主代理 (LLM)
  ↕  codex_task / codex_revive 工具调用
codex-websocket 插件
  ├── CodexBridge (单例, 管理生命周期)
  ├── MessageHandler (处理入站帧)
  └── notify.py (推送到聊天平台)
       ↕  WebSocket (JSON-RPC 2.0)
codex app-server 子进程
       ↕
Codex 模型 (gpt-5 / 自定义模型)
```

**核心设计**：单进程 + 单连接，多任务（thread）并发运行，通知通过 hermes 消息路由推送。

## 前置条件

- Python >= 3.10
- `codex` CLI 已安装且在 PATH 中
- hermes 插件宿主环境
- Python 依赖：`websockets>=11.0`、`pydantic>=2.0`

## 安装

将 `codex-websocket/` 目录放入 hermes 的 `plugins/` 目录即可，hermes 会自动发现并加载。

```bash
# 如需手动安装 Python 依赖
pip install websockets pydantic
```

## 工具与命令

插件注册两个 LLM 工具（toolset: `codex_bridge`）和一系列斜杠命令。

### `codex_task` — 委派编码任务

将编码任务委派给 Codex。**立即返回 `task_id`**，Codex 后台异步执行；进度、审批请求和结果作为独立消息推送。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `cwd` | string | 是 | 项目目录的绝对路径 |
| `prompt` | string | 是 | 任务描述 / 指令 |
| `approval_policy` | enum | 否 | 审批策略：`on-request`、`on-failure`、`never`、`untrusted`（默认 `never`） |
| `sandbox_policy` | enum | 否 | 沙箱策略：`read-only`、`workspace-write`、`danger-full-access`（默认 `workspace-write`） |
| `base_instructions` | string | 否 | 预置指令，追加到 Codex thread 开头 |

**示例**（LLM 自动调用）：
```json
{
  "cwd": "/home/user/my-project",
  "prompt": "Fix the login bug where users get 401 after password reset",
  "approval_policy": "on-request",
  "sandbox_policy": "workspace-write"
}
```

### `codex_revive` — 恢复历史任务

恢复历史 Codex thread（如 gateway 重启后），使其重新进入活跃任务列表，用户可通过 `/codex reply` 继续对话。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `thread_id` | string | 是 | Codex thread 的完整 UUID |
| `sandbox_policy` | enum | 否 | 同上 |
| `approval_policy` | enum | 否 | 同上 |

**注意**：`thread/read` 不返回最后一次 turn 的 `model`、`sandbox_policy`、`approval_policy`（均为 per-turn 覆盖），需显式传入以保持原配置。

## 斜杠命令

### `/codex` — 任务管理与交互

```
/codex                              # 列出当前会话的任务
/codex list                         # 同上
/codex list --threads               # 列出服务器上的所有 thread
/codex models                       # 列出可用模型
/codex model                        # 查看当前默认模型
/codex model <id>                   # 设置默认模型（内存级，不持久化）
/codex reply <task_id> <message>    # 向 Codex 发送追问 / 回复
/codex approve <task_id>            # 批准待审批的命令
/codex deny <task_id>               # 拒绝待审批的命令
/codex archive <task_id>            # 归档指定任务
/codex archive all                  # 归档当前会话所有任务
/codex archive allthreads           # 归档服务器上所有 thread
/codex plan on                      # 开启计划模式（Codex 会先输出方案再执行）
/codex plan off                     # 关闭计划模式
/codex verbose on                   # 开启详细输出（显示命令执行、文件变更等中间步骤）
/codex verbose off                  # 关闭详细输出
/codex help [topic]                 # 查看帮助
```

## 审批流程

当 Codex 需要执行命令或修改文件时（取决于 `approval_policy`），触发审批流程：

```
1. Codex 决定执行命令（如 rm -rf /tmp/cache）
2. app-server 发送 JSON-RPC 请求: item/commandExecution/requestApproval
3. 插件查找对应的 task_id 和聊天目标
4. 推送审批通知：
   ⚠️ Codex task a1b2c3d4 requests to run a command:
   ```
   rm -rf /tmp/cache
   ```
   /codex approve a1b2c3d4  /  /codex deny a1b2c3d4
5. 用户回复 approve 或 deny
6. 插件通过 WebSocket 发回 JSON-RPC 响应:
   {"result": {"decision": "accept"}}
7. Codex 继续执行或中止该命令
```

**支持的审批类型**：
- 命令执行审批 (`item/commandExecution/requestApproval`) — 显示命令预览
- 文件变更审批 (`item/fileChange/requestApproval`) — 显示文件路径
- 权限审批 (`item/permissions/requestApproval`) — 显示写入路径和网络访问
- MCP Server Elicitation (`mcpServer/elicitation/request`) — URL 访问或表单填写

**审批策略**：
| 策略 | 行为 |
|------|------|
| `never` | 不需要审批，Codex 自由执行 |
| `on-request` | Codex 主动请求审批时需要用户确认 |
| `on-failure` | 仅在命令失败时需要审批 |
| `untrusted` | 不信任环境下的严格审批 |

## 沙箱策略

| 策略 | 读 | 写 | 网络 | 说明 |
|------|----|----|------|------|
| `read-only` | ✅ | ❌ | ✅ | 只读访问 |
| `workspace-write` | ✅ | ✅ | ✅ | 读写 cwd（默认） |
| `danger-full-access` | ✅ | ✅ | ✅ | 完全访问 |

## 配置（Codex CLI）

插件读取的是 **Codex CLI 自身的配置文件** `~/.codex/config.toml`，不是 Hermes Agent 的配置。示例：

```toml
# ~/.codex/config.toml
model = "gpt-5"
model_provider = "openai"

[model_providers.litellm]
base_url = "http://localhost:4001/v1"
env_key = "LITELLM_API_KEY"
```

| 键 | 说明 |
|---|---|
| `model` | 默认模型 ID |
| `model_provider` | 使用哪个 provider（如 `litellm`、`openai`） |
| `model_providers.<id>.base_url` | Provider 的 API base URL |
| `model_providers.<id>.env_key` | 持有 API Key 的环境变量名 |

### 自定义 Provider

`model/list` RPC 仅返回内置 OpenAI 模型，不包含自定义 provider。插件通过 HTTP GET `{base_url}/models` 获取自定义模型列表（标准 OpenAI 格式）。

运行时切换模型：
```
/codex model mimo-v2.5-pro    # 切换到指定模型（内存级，不修改 config.toml）
/codex models                  # 列出所有可用模型
```

设置未知模型时记录警告但仍然允许（软校验）。

## 计划模式

开启后 Codex 在执行前先输出实施方案供审阅：

```
/codex plan on     # 开启
/codex plan off    # 关闭
```

计划模式下 `_build_turn_start` 使用 `CollaborationMode(mode="plan")`，Codex 进入 plan phase 输出方案后等待确认。

## 详细输出模式

默认仅显示最终回复（`agentMessage`）。开启 verbose 后额外显示：

| 类型 | 图标 | 说明 |
|------|------|------|
| `agentMessage` | - | 最终回复（始终显示） |
| `plan` | 📋 | 计划内容 |
| `commandExecution` | ✅/❌ | 命令执行结果（含退出码和输出） |
| `fileChange` | 📝 | 文件变更列表 |
| `webSearch` | 🔍 | 网络搜索结果 |
| `enteredReviewMode` / `exitedReviewMode` | 👁️ | 审查模式进出 |
| `contextCompaction` | 🗜️ | 上下文压缩 |

## 项目结构

```
codex-websocket/
├── __init__.py              # 插件入口，register(ctx) 注册工具和命令
├── schemas.py               # LLM 工具 schema
├── tools.py                 # 工具处理函数
├── plugin.yaml              # 插件清单（元数据、依赖声明）
├── requirements.txt         # Python 依赖
├── codex_websocket/
│   ├── bridge.py            # CodexBridge 单例 — 生命周期、RPC、任务管理
│   ├── commands.py          # /codex 斜杠命令处理
│   ├── handlers.py          # WebSocket 入站帧分发
│   ├── notify.py            # 用户通知推送（跨平台）
│   ├── policies.py          # 默认值、超时常量、模式构建器
│   ├── provider.py          # 模型发现与 provider 管理
│   ├── state.py             # 数据容器（Result、_Pending*）
│   ├── utils.py             # 纯工具函数
│   ├── wire.py              # JSON-RPC 序列化/反序列化
│   └── codex-app-server-schema/  # pydantic wire 模型（150+）
└── tests/
    ├── conftest.py                  # 测试夹具
    ├── fake_codex_server.py         # 内存 WebSocket 测试服务器
    ├── test_bridge_integration.py   # 集成测试（握手、任务流、审批）
    ├── test_bridge_lifecycle.py     # 生命周期边界测试
    ├── test_policies.py             # 策略单元测试
    ├── test_provider.py             # provider 单元测试
    ├── test_rpc_dispatch.py         # RPC id 分发单元测试
    ├── test_state.py                # 状态容器单元测试
    └── test_utils.py                # 工具函数单元测试
```

## 运行测试

```bash
cd plugins/codex-websocket
pip install pytest pytest-asyncio websockets pydantic
python -m pytest tests/ -v
```

测试使用内存 WebSocket 服务器 `FakeCodexServer` 替代真实子进程，通过 `CodexBridge(ws_url=...)` 注入跳过进程启动。共 75 个测试，覆盖：

- 桥接生命周期（握手、幂等性、配置同步、loop 启动超时、WebSocket 泄漏）
- RPC 分发（string/int id 互转、error response 处理）
- 任务全流程（thread/start → turn/start → 通知）
- 审批往返（请求 → 暂存 → 用户操作 → 响应）
- 沙箱策略构建（别名解析、cwd 注入、字典透传）
- 模型发现（HTTP 直连、RPC 回退、分页、错误处理）
- 状态容器与工具函数

## 内部设计

### 结果约定

所有可失败操作返回 `Result` 字典而非抛异常：

```python
{"ok": True, "task_id": "a1b2c3d4", ...}   # 成功
{"ok": False, "error": "description"}       # 失败
```

### 线程模型

```
hermes 主线程 (同步)
  ↕ asyncio.run_coroutine_threadsafe
codex-ws-bridge-loop 线程 (asyncio 事件循环)
  ├── WebSocket 读写
  ├── _reader_loop (帧分发)
  ├── _drive_task / _drive_reply (后台任务)
  └── JSON-RPC 请求/响应配对
```

同步调用者通过 `_run_sync(coro)` 经 `asyncio.run_coroutine_threadsafe` 桥接到异步循环。

### RPC 配对

出站请求分配递增 `id`，对应 `asyncio.Future` 存入 `_pending_rpc[id]`。`_reader_loop` 收到响应后通过 `MessageHandler._resolve_rpc` 完成 Future。

### 后台任务

`start_task` 和 `send_reply` 采用 fire-and-forget 模式：将协程调度到异步循环后立即返回，失败时通过 `report_failure` 推送错误通知。

### Singleton 模式

`CodexBridge.instance()` 提供进程级单例（`_instance_lock` 保证线程安全），每进程仅一条 WebSocket 连接和一个子进程。

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `RUST_LOG` | codex app-server 日志级别 | `codex_app_server=debug,codex_core=info` |

## 平台支持

通知推送到 hermes 支持的所有聊天平台：Telegram、Discord、Slack、WhatsApp、Signal、BlueBubbles、QQBot、Matrix、Mattermost、Home Assistant、钉钉、飞书、企业微信、微信、Email、SMS。
