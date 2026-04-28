"""Shared test fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))


@pytest.fixture
def fresh_bridge_class():
    """Reset the CodexBridge singleton cache between tests."""
    from codex_websocket.bridge import CodexBridge
    CodexBridge._instance = None
    yield CodexBridge
    CodexBridge._instance = None
