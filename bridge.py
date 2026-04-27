"""Codex app-server bridge — WebSocket client for delegating coding tasks.

Same shape as the legacy ``codex`` plugin's bridge (single shared
``codex app-server`` subprocess, lazy-start, thread-per-task via
``_drive_task``) but **all wire types go through pydantic models** from
``codex-app-server-schema``.

Error-handling convention
-------------------------
Every fallible op returns a **Result dict** — ``{"ok": bool, ...}`` — rather
than raising. ``_rpc``, ``_run_sync``, ``_ws_send``, ``ensure_started``,
``start_task``, ``revive_task``, and the rest of the public API all follow
this. Callers compose Results: if a dependency returns ``{"ok": False,
"error": ...}``, pass it through; on success, build the next step. No nested
``try`` blocks in the happy path, no ``_safe_result`` wrapper.

The fire-and-forget background tasks ``_drive_task`` / ``_drive_reply`` have
no caller to return to, so they report failures via ``_report_failure``
(notify + log) instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

from pydantic import BaseModel

from . import wire
from .handlers import MessageHandler

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gpt-5"
DEFAULT_APPROVAL_POLICY = "never"
DEFAULT_SANDBOX_POLICY = "workspace-write"

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


def _plan_collaboration_mode(model: str) -> "wire.CollaborationMode":
    """Build the CollaborationMode payload for plan mode.

    `settings.model` is required by the schema, so echo the caller's model.
    """
    return wire.CollaborationMode(
        mode=wire.ModeKind("plan"),
        settings=wire.CollaborationSettings(model=model),
    )


def _default_collaboration_mode(model: str) -> "wire.CollaborationMode":
    """Build the CollaborationMode payload for default mode.

    We send this explicitly when plan mode is off so turn/start does not rely
    on server-side interpretation of an omitted collaborationMode field.
    """
    return wire.CollaborationMode(
        mode=wire.ModeKind("default"),
        settings=wire.CollaborationSettings(model=model),
    )


Result = Dict[str, Any]


def ok(**data: Any) -> Result:
    return {"ok": True, **data}


def err(message: str) -> Result:
    return {"ok": False, "error": message}


# ── Sandbox policy shapes (Codex wire format). ─────────────────────────────
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


def _prepare_sandbox(sandbox_policy: str, cwd: str) -> Any:
    sandbox = _normalize_sandbox_policy(sandbox_policy)
    if cwd and isinstance(sandbox, dict) and sandbox.get("type") == "workspaceWrite":
        roots = sandbox.get("writableRoots") or []
        if cwd not in roots:
            sandbox = {**sandbox, "writableRoots": roots + [cwd]}
    return sandbox


def _get_session_context() -> "tuple[str, TaskTarget]":
    from tools.approval import get_current_session_key
    from gateway.session_context import get_session_env
    session_key = get_current_session_key()
    target = TaskTarget(
        platform=get_session_env("HERMES_SESSION_PLATFORM", ""),
        chat_id=get_session_env("HERMES_SESSION_CHAT_ID", ""),
        thread_id=get_session_env("HERMES_SESSION_THREAD_ID", ""),
    )
    return session_key, target


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
    approval_policy: str = DEFAULT_APPROVAL_POLICY
    target: Optional[TaskTarget] = None


@dataclass
class _PendingInput:
    task_id: str
    thread_id: str
    rpc_id: Any
    questions: list


@dataclass
class _PendingApproval:
    rpc_id: Any
    task_id: str
    command: str
    reason: str
    target: Optional[TaskTarget]
    approval_type: str = "command"  # "command", "permissions", "elicitation"


class CodexBridge:
    _instance: Optional["CodexBridge"] = None
    _instance_lock = threading.Lock()

    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.port: Optional[int] = None
        self.ws = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.loop_thread: Optional[threading.Thread] = None
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._threads: Dict[str, _PendingThread] = {}
        self._pending_rpc: Dict[int, asyncio.Future] = {}
        self._pending_inputs: Dict[str, _PendingInput] = {}
        self._pending_approvals: Dict[str, _PendingApproval] = {}
        self._task_map: Dict[str, str] = {}
        self._ready = threading.Event()
        self._start_lock = threading.Lock()
        self._handler: Optional[MessageHandler] = None
        self._plan_enabled: bool = False
        self._verbose_enabled: bool = False
        self._default_model: str = DEFAULT_MODEL
        # Provider info read from `config/read` so we can list models from the
        # provider's own /v1/models endpoint instead of codex's bundled OpenAI
        # catalog. None until the first successful sync.
        self._provider_id: Optional[str] = None
        self._provider_base_url: Optional[str] = None
        self._provider_env_key: Optional[str] = None

    @classmethod
    def instance(cls) -> "CodexBridge":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ==================================================================
    # Lifecycle — all return Result.
    # ==================================================================

    def ensure_started(self) -> Result:
        """Start server + ws + handler on first call. Idempotent."""
        if self._ready.is_set():
            return ok()
        with self._start_lock:
            if self._ready.is_set():
                return ok()
            self._start_loop_thread()

            spawn = self._spawn_server()
            if not spawn["ok"]:
                return spawn

            connect = self._run_sync(self._connect_and_initialize(), timeout=STARTUP_TIMEOUT)
            if not connect["ok"]:
                return connect

            sync = self._run_sync(self._sync_config_from_server(), timeout=STARTUP_TIMEOUT)
            if sync["ok"]:
                self._default_model = sync["model"]
            else:
                logger.warning("codex bridge: failed to sync default model: %s", sync["error"])

            self._ready.set()
            return ok()

    def _start_loop_thread(self) -> None:
        if self.loop is not None:
            return
        loop_ready = threading.Event()

        def _run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            loop_ready.set()
            self.loop.run_forever()

        self.loop_thread = threading.Thread(
            target=_run, name="codex-ws-bridge-loop", daemon=True,
        )
        self.loop_thread.start()
        loop_ready.wait(timeout=LOOP_READY_TIMEOUT)

    def _spawn_server(self) -> Result:
        try:
            self.port = _pick_free_port()
            cmd = ["codex", "app-server", "--listen", f"ws://127.0.0.1:{self.port}"]
            logger.info("Starting codex app-server on port %d", self.port)
            log_path = os.path.expanduser("~/.hermes/logs/codex-app-server.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            env = os.environ.copy()
            env["RUST_LOG"] = env.get("RUST_LOG", "codex_app_server=debug,codex_core=info")
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL,
                stderr=open(log_path, "a"), env=env,
            )
        except Exception as exc:
            return err(f"failed to spawn codex app-server: {exc}")

        deadline = time.monotonic() + STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                return err(
                    f"codex app-server exited with code {self.proc.returncode}; "
                    f"see {log_path}"
                )
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=PORT_PROBE_TIMEOUT):
                    return ok()
            except OSError:
                time.sleep(0.2)
        return err("codex app-server failed to open port within timeout")

    async def _connect_and_initialize(self) -> None:
        import websockets
        url = f"ws://127.0.0.1:{self.port}"
        self.ws = await websockets.connect(url, max_size=None, ping_interval=20)
        self._handler = MessageHandler(
            pending_rpc=self._pending_rpc,
            threads=self._threads,
            pending_inputs=self._pending_inputs,
            pending_approvals=self._pending_approvals,
            task_map=self._task_map,
            ws_send=self._ws_send,
            notify=self._notify,
            is_verbose=lambda: self._verbose_enabled,
        )
        asyncio.create_task(self._reader_loop())
        init = await self._rpc(
            "initialize",
            wire.InitializeParams(
                clientInfo={"name": "hermes-codex-ws-bridge", "version": "0.1"},
                capabilities=wire.InitializeCapabilities(experimentalApi=True),
            ),
        )
        if not init["ok"]:
            raise RuntimeError(f"initialize failed: {init['error']}")
        notified = await self._ws_send(json.dumps({"jsonrpc": "2.0", "method": "initialized"}))
        if not notified["ok"]:
            raise RuntimeError(f"initialized notification failed: {notified['error']}")

    async def _sync_config_from_server(self) -> Result:
        """Pull effective config from the app-server.

        Reads `config/read` to learn (a) the user's default `model = ...`
        from config.toml, and (b) the active `model_provider`'s base_url and
        env_key — needed so `list_models()` can hit the provider's own
        /v1/models instead of the bundled OpenAI catalog (which is what
        `model/list` returns regardless of the configured provider).

        Falls back to `model/list` + isDefault for users who haven't set
        `model = ...` explicitly (e.g. plain OpenAI provider).
        """
        cfg_rpc = await self._rpc(
            "config/read", wire.ConfigReadParams(), timeout=RPC_TIMEOUT,
        )
        if cfg_rpc["ok"]:
            config = (cfg_rpc["result"] or {}).get("config") or {}
            provider_id = (config.get("model_provider") or "").strip() or None
            self._provider_id = provider_id
            providers = config.get("model_providers") or {}
            if provider_id and isinstance(providers, dict):
                pinfo = providers.get(provider_id) or {}
                self._provider_base_url = (pinfo.get("base_url") or "").strip() or None
                self._provider_env_key = (pinfo.get("env_key") or "").strip() or None

            model = (config.get("model") or "").strip()
            if model:
                return ok(model=model)

        cursor = None
        while True:
            rpc = await self._rpc(
                "model/list",
                wire.ModelListParams(cursor=cursor, includeHidden=True),
                timeout=RPC_TIMEOUT,
            )
            if not rpc["ok"]:
                return rpc

            payload = rpc["result"] or {}
            for item in payload.get("data") or []:
                if not isinstance(item, dict) or not item.get("isDefault"):
                    continue
                model = (item.get("model") or item.get("id") or "").strip()
                if model:
                    return ok(model=model)

            cursor = payload.get("nextCursor")
            if not cursor:
                break

        return err("no default model: config.toml has no `model = ...` and model/list returned no isDefault entry")

    def shutdown(self) -> None:
        if self.loop and self.loop.is_running():
            self._run_sync(self._close_ws(), timeout=SHUTDOWN_TIMEOUT)  # ignore Result — teardown path
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=SHUTDOWN_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._ready.clear()

    async def _close_ws(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass

    # ==================================================================
    # JSON-RPC plumbing — _rpc / _ws_send never raise, return Result.
    # ==================================================================

    def _next_rpc_id(self) -> int:
        with self._id_lock:
            rpc_id = self._next_id
            self._next_id += 1
            return rpc_id

    async def _rpc(
        self,
        method: str,
        params: Union[BaseModel, dict, None] = None,
        timeout: float = 30.0,
    ) -> Result:
        """Send a JSON-RPC request and wait for the response.

        Returns ``{"ok": True, "result": <server payload>}`` or
        ``{"ok": False, "error": <str>}``. Never raises.
        """
        rpc_id = self._next_rpc_id()
        fut: asyncio.Future = self.loop.create_future()
        self._pending_rpc[rpc_id] = fut
        try:
            await self.ws.send(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id,
                "method": method, "params": wire.serialize(params),
            }))
            result = await asyncio.wait_for(fut, timeout=timeout)
            return ok(result=result)
        except asyncio.TimeoutError:
            return err(f"{method}: timeout after {timeout}s")
        except Exception as exc:
            return err(f"{method}: {exc}")
        finally:
            self._pending_rpc.pop(rpc_id, None)

    async def _ws_send(self, payload: str) -> Result:
        """Send a raw frame on the ws. Never raises."""
        try:
            await self.ws.send(payload)
            return ok()
        except Exception as exc:
            return err(f"ws send failed: {exc}")

    def _run_sync(self, coro, timeout: float = 12.0) -> Result:
        """Schedule ``coro`` on the bridge loop from a sync caller.

        The coroutine's return value is surfaced in the ``result`` key when it
        doesn't already return a Result dict; if it does, that Result is
        returned verbatim.
        """
        try:
            value = asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)
        except Exception as exc:
            return err(f"bridge loop call failed: {exc}")

        if isinstance(value, dict) and "ok" in value:
            return value
        return ok(result=value)

    async def _reader_loop(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("codex bridge: non-JSON frame dropped")
                    continue
                logger.debug("codex ws ← %s", json.dumps(msg, ensure_ascii=False)[:500])
                try:
                    await self._handler.dispatch(msg)
                except Exception as exc:
                    logger.exception("codex handler failed on frame: %s", exc)
        except Exception as exc:
            logger.warning("codex bridge reader exited: %s", exc)
            for fut in list(self._pending_rpc.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError(f"websocket closed: {exc}"))

    # ==================================================================
    # Notify — best-effort, always swallows errors (user-visible side-effect).
    # ==================================================================

    async def _notify(self, target: Optional[TaskTarget], message: str) -> None:
        if target is None or not target.platform or not target.chat_id:
            logger.info("codex notify (no target): %s", message[:200])
            return
        try:
            from gateway.config import load_gateway_config, Platform
            from tools.send_message_tool import _send_to_platform

            platform_map = {
                "telegram": Platform.TELEGRAM, "discord": Platform.DISCORD,
                "slack": Platform.SLACK, "whatsapp": Platform.WHATSAPP,
                "signal": Platform.SIGNAL, "bluebubbles": Platform.BLUEBUBBLES,
                "qqbot": Platform.QQBOT, "matrix": Platform.MATRIX,
                "mattermost": Platform.MATTERMOST,
                "homeassistant": Platform.HOMEASSISTANT,
                "dingtalk": Platform.DINGTALK, "feishu": Platform.FEISHU,
                "wecom": Platform.WECOM, "weixin": Platform.WEIXIN,
                "email": Platform.EMAIL, "sms": Platform.SMS,
            }
            platform = platform_map.get(target.platform.lower())
            if platform is None:
                logger.warning("codex notify: unknown platform %r", target.platform)
                return

            cfg = load_gateway_config()
            pconfig = cfg.platforms.get(platform)
            if pconfig is None:
                logger.warning("codex notify: platform %s not configured", platform)
                return

            await _send_to_platform(
                platform, pconfig, target.chat_id, message,
                thread_id=target.thread_id or None,
            )
        except Exception as exc:
            logger.warning("codex notify failed: %s", exc)

    async def _report_failure(
        self, target: Optional[TaskTarget], task_id: str, stage: str, detail: str,
    ) -> None:
        """Fire-and-forget error reporter for background tasks."""
        logger.warning("codex task %s failed at %s: %s", task_id, stage, detail)
        await self._notify(target, f"❌ Codex task `{task_id}` {stage}: {detail}")

    # ==================================================================
    # Public API — all compose Result dicts from lower layers.
    # ==================================================================

    def start_task(
        self,
        *,
        cwd: str,
        prompt: str,
        approval_policy: str = DEFAULT_APPROVAL_POLICY,
        sandbox_policy: str = DEFAULT_SANDBOX_POLICY,
        session_key: str = "",
        target: Optional[TaskTarget] = None,
        base_instructions: Optional[str] = None,
    ) -> Result:
        """Launch a task. Returns ``{ok, task_id}`` or ``{ok: False, error}``."""
        start = self.ensure_started()
        if not start["ok"]:
            return start

        task_id = _new_task_id()

        async def _boot() -> None:
            asyncio.create_task(self._drive_task(
                task_id=task_id, cwd=cwd, prompt=prompt,
                approval_policy=approval_policy, sandbox_policy=sandbox_policy,
                session_key=session_key, target=target,
                base_instructions=base_instructions,
            ))

        boot = self._run_sync(_boot(), timeout=SHORT_RPC_TIMEOUT)
        if not boot["ok"]:
            return boot
        return ok(task_id=task_id, model=self._default_model)

    def _build_turn_start(
        self,
        *,
        thread_id: str,
        text: str,
        cwd: str,
        sandbox_policy: str,
        approval_policy: str,
    ) -> "wire.TurnStartParams":
        """Render a TurnStartParams using the bridge's current default model
        and plan-mode setting. Used by both initial turns and replies so the
        two paths don't drift."""
        model = self._default_model
        return wire.TurnStartParams(
            threadId=thread_id,
            input=[{"type": "text", "text": text}],
            model=model,
            approvalPolicy=approval_policy,
            sandboxPolicy=_prepare_sandbox(sandbox_policy, cwd),
            collaborationMode=(
                _plan_collaboration_mode(model)
                if self._plan_enabled
                else _default_collaboration_mode(model)
            ),
        )

    async def _drive_task(
        self,
        *,
        task_id: str,
        cwd: str,
        prompt: str,
        approval_policy: str,
        sandbox_policy: str,
        session_key: str,
        target: Optional[TaskTarget],
        base_instructions: Optional[str],
    ) -> None:
        """Fire-and-forget — failures go to _report_failure, no return value."""
        model = self._default_model

        thread_rpc = await self._rpc(
            "thread/start",
            wire.ThreadStartParams(cwd=cwd, model=model, baseInstructions=base_instructions),
        )
        if not thread_rpc["ok"]:
            await self._report_failure(target, task_id, "thread/start failed", thread_rpc["error"])
            return

        thread_id = _extract_thread_id(thread_rpc["result"])
        if not thread_id:
            await self._report_failure(target, task_id, "thread/start", "no thread id in response")
            return

        self._task_map[task_id] = thread_id
        self._threads[thread_id] = _PendingThread(
            thread_id=thread_id, task_id=task_id, session_key=session_key,
            cwd=cwd, sandbox_policy=sandbox_policy,
            approval_policy=approval_policy, target=target,
        )

        await self._notify(target, (
            f"🤖 Codex task `{task_id}` started\n"
            f"cwd: `{cwd}`\nmodel: `{model}`"
            + ("\nmode: `plan`" if self._plan_enabled else "")
        ))

        turn_rpc = await self._rpc(
            "turn/start",
            self._build_turn_start(
                thread_id=thread_id, text=prompt, cwd=cwd,
                sandbox_policy=sandbox_policy, approval_policy=approval_policy,
            ),
        )
        if not turn_rpc["ok"]:
            self._task_map.pop(task_id, None)
            self._threads.pop(thread_id, None)
            await self._report_failure(target, task_id, "turn/start failed", turn_rpc["error"])

    def send_reply(self, task_id: str, message: str) -> Result:
        started = self.ensure_started()
        if not started["ok"]:
            return started

        thread_id = self._task_map.get(task_id)
        if not thread_id:
            return err(f"unknown task/thread id {task_id!r}")

        if task_id in self._pending_inputs:
            return self._run_sync(
                self._handler.submit_input_answers(task_id, [message]),
                timeout=SHORT_RPC_TIMEOUT,
            )

        async def _boot() -> None:
            asyncio.create_task(self._drive_reply(task_id, message))

        boot = self._run_sync(_boot(), timeout=SHORT_RPC_TIMEOUT)
        if not boot["ok"]:
            return boot
        return ok(task_id=task_id)

    async def _drive_reply(self, task_id: str, message: str) -> None:
        thread_id = self._task_map.get(task_id)
        if not thread_id:
            await self._report_failure(None, task_id, "reply failed", "task not found")
            return

        pt = self._threads.get(thread_id)
        if not pt:
            await self._report_failure(None, task_id, "reply failed", "thread missing for task")
            return

        rpc = await self._rpc(
            "turn/start",
            self._build_turn_start(
                thread_id=thread_id, text=message, cwd=pt.cwd,
                sandbox_policy=pt.sandbox_policy, approval_policy=pt.approval_policy,
            ),
        )
        if not rpc["ok"]:
            await self._report_failure(pt.target, task_id, "reply failed", rpc["error"])

    def set_plan_mode(self, enabled: bool) -> Result:
        """Toggle plan collaboration mode for all subsequent turns on every thread."""
        self._plan_enabled = bool(enabled)
        return ok(plan=self._plan_enabled)

    def plan_mode(self) -> bool:
        return self._plan_enabled

    def set_verbose_mode(self, enabled: bool) -> Result:
        """Toggle verbose mode: when on, item/completed notifications are shown."""
        self._verbose_enabled = bool(enabled)
        return ok(verbose=self._verbose_enabled)

    def verbose_mode(self) -> bool:
        return self._verbose_enabled

    def get_default_model(self) -> str:
        return self._default_model

    def known_model_ids(self, *, include_hidden: bool = True) -> Result:
        """Flat set of model identifiers from list_models, considering both
        the `id` and `model` fields each entry may carry."""
        listed = self.list_models(include_hidden=include_hidden)
        if not listed["ok"]:
            return listed
        ids: set[str] = set()
        for item in listed.get("data") or []:
            if not isinstance(item, dict):
                continue
            for key in ("id", "model"):
                value = str(item.get(key) or "").strip()
                if value:
                    ids.add(value)
        return ok(ids=ids)

    def set_default_model(self, model: str) -> Result:
        normalized = (model or "").strip()
        if not normalized:
            return err("model id is required")

        known = self.known_model_ids()
        if known["ok"]:
            available = known["ids"]
            if available and normalized not in available:
                logger.warning(
                    "codex bridge: model %r not in provider list; setting anyway",
                    normalized,
                )
        else:
            logger.warning(
                "codex bridge: list_models failed (%s); setting %r without validation",
                known.get("error"), normalized,
            )

        self._default_model = normalized
        return ok(model=normalized)

    def list_pending_approvals(self) -> list:
        return [
            {"task_id": p.task_id, "command": p.command[:120], "reason": p.reason}
            for p in self._pending_approvals.values()
        ]

    def resolve_approval(self, task_id: str, decision: str) -> Result:
        pending = self._pending_approvals.pop(task_id, None)
        if pending is None:
            return err(f"no pending approval for task `{task_id}`")

        if pending.approval_type == "elicitation":
            action = "accept" if decision == "accept" else "decline"
            payload = {"action": action, "content": None}
        else:
            payload = {"decision": decision}

        send = self._run_sync(
            self._ws_send(json.dumps({"jsonrpc": "2.0", "id": pending.rpc_id, "result": payload})),
            timeout=SHORT_RPC_TIMEOUT,
        )
        if not send["ok"]:
            return send
        return ok(decision=decision)

    def list_tasks(self) -> Result:
        """Call thread/list on the server. Returns ``{ok, data: [...]}`` on success."""
        started = self.ensure_started()
        if not started["ok"]:
            return started

        rpc = self._run_sync(self._rpc("thread/list", wire.ThreadListParams(), timeout=RPC_TIMEOUT))
        if not rpc["ok"]:
            return rpc
        server_data = rpc["result"] or {}
        return ok(data=server_data.get("data") or [])

    def list_models(
        self,
        *,
        include_hidden: bool = False,
        limit: Optional[int] = None,
    ) -> Result:
        """List models for the configured provider.

        Prefers the provider's own /v1/models endpoint (works for LiteLLM,
        Ollama, LM Studio, etc.) since codex's `model/list` returns its
        bundled OpenAI catalog regardless of `model_provider` (see TODO in
        codex-rs/models-manager: cache eligibility doesn't include provider
        identity). Falls back to `model/list` when the provider didn't come
        through `config/read` (e.g. plain OpenAI).
        """
        started = self.ensure_started()
        if not started["ok"]:
            return started

        if self._provider_base_url:
            direct = self._fetch_provider_models(self._provider_base_url, self._provider_env_key)
            if direct["ok"]:
                return direct
            logger.warning(
                "codex bridge: provider /v1/models fetch failed (%s); falling back to model/list",
                direct.get("error"),
            )

        cursor = None
        models = []

        while True:
            rpc = self._run_sync(
                self._rpc(
                    "model/list",
                    wire.ModelListParams(
                        cursor=cursor,
                        includeHidden=include_hidden or None,
                        limit=limit,
                    ),
                    timeout=RPC_TIMEOUT,
                )
            )
            if not rpc["ok"]:
                return rpc

            payload = rpc["result"] or {}
            models.extend(payload.get("data") or [])
            cursor = payload.get("nextCursor")
            if not cursor:
                break

        return ok(data=models)

    def _fetch_provider_models(self, base_url: str, env_key: Optional[str]) -> Result:
        """GET {base_url}/models against the configured provider directly."""
        import urllib.error
        import urllib.request

        url = base_url.rstrip("/") + "/models"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        if env_key:
            token = os.environ.get(env_key, "").strip()
            if token:
                req.add_header("Authorization", f"Bearer {token}")

        try:
            with urllib.request.urlopen(req, timeout=PROVIDER_HTTP_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            return err(f"GET {url}: {exc}")

        raw = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return err(f"unexpected /models payload from {url}")

        # Map OpenAI-style {id, object, owned_by} → codex Model-shape entries
        # so /codex models renders the same way as the model/list path.
        normalized = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            mid = (item.get("id") or "").strip()
            if not mid:
                continue
            normalized.append({
                "id": mid,
                "model": mid,
                "displayName": item.get("display_name") or "",
                "isDefault": False,
            })
        return ok(data=normalized)

    def remove_task(self, task_id: str) -> Result:
        started = self.ensure_started()
        if not started["ok"]:
            return started

        thread_id = self._task_map.get(task_id)
        if not thread_id:
            return err(f"unknown task/thread id {task_id!r}")

        archived = self._run_sync(
            self._rpc("thread/archive", wire.ThreadArchiveParams(threadId=thread_id), timeout=RPC_TIMEOUT),
        )
        if not archived["ok"]:
            return archived

        self._task_map.pop(task_id, None)
        self._threads.pop(thread_id, None)
        return ok(task_id=task_id)

    def remove_all_tasks(self) -> Result:
        task_ids = list(self._task_map.keys())
        errors = []
        for task_id in task_ids:
            result = self.remove_task(task_id)
            if not result["ok"]:
                errors.append(f"{task_id}: {result['error']}")
        return {
            "ok": not errors,
            "removed": len(task_ids) - len(errors),
            "errors": errors,
        }

    def archive_all_threads(self) -> Result:
        started = self.ensure_started()
        if not started["ok"]:
            return {"ok": False, "removed": 0, "errors": [started["error"]]}

        listed = self.list_tasks()
        if not listed["ok"]:
            return {"ok": False, "removed": 0, "errors": [listed["error"]]}

        errors, removed = [], 0
        for t in listed["data"]:
            thread_id = t.get("id") or ""
            if not thread_id:
                continue
            archived = self._run_sync(
                self._rpc("thread/archive", wire.ThreadArchiveParams(threadId=thread_id), timeout=RPC_TIMEOUT),
            )
            if archived["ok"]:
                removed += 1
            else:
                errors.append(f"{thread_id}: {archived['error']}")

        self._task_map.clear()
        self._threads.clear()
        return {"ok": not errors, "removed": removed, "errors": errors}

    def revive_task(
        self,
        thread_id: str,
        *,
        target: Optional[TaskTarget] = None,
        session_key: str = "",
        sandbox_policy: str = DEFAULT_SANDBOX_POLICY,
        approval_policy: str = DEFAULT_APPROVAL_POLICY,
    ) -> Result:
        """Re-attach a known thread_id as a task in this session.

        The Codex server does not echo prior-turn policies on ``thread/read``
        (they're per-turn overrides), so the caller must supply whatever
        ``sandbox_policy`` / ``approval_policy`` should apply to
        subsequent replies. Omit to fall back to plugin defaults.

        Plan collaboration mode is **not** per-task — it's a session-wide
        toggle managed via ``set_plan_mode`` (`/codex plan on|off`).
        """
        started = self.ensure_started()
        if not started["ok"]:
            return started

        if thread_id in self._threads:
            existing = self._threads[thread_id]
            return ok(task_id=existing.task_id, thread_id=thread_id,
                      message="thread already tracked")

        read = self._run_sync(
            self._rpc("thread/read", wire.ThreadReadParams(threadId=thread_id), timeout=RPC_TIMEOUT),
        )
        if not read["ok"]:
            return err(f"thread {thread_id!r} not found: {read['error']}")

        thread_obj = (read["result"] or {}).get("thread") or {}
        if not thread_obj.get("id"):
            return err(f"thread {thread_id!r} not found on server")
        cwd = thread_obj.get("cwd") or ""

        status = thread_obj.get("status") or {}
        if status.get("type") == "notLoaded":
            resumed = self._run_sync(
                self._rpc("thread/resume", wire.ThreadResumeParams(threadId=thread_id), timeout=RPC_TIMEOUT),
            )
            if not resumed["ok"]:
                return err(f"thread/resume failed: {resumed['error']}")

        task_id = _new_task_id()
        self._task_map[task_id] = thread_id
        self._threads[thread_id] = _PendingThread(
            thread_id=thread_id, task_id=task_id, session_key=session_key,
            cwd=cwd, sandbox_policy=sandbox_policy,
            approval_policy=approval_policy, target=target,
        )
        return ok(task_id=task_id, thread_id=thread_id, model=self._default_model)


# ==================================================================
# Module-level helpers
# ==================================================================

def _extract_thread_id(obj: Any) -> str:
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


def _new_task_id() -> str:
    import secrets
    return secrets.token_hex(4)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
