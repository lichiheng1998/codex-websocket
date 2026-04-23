"""Tool handlers for the codex-websocket plugin."""

from __future__ import annotations

import json
import os
from typing import Any

from .bridge import (
    CodexBridge,
    DEFAULT_APPROVAL_POLICY,
    DEFAULT_SANDBOX_POLICY,
    _get_session_context,
)


def _error(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


def codex_task(args: dict, **kwargs: Any) -> str:
    cwd = args.get("cwd", "")
    prompt = args.get("prompt", "")
    approval_policy = args.get("approval_policy", DEFAULT_APPROVAL_POLICY)
    sandbox_policy = args.get("sandbox_policy", DEFAULT_SANDBOX_POLICY)
    base_instructions = args.get("base_instructions")

    if not cwd or not os.path.isabs(cwd):
        return _error("cwd must be an absolute path")
    if not os.path.isdir(cwd):
        return _error(f"cwd does not exist or is not a directory: {cwd}")
    if not prompt or not prompt.strip():
        return _error("prompt is required")

    try:
        session_key, target = _get_session_context()
    except Exception as exc:
        return _error(f"hermes runtime unavailable: {exc}")

    bridge = CodexBridge.instance()
    result = bridge.start_task(
        cwd=cwd,
        prompt=prompt.strip(),
        approval_policy=approval_policy,
        sandbox_policy=sandbox_policy,
        session_key=session_key,
        target=target,
        base_instructions=base_instructions,
    )
    if not result["ok"]:
        return _error(f"codex bridge error: {result['error']}")

    task_id = result["task_id"]
    return json.dumps({
        "ok": True,
        "status": "started",
        "task_id": task_id,
        "cwd": cwd,
        "model": result.get("model", bridge.get_default_model()),
        "message": (
            f"Codex task {task_id} started in the background. "
            f"Progress, approval requests, and the final result will be "
            f"pushed to the current channel as separate messages. "
            f"You do NOT need to poll — return control to the user."
        ),
    }, ensure_ascii=False)


def codex_revive(args: dict, **kwargs: Any) -> str:
    thread_id = (args.get("thread_id") or "").strip()
    if not thread_id:
        return _error("thread_id is required")

    sandbox_policy = args.get("sandbox_policy", DEFAULT_SANDBOX_POLICY)
    approval_policy = args.get("approval_policy", DEFAULT_APPROVAL_POLICY)

    try:
        session_key, target = _get_session_context()
    except Exception as exc:
        return _error(f"hermes runtime unavailable: {exc}")

    bridge = CodexBridge.instance()
    result = bridge.revive_task(
        thread_id,
        target=target,
        session_key=session_key,
        sandbox_policy=sandbox_policy,
        approval_policy=approval_policy,
    )
    if not result.get("ok"):
        return _error(result.get("error", "unknown error"))
    return json.dumps({
        "ok": True,
        "task_id": result["task_id"],
        "thread_id": result["thread_id"],
        "model": result.get("model", bridge.get_default_model()),
        "message": (
            f"Thread revived as task {result['task_id']}. "
            f"Use `/codex reply {result['task_id']} <message>` to continue."
        ),
    }, ensure_ascii=False)
