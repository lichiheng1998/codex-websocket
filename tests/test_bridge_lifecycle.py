"""Tests for CodexBridge lifecycle edge cases.

Covers:
- Event loop thread startup timeout propagated as Result
- WebSocket not leaked when handshake fails
- Retryability after a failed ensure_started
"""

from __future__ import annotations

import asyncio
import threading
import unittest.mock as mock

import pytest

from codex_websocket.bridge import CodexBridge

from fake_codex_server import FakeCodexServer


# ── shared server fixture ─────────────────────────────────────────────────────

class _ServerThread:
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

        self._thread = threading.Thread(target=_run, daemon=True, name="fake-server-lifecycle")
        self._thread.start()
        assert self._loop_ready.wait(timeout=5.0)

    def stop(self) -> None:
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self.server.stop(), self._loop).result(timeout=5.0)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.server.port}"


@pytest.fixture
def fake_server():
    s = _ServerThread()
    s.start()
    s.server.on("config/read", lambda _: {
        "config": {"model": "test-model", "model_provider": None},
    })
    yield s
    s.stop()


@pytest.fixture
def bridge_factory(fresh_bridge_class, fake_server):
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


# ── loop thread startup timeout ───────────────────────────────────────────────

class TestLoopThreadStartup:
    def test_returns_err_when_loop_thread_never_signals_ready(self, fresh_bridge_class):
        """A thread that never calls loop_ready.set() must cause
        _start_loop_thread to return an err Result instead of silently
        continuing with self.loop still None."""
        b = CodexBridge.__new__(CodexBridge)
        CodexBridge.__init__(b)

        original = threading.Thread

        class _HungThread(original):
            def start(self):
                pass  # never runs — loop_ready.wait() will time out

        with mock.patch("codex_websocket.bridge.threading.Thread", _HungThread):
            with mock.patch("codex_websocket.bridge.LOOP_READY_TIMEOUT", 0.05):
                result = b._start_loop_thread()

        assert not result["ok"]
        assert "timeout" in result["error"].lower() or "loop" in result["error"].lower()
        assert b.loop is None

    def test_ensure_started_propagates_loop_failure(self, fresh_bridge_class):
        """ensure_started must surface the loop-start failure rather than
        proceeding and crashing with AttributeError when self.loop is None."""
        b = CodexBridge.__new__(CodexBridge)
        CodexBridge.__init__(b)

        with mock.patch.object(
            b, "_start_loop_thread", return_value={"ok": False, "error": "loop failed"}
        ):
            result = b.ensure_started()

        assert not result["ok"]
        assert "loop failed" in result["error"]


# ── WebSocket not leaked on handshake failure ─────────────────────────────────

class TestHandshakeFailureCleanup:
    def test_ws_is_none_after_initialize_rejected(self, fake_server, bridge_factory):
        """When the server rejects initialize, the bridge must close the
        WebSocket it just opened rather than leaving it dangling."""
        fake_server.server.on(
            "initialize",
            lambda _: (_ for _ in ()).throw(ValueError("server rejects initialize")),
        )

        b = bridge_factory()
        result = b.ensure_started()

        assert not result["ok"]
        assert b.ws is None

    def test_ensure_started_retryable_after_handshake_failure(
        self, fake_server, bridge_factory,
    ):
        """After a failed handshake ensure_started must be retryable —
        the next call should open a fresh connection and succeed."""
        call_count = {"n": 0}

        def flaky_initialize(_params):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise ValueError("first attempt fails")
            return {"ok": True}

        fake_server.server.on("initialize", flaky_initialize)

        b = bridge_factory()
        first = b.ensure_started()
        assert not first["ok"]
        assert b.ws is None

        second = b.ensure_started()
        assert second["ok"]
        assert b.ws is not None