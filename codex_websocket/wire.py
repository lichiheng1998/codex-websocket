"""Pydantic wire helpers for the codex-websocket plugin.

Centralizes imports from ``codex-app-server-schema`` and offers two helpers:

* ``serialize(model)`` — turn an outbound pydantic params object into a JSON-ready
  dict (alias-correct, no ``None`` leaves), or pass a dict through unchanged.
* ``parse_incoming(raw)`` — classify a raw ws frame (already JSON-decoded) as
  one of ``{"response", "error", "request", "notification", "unknown"}`` and
  return the matching pydantic object where possible.

Only the *outermost* JSON-RPC envelope is parsed via pydantic union dispatch
(`JSONRPCMessage` → response vs error vs request vs notification). Method-level
branching is the caller's job — they ``match`` on ``obj.method.value`` instead
of ``isinstance`` against the 50+ wrapper classes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Tuple, Union

from pydantic import BaseModel, ValidationError

# codex-app-server-schema has a hyphenated directory name that Python's normal
# import machinery can't resolve. We load each module explicitly via importlib
# so the schema directory is never added to the global sys.path.
_SCHEMA_DIR = Path(__file__).parent / "codex-app-server-schema"


def _import_schema(name: str):
    """Load a module from the schema directory without touching sys.path."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCHEMA_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_import_schema("ClientRequest")
_import_schema("JSONRPCMessage")
_import_schema("ServerNotification")
_import_schema("ServerRequest")

# Outbound client→server params (used by bridge when calling _rpc).
from ClientRequest import (  # noqa: E402
    ConfigReadParams,
    InitializeParams,
    InitializeCapabilities,
    ModelListParams,
    ThreadStartParams,
    ThreadReadParams,
    ThreadArchiveParams,
    ThreadResumeParams,
    ThreadListParams,
    TurnStartParams,
    CollaborationMode,
    Settings as CollaborationSettings,
    ModeKind,
)

# JSON-RPC envelope (used to classify inbound frames).
from JSONRPCMessage import (  # noqa: E402
    JSONRPCMessage,
    JSONRPCRequest,
    JSONRPCResponse,
    JSONRPCNotification,
    JSONRPCError,
)

# Inbound unions.
from ServerNotification import ServerNotification  # noqa: E402
from ServerRequest import ServerRequest  # noqa: E402

__all__ = [
    "ConfigReadParams",
    "InitializeParams",
    "InitializeCapabilities",
    "ModelListParams",
    "ThreadStartParams",
    "ThreadReadParams",
    "ThreadArchiveParams",
    "ThreadResumeParams",
    "ThreadListParams",
    "TurnStartParams",
    "CollaborationMode",
    "CollaborationSettings",
    "ModeKind",
    "JSONRPCMessage",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "JSONRPCNotification",
    "JSONRPCError",
    "ServerNotification",
    "ServerRequest",
    "serialize",
    "parse_incoming",
    "IncomingKind",
    "ParsedIncoming",
]

IncomingKind = str  # "response" | "error" | "request" | "notification" | "unknown"
ParsedIncoming = Tuple[IncomingKind, Any, dict]


def serialize(params: Union[BaseModel, dict, None]) -> dict:
    """Render outbound params as a JSON-RPC ``params`` dict.

    ``None`` becomes ``{}``. Pydantic models use ``by_alias`` and drop ``None``
    leaves so the wire payload stays minimal; already-dict params pass through.
    """
    if params is None:
        return {}
    if isinstance(params, BaseModel):
        return params.model_dump(by_alias=True, exclude_none=True, mode="json")
    return dict(params)


def parse_incoming(raw: dict) -> ParsedIncoming:
    """Classify a decoded JSON-RPC frame.

    Returns ``(kind, parsed, raw)`` where ``parsed`` is:

    * JSONRPCResponse for ``"response"``
    * JSONRPCError for ``"error"``
    * ServerRequest.root member (has ``.method`` + ``.params``) for ``"request"``
    * ServerNotification.root member for ``"notification"``
    * ``None`` for ``"unknown"``

    The raw dict is returned as a third element so callers that need fields not
    modeled by the schema (or that want to log the original payload on
    error) don't have to re-plumb it.
    """
    try:
        envelope = JSONRPCMessage.model_validate(raw).root
    except ValidationError:
        return ("unknown", None, raw)

    if isinstance(envelope, JSONRPCResponse):
        return ("response", envelope, raw)
    if isinstance(envelope, JSONRPCError):
        return ("error", envelope, raw)
    if isinstance(envelope, JSONRPCRequest):
        try:
            return ("request", ServerRequest.model_validate(raw).root, raw)
        except ValidationError:
            return ("unknown", envelope, raw)
    if isinstance(envelope, JSONRPCNotification):
        try:
            return ("notification", ServerNotification.model_validate(raw).root, raw)
        except ValidationError:
            return ("unknown", envelope, raw)
    return ("unknown", envelope, raw)
