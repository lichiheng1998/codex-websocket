"""Unit tests for utils.py — pure helpers for the bridge."""

from __future__ import annotations

import re
import socket

from codex_websocket.utils import (
    extract_thread_id,
    new_task_id,
    pick_free_port,
)


class TestExtractThreadId:
    def test_top_level_threadId(self):
        assert extract_thread_id({"threadId": "abc-12345678"}) == "abc-12345678"

    def test_top_level_conversation_id_legacy(self):
        assert extract_thread_id({"conversationId": "uuid-deadbeef"}) == "uuid-deadbeef"

    def test_top_level_thread_id_snake(self):
        assert extract_thread_id({"thread_id": "snake-12345678"}) == "snake-12345678"

    def test_priority_threadId_over_conversationId(self):
        assert extract_thread_id({
            "threadId": "winner-1234",
            "conversationId": "loser-5678",
        }) == "winner-1234"

    def test_nested_thread_id(self):
        assert extract_thread_id({"thread": {"id": "nested-1234"}}) == "nested-1234"

    def test_nested_thread_threadId_alt_key(self):
        assert extract_thread_id({"thread": {"threadId": "alt-1234"}}) == "alt-1234"

    def test_top_level_id_uuid_shaped(self):
        assert extract_thread_id({"id": "abc-1234-efgh"}) == "abc-1234-efgh"

    def test_top_level_id_too_short_rejected(self):
        # Heuristic: needs len ≥ 8 and a dash.
        assert extract_thread_id({"id": "short"}) == ""

    def test_top_level_id_no_dash_rejected(self):
        assert extract_thread_id({"id": "longstring_no_dash"}) == ""

    def test_non_dict_returns_empty(self):
        assert extract_thread_id("not-a-dict") == ""
        assert extract_thread_id(None) == ""
        assert extract_thread_id(["thread", "list"]) == ""

    def test_empty_dict_returns_empty(self):
        assert extract_thread_id({}) == ""

    def test_non_string_threadId_rejected(self):
        assert extract_thread_id({"threadId": 12345}) == ""


class TestNewTaskId:
    def test_format_is_8_hex_lowercase(self):
        for _ in range(50):
            tid = new_task_id()
            assert re.fullmatch(r"[0-9a-f]{8}", tid), tid

    def test_collision_unlikely_in_small_sample(self):
        # 32 bits of entropy ≈ no collision in 100 samples.
        ids = {new_task_id() for _ in range(100)}
        assert len(ids) == 100


class TestPickFreePort:
    def test_returns_actually_bindable_port(self):
        p = pick_free_port()
        assert isinstance(p, int) and 1024 <= p <= 65535
        # Confirm we can bind to it (race-free immediately after).
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", p))

    def test_returns_different_ports_across_calls(self):
        # SO_REUSEADDR can let two calls alias, but pick_free_port closes
        # before returning so the kernel typically rotates. Worst case
        # this is flaky; if it fails reliably, drop the assertion.
        ports = {pick_free_port() for _ in range(5)}
        assert len(ports) >= 2
