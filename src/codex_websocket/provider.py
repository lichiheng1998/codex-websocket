"""Model + provider helpers — the parts that don't need bridge state.

Why this exists separately
--------------------------
Codex's ``model/list`` RPC returns its bundled OpenAI catalog regardless
of the configured ``model_provider`` (cache eligibility doesn't include
provider identity, per upstream TODO in codex-rs/models-manager). With a
custom provider like LiteLLM, that catalog is irrelevant — the actual
list of usable models lives at ``{base_url}/models`` on the provider
itself.

This module owns:

* ``ProviderInfo`` — the (id, base_url, env_key) triple bridge learns
  from ``config/read``.
* ``sync_default_model`` — read effective config, return both the
  default model id and the provider triple.
* ``list_models_for`` — pick the right path (HTTP-direct vs RPC) and
  page through.
* ``fetch_provider_models_http`` — the direct GET that serves
  LiteLLM/Ollama/LM Studio.
* ``known_ids_from_listing`` — flatten a list_models response into a
  set of ids for soft-validating user input.

The bridge stays the owner of the resulting state (``self._default_model``,
``self._provider``); these helpers are stateless aside from the
``ProviderInfo`` value object.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from . import wire
from .policies import PROVIDER_HTTP_TIMEOUT, RPC_TIMEOUT
from .state import Result, err, ok

logger = logging.getLogger(__name__)


@dataclass
class ProviderInfo:
    """Just enough provider config to hit ``{base_url}/models`` directly."""

    id: Optional[str] = None
    base_url: Optional[str] = None
    env_key: Optional[str] = None

    def has_base_url(self) -> bool:
        return bool(self.base_url)


RpcCallable = Callable[..., Awaitable[Result]]
SyncRunner = Callable[..., Result]


async def sync_default_model(rpc: RpcCallable) -> "tuple[Result, ProviderInfo]":
    """Read ``config/read`` for the user's effective ``model = ...`` and
    active provider triple. Falls back to ``model/list`` + ``isDefault``
    for users who didn't pin a model in config.toml (e.g. plain OpenAI).

    Returns ``(model_result, provider_info)``. ``provider_info`` is
    populated from ``config/read`` whether or not we end up using the
    fallback path — the bridge wants it for ``list_models`` either way.
    """
    provider = ProviderInfo()

    cfg_rpc = await rpc("config/read", wire.ConfigReadParams(), timeout=RPC_TIMEOUT)
    if cfg_rpc["ok"]:
        config = (cfg_rpc["result"] or {}).get("config") or {}
        provider_id = (config.get("model_provider") or "").strip() or None
        provider.id = provider_id
        providers = config.get("model_providers") or {}
        if provider_id and isinstance(providers, dict):
            pinfo = providers.get(provider_id) or {}
            provider.base_url = (pinfo.get("base_url") or "").strip() or None
            provider.env_key = (pinfo.get("env_key") or "").strip() or None

        model = (config.get("model") or "").strip()
        if model:
            return ok(model=model), provider

    cursor = None
    while True:
        rpc_result = await rpc(
            "model/list",
            wire.ModelListParams(cursor=cursor, includeHidden=True),
            timeout=RPC_TIMEOUT,
        )
        if not rpc_result["ok"]:
            return rpc_result, provider

        payload = rpc_result["result"] or {}
        for item in payload.get("data") or []:
            if not isinstance(item, dict) or not item.get("isDefault"):
                continue
            model = (item.get("model") or item.get("id") or "").strip()
            if model:
                return ok(model=model), provider

        cursor = payload.get("nextCursor")
        if not cursor:
            break

    return err(
        "no default model: config.toml has no `model = ...` and "
        "model/list returned no isDefault entry"
    ), provider


def fetch_provider_models_http(base_url: str, env_key: Optional[str]) -> Result:
    """GET ``{base_url}/models`` against the configured provider directly.

    Returns codex Model-shape entries (id, model, displayName, isDefault)
    so callers render uniformly with the model/list path.
    """
    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if env_key:
        token = os.environ.get(env_key, "").strip()
        if token:
            req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=PROVIDER_HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
        return err(f"GET {url}: {exc}")

    raw = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return err(f"unexpected /models payload from {url}")

    normalized = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        mid = (item.get("id") or "").strip()
        if not mid:
            continue
        normalized.append({
            "id": mid,
            "model": mid,
            "displayName": item.get("display_name") or "",
            "isDefault": False,
        })
    return ok(data=normalized)


def list_models_for(
    provider: ProviderInfo,
    run_sync: SyncRunner,
    rpc: RpcCallable,
    *,
    include_hidden: bool = False,
    limit: Optional[int] = None,
) -> Result:
    """Pick the right model-list path given the active provider.

    With a known provider base_url, prefer the direct HTTP path
    (LiteLLM/Ollama/LM Studio answer here truthfully). Falls back to
    ``model/list`` paged via ``run_sync(rpc(...))`` when no provider info
    is available or the HTTP path failed.
    """
    if provider.has_base_url():
        direct = fetch_provider_models_http(provider.base_url, provider.env_key)
        if direct["ok"]:
            return direct
        logger.warning(
            "codex bridge: provider /v1/models fetch failed (%s); falling back to model/list",
            direct.get("error"),
        )

    cursor = None
    models: list = []
    while True:
        rpc_result = run_sync(
            rpc(
                "model/list",
                wire.ModelListParams(
                    cursor=cursor,
                    includeHidden=include_hidden or None,
                    limit=limit,
                ),
                timeout=RPC_TIMEOUT,
            )
        )
        if not rpc_result["ok"]:
            return rpc_result

        payload = rpc_result["result"] or {}
        models.extend(payload.get("data") or [])
        cursor = payload.get("nextCursor")
        if not cursor:
            break

    return ok(data=models)


def known_ids_from_listing(listed: Result) -> "set[str]":
    """Flat set of model identifiers in a list_models response, drawing
    from both ``id`` and ``model`` fields each entry can carry. Returns
    an empty set if the listing failed or had no usable entries."""
    if not listed.get("ok"):
        return set()
    ids: set[str] = set()
    for item in listed.get("data") or []:
        if not isinstance(item, dict):
            continue
        for key in ("id", "model"):
            value = str(item.get(key) or "").strip()
            if value:
                ids.add(value)
    return ids


