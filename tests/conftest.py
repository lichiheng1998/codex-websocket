"""Shared test fixtures.

The plugin directory name (``codex-websocket``) contains a hyphen, so
Python can't import it as a normal package. We register the plugin
under an alias (``codex_websocket``) in ``sys.modules`` so the
intra-plugin relative imports (``from . import wire``, ``from .state
import ...``) resolve.

Tests should import as::

    from codex_websocket import bridge, state, provider, ...
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_PLUGIN_PARENT = _PLUGIN_DIR.parent  # plugins/
_ALIAS = "codex_websocket"


def _install_alias() -> None:
    if _ALIAS in sys.modules:
        return

    # Make `codex-app-server-schema` siblings importable (wire.py adds
    # this to sys.path itself, but we touch wire indirectly via package
    # init, and the schema files use bare names like `from ClientRequest
    # import ...`).
    schema_dir = _PLUGIN_DIR / "codex-app-server-schema"
    if schema_dir.is_dir() and str(schema_dir) not in sys.path:
        sys.path.insert(0, str(schema_dir))

    # Load the plugin as a package under the alias.
    init_path = _PLUGIN_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        _ALIAS, init_path,
        submodule_search_locations=[str(_PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ALIAS] = module
    # Don't run __init__ — it expects the hermes plugin host. We just
    # need the alias entry so submodule imports resolve.


_install_alias()


@pytest.fixture
def fresh_bridge_class():
    """Reset the CodexBridge singleton cache between tests."""
    from codex_websocket.bridge import CodexBridge
    CodexBridge._instance = None
    yield CodexBridge
    CodexBridge._instance = None
