"""Integration tests for CodexBridge against a fake codex app-server.

These exercise the real bridge code (lifecycle, RPC pairing, drivers,
approval handling, reader_loop dispatch) but with a Python WebSocket
server in place of the real codex subprocess. Spawned via the
``ws_url`` constructor seam so _spawn_server is a no-op.

The fake server runs in its own thread with its own asyncio loop. The
bridge runs its own loop in its own thread (production code path).
Tests synchronise on the bridge's sync API and on the
notifications captured by the in-test ``notify`` callback.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Dict, List, Tuple

import pytest

from codex_websocket import bridge as bridge_mod
from codex_websocket.bridge import CodexBridge

from fake_codex_server import FakeCodexServer


# ── fake server fixture (runs in a side thread with its own loop) ──────────

class _ServerThread:
    """Wrap FakeCodexServer in a background thread with its own loop.

    Tests call ``configure(method, handler)`` from the test thread and the
    handler runs on the server's loop when a request arrives.
    """

    def __init__(self) -> None:
        self.server = FakeCodexServer()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self.server.start())
            self._loop_ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True, name="fake-codex-server")
        self._thread.start()
        assert self._loop_ready.wait(timeout=5.0), "fake server thread didn't start"

    def stop(self) -> None:
        if self._loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self.server.stop(), self._loop)
        fut.result(timeout=5.0)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)

    def submit(self, coro):
        """Schedule a coroutine on the server loop and wait for the result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=5.0)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.server.port}"


@pytest.fixture
def fake_server():
    s = _ServerThread()
    s.start()
    # Default handlers required by bridge.ensure_started()'s startup chain.
    s.server.on("config/read", lambda _: {
        "config": {"model": "test-model", "model_provider": None},
    })
    yield s
    s.stop()


@pytest.fixture
def bridge_factory(fresh_bridge_class, fake_server):
    """Factory that builds a CodexBridge wired to the fake server. The
    test owns shutdown so we can assert post-shutdown invariants."""
    created: list[CodexBridge] = []

    def _build() -> CodexBridge:
        b = CodexBridge(ws_url=fake_server.url)
        created.append(b)
        return b

    yield _build
    for b in created:
        try:
            b.shutdown()
        except Exception:
            pass


# ── notify capture ─────────────────────────────────────────────────────────

@pytest.fixture
def captured_notifications(monkeypatch):
    """Replace notify_user with a recorder so assertions can inspect what
    the bridge would have sent to the user.

    ``notify_user`` is referenced as a free function inside bridge.py
    after the refactor, so we patch the binding on the bridge module.
    """
    captured: List[Tuple[Any, str]] = []

    async def fake_notify(target, message):
        captured.append((target, message))

    monkeypatch.setattr(bridge_mod, "notify_user", fake_notify)
    # report_failure imports notify_user from .notify, so patch there too.
    from codex_websocket import notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify_user", fake_notify)
    return captured


# ── ensure_started ─────────────────────────────────────────────────────────

class TestEnsureStarted:
    def test_handshake_runs_initialize_then_config_read(self, fake_server, bridge_factory):
        b = bridge_factory()
        res = b.ensure_started()
        assert res["ok"]
        # Bridge-issued requests, in order. config/read must come after
        # initialize (it's part of _sync_config_from_server).
        methods = fake_server.server.methods_received()
        assert "initialize" in methods
        assert "config/read" in methods
        assert methods.index("initialize") < methods.index("config/read")
        # The bridge also sends an "initialized" notification (no id).
        notifications = [m for m in fake_server.server.received if "id" not in m]
        assert any(n.get("method") == "initialized" for n in notifications)

    def test_default_model_comes_from_config_read(self, fake_server, bridge_factory):
        fake_server.server.on("config/read", lambda _: {
            "config": {
                "model": "mimo-v2.5-pro",
                "model_provider": "litellm",
                "model_providers": {
                    "litellm": {"base_url": "http://x/v1", "env_key": "K"},
                },
            },
        })
        b = bridge_factory()
        assert b.ensure_started()["ok"]
        assert b.get_default_model() == "mimo-v2.5-pro"
        assert b._provider.id == "litellm"
        assert b._provider.base_url == "http://x/v1"
        assert b._provider.env_key == "K"

    def test_idempotent_second_call_is_no_op(self, fake_server, bridge_factory):
        b = bridge_factory()
        assert b.ensure_started()["ok"]
        before = len(fake_server.server.received)
        assert b.ensure_started()["ok"]
        # No new frames went over the wire.
        assert len(fake_server.server.received) == before


# ── start_task → turn/start path ───────────────────────────────────────────

