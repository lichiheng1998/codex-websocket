"""WebSocket message handlers for the codex-websocket bridge.

All inbound frames are first parsed via ``wire.parse_incoming`` into pydantic
objects; business dispatch then runs on ``method.value`` string matches. This
keeps schema validation at the boundary while avoiding ``isinstance`` ladders
for the 50+ notification/request variants.

For ``item/completed`` the nested item union is unwrapped via ``item.root.type``
(another enum value match). In both cases the handler accesses typed fields,
not bare dicts.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine, Dict, Optional, TYPE_CHECKING

from . import wire

if TYPE_CHECKING:
    from .bridge import _PendingThread, _PendingInput, TaskTarget

logger = logging.getLogger(__name__)

MAX_NOTIFY_TEXT = 4000
MAX_COMMAND_OUTPUT = 1000
MAX_ELICITATION_SCHEMA_PREVIEW = 300
MAX_APPROVAL_CMD_PREVIEW = 200


class MessageHandler:
    """Handles all messages arriving from the Codex WebSocket server.

    Same external contract as the legacy handler — bridge injects shared state
    + ``ws_send`` + ``notify`` coroutines. Internally we rely on the pydantic
    parse done in ``bridge._reader_loop`` so all params are typed objects.
    """

    def __init__(
        self,
        pending_rpc: Dict[int, asyncio.Future],
        threads: Dict[str, "_PendingThread"],
        pending_inputs: Dict[str, "_PendingInput"],
        pending_approvals: Dict[str, Any],
        task_map: Dict[str, str],
        ws_send: Callable[[str], Coroutine],
        notify: Callable[["TaskTarget", str], Coroutine],
        is_verbose: Callable[[], bool] = lambda: False,
    ) -> None:
        self._pending_rpc = pending_rpc
        self._threads = threads
        self._pending_inputs = pending_inputs
        self._pending_approvals = pending_approvals
        self._task_map = task_map
        self._ws_send = ws_send
        self._notify = notify
        self._is_verbose = is_verbose

    # ------------------------------------------------------------------
    # Top-level dispatch
    # ------------------------------------------------------------------

    async def dispatch(self, raw: dict) -> None:
        kind, parsed, _ = wire.parse_incoming(raw)

        if kind == "response":
            self._resolve_rpc(parsed.id.root, result=parsed.result)
            return
        if kind == "error":
            self._resolve_rpc(parsed.id.root, error=parsed.error)
            return
        if kind == "request":
            await self._on_server_request(parsed, raw)
            return
        if kind == "notification":
            await self._on_server_notification(parsed)
            return
        logger.debug("codex bridge: unparseable frame dropped")

    def _resolve_rpc(self, rpc_id: Any, *, result: Any = None, error: Any = None) -> None:
        # RequestId may be a RootModel wrapping str|int; bridge keys by int.
        key = rpc_id
        fut = self._pending_rpc.get(key)
        if fut is None and isinstance(rpc_id, int):
            fut = self._pending_rpc.get(str(rpc_id))
        if fut is None or fut.done():
            return
        if error is not None:
            fut.set_exception(RuntimeError(f"{error.code}: {error.message}"))
        else:
            fut.set_result(result)

    # ------------------------------------------------------------------
    # Server → client requests (approvals, elicitation, user input)
    # ------------------------------------------------------------------

    async def _on_server_request(self, req: Any, raw: dict) -> None:
        """``req`` is one of ServerRequest1..9 — has .id, .method, .params."""
        method = req.method.value
        rpc_id = req.id.root

        match method:
            case "item/commandExecution/requestApproval":
                await self._handle_command_approval(req.params, rpc_id)
            case "item/fileChange/requestApproval":
                await self._handle_file_change_approval(req.params, rpc_id)
            case "item/permissions/requestApproval":
                await self._handle_permissions_approval(req.params, rpc_id)
            case "item/tool/requestUserInput":
                await self._handle_user_input_request(req.params, rpc_id)
            case "mcpServer/elicitation/request":
                await self._handle_elicitation_request(req.params, rpc_id)
            case "execCommandApproval" | "applyPatchApproval":
                # Legacy approval shapes — route to command approval handler.
                await self._handle_command_approval(req.params, rpc_id)
            case _:
                logger.debug("codex bridge: unhandled server request %s", method)
                # Decline by default so the server isn't left hanging.
                await self._ws_send(json.dumps({
                    "jsonrpc": "2.0", "id": rpc_id,
                    "error": {"code": -32601, "message": f"unhandled: {method}"},
                }))

    # ------------------------------------------------------------------
    # Server → client notifications
    # ------------------------------------------------------------------

    async def _on_server_notification(self, notif: Any) -> None:
        method = notif.method.value
        params = notif.params

        match method:
            case "item/agentMessage/delta":
                # Streaming token — currently only used to keep threads "warm".
                return

            case "item/completed":
                pt = self._threads.get(params.threadId)
                if pt is not None:
                    asyncio.create_task(self._safe(self._on_item_completed(pt, params)))

            case "turn/completed":
                pt = self._threads.get(params.threadId)
                if pt is not None:
                    asyncio.create_task(self._safe(self._on_turn_completed(pt, params)))

            case _:
                # Thread/started, turn/started, token/usage, plan/updated, etc.
                # Not surfaced to the user — logged for debugging only.
                logger.debug("codex bridge: notification %s ignored", method)

    @staticmethod
    async def _safe(coro) -> None:
        try:
            await coro
        except Exception as exc:
            logger.warning("codex handler task failed: %s", exc)

    # ------------------------------------------------------------------
    # item/completed → per-item formatter
    # ------------------------------------------------------------------

    async def _on_item_completed(self, pt: "_PendingThread", params: Any) -> None:
        """``params`` is ItemCompletedNotification with ``item: ThreadItem``."""
        item = params.item.root  # unwrap the ThreadItem union
        item_type = getattr(getattr(item, "type", None), "value", None) or getattr(item, "type", "")

        # agentMessage (final reply) is always sent; other items need verbose on.
        if item_type != "agentMessage" and not self._is_verbose():
            return

        match item_type:
            case "agentMessage":
                await self._on_agent_message(pt, item)
            case "plan":
                await self._on_plan(pt, item)
            case "commandExecution":
                await self._on_command_execution(pt, item)
            case "fileChange":
                await self._on_file_change(pt, item)
            case "webSearch":
                await self._on_web_search(pt, item)
            case "enteredReviewMode":
                await self._on_entered_review_mode(pt, item)
            case "exitedReviewMode":
                await self._on_exited_review_mode(pt, item)
            case "contextCompaction":
                await self._notify(pt.target, f"🗜️ `{pt.task_id}` context compacted")
            case _:
                logger.debug("codex bridge: item type %r ignored", item_type)

    async def _on_agent_message(self, pt: "_PendingThread", item: Any) -> None:
        text = (getattr(item, "text", "") or "").strip()
        if not text:
            return
        prefix = f"🤖 `{pt.task_id}`\n\n"
        max_text = MAX_NOTIFY_TEXT - len(prefix)
        if len(text) > max_text:
            text = text[:max_text] + "\n…(truncated)"
        await self._notify(pt.target, prefix + text)

    async def _on_plan(self, pt: "_PendingThread", item: Any) -> None:
        text = (getattr(item, "text", "") or "").strip()
        if text:
            await self._notify(pt.target, f"📋 `{pt.task_id}` plan\n\n{text}")

    async def _on_command_execution(self, pt: "_PendingThread", item: Any) -> None:
        cmd = (getattr(item, "command", "") or "").strip()
        exit_code = getattr(item, "exitCode", None)
        output = (getattr(item, "aggregatedOutput", "") or "").strip()
        icon = "✅" if exit_code == 0 else "❌"
        lines = [f"{icon} `{cmd}` (exit {exit_code})"]
        if output:
            lines.append(f"```\n{output[:MAX_COMMAND_OUTPUT]}\n```")
        await self._notify(pt.target, "\n".join(lines))

    async def _on_file_change(self, pt: "_PendingThread", item: Any) -> None:
        changes = getattr(item, "changes", None) or []
        if not changes:
            return
        lines = [f"📝 `{pt.task_id}` file changes"]
        for c in changes:
            path = getattr(c, "path", None) or "?"
            kind = getattr(c, "kind", None) or "modify"
            kind_value = getattr(kind, "value", kind)
            icon = {"create": "➕", "delete": "➖"}.get(kind_value, "✏️")
            lines.append(f"  {icon} `{path}`")
        await self._notify(pt.target, "\n".join(lines))

    async def _on_web_search(self, pt: "_PendingThread", item: Any) -> None:
        query = (getattr(item, "query", "") or "").strip()
        if query:
            await self._notify(pt.target, f"🔍 `{pt.task_id}` search: {query}")

    async def _on_entered_review_mode(self, pt: "_PendingThread", item: Any) -> None:
        review = (getattr(item, "review", "") or "").strip()
        await self._notify(pt.target, f"👁️ `{pt.task_id}` entered review mode: {review}")

    async def _on_exited_review_mode(self, pt: "_PendingThread", item: Any) -> None:
        review = (getattr(item, "review", "") or "").strip()
        msg = f"👁️ `{pt.task_id}` review complete"
        if review:
            msg += f"\n\n{review}"
        await self._notify(pt.target, msg)

    # ------------------------------------------------------------------
    # turn/completed
    # ------------------------------------------------------------------

    async def _on_turn_completed(self, pt: "_PendingThread", params: Any) -> None:
        turn = params.turn
        status = getattr(turn.status, "value", turn.status)

        if status == "failed":
            err = turn.error
            msg_text = getattr(err, "message", "") or "unknown error"
            code = getattr(err, "codexErrorInfo", None)
            code_str = getattr(code, "value", code) if code else ""
            error = f"{msg_text} ({code_str})" if code_str else msg_text
            await self._notify(pt.target, f"❌ Codex task `{pt.task_id}` failed: {error}")

        elif status == "interrupted":
            await self._notify(pt.target, f"⏱️ Codex task `{pt.task_id}` interrupted")

        else:
            await self._notify(
                pt.target,
                f"✅ Codex task `{pt.task_id}` completed\n"
                f"Continue: `/codex reply {pt.task_id} <message>`",
            )

    # ------------------------------------------------------------------
    # Approval requests — stash a _PendingApproval, notify user, wait for
    # /codex approve|deny to call resolve_approval() on the bridge.
    # ------------------------------------------------------------------

    async def _handle_command_approval(self, params: Any, rpc_id: Any) -> None:
        thread_id = getattr(params, "threadId", None) or getattr(params, "conversationId", None)
        pt = self._threads.get(thread_id) if thread_id else None
        reason = (getattr(params, "reason", "") or "").strip() or "Codex approval"
        command = getattr(params, "command", None) or getattr(params, "commandText", None) or ""
        if isinstance(command, list):
            command = " ".join(str(x) for x in command)
        cmd_preview = (str(command) or "(codex command)")[:MAX_APPROVAL_CMD_PREVIEW]

        target = pt.target if pt else None
        task_id = pt.task_id if pt else "?"
        notify_text = (
            f"⚠️ Codex task `{task_id}` requests to run a command:\n"
            f"```\n{cmd_preview}\n```\n"
            f"Reason: {reason}\n\n"
            f"Approve: `/codex approve {task_id}`\n"
            f"Deny: `/codex deny {task_id}`"
        )
        self._stash_approval(task_id, rpc_id, str(command) or "(codex command)", reason, target, "command")
        await self._notify(target, notify_text)

    async def _handle_file_change_approval(self, params: Any, rpc_id: Any) -> None:
        # fileChange approvals share the "command" response shape in our bridge
        # — we just render the change set differently.
        thread_id = getattr(params, "threadId", None)
        pt = self._threads.get(thread_id) if thread_id else None
        reason = (getattr(params, "reason", "") or "").strip() or "Codex file change"
        target = pt.target if pt else None
        task_id = pt.task_id if pt else "?"

        change = getattr(params, "fileChange", None)
        preview = str(change)[:MAX_APPROVAL_CMD_PREVIEW] if change else "(file change)"

        notify_text = (
            f"⚠️ Codex task `{task_id}` requests file changes:\n"
            f"```\n{preview}\n```\n"
            f"Reason: {reason}\n\n"
            f"Approve: `/codex approve {task_id}`\n"
            f"Deny: `/codex deny {task_id}`"
        )
        self._stash_approval(task_id, rpc_id, preview, reason, target, "command")
        await self._notify(target, notify_text)

    async def _handle_permissions_approval(self, params: Any, rpc_id: Any) -> None:
        thread_id = getattr(params, "threadId", None)
        pt = self._threads.get(thread_id) if thread_id else None
        reason = (getattr(params, "reason", "") or "").strip() or "Codex permissions"
        target = pt.target if pt else None
        task_id = pt.task_id if pt else "?"

        perms = getattr(params, "permissions", None)
        fs = getattr(perms, "fileSystem", None) if perms else None
        writes = getattr(fs, "write", None) if fs else None
        net = getattr(perms, "network", None) if perms else None
        parts = []
        if writes:
            parts.append("Write paths: " + ", ".join(f"`{getattr(p, 'root', p)}`" for p in writes))
        if net and getattr(net, "enabled", False):
            parts.append("Network access")
        preview = "\n".join(parts) or "(no details)"

        notify_text = (
            f"⚠️ Codex task `{task_id}` requests permissions:\n{preview}\n"
            f"Reason: {reason}\n\n"
            f"Approve: `/codex approve {task_id}`\n"
            f"Deny: `/codex deny {task_id}`"
        )
        self._stash_approval(task_id, rpc_id, preview, reason, target, "permissions")
        await self._notify(target, notify_text)

    async def _handle_elicitation_request(self, params: Any, rpc_id: Any) -> None:
        inner = params.root if hasattr(params, "root") else params
        thread_id = getattr(inner, "threadId", None)
        pt = self._threads.get(thread_id) if thread_id else None
        server_name = getattr(inner, "serverName", None) or "MCP server"
        target = pt.target if pt else None
        task_id = pt.task_id if pt else "?"

        elicitation = getattr(inner, "elicitation", None)
        mode = getattr(getattr(elicitation, "mode", None), "value", "form") if elicitation else "form"
        elicit_msg = getattr(elicitation, "message", "") if elicitation else ""

        if mode == "url":
            url = getattr(elicitation, "url", "") or ""
            notify_text = (
                f"🔗 `{task_id}` MCP `{server_name}` needs you to visit a link:\n{url}\n"
                f"{elicit_msg}\n\n"
                f"When done: `/codex approve {task_id}`\n"
                f"Cancel: `/codex deny {task_id}`"
            )
        else:
            schema = getattr(elicitation, "requestedSchema", None) if elicitation else None
            schema_json = json.dumps(
                schema.model_dump(mode="json") if hasattr(schema, "model_dump") else (schema or {}),
                ensure_ascii=False,
            )[:MAX_ELICITATION_SCHEMA_PREVIEW]
            notify_text = (
                f"❓ `{task_id}` MCP `{server_name}` requests input:\n{elicit_msg}\n"
                f"Schema: `{schema_json}`\n\n"
                f"Accept: `/codex approve {task_id}`\n"
                f"Decline: `/codex deny {task_id}`"
            )

        self._stash_approval(
            task_id, rpc_id, f"MCP elicitation: {server_name}", elicit_msg, target, "elicitation",
        )
        await self._notify(target, notify_text)

    def _stash_approval(
        self, task_id: str, rpc_id: Any, command: str,
        reason: str, target: Optional["TaskTarget"], approval_type: str,
    ) -> None:
        from .bridge import _PendingApproval
        self._pending_approvals[task_id] = _PendingApproval(
            rpc_id=rpc_id,
            task_id=task_id,
            command=command,
            reason=reason,
            target=target,
            approval_type=approval_type,
        )

    # ------------------------------------------------------------------
    # requestUserInput (server asks the user a question — deferred RPC reply)
    # ------------------------------------------------------------------

    async def _handle_user_input_request(self, params: Any, rpc_id: Any) -> None:
        from .bridge import _PendingInput

        thread_id = getattr(params, "threadId", "")
        questions = getattr(params, "questions", None) or []
        pt = self._threads.get(thread_id) if thread_id else None

        if pt is None:
            logger.warning(
                "codex requestUserInput for unknown thread %s — denying", thread_id)
            await self._ws_send(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32000, "message": "no active task for thread"},
            }))
            return

        self._pending_inputs[pt.task_id] = _PendingInput(
            task_id=pt.task_id,
            thread_id=thread_id,
            rpc_id=rpc_id,
            questions=questions,
        )

        lines = [f"🤔 Codex task `{pt.task_id}` needs you to answer:"]
        for idx, q in enumerate(questions, 1):
            question_text = getattr(q, "question", "") or ""
            lines.append(f"\n{idx}. {question_text}")
            if getattr(q, "isOther", False):
                lines.append("   _(free-text reply accepted)_")
        lines.append(f"\nReply with: `/codex reply {pt.task_id} <your answer>`")
        await self._notify(pt.target, "\n".join(lines))

    async def submit_input_answers(
        self, task_id: str, answers: list,
    ) -> Dict[str, Any]:
        pending = self._pending_inputs.pop(task_id, None)
        if pending is None:
            return {"ok": False, "error": f"no pending input for task {task_id}"}

        if len(answers) != len(pending.questions):
            if len(pending.questions) == 1 and answers:
                joined = " ".join(
                    " / ".join(str(a) for a in x) if isinstance(x, list) else str(x)
                    for x in answers
                )
                answers = [joined]
            else:
                self._pending_inputs[task_id] = pending
                return {
                    "ok": False,
                    "error": (
                        f"expected {len(pending.questions)} answers, "
                        f"got {len(answers)}"
                    ),
                }

        flat_responses = [
            " / ".join(str(a) for a in ans) if isinstance(ans, list) else str(ans)
            for ans in answers
        ]

        await self._ws_send(json.dumps({
            "jsonrpc": "2.0",
            "id": pending.rpc_id,
            "result": {"responses": flat_responses},
        }))
        return {"ok": True, "task_id": task_id}
