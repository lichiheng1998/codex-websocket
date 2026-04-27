"""A minimal in-process WebSocket server speaking just enough of the
codex app-server JSON-RPC protocol for bridge integration tests.

Tests register per-method handlers; everything else is replied to with a
``method not handled`` error so failures surface loudly. Client-bound
notifications and server→client requests can be pushed at will via
``send_notification`` / ``send_request``.

Not meant to be exhaustive — it only exists so we can drive bridge
lifecycle and RPC-pairing logic without spawning the real codex binary.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable, Dict, List, Optional

import websockets


HandlerFn = Callable[[Dict[str, Any]], Any]


class FakeCodexServer:
    def __init__(self) -> None:
        self.handlers: Dict[str, HandlerFn] = {}
        self.received: List[Dict[str, Any]] = []
        self.port: Optional[int] = None
        self._server: Optional[websockets.WebSocketServer] = None
        self._connected: asyncio.Event = asyncio.Event()
        self._client_ws = None
        # Default handlers — required for bridge.ensure_started() to
        # succeed without bespoke setup in every test.
        self.on("initialize", lambda _params: {"ok": True})

    # ── Setup ──────────────────────────────────────────────────────────

    def on(self, method: str, handler: HandlerFn) -> None:
        """Register a handler returning the ``result`` field of a
        JSON-RPC response. If the handler raises ``ValueError`` we send
        a JSON-RPC error response instead.
        """
        self.handlers[method] = handler

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handle, "127.0.0.1", 0,
            ping_interval=None,  # don't fight bridge's own ping policy
        )
        sock = next(iter(self._server.sockets))
        self.port = sock.getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ── Server ↔ bridge plumbing ───────────────────────────────────────

    async def _handle(self, ws) -> None:
        self._client_ws = ws
        self._connected.set()
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self.received.append(msg)
                # Notifications (no id) — record only.
                if "method" in msg and "id" in msg:
                    await self._reply_to_request(ws, msg)
        except websockets.ConnectionClosed:
            pass

    async def _reply_to_request(self, ws, msg: Dict[str, Any]) -> None:
        method = msg["method"]
        rpc_id = msg["id"]
        handler = self.handlers.get(method)
        if handler is None:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32601, "message": f"method not handled: {method}"},
            }))
            return
        try:
            result = handler(msg.get("params") or {})
        except ValueError as exc:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32000, "message": str(exc)},
            }))
            return
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": rpc_id, "result": result,
        }))

    # ── Push messages out to the connected bridge ──────────────────────

    async def wait_connected(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    async def send_notification(self, method: str, params: Dict[str, Any]) -> None:
        assert self._client_ws is not None, "no client connected"
        await self._client_ws.send(json.dumps({
            "jsonrpc": "2.0", "method": method, "params": params,
        }))

    async def send_request(self, method: str, params: Dict[str, Any], rpc_id: int) -> None:
        assert self._client_ws is not None, "no client connected"
        await self._client_ws.send(json.dumps({
            "jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params,
        }))

    # ── Convenience accessors ──────────────────────────────────────────

    def methods_received(self) -> List[str]:
        return [m["method"] for m in self.received if "method" in m]

    def find_request(self, method: str) -> Optional[Dict[str, Any]]:
        """Return the first request frame the bridge sent for ``method``,
        or None."""
        for m in self.received:
            if m.get("method") == method and "id" in m:
                return m
        return None
