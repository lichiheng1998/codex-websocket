"""Small pure utilities used by the bridge and the tool entry points.

Nothing in this module touches the bridge instance. Everything is either
a one-liner over stdlib (``new_task_id``, ``pick_free_port``) or extracts
something from a dict that came back over the wire (``extract_thread_id``).

``get_session_context`` reaches into the hermes runtime (``tools.approval``,
``gateway.session_context``) and is therefore best kept off the bridge
class — those modules may not be importable in standalone test contexts.
"""

from __future__ import annotations

import secrets
import socket
from typing import Any, Tuple

from .state import TaskTarget


def get_session_context() -> "Tuple[str, TaskTarget]":
    """Pull the current hermes session-key and chat coordinates.

    Imports are deferred so this module can be imported in environments
    where the hermes runtime isn't available; callers in such environments
    should never call this function.
    """
    from tools.approval import get_current_session_key
    from gateway.session_context import get_session_env

    session_key = get_current_session_key()
    target = TaskTarget(
        platform=get_session_env("HERMES_SESSION_PLATFORM", ""),
        chat_id=get_session_env("HERMES_SESSION_CHAT_ID", ""),
        thread_id=get_session_env("HERMES_SESSION_THREAD_ID", ""),
    )
    return session_key, target


def extract_thread_id(obj: Any) -> str:
    """Best-effort scrape of a thread/conversation id from a server payload.

    Codex's response shapes have varied between revisions: top-level
    ``threadId`` / ``conversationId`` / ``thread_id``, a nested ``thread``
    dict with ``id`` or ``threadId``, or a top-level ``id`` that happens
    to look like a UUID. Empty string when nothing matches.
    """
    if not isinstance(obj, dict):
        return ""
    for key in ("threadId", "conversationId", "thread_id"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val
    thread = obj.get("thread")
    if isinstance(thread, dict):
        tid = thread.get("id") or thread.get("threadId")
        if isinstance(tid, str) and tid:
            return tid
    tid = obj.get("id")
    if isinstance(tid, str) and len(tid) >= 8 and "-" in tid:
        return tid
    return ""


def new_task_id() -> str:
    """8-hex-char id used as the bridge-internal handle for a task."""
    return secrets.token_hex(4)


def pick_free_port() -> int:
    """Bind ephemeral, return the kernel's chosen port. Caller races to
    use it before something else grabs it — fine for a localhost spawn."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
