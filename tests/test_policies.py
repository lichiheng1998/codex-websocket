"""Unit tests for policies.py — sandbox + collaboration mode helpers."""

from __future__ import annotations

import pytest

from codex_websocket.policies import (
    DEFAULT_APPROVAL_POLICY,
    DEFAULT_MODEL,
    DEFAULT_SANDBOX_POLICY,
    default_collaboration_mode,
    plan_collaboration_mode,
    prepare_sandbox,
)


class TestPrepareSandbox:
    def test_readonly_alias_resolves_and_ignores_cwd(self):
        s = prepare_sandbox("readonly", "/some/path")
        assert s["type"] == "readOnly"
        # readOnly has no writable roots — cwd is irrelevant.
        assert "writableRoots" not in s

    def test_read_only_with_dash_alias_resolves_too(self):
        assert prepare_sandbox("read-only", "/x")["type"] == "readOnly"

    def test_workspace_write_injects_cwd_into_writable_roots(self):
        s = prepare_sandbox("workspace-write", "/proj/repo")
        assert s["type"] == "workspaceWrite"
        assert "/proj/repo" in s["writableRoots"]

    def test_workspace_write_does_not_duplicate_cwd(self):
        s1 = prepare_sandbox("workspace-write", "/proj")
        # Round-trip: feed result back as input dict — cwd should not double.
        s2 = prepare_sandbox(s1, "/proj")
        assert s2["writableRoots"].count("/proj") == 1

    def test_workspace_write_with_empty_cwd_keeps_roots_empty(self):
        s = prepare_sandbox("workspace-write", "")
        assert s["writableRoots"] == []

    def test_danger_full_access_alias(self):
        assert prepare_sandbox("danger-full-access", "/x")["type"] == "dangerFullAccess"
        assert prepare_sandbox("dangerfullaccess", "/x")["type"] == "dangerFullAccess"

    def test_dict_passthrough_with_cwd_appended(self):
        custom = {"type": "workspaceWrite", "writableRoots": ["/a"]}
        s = prepare_sandbox(custom, "/b")
        assert s["writableRoots"] == ["/a", "/b"]
        # Original dict should not be mutated.
        assert custom["writableRoots"] == ["/a"]

    def test_unknown_string_passes_through_untouched(self):
        # Caller's problem if codex rejects it; we don't second-guess.
        assert prepare_sandbox("not-a-real-policy", "/x") == "not-a-real-policy"


class TestCollaborationMode:
    @pytest.mark.parametrize("model", ["gpt-5", "mimo-v2.5-pro", "claude-opus-4"])
    def test_plan_mode_echoes_model(self, model):
        m = plan_collaboration_mode(model)
        assert m.mode.value == "plan"
        assert m.settings.model == model

    @pytest.mark.parametrize("model", ["gpt-5", "mimo-v2.5-pro"])
    def test_default_mode_echoes_model(self, model):
        m = default_collaboration_mode(model)
        assert m.mode.value == "default"
        assert m.settings.model == model


class TestDefaults:
    def test_default_constants_are_strings(self):
        # Minimal sanity check; the actual values are user-facing config and
        # may change. We just want to catch the case where someone
        # accidentally redefines them as something weird.
        assert isinstance(DEFAULT_MODEL, str) and DEFAULT_MODEL
        assert isinstance(DEFAULT_APPROVAL_POLICY, str) and DEFAULT_APPROVAL_POLICY
        assert isinstance(DEFAULT_SANDBOX_POLICY, str) and DEFAULT_SANDBOX_POLICY