class TestStartTask:
    def test_thread_start_then_turn_start_called_with_user_prompt(
        self, fake_server, bridge_factory, captured_notifications, tmp_path,
    ):
        thread_id = "thr-deadbeef-0001"
        fake_server.server.on(
            "thread/start", lambda _: {"thread": {"id": thread_id}},
        )
        # turn/start succeeds — the bridge fire-and-forgets the rest.
        fake_server.server.on("turn/start", lambda _: {"turnId": "turn-x"})

        b = bridge_factory()
        assert b.ensure_started()["ok"]

        cwd = str(tmp_path)
        result = b.start_task(cwd=cwd, prompt="hello world")
        assert result["ok"]
        task_id = result["task_id"]

        # Wait for the background driver to finish thread/start + turn/start.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if fake_server.server.find_request("turn/start") is not None:
                break
            time.sleep(0.05)
        else:
            pytest.fail("turn/start was never called")

        # Bridge tracks the task → thread mapping.
        assert b._task_map[task_id] == thread_id

        # The user got the "task started" notification.
        msgs = [m for _, m in captured_notifications]
        assert any(f"`{task_id}`" in m and "started" in m for m in msgs)

        # turn/start carried the prompt and the configured cwd.
        turn_req = fake_server.server.find_request("turn/start")
        params = turn_req["params"]
        assert params["threadId"] == thread_id
        assert params["input"][0]["text"] == "hello world"


# ── approval round-trip ────────────────────────────────────────────────────

class TestApprovalRoundTrip:
    def test_command_approval_records_pending_then_resolves(
        self, fake_server, bridge_factory, captured_notifications, tmp_path,
    ):
        thread_id = "thr-deadbeef-0002"
        fake_server.server.on("thread/start", lambda _: {"thread": {"id": thread_id}})
        fake_server.server.on("turn/start", lambda _: {"turnId": "turn-y"})

        b = bridge_factory()
        assert b.ensure_started()["ok"]
        result = b.start_task(cwd=str(tmp_path), prompt="please run a thing")
        task_id = result["task_id"]

        # Wait for the bridge to be in a state where the thread is tracked.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if thread_id in b._threads:
                break
            time.sleep(0.05)
        else:
            pytest.fail("bridge never registered the started thread")

        # Server pushes a command-execution approval request. The required
        # fields per the schema are itemId/threadId/turnId; everything
        # else is optional.
        APPROVAL_RPC_ID = 9001
        fake_server.submit(fake_server.server.send_request(
            "item/commandExecution/requestApproval",
            {
                "itemId": "item-1",
                "threadId": thread_id,
                "turnId": "turn-y",
                "command": "rm -rf /tmp/foo",
                "reason": "cleanup",
            },
            APPROVAL_RPC_ID,
        ))

        # Bridge surfaces the approval to the user via notify and stores
        # a _PendingApproval keyed by the task id.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if task_id in b._pending_approvals:
                break
            time.sleep(0.05)
        else:
            pytest.fail("bridge never registered the approval")

        # The user-visible message includes the command preview and footer.
        msgs = [m for _, m in captured_notifications]
        assert any("rm -rf /tmp/foo" in m for m in msgs)
        assert any(f"/codex approve {task_id}" in m for m in msgs)

        # User approves; bridge sends back the JSON-RPC response.
        res = b.resolve_approval(task_id, "accept")
        assert res["ok"]
        assert task_id not in b._pending_approvals

        # Server saw the response frame for our approval rpc id.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            for m in fake_server.server.received:
                if m.get("id") == APPROVAL_RPC_ID and "result" in m:
                    assert m["result"] == {"decision": "accept"}
                    return
            time.sleep(0.05)
        pytest.fail("server never saw the approval response")


# ── set_default_model soft-validation ──────────────────────────────────────

class TestSetDefaultModel:
    def test_soft_validation_warns_but_succeeds_for_unknown_model(
        self, fake_server, bridge_factory, caplog,
    ):
        # Provider with HTTP path disabled (no base_url) → list_models
        # falls back to model/list, which returns a known set.
        fake_server.server.on("config/read", lambda _: {
            "config": {"model": "known", "model_provider": None},
        })
        fake_server.server.on("model/list", lambda _: {
            "data": [{"id": "known"}], "nextCursor": None,
        })

        b = bridge_factory()
        assert b.ensure_started()["ok"]

        # Setting an unknown model: warning logged, but state updated.
        with caplog.at_level("WARNING", logger="codex_websocket.bridge"):
            res = b.set_default_model("brand-new-model")
        assert res["ok"]
        assert b.get_default_model() == "brand-new-model"
        assert any("brand-new-model" in r.message for r in caplog.records)
