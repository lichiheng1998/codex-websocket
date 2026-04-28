"""Tests for JSON-RPC response dispatch in MessageHandler.

Covers:
- _resolve_rpc matching a string id against an int-keyed Future
- _resolve_rpc normal path (int id)
- End-to-end: server sends string ids, bridge RPCs complete without timeout
"""

from __future__ import annotations

import asyncio
import json
import threading
import unittest.mock as mock

import pytest

from codex_websocket.handlers import MessageHandler

from fake_codex_server import FakeCodexServer


def _make_handler() -> MessageHandler:
    return MessageHandler(
        pending_rpc={},
        threads={},
        pending_inputs={},
        pending_approvals={},
        task_map={},
        ws_send=mock.AsyncMock(),
        notify=mock.AsyncMock(),
    )


class TestResolveRpc:
    def test_string_id_resolves_int_keyed_future(self):
        """Server echoes id as a string; bridge registered the Future with
        an int key. _resolve_rpc must find and resolve the Future."""
        handler = _make_handler()
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            handler._pending_rpc[1] = fut

            handler._resolve_rpc("1", result={"data": "ok"})

            assert fut.done()
            assert fut.result() == {"data": "ok"}
        finally:
            loop.close()

    def test_int_id_resolves_int_keyed_future(self):
        """Normal path — server sends int id, Future registered with int key."""
        handler = _make_handler()
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            handler._pending_rpc[42] = fut

            handler._resolve_rpc(42, result={"x": 1})

            assert fut.done()
            assert fut.result() == {"x": 1}
        finally:
            loop.close()

    def test_int_id_falls_back_to_string_keyed_future(self):
        """If somehow the Future was stored under a string key and the server
        sends an int id, the fallback lookup must still find it."""
        handler = _make_handler()
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            handler._pending_rpc["7"] = fut

            handler._resolve_rpc(7, result={"y": 2})

            assert fut.done()
            assert fut.result() == {"y": 2}
        finally:
            loop.close()

    def test_non_numeric_string_id_does_not_raise(self):
        """A string id that can't be parsed as int must be handled gracefully,
        not raise ValueError."""
        handler = _make_handler()
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            handler._pending_rpc[1] = fut

            # Should not raise even though "abc" can't convert to int.
            handler._resolve_rpc("abc", result={"z": 0})

            assert not fut.done()
        finally:
            loop.close()

    def test_error_response_sets_exception_on_future(self):
        """An error response must set an exception on the Future, not a result."""
        handler = _make_handler()
        loop = asyncio.new_event_loop()
        try:
            fut = loop.create_future()
            handler._pending_rpc[5] = fut

            error = mock.Mock()
            error.code = -32000
            error.message = "something went wrong"
            handler._resolve_rpc(5, error=error)

            assert fut.done()
            with pytest.raises(RuntimeError, match="something went wrong"):
                fut.result()
        finally:
            loop.close()


# ── End-to-end: string ids don't cause RPC timeout ───────────────────────────

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

        self._thread = threading.Thread(target=_run, daemon=True, name="fake-server-rpc")
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
def string_id_server():
    """A fake server that sends every response with the id serialised as
    a string rather than a number."""
    s = _ServerThread()
    s.start()
    s.server.on("config/read", lambda _: {
        "config": {"model": "test-model", "model_provider": None},
    })

    original_reply = s.server._reply_to_request

    async def _string_id_reply(ws, msg):
        method = msg["method"]
        rpc_id = msg["id"]
        handler = s.server.handlers.get(method)
        if handler is None:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": str(rpc_id),
                "error": {"code": -32601, "message": f"not handled: {method}"},
            }))
            return
        try:
            result = handler(msg.get("params") or {})
        except ValueError as exc:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": str(rpc_id),
                "error": {"code": -32000, "message": str(exc)},
            }))
            return
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": str(rpc_id), "result": result,
        }))

    s.server._reply_to_request = _string_id_reply
    yield s
    s.stop()


class TestStringIdEndToEnd:
    def test_ensure_started_succeeds_when_server_sends_string_ids(
        self, fresh_bridge_class, string_id_server,
    ):
        """If every RPC response has a string id the bridge must still
        complete the handshake instead of timing out on each call."""
        from codex_websocket.bridge import CodexBridge

        b = CodexBridge(ws_url=string_id_server.url)
        try:
            result = b.ensure_started()
            assert result["ok"], f"ensure_started failed: {result.get('error')}"
        finally:
            b.shutdown()