"""Unit tests for state.py — Result helpers and pending-* dataclasses."""

from __future__ import annotations

from codex_websocket.state import (
    _PendingApproval,
    _PendingElicitation,
    _PendingInput,
    _PendingThread,
    TaskTarget,
    err,
    ok,
)


# ── Result helpers ──────────────────────────────────────────────────────────

class TestResultHelpers:
    def test_ok_marks_truthy_and_passes_data(self):
        assert ok(model="foo", count=3) == {"ok": True, "model": "foo", "count": 3}

    def test_ok_with_no_kwargs_is_just_ok_true(self):
        assert ok() == {"ok": True}

    def test_err_marks_falsy_and_carries_message(self):
        assert err("nope") == {"ok": False, "error": "nope"}


# ── Approval payload polymorphism (the real reason these subclasses exist) ──

class TestApprovalPayload:
    def _approval(self, **overrides):
        defaults = dict(rpc_id=42, task_id="t1", command="ls", reason="r", target=None)
        defaults.update(overrides)
        return defaults

    def test_command_approval_echoes_decision(self):
        p = _PendingApproval(**self._approval())
        assert p.to_response_payload("accept") == {"decision": "accept"}
        assert p.to_response_payload("decline") == {"decision": "decline"}
        # Codex uses "approve"/"deny" elsewhere; we don't translate here.
        assert p.to_response_payload("approve") == {"decision": "approve"}

    def test_elicitation_only_accept_is_accept(self):
        e = _PendingElicitation(**self._approval())
        assert e.to_response_payload("accept") == {"action": "accept", "content": None}

    def test_elicitation_anything_else_is_decline(self):
        e = _PendingElicitation(**self._approval())
        for d in ("decline", "approve", "deny", "cancel", ""):
            assert e.to_response_payload(d) == {"action": "decline", "content": None}, (
                f"decision {d!r} should map to decline"
            )


# ── Dataclass shapes ────────────────────────────────────────────────────────

class TestDataclassShapes:
    def test_task_target_defaults_to_empty_strings(self):
        t = TaskTarget()
        assert t.platform == "" and t.chat_id == "" and t.thread_id == ""

    def test_pending_thread_requires_approval_policy_explicitly(self):
        # The default was removed because every caller passes one — this
        # test pins that down so a future drive-by re-add gets caught.
        pt = _PendingThread(
            thread_id="th1", task_id="t1", session_key="s",
            cwd="/tmp", sandbox_policy="workspace-write",
            approval_policy="never",
        )
        assert pt.approval_policy == "never"
        assert pt.target is None

    def test_pending_input_has_no_thread_id(self):
        # Field was removed; make sure nobody adds it back without a
        # consumer.
        pi = _PendingInput(task_id="t1", rpc_id=7, questions=[])
        assert not hasattr(pi, "thread_id")
