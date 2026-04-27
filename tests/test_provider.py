"""Unit tests for provider.py — model + provider lookup logic.

Covered:

* fetch_provider_models_http normalizes OpenAI-style /v1/models payloads
  to the codex Model shape, surfaces useful errors for malformed
  responses, and applies the env_key as a Bearer token when present.
* sync_default_model picks the user's config.toml model first and
  falls back to the model/list isDefault path; either way it captures
  the provider triple from config/read.
* known_ids_from_listing dedupes id and model fields, returns empty
  on failed listings.
* list_models_for prefers the HTTP path, falls back to RPC when no
  base_url or HTTP fails.
"""

from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from codex_websocket.provider import (
    ProviderInfo,
    fetch_provider_models_http,
    known_ids_from_listing,
    list_models_for,
    sync_default_model,
)
from codex_websocket.state import err, ok


# ── HTTP fixtures ───────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body
    def read(self) -> bytes:
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _http_returns(payload: dict):
    return patch(
        "urllib.request.urlopen",
        return_value=_FakeResp(json.dumps(payload).encode("utf-8")),
    )


# ── fetch_provider_models_http ──────────────────────────────────────────────

class TestFetchProviderModelsHttp:
    def test_normalizes_openai_shape(self):
        payload = {"data": [
            {"id": "foo", "object": "model"},
            {"id": "bar", "display_name": "Bar Pretty"},
        ]}
        with _http_returns(payload):
            r = fetch_provider_models_http("http://x/v1", env_key=None)
        assert r["ok"]
        assert {m["id"] for m in r["data"]} == {"foo", "bar"}
        bar = next(m for m in r["data"] if m["id"] == "bar")
        assert bar["displayName"] == "Bar Pretty"
        assert bar["isDefault"] is False
        # `model` echoes id so callers can union both fields uniformly.
        assert bar["model"] == "bar"

    def test_skips_entries_without_id(self):
        payload = {"data": [{"id": "ok"}, {}, {"object": "model"}, {"id": ""}]}
        with _http_returns(payload):
            r = fetch_provider_models_http("http://x/v1", env_key=None)
        assert r["ok"]
        assert [m["id"] for m in r["data"]] == ["ok"]

    def test_missing_data_field_is_explicit_error(self):
        with _http_returns({"oops": "no data"}):
            r = fetch_provider_models_http("http://x/v1", env_key=None)
        assert not r["ok"]
        assert "unexpected" in r["error"]

    def test_data_must_be_list(self):
        with _http_returns({"data": "not-a-list"}):
            r = fetch_provider_models_http("http://x/v1", env_key=None)
        assert not r["ok"]

    def test_env_key_with_token_adds_bearer(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-abc")
        captured = {}

        def fake_open(req, timeout):
            captured["headers"] = dict(req.header_items())
            return _FakeResp(b'{"data":[]}')

        with patch("urllib.request.urlopen", side_effect=fake_open):
            fetch_provider_models_http("http://x/v1", env_key="MY_KEY")
        # urllib title-cases header names, so we check case-insensitively.
        auth = next(
            (v for k, v in captured["headers"].items() if k.lower() == "authorization"),
            None,
        )
        assert auth == "Bearer sk-abc"

    def test_env_key_unset_does_not_add_auth(self, monkeypatch):
        monkeypatch.delenv("MY_KEY", raising=False)
        captured = {}

        def fake_open(req, timeout):
            captured["headers"] = dict(req.header_items())
            return _FakeResp(b'{"data":[]}')

        with patch("urllib.request.urlopen", side_effect=fake_open):
            fetch_provider_models_http("http://x/v1", env_key="MY_KEY")
        assert not any(k.lower() == "authorization" for k in captured["headers"])

    def test_url_error_returns_error_result(self):
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
            r = fetch_provider_models_http("http://x/v1", env_key=None)
        assert not r["ok"]
        assert "boom" in r["error"]

    def test_invalid_json_returns_error_result(self):
        with patch("urllib.request.urlopen", return_value=_FakeResp(b"<html>nope")):
            r = fetch_provider_models_http("http://x/v1", env_key=None)
        assert not r["ok"]


# ── sync_default_model ──────────────────────────────────────────────────────

def _make_rpc(responses: dict):
    """Build a fake async rpc that returns from a {method: result_dict} map.

    Method handlers can be dicts (returned wrapped as ok(result=...)) or
    Result dicts already (returned verbatim).
    """
    async def _rpc(method, params=None, timeout=None):
        if method not in responses:
            return err(f"no fake handler for {method}")
        v = responses[method]
        if isinstance(v, dict) and "ok" in v:
            return v
        return ok(result=v)
    return _rpc


class TestSyncDefaultModel:
    @pytest.mark.asyncio
    async def test_uses_config_toml_model_when_present(self):
        rpc = _make_rpc({
            "config/read": {"config": {
                "model": "mimo-v2.5-pro",
                "model_provider": "litellm",
                "model_providers": {
                    "litellm": {"base_url": "http://localhost:4001/v1", "env_key": "LK"},
                },
            }},
        })
        res, prov = await sync_default_model(rpc)
        assert res == {"ok": True, "model": "mimo-v2.5-pro"}
        assert prov.id == "litellm"
        assert prov.base_url == "http://localhost:4001/v1"
        assert prov.env_key == "LK"

    @pytest.mark.asyncio
    async def test_falls_back_to_model_list_isDefault(self):
        rpc = _make_rpc({
            "config/read": {"config": {"model": ""}},  # no top-level model
            "model/list": {
                "data": [
                    {"id": "a"},
                    {"id": "b", "isDefault": True},
                ],
                "nextCursor": None,
            },
        })
        res, prov = await sync_default_model(rpc)
        assert res["ok"] and res["model"] == "b"
        # No provider info captured because config/read had no provider info.
        assert prov.id is None
        assert prov.base_url is None

    @pytest.mark.asyncio
    async def test_returns_explicit_error_when_neither_path_yields_a_model(self):
        rpc = _make_rpc({
            "config/read": {"config": {"model": ""}},
            "model/list": {"data": [{"id": "a"}, {"id": "b"}], "nextCursor": None},
        })
        res, prov = await sync_default_model(rpc)
        assert not res["ok"]
        assert "no default model" in res["error"]

    @pytest.mark.asyncio
    async def test_pages_through_model_list_until_isDefault_or_exhausted(self):
        pages = {
            None: {"data": [{"id": "a"}], "nextCursor": "page2"},
            "page2": {"data": [{"id": "b", "isDefault": True}], "nextCursor": None},
        }

        async def rpc(method, params=None, timeout=None):
            if method == "config/read":
                return ok(result={"config": {"model": ""}})
            if method == "model/list":
                cursor = getattr(params, "cursor", None)
                return ok(result=pages[cursor])
            return err("unexpected")

        res, _ = await sync_default_model(rpc)
        assert res["ok"] and res["model"] == "b"


# ── known_ids_from_listing ──────────────────────────────────────────────────

class TestKnownIdsFromListing:
    def test_unions_id_and_model_fields(self):
        listed = ok(data=[{"id": "foo", "model": "bar"}, {"id": "baz"}])
        assert known_ids_from_listing(listed) == {"foo", "bar", "baz"}

    def test_skips_blanks_and_non_dicts(self):
        listed = ok(data=[{"id": ""}, {"model": ""}, "string-entry", None, {"id": "real"}])
        assert known_ids_from_listing(listed) == {"real"}

    def test_failed_listing_returns_empty_set(self):
        assert known_ids_from_listing(err("boom")) == set()

    def test_empty_data_returns_empty_set(self):
        assert known_ids_from_listing(ok(data=[])) == set()

    def test_missing_data_key_returns_empty_set(self):
        assert known_ids_from_listing(ok()) == set()


# ── list_models_for ─────────────────────────────────────────────────────────

class TestListModelsFor:
    def test_prefers_provider_http_when_base_url_present(self):
        provider = ProviderInfo(id="x", base_url="http://x/v1", env_key=None)
        with _http_returns({"data": [{"id": "from-http"}]}):
            r = list_models_for(provider, run_sync=None, rpc=None)
        assert r["ok"]
        assert r["data"][0]["id"] == "from-http"

    def test_falls_back_to_rpc_when_no_base_url(self):
        provider = ProviderInfo()  # all None

        def fake_run_sync(coro, **kw):
            # Discard the coroutine to avoid "never awaited" warnings; we
            # short-circuit with the Result we want list_models_for to see.
            coro.close()
            return ok(result={"data": [{"id": "from-rpc"}], "nextCursor": None})

        # rpc is a regular callable returning a coroutine; the test never
        # awaits it because fake_run_sync immediately closes it.
        def fake_rpc(method, params, timeout):
            async def _coro():
                return ok()
            return _coro()

        r = list_models_for(provider, run_sync=fake_run_sync, rpc=fake_rpc)
        assert r["ok"]
        assert r["data"][0]["id"] == "from-rpc"

    def test_falls_back_to_rpc_when_provider_http_fails(self):
        provider = ProviderInfo(id="x", base_url="http://x/v1", env_key=None)

        def fake_run_sync(coro, **kw):
            coro.close()
            return ok(result={"data": [{"id": "from-rpc"}], "nextCursor": None})

        def fake_rpc(method, params, timeout):
            async def _coro():
                return ok()
            return _coro()

        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            r = list_models_for(provider, run_sync=fake_run_sync, rpc=fake_rpc)
        assert r["ok"]
        assert r["data"][0]["id"] == "from-rpc"
