# SPDX-License-Identifier: Apache-2.0
"""Tests for HTTP parsing and acknowledgement of request cache policies."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

import omlx.server as srv
from omlx.api.cache_policy import (
    CachePolicyRequest,
    effective_cache_policy,
    ensure_cache_policy_supported,
    resolve_request_cache_policy,
)
from omlx.api.anthropic_models import MessagesRequest
from omlx.api.openai_models import ChatCompletionRequest, CompletionRequest
from omlx.api.responses_models import ResponsesRequest
from omlx.cache.policy import CachePolicy


def _http_request(headers=()):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/completions",
            "headers": [
                (name.lower().encode(), value.encode()) for name, value in headers
            ],
        }
    )


def test_body_and_headers_resolve_independent_components():
    body = CachePolicyRequest.model_validate({"kv": "memory"})
    policy, explicit = resolve_request_cache_policy(
        body,
        _http_request([("X-oMLX-Image-Cache", "none")]),
        body_present=True,
    )

    assert policy == CachePolicy(kv="memory", images="none")
    assert explicit == frozenset({"kv", "images"})


@pytest.mark.parametrize(
    "headers,body,body_present,detail",
    [
        ([('X-oMLX-KV-Cache', 'memory')], {"kv": "none"}, True, "conflicts"),
        ([('X-oMLX-KV-Cache', 'sometimes')], None, False, "must be default"),
        ([('X-oMLX-KV-Cache', 'memory'), ('x-omlx-kv-cache', 'memory')], None, False, "duplicate"),
        ([], None, True, "must be an object"),
    ],
)
def test_malformed_or_conflicting_policy_inputs_rejected(
    headers, body, body_present, detail
):
    parsed = CachePolicyRequest.model_validate(body) if body is not None else None
    with pytest.raises(HTTPException, match=detail):
        resolve_request_cache_policy(
            parsed, _http_request(headers), body_present=body_present
        )


@pytest.mark.parametrize(
    "model,body",
    [
        (CompletionRequest, {"model": "m", "prompt": "p"}),
        (ChatCompletionRequest, {"model": "m", "messages": [{"role": "user", "content": "p"}]}),
        (
            MessagesRequest,
            {
                "model": "m",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "p"}],
            },
        ),
        (ResponsesRequest, {"model": "m", "input": "p"}),
    ],
)
def test_all_generation_request_models_accept_policy(model, body):
    request = model.model_validate({**body, "cache_policy": {"images": "memory"}})
    assert request.cache_policy == CachePolicyRequest(images="memory")
    assert "cache_policy" in request.model_fields_set


def test_invalid_policy_component_fails_request_model_validation():
    with pytest.raises(ValueError):
        CompletionRequest.model_validate(
            {"model": "m", "prompt": "p", "cache_policy": {"kv": "ssd"}}
        )


def test_unsupported_restriction_is_rejected_but_default_is_compatible():
    class Engine:
        def supports_cache_policy_component(self, component):
            return component == "kv"

    engine = Engine()
    ensure_cache_policy_supported(
        engine, CachePolicy(kv="memory"), frozenset({"kv"})
    )
    ensure_cache_policy_supported(
        engine, CachePolicy(), frozenset({"images"})
    )
    with pytest.raises(HTTPException, match="images cache policy is not supported"):
        ensure_cache_policy_supported(
            engine, CachePolicy(images="none"), frozenset({"images"})
        )


def test_effective_acknowledgement_respects_hot_only_and_cache_availability():
    scheduler = type(
        "Scheduler",
        (),
        {
            "block_aware_cache": object(),
            "paged_ssd_cache_manager": type("Manager", (), {"_hot_cache_enabled": True})(),
            "config": type("Config", (), {"hot_cache_only": True})(),
        },
    )()
    engine = type(
        "Engine",
        (),
        {
            "_engine": type(
                "Runtime",
                (),
                {"engine": type("Core", (), {"scheduler": scheduler})()},
            )(),
            "prefix_cache_enabled": True,
            "_vision_cache": None,
        },
    )()

    assert effective_cache_policy(engine, CachePolicy(), None) == CachePolicy(
        kv="memory", images="none"
    )
    assert effective_cache_policy(engine, CachePolicy(kv="none"), None).kv == "none"


def test_completion_endpoint_rejects_malformed_header_before_engine_lookup(monkeypatch):
    monkeypatch.setattr(srv._server_state, "api_key", None)
    monkeypatch.setattr(srv._server_state, "global_settings", None)
    monkeypatch.setattr(
        srv,
        "get_engine_for_model",
        lambda *a, **kw: pytest.fail("engine lookup must not run"),
    )
    client = TestClient(srv.app, raise_server_exceptions=False)
    response = client.post(
        "/v1/completions",
        json={"model": "m", "prompt": "p"},
        headers={"X-oMLX-KV-Cache": "ssd"},
    )
    assert response.status_code == 400
    assert "must be default, memory or none" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_streaming_completion_acknowledges_effective_policy(monkeypatch):
    from omlx.api.openai_models import CompletionRequest

    scheduler = SimpleNamespace(
        block_aware_cache=object(),
        paged_ssd_cache_manager=SimpleNamespace(_hot_cache_enabled=True),
        config=SimpleNamespace(hot_cache_only=True),
    )
    engine = SimpleNamespace(
        tokenizer=SimpleNamespace(encode=lambda prompt: [1]),
        prefix_cache_enabled=True,
        _engine=SimpleNamespace(engine=SimpleNamespace(scheduler=scheduler)),
        supports_cache_policy_component=lambda component: component == "kv",
        preflight_completion=AsyncMock(),
    )

    class Lease:
        def abort_reason(self):
            return None

        async def release(self):
            return None

    monkeypatch.setattr(srv, "_LLMEngineLease", Lease)
    monkeypatch.setattr(srv, "get_engine_for_model", AsyncMock(return_value=engine))
    monkeypatch.setattr(srv, "_serving_model_id", lambda lease, requested: requested)
    monkeypatch.setattr(srv._server_state, "global_settings", None)
    response = await srv.create_completion(
        CompletionRequest(
            model="m", prompt="hello", stream=True, cache_policy={"kv": "memory"}
        ),
        _http_request(),
        True,
    )

    assert response.headers["x-omlx-kv-cache"] == "memory"
    assert response.headers["x-omlx-image-cache"] == "none"
    engine.preflight_completion.assert_awaited_once()
    assert engine.preflight_completion.await_args.kwargs["cache_policy"] == CachePolicy(
        kv="memory"
    )


@pytest.mark.asyncio
async def test_chat_rejects_unsupported_restriction_before_file_preprocessing(monkeypatch):
    from omlx.api.openai_models import ChatCompletionRequest

    engine = SimpleNamespace(
        supports_cache_policy_component=lambda component: False,
    )

    class Lease:
        async def release(self):
            return None

    monkeypatch.setattr(srv, "_LLMEngineLease", Lease)
    monkeypatch.setattr(srv, "get_engine_for_model", AsyncMock(return_value=engine))
    monkeypatch.setattr(
        srv,
        "_preprocess_markitdown_files_for_llm",
        AsyncMock(side_effect=AssertionError("preprocessing must not run")),
    )
    request = ChatCompletionRequest(
        model="m",
        messages=[{"role": "user", "content": "see attached file"}],
        cache_policy={"images": "none"},
    )

    with pytest.raises(HTTPException, match="images cache policy is not supported"):
        await srv.create_chat_completion(request, _http_request(), True)
