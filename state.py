"""Plain data containers used across the codex-websocket bridge.

Three groups:

* ``Result`` / ``ok`` / ``err`` — the project-wide "fallible op returned a
  dict" convention; isolated here so non-bridge modules (handlers, provider
  helpers) can produce Results without circular-importing bridge.

* ``TaskTarget`` — chat coordinates the bridge pushes user-facing messages
  to. Threaded through start/reply/notify paths.

* ``_PendingThread`` / ``_PendingInput`` / ``_PendingApproval`` /
  ``_PendingElicitation`` — in-flight state the bridge keeps about each
  active task. Kept dumb on purpose: the bridge owns the dicts, these
  classes only hold fields and (for approvals) render their own response
  payload so resolve_approval doesn't need a type switch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


Result = Dict[str, Any]


def ok(**data: Any) -> Result:
    return {"ok": True, **data}


def err(message: str) -> Result:
    return {"ok": False, "error": message}


@dataclass
class TaskTarget:
    platform: str = ""
    chat_id: str = ""
    thread_id: str = ""


@dataclass
class _PendingThread:
    thread_id: str
    task_id: str
    session_key: str
    cwd: str
    sandbox_policy: str
    approval_policy: str
    target: Optional[TaskTarget] = None


@dataclass
class _PendingInput:
    task_id: str
    rpc_id: Any
    questions: list


@dataclass
class _PendingApproval:
    """Server→client approval/elicitation request awaiting a user verdict.

    The wire shape of the response depends on what kind of request it was;
    each subclass renders its own payload in ``to_response_payload`` so
    ``resolve_approval`` doesn't carry a method-typed switch.
    """

    rpc_id: Any
    task_id: str
    command: str
    reason: str
    target: Optional[TaskTarget]

    def to_response_payload(self, decision: str) -> Dict[str, Any]:
        # commandExecution / fileChange / permissions all use the same shape.
        return {"decision": decision}


@dataclass
class _PendingElicitation(_PendingApproval):
    """MCP elicitation: server expects an MCP-spec accept/decline action."""

    def to_response_payload(self, decision: str) -> Dict[str, Any]:
        action = "accept" if decision == "accept" else "decline"
        return {"action": action, "content": None}
