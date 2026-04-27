"""Wire-format constants and pure transforms for the codex-websocket bridge.

Three groups:

* **Default policies** — the values used when the caller didn't pass one.
* **Timeouts** — every numeric timeout used inside the bridge has a name
  here, with a comment describing what stage it covers.
* **Sandbox & collaboration mode helpers** — pure functions that turn a
  short string ("workspace-write", model id) into the dict/pydantic shape
  the codex app-server expects.

No bridge state. Other modules can import from here freely.
"""

from __future__ import annotations

from typing import Any

from . import wire


# ── Default policies ────────────────────────────────────────────────────────

DEFAULT_MODEL = "gpt-5"
DEFAULT_APPROVAL_POLICY = "never"
DEFAULT_SANDBOX_POLICY = "workspace-write"


# ── Timeouts ────────────────────────────────────────────────────────────────

# Wait for the bridge's asyncio loop thread to enter run_forever().
LOOP_READY_TIMEOUT = 5.0
# Each socket-probe attempt while waiting for the spawned codex app-server
# port to open (we retry until STARTUP_TIMEOUT elapses).
PORT_PROBE_TIMEOUT = 0.5
# Cap for the whole spawn → connect → initialize → first-sync chain.
STARTUP_TIMEOUT = 15.0
# Standard JSON-RPC request timeout (config/read, model/list, thread/*, etc.).
RPC_TIMEOUT = 10.0
# Used when scheduling a fast coroutine onto the loop or doing local-only
# work (kicking off _drive_*, _ws_send for an approval reply).
SHORT_RPC_TIMEOUT = 5.0
# Closing the websocket and waiting for the codex subprocess to exit.
SHUTDOWN_TIMEOUT = 3.0
# Direct GET to the configured provider's /models endpoint.
PROVIDER_HTTP_TIMEOUT = 5.0


# ── Collaboration mode (plan vs default) ────────────────────────────────────

def plan_collaboration_mode(model: str) -> "wire.CollaborationMode":
    """Build the CollaborationMode payload for plan mode.

    `settings.model` is required by the schema, so echo the caller's model.
    """
    return wire.CollaborationMode(
        mode=wire.ModeKind("plan"),
        settings=wire.CollaborationSettings(model=model),
    )


def default_collaboration_mode(model: str) -> "wire.CollaborationMode":
    """Build the CollaborationMode payload for default mode.

    Sent explicitly when plan mode is off so turn/start does not rely on
    server-side interpretation of an omitted collaborationMode field.
    """
    return wire.CollaborationMode(
        mode=wire.ModeKind("default"),
        settings=wire.CollaborationSettings(model=model),
    )


# ── Sandbox policy shapes (Codex wire format) ───────────────────────────────

_READ_ONLY = {"type": "readOnly", "access": {"type": "fullAccess"}, "networkAccess": False}
_WORKSPACE_WRITE = {
    "type": "workspaceWrite",
    "writableRoots": [],
    "readOnlyAccess": {"type": "fullAccess"},
    "networkAccess": True,
    "excludeTmpdirEnvVar": False,
    "excludeSlashTmp": False,
}
_DANGER_FULL_ACCESS = {"type": "dangerFullAccess"}

_SANDBOX_POLICY_ALIASES = {
    "read-only": _READ_ONLY,
    "readonly": _READ_ONLY,
    "workspace-write": _WORKSPACE_WRITE,
    "workspacewrite": _WORKSPACE_WRITE,
    "danger-full-access": _DANGER_FULL_ACCESS,
    "dangerfullaccess": _DANGER_FULL_ACCESS,
}


def _normalize_sandbox_policy(policy: Any) -> Any:
    if isinstance(policy, dict):
        return policy
    if isinstance(policy, str):
        alias = _SANDBOX_POLICY_ALIASES.get(policy.lower())
        if alias is not None:
            return alias
    return policy


def prepare_sandbox(sandbox_policy: str, cwd: str) -> Any:
    """Resolve a sandbox-policy alias and inject the project cwd as a
    writable root (only meaningful for workspaceWrite — readOnly and
    dangerFullAccess pass through unchanged)."""
    sandbox = _normalize_sandbox_policy(sandbox_policy)
    if cwd and isinstance(sandbox, dict) and sandbox.get("type") == "workspaceWrite":
        roots = sandbox.get("writableRoots") or []
        if cwd not in roots:
            sandbox = {**sandbox, "writableRoots": roots + [cwd]}
    return sandbox
