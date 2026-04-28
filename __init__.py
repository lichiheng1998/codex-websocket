"""Codex-WebSocket plugin — delegate coding tasks via app-server over WebSocket.

Same architecture as the legacy ``codex`` plugin (one shared codex-app-server
process, WS transport, async approval flow) but all wire types go through the
``codex-app-server-schema`` pydantic models.

Registers:
  * ``codex_task`` / ``codex_revive`` tools (toolset: codex_bridge)
  * ``/codex`` slash command for user-driven replies & status checks
"""

from __future__ import annotations

import os
import sys
from shutil import which

# Add src/ to sys.path so `codex_websocket` package resolves after
# the directory restructure into src/codex_websocket/.
# Also add plugin root so schemas.py and tools.py are importable as
# top-level modules.
_src_dir = os.path.join(os.path.dirname(__file__), "src")
_root_dir = os.path.dirname(__file__)
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)
if _root_dir not in sys.path:
    sys.path.insert(0, _root_dir)


def _codex_available() -> bool:
    return which("codex") is not None


def register(ctx) -> None:
    from . import schemas
    from . import tools
    from codex_websocket.commands import handle_slash

    ctx.register_tool(
        name="codex_task",
        toolset="codex_bridge",
        schema=schemas.CODEX_TASK,
        handler=tools.codex_task,
        check_fn=_codex_available,
    )
    ctx.register_tool(
        name="codex_revive",
        toolset="codex_bridge",
        schema=schemas.CODEX_REVIVE,
        handler=tools.codex_revive,
        check_fn=_codex_available,
    )
    ctx.register_command(
        "codex",
        handler=handle_slash,
        description="Reply to or list pending Codex task questions (WS variant)",
    )
