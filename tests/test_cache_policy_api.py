# SPDX-License-Identifier: Apache-2.0
"""Tests for HTTP parsing and acknowledgement of request cache policies."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.responses import Response, StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

import omlx.server as srv
from omlx.api.cache_policy import (
    CachePolicyAcknowledgement,
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
        body, _http_request([("X-oMLX-Image-Cache", "none")])
    )

    assert policy == CachePolicy(kv="memory", images="none")
    assert explicit == frozenset({"kv", "images"})


@pytest.mark.parametrize(
    "headers,body,detail",
    [
        ([('X-oMLX-KV-Cache', 'memory')], {"kv": "none"}, "conflicts"),
        ([('X-oMLX-KV-Cache', 'sometimes')], None, "must be default"),
        ([('X-oMLX-KV-Cache', 'Memory')], None, "must be default"),
        (
            [('X-oMLX-KV-Cache', 'memory'), ('x-omlx-kv-cache', 'none')],
            None,
            "conflicting duplicate",
        ),
    ],
)
def test_malformed_or_conflicting_policy_inputs_rejected(headers, body, detail):
    parsed = CachePolicyRequest.model_validate(body) if body is not None else None
    with pytest.raises(HTTPException, match=detail):
        resolve_request_cache_policy(parsed, _http_request(headers))


def test_identical_duplicate_headers_and_null_body_policy_mean_omission():
    policy, explicit = resolve_request_cache_policy(
        None,
        _http_request(
            [("X-oMLX-KV-Cache", "memory"), ("x-omlx-kv-cache", "memory")]
        ),
    )
    assert policy == CachePolicy(kv="memory")
    assert explicit == frozenset({"kv"})


def test_explicit_null_body_policy_is_an_omission():
    request = CompletionRequest.model_validate(
        {"model": "m", "prompt": "p", "cache_policy": None}
    )
    assert "cache_policy" in request.model_fields_set
    policy, explicit = resolve_request_cache_policy(
        request.cache_policy, _http_request()
    )
    assert policy == CachePolicy()
    assert explicit == frozenset()


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
            "paged_ssd_cache_manager": type(
                "Manager",
                (),
                {
                    "_hot_cache_enabled": True,
                    "_hot_cache_only": True,
                    "_cache_dir": None,
                },
            )(),
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

    assert effective_cache_policy(engine, CachePolicy()) == CachePolicyAcknowledgement(
        kv="memory", images="disabled"
    )
    assert effective_cache_policy(engine, CachePolicy(kv="none")).kv == "none"


def test_effective_ack_uses_active_manager_and_best_effort_memory_choice():
    manager = SimpleNamespace(
        _hot_cache_enabled=False,
        _hot_cache_only=False,
        _cache_dir="/runtime/cache",
    )
    scheduler = SimpleNamespace(
        block_aware_cache=object(), paged_ssd_cache_manager=manager
    )
    engine = SimpleNamespace(
        _engine=SimpleNamespace(engine=SimpleNamespace(scheduler=scheduler)),
        prefix_cache_enabled=True,
        _vision_cache=SimpleNamespace(_cache_dir=None, _max_memory_bytes=0),
    )

    assert effective_cache_policy(engine, CachePolicy()) == CachePolicyAcknowledgement(
        kv="default", images="disabled"
    )
    assert effective_cache_policy(
        engine, CachePolicy(kv="memory", images="memory")
    ) == CachePolicyAcknowledgement(kv="memory", images="memory")

    engine._engine.engine.scheduler.paged_ssd_cache_manager = None
    assert effective_cache_policy(engine, CachePolicy()).kv == "disabled"


@pytest.mark.parametrize(
    "unsupported_flag",
    ["_vision_cache_enabled", "_vision_feature_cache_supported"],
)
def test_image_ack_is_disabled_when_model_does_not_use_feature_cache(
    unsupported_flag,
):
    engine = SimpleNamespace(
        _vision_cache=SimpleNamespace(_cache_dir="/cache", _max_memory_bytes=1024),
        **{unsupported_flag: False},
    )
    for requested in ("default", "memory", "none"):
        ack = effective_cache_policy(
            engine, CachePolicy(images=requested)
        )
        assert ack.images == "disabled"


@pytest.mark.asyncio
async def test_markitdown_chat_acknowledges_disabled_caches(monkeypatch):
    response = Response(content="{}")
    handler = AsyncMock(return_value=response)
    monkeypatch.setattr(srv, "is_markitdown_model", lambda model: True)
    monkeypatch.setattr(srv, "_create_markitdown_chat_completion", handler)

    request = ChatCompletionRequest(
        model="markitdown", messages=[{"role": "user", "content": "hello"}]
    )
    result = await srv.create_chat_completion(request, _http_request(), True)

    assert result is response
    assert handler.await_args.kwargs["policy_headers"] == {
        "X-oMLX-KV-Cache": "disabled",
        "X-oMLX-Image-Cache": "disabled",
    }


@pytest.mark.parametrize(
    "path,body",
    [
        ("/v1/completions", {"model": "m", "prompt": "p"}),
        (
            "/v1/chat/completions",
            {"model": "m", "messages": [{"role": "user", "content": "p"}]},
        ),
        (
            "/v1/messages",
            {"model": "m", "max_tokens": 1, "messages": [{"role": "user", "content": "p"}]},
        ),
        ("/v1/responses", {"model": "m", "input": "p"}),
    ],
)
def test_endpoint_conflict_rejected_before_engine_lookup(monkeypatch, path, body):
    monkeypatch.setattr(srv._server_state, "api_key", None)
    monkeypatch.setattr(srv._server_state, "global_settings", None)
    monkeypatch.setattr(
        srv,
        "get_engine_for_model",
        lambda *a, **kw: pytest.fail("engine lookup must not run"),
    )
    client = TestClient(srv.app, raise_server_exceptions=False)
    response = client.post(
        path,
        json={**body, "cache_policy": {"kv": "none"}},
        headers={"X-oMLX-KV-Cache": "memory"},
    )
    assert response.status_code == 400
    assert "conflicts" in response.json()["error"]["message"]


@pytest.mark.parametrize("endpoint", ["completion", "chat", "messages", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_all_generation_endpoints_acknowledge_and_forward_policy(
    monkeypatch, endpoint, stream
):
    scheduler = SimpleNamespace(
        block_aware_cache=object(),
        paged_ssd_cache_manager=SimpleNamespace(
            _hot_cache_enabled=True, _hot_cache_only=True, _cache_dir=None
        ),
        config=SimpleNamespace(),
    )
    engine = SimpleNamespace(
        tokenizer=SimpleNamespace(encode=lambda prompt: [1]),
        prefix_cache_enabled=True,
        model_type="test",
        _engine=SimpleNamespace(engine=SimpleNamespace(scheduler=scheduler)),
        _vision_cache=SimpleNamespace(_cache_dir=None, _max_memory_bytes=1024),
        supports_cache_policy_component=lambda component: True,
        preflight_completion=AsyncMock(),
        preflight_chat=AsyncMock(),
    )

    class Lease:
        def abort_reason(self):
            return None

        async def release(self):
            return None

    async def capture_json_response(http_request, coro, *, headers=None, **kwargs):
        coro.close()
        return Response(content="{}", headers=headers)

    messages = [{"role": "user", "content": "hello"}]
    monkeypatch.setattr(srv, "_LLMEngineLease", Lease)
    monkeypatch.setattr(srv, "get_engine_for_model", AsyncMock(return_value=engine))
    monkeypatch.setattr(srv, "_serving_model_id", lambda lease, requested: requested)
    monkeypatch.setattr(srv, "get_engine_pool", lambda: SimpleNamespace(get_entry=lambda _: None))
    monkeypatch.setattr(srv._server_state, "global_settings", None)
    monkeypatch.setattr(srv._server_state, "oq_manager", None)
    monkeypatch.setattr(srv._server_state, "settings_manager", None)
    monkeypatch.setattr(srv._server_state, "mcp_manager", None)
    monkeypatch.setattr(
        srv,
        "get_sampling_params",
        lambda *a, **kw: (0.0, 0.9, 0, 1.0, 0.0, 0.0, 0.0, 4, 0.0, 0.1),
    )
    monkeypatch.setattr(srv, "_ensure_tokenizer_for_system_probe", AsyncMock())
    monkeypatch.setattr(srv, "prepare_system_messages_for_template", lambda msgs, *a, **kw: msgs)
    monkeypatch.setattr(srv, "_prepare_chat_prompt_for_request", lambda *a, **kw: (1, None))
    monkeypatch.setattr(srv, "validate_context_window", lambda *a, **kw: None)
    monkeypatch.setattr(srv, "_resolve_thinking_budget", lambda *a, **kw: None)
    monkeypatch.setattr(srv, "_no_repeat_ngram_kwargs", lambda *a, **kw: {})
    monkeypatch.setattr(srv, "cache_reasoning_output", lambda *a, **kw: False)
    monkeypatch.setattr(srv, "_json_response_or_keepalive", capture_json_response)
    monkeypatch.setattr(srv, "_merge_chat_template_kwargs", lambda *a, **kw: ({}, None))
    monkeypatch.setattr(srv, "merge_chat_template_request_kwargs", lambda *a, **kw: {})
    monkeypatch.setattr(
        srv,
        "_extract_chat_messages",
        lambda *a, **kw: srv._ChatTemplateMessages(messages, False, False),
    )
    monkeypatch.setattr(srv, "_resolve_template_tools", lambda *a, **kw: None)
    monkeypatch.setattr(srv, "_apply_preserve_thinking_default", lambda *a, **kw: None)
    monkeypatch.setattr(srv, "_engine_accepts_images", lambda *a, **kw: True)
    monkeypatch.setattr(srv, "convert_anthropic_to_internal", lambda *a, **kw: messages)
    monkeypatch.setattr(srv, "convert_anthropic_to_internal_harmony", lambda *a, **kw: messages)
    monkeypatch.setattr(srv, "uses_native_reasoning_content", lambda *a, **kw: False)
    monkeypatch.setattr(srv, "convert_anthropic_tools_to_internal", lambda *a, **kw: None)
    monkeypatch.setattr(srv, "forced_ct_keys", lambda *a, **kw: set())
    monkeypatch.setattr(srv, "convert_responses_input_to_messages", lambda *a, **kw: messages)
    monkeypatch.setattr(srv, "convert_responses_tools", lambda *a, **kw: (None, {}))
    monkeypatch.setattr(srv, "apply_namespace_tool_aliases", lambda *a, **kw: None)
    monkeypatch.setattr(srv, "mcp_tools_exposed", lambda: False)

    policy = {"kv": "memory", "images": "none"}
    if endpoint == "completion":
        api_request = CompletionRequest(
            model="m", prompt="hello", stream=stream, cache_policy=policy
        )
        response = await srv.create_completion(api_request, _http_request(), True)
        checked = engine.preflight_completion
    elif endpoint == "chat":
        api_request = ChatCompletionRequest(
            model="m", messages=messages, stream=stream, cache_policy=policy
        )
        response = await srv.create_chat_completion(api_request, _http_request(), True)
        checked = engine.preflight_chat
    elif endpoint == "messages":
        api_request = MessagesRequest(
            model="m", max_tokens=4, messages=messages, stream=stream, cache_policy=policy
        )
        response = await srv.create_anthropic_message(api_request, _http_request(), True)
        checked = engine.preflight_chat
    else:
        api_request = ResponsesRequest(
            model="m", input="hello", stream=stream, cache_policy=policy
        )
        response = await srv.create_response(api_request, _http_request(), True)
        checked = engine.preflight_chat

    assert response.headers["x-omlx-kv-cache"] == "memory"
    assert response.headers["x-omlx-image-cache"] == "none"
    if stream:
        assert isinstance(response, StreamingResponse)
    else:
        assert isinstance(response, Response)
    checked.assert_awaited_once()
    assert checked.await_args.kwargs["cache_policy"] == CachePolicy(**policy)


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
    assert response.headers["x-omlx-image-cache"] == "disabled"
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
