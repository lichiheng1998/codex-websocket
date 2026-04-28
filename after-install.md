# codex-websocket 已安装 ✓

## 下一步

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
# 或
pip install websockets pydantic
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

- **通过 LLM**：Hermes 会自动使用 `codex_task` 工具委派编码任务
- **手动命令**：`/codex` — 查看/管理 Codex 任务
  - `/codex status` — 查看线程状态
  - `/codex reply <thread_id> <answer>` — 回复 Codex 提问

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `RUST_LOG` | codex app-server 日志级别 | `codex_app_server=debug,codex_core=info` |

可在 `~/.hermes/.env` 中设置。