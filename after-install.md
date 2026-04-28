# codex-websocket 已安装 ✓

## 下一步

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 确保 Codex CLI 已安装

```bash
codex --version
```

如未安装，请参考 [OpenAI Codex CLI](https://github.com/openai/codex) 安装。

### 3. 配置 Codex（可选）

Codex 的配置文件位于 `~/.codex/config.toml`，可设置模型、审批策略等。

### 4. 启用插件

```bash
hermes plugins enable codex-websocket
hermes gateway restart
```

## 使用方法

Hermes 会通过 `codex_task` 工具自动委派编码任务。也可以用 `/codex` 命令手动管理：

| 命令 | 说明 |
|------|------|
| `/codex` | 列出本 session 的任务（同 `list`） |
| `/codex list` | 列出本 session 的任务 |
| `/codex list --threads` | 列出服务器上所有线程 |
| `/codex status` | 显示 bridge 连接状态、任务数、当前模型 |
| `/codex models` | 列出可用模型 |
| `/codex model` | 显示当前默认模型 |
| `/codex model <model_id>` | 设置默认模型 |
| `/codex reply <task_id> <message>` | 向任务发送后续消息 |
| `/codex approve <task_id>` | 批准待审批的命令 |
| `/codex deny <task_id>` | 拒绝待审批的命令 |
| `/codex archive <task_id>` | 归档指定任务线程 |
| `/codex archive all` | 归档本 session 所有任务 |
| `/codex archive allthreads` | 归档服务器上所有线程 |
| `/codex plan on\|off` | 开启/关闭 plan 协作模式 |
| `/codex verbose on\|off` | 开启/关闭详细通知（显示每个 item 完成情况） |

也可通过 `codex_revive` 工具恢复上一个 session 的历史线程。

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `RUST_LOG` | codex app-server 日志级别 | `codex_app_server=debug,codex_core=info` |

可在 `~/.hermes/.env` 中设置。
