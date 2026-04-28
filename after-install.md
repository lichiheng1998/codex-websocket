# codex-websocket installed ✓

## Next steps

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Ensure Codex CLI is installed

```bash
codex --version
```

If not installed, see [OpenAI Codex CLI](https://github.com/openai/codex).

### 3. Configure Codex (optional)

Codex reads `~/.codex/config.toml` for model, approval policy, and other settings. For models that don't support the OpenAI Responses API, use LiteLLM as a compatibility bridge. When using LiteLLM with newer versions of Codex, disable unsupported features:

```toml
[features]
image_generation = false
browser_use = false
computer_use = false
```

### 4. Enable the plugin

```bash
hermes plugins enable codex-websocket
hermes gateway restart
```

## Usage

Hermes will automatically delegate coding tasks via the `codex_task` tool. You can also manage tasks manually with `/codex`:

| Command | Description |
|---------|-------------|
| `/codex` | List tasks in this session (same as `list`) |
| `/codex list` | List tasks in this session |
| `/codex list --threads` | List all threads on the server |
| `/codex status` | Show bridge connection state, task count, and current model |
| `/codex models` | List available models |
| `/codex model` | Show current default model |
| `/codex model <model_id>` | Set default model for future tasks |
| `/codex reply <task_id> <message>` | Send a follow-up message to a task |
| `/codex approve <task_id>` | Approve a pending command |
| `/codex deny <task_id>` | Deny a pending command |
| `/codex archive <task_id>` | Archive a task thread |
| `/codex archive all` | Archive all tasks in this session |
| `/codex archive allthreads` | Archive every thread on the server |
| `/codex plan on\|off` | Toggle plan collaboration mode |
| `/codex verbose on\|off` | Toggle verbose notifications (show each completed item) |

Use the `codex_revive` tool to reattach a thread from a previous session.

## Environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `RUST_LOG` | Log level for codex app-server | `codex_app_server=debug,codex_core=info` |

Set in `~/.hermes/.env` if needed.
