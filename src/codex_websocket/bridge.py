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
from typing import Dict, Optional, Union

from pydantic import BaseModel

from . import wire
from .handlers import MessageHandler
from .notify import notify_user, report_failure
from .provider import (
    ProviderInfo,
    known_ids_from_listing,
    list_models_for,
    sync_default_model,
)
from .policies import (
    DEFAULT_APPROVAL_POLICY,
    DEFAULT_MODEL,
    DEFAULT_SANDBOX_POLICY,
    LOOP_READY_TIMEOUT,
    PORT_PROBE_TIMEOUT,
    RPC_TIMEOUT,
    SHORT_RPC_TIMEOUT,
    SHUTDOWN_TIMEOUT,
    STARTUP_TIMEOUT,
    default_collaboration_mode,
    plan_collaboration_mode,
    prepare_sandbox,
)
from .state import (
    Result,
    TaskTarget,
    _PendingApproval,
    _PendingInput,
    _PendingThread,
    err,
    ok,
)
from .utils import extract_thread_id, new_task_id, pick_free_port

logger = logging.getLogger(__name__)


class CodexBridge:
    _instance: Optional["CodexBridge"] = None
    _instance_lock = threading.Lock()

    def __init__(self, *, ws_url: Optional[str] = None) -> None:
        """Construct a bridge instance.

        ``ws_url`` is a test-only injection point: when provided, the
        bridge skips spawning a ``codex app-server`` subprocess and
        connects to the given WebSocket URL instead. Production callers
        omit it and let the bridge spawn its own server.
        """
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
        # Provider info read from `config/read` so list_models_for can hit
        # the provider's own /v1/models endpoint instead of codex's bundled
        # OpenAI catalog. Empty triple until the first successful sync.
        self._provider: ProviderInfo = ProviderInfo()
        self._injected_ws_url: Optional[str] = ws_url

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
        if self._injected_ws_url:
            from urllib.parse import urlparse
            parsed = urlparse(self._injected_ws_url)
            if parsed.port is None:
                return err(f"injected ws_url missing port: {self._injected_ws_url}")
            self.port = parsed.port
            return ok()

        try:
            self.port = pick_free_port()
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
            notify=notify_user,
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
        """Pull effective config + provider triple from the app-server.

        See ``provider.sync_default_model`` for the strategy. The provider
        triple is captured even when the model lookup fails — list_models
        can use the provider's own /models endpoint independently.
        """
        result, provider = await sync_default_model(self._rpc)
        self._provider = provider
        return result

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

        task_id = new_task_id()

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
            sandboxPolicy=prepare_sandbox(sandbox_policy, cwd),
            collaborationMode=(
                plan_collaboration_mode(model)
                if self._plan_enabled
                else default_collaboration_mode(model)
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
            await report_failure(target, task_id, "thread/start failed", thread_rpc["error"])
            return

        thread_id = extract_thread_id(thread_rpc["result"])
        if not thread_id:
            await report_failure(target, task_id, "thread/start", "no thread id in response")
            return

        self._task_map[task_id] = thread_id
        self._threads[thread_id] = _PendingThread(
            thread_id=thread_id, task_id=task_id, session_key=session_key,
            cwd=cwd, sandbox_policy=sandbox_policy,
            approval_policy=approval_policy, target=target,
        )

        await notify_user(target, (
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
            await report_failure(target, task_id, "turn/start failed", turn_rpc["error"])

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
            await report_failure(None, task_id, "reply failed", "task not found")
            return

        pt = self._threads.get(thread_id)
        if not pt:
            await report_failure(None, task_id, "reply failed", "thread missing for task")
            return

        rpc = await self._rpc(
            "turn/start",
            self._build_turn_start(
                thread_id=thread_id, text=message, cwd=pt.cwd,
                sandbox_policy=pt.sandbox_policy, approval_policy=pt.approval_policy,
            ),
        )
        if not rpc["ok"]:
            await report_failure(pt.target, task_id, "reply failed", rpc["error"])

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

    def set_default_model(self, model: str) -> Result:
        normalized = (model or "").strip()
        if not normalized:
            return err("model id is required")

        listed = self.list_models(include_hidden=True)
        if listed["ok"]:
            available = known_ids_from_listing(listed)
            if available and normalized not in available:
                logger.warning(
                    "codex bridge: model %r not in provider list; setting anyway",
                    normalized,
                )
        else:
            logger.warning(
                "codex bridge: list_models failed (%s); setting %r without validation",
                listed.get("error"), normalized,
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

        send = self._run_sync(
            self._ws_send(json.dumps({
                "jsonrpc": "2.0",
                "id": pending.rpc_id,
                "result": pending.to_response_payload(decision),
            })),
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
        """List models for the configured provider — see provider.list_models_for."""
        started = self.ensure_started()
        if not started["ok"]:
            return started
        return list_models_for(
            self._provider, self._run_sync, self._rpc,
            include_hidden=include_hidden, limit=limit,
        )

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

        task_id = new_task_id()
        self._task_map[task_id] = thread_id
        self._threads[thread_id] = _PendingThread(
            thread_id=thread_id, task_id=task_id, session_key=session_key,
            cwd=cwd, sandbox_policy=sandbox_policy,
            approval_policy=approval_policy, target=target,
        )
        return ok(task_id=task_id, thread_id=thread_id, model=self._default_model)


