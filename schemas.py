"""Tool schemas for the codex-websocket plugin (LLM-facing)."""

from codex_websocket.policies import DEFAULT_APPROVAL_POLICY, DEFAULT_SANDBOX_POLICY

CODEX_REVIVE = {
    "name": "codex_revive",
    "description": (
        "Revive a Codex thread from a previous session (e.g. after gateway "
        "restart). Restores the thread into the active task map so the user "
        "can reply to it via `/codex reply`. Use when the user references an "
        "existing Codex thread_id that is no longer tracked in the current "
        "session. Codex's `thread/read` does NOT return the thread's last "
        "`model`/`sandbox_policy`/`approval_policy` (those are per-turn "
        "overrides), so pass them explicitly if the user wants follow-up "
        "turns to keep the original configuration; otherwise plugin defaults "
        "are used. Plan collaboration mode is a session-wide toggle — use "
        "`/codex plan on|off` rather than passing it here."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "thread_id": {
                "type": "string",
                "description": "Full Codex thread UUID to revive.",
            },
            "sandbox_policy": {
                "type": "string",
                "enum": ["read-only", "workspace-write", "danger-full-access"],
                "description": (
                    f"Sandbox policy for follow-up turns (default "
                    f"'{DEFAULT_SANDBOX_POLICY}')."
                ),
            },
            "approval_policy": {
                "type": "string",
                "enum": ["on-request", "on-failure", "never", "untrusted"],
                "description": (
                    f"Approval policy for follow-up turns (default "
                    f"'{DEFAULT_APPROVAL_POLICY}')."
                ),
            },
        },
        "required": ["thread_id"],
    },
}

CODEX_TASK = {
    "name": "codex_task",
    "description": (
        "Delegate a coding task to Codex (OpenAI's code agent) running as a "
        "persistent background thread over the codex-app-server WebSocket. "
        "**Returns immediately with a task_id** — Codex runs asynchronously; "
        "progress updates, approval requests, and the final result are pushed "
        "to the current chat as separate messages. Command/file-change "
        "approvals route through `/codex approve`/`/codex deny`. If Codex "
        "asks the user a question (e.g. plan mode is enabled via "
        "`/codex plan on`), the user replies via `/codex reply <task_id> "
        "<answer>`. Use this when a task is "
        "well-scoped for a code-focused sub-agent (bug fix, feature, "
        "refactor). After calling, report the task_id to the user and return "
        "control — do NOT poll for the result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cwd": {
                "type": "string",
                "description": "Absolute path to the project directory Codex should operate in.",
            },
            "prompt": {
                "type": "string",
                "description": "The task description / instructions for Codex.",
            },
            "approval_policy": {
                "type": "string",
                "enum": ["on-request", "on-failure", "never", "untrusted"],
                "description": (
                    f"Codex approval policy (default '{DEFAULT_APPROVAL_POLICY}'). "
                    "'on-request' surfaces approvals to the user in chat."
                ),
            },
            "sandbox_policy": {
                "type": "string",
                "enum": ["read-only", "workspace-write", "danger-full-access"],
                "description": f"Codex sandbox policy (default '{DEFAULT_SANDBOX_POLICY}').",
            },
            "base_instructions": {
                "type": "string",
                "description": "Optional base instructions prepended to the Codex thread.",
            },
        },
        "required": ["cwd", "prompt"],
    },
}
