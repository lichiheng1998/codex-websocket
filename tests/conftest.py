"""Shared test fixtures.

The plugin directory name (``codex-websocket``) contains a hyphen, so
Python can't import it as a normal package. We register
``codex_websocket`` in ``sys.modules`` pointing at
``src/codex_websocket/`` so all intra-package imports resolve.

Tests import as::

    from codex_websocket import bridge, state, provider, ...
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_PKG_DIR = _PLUGIN_ROOT / "src" / "codex_websocket"
_ALIAS = "codex_websocket"


def _install_alias() -> None:
    if _ALIAS in sys.modules:
        return

    # Make `codex-app-server-schema` importable (wire.py adds this to
    # sys.path itself, but we touch wire indirectly via package init,
    # and the schema files use bare names like `from ClientRequest
    # import ...`).
    schema_dir = _PKG_DIR / "codex-app-server-schema"
    if schema_dir.is_dir() and str(schema_dir) not in sys.path:
        sys.path.insert(0, str(schema_dir))

    # Register codex_websocket as a package backed by src/codex_websocket/.
    init_path = _PKG_DIR / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        _ALIAS, init_path,
        submodule_search_locations=[str(_PKG_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_ALIAS] = module


_install_alias()


@pytest.fixture
def fresh_bridge_class():
    """Reset the CodexBridge singleton cache between tests."""
    from codex_websocket.bridge import CodexBridge
    CodexBridge._instance = None
    yield CodexBridge
    CodexBridge._instance = None
