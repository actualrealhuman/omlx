# SPDX-License-Identifier: Apache-2.0
"""Production API routes/auth/formatters with synthetic, instrumented generation.

No server lifespan, listeners, model loaders or real scheduler generation run.
Core/scheduler instrumentation is qualified separately in test_activity_engine.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from omlx import server
from omlx.activity import ActivityRecorder, ActivitySettings
from omlx.activity.middleware import ActivityTransportMiddleware
from omlx.activity.runtime import configure_recorder, current_capture
from omlx.admin import auth
from omlx.engine import BatchedEngine
from omlx.engine.base import GenerationOutput
from omlx.settings import GlobalSettings, SubKeyEntry

PATHS = ("/v1/completions", "/v1/chat/completions", "/v1/messages", "/v1/responses")
ANSWER = "route-answer-世界"


def payload(path, stream=False):
    body = {"model": "requested-alias", "stream": stream, "max_tokens": 16}
    if path == "/v1/completions":
        body["prompt"] = "submitted prompt 世界"
    elif path == "/v1/responses":
        body["input"] = "submitted prompt 世界"
        body.pop("max_tokens")
        body["max_output_tokens"] = 16
        body["store"] = False
    else:
        body["messages"] = [{"role": "user", "content": "submitted prompt 世界"}]
    return body


@pytest.fixture
def api(tmp_path, monkeypatch):
    settings = GlobalSettings(base_path=tmp_path)
    settings.auth.api_key = "test-owner"
    entry = SubKeyEntry(key="test-client", name="Example client")
    settings.auth.sub_keys = [entry]
    pool = SimpleNamespace(
        get_entry=lambda _: None,
        get_abort_requested_reason=lambda _: None,
        release_engine=AsyncMock(),
    )
    state = server.ServerState(
        engine_pool=pool,
        default_model="resolved-model",
        api_key="test-owner",
        global_settings=settings,
    )
    monkeypatch.setattr(server, "_server_state", state)
    monkeypatch.setattr(server.app, "dependency_overrides", {})
    monkeypatch.setattr(auth, "_key_failures", {})
    monkeypatch.setattr(server, "resolve_model_id", lambda _: "resolved-model")
    monkeypatch.setattr(server, "get_server_metrics", lambda: MagicMock())
    recorder = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    assert recorder.wait_ready()
    previous = configure_recorder(recorder)
    try:
        calls = []
        engine = BatchedEngine.__new__(BatchedEngine)
        engine._model = None
        engine._tokenizer = SimpleNamespace(
            encode=lambda text, **kwargs: list(text.encode("utf-8")),
            decode=lambda ids, **kwargs: bytes(ids).decode("utf-8", errors="replace"),
            chat_template="plain-template",
            eos_token="<eos>",
            bos_token="",
        )

        def prepare(messages, *args, **kwargs):
            text = "<synthetic-prepared>" + json.dumps(messages, ensure_ascii=False)
            return text, engine.tokenizer.encode(text)

        engine.prepare_chat_prompt = prepare
        engine.count_chat_tokens = lambda messages, *args, **kwargs: len(
            prepare(messages)[1]
        )
        engine.preflight_completion = AsyncMock()
        engine.preflight_chat = AsyncMock()
        engine.start = AsyncMock()
        engine.abort_request = AsyncMock()
        engine.fail_generation = False

        async def resolve(model, engine_type, **kwargs):
            if kwargs.get("_lease"):
                kwargs["_leased_out"].append("resolved-model")
            return engine

        monkeypatch.setattr(server, "get_engine", resolve)

        def begin(kind, prompt, messages, kwargs):
            text, ids = (
                (prompt, engine.tokenizer.encode(prompt))
                if prompt is not None
                else kwargs.get("_prepared_prompt") or prepare(messages)
            )
            capture = current_capture()
            observation = (
                capture.invocation(
                    kwargs.get("_request_id") or f"synthetic-{len(calls) + 1}",
                    options=tuple(
                        (key, kwargs[key])
                        for key in ("max_tokens", "temperature", "seed")
                        if key in kwargs
                    ),
                )
                if capture is not None
                else None
            )
            if capture is not None:
                assert observation is not None
                observation.prepared(text, ids)
            calls.append(
                {
                    "kind": kind,
                    "text": text,
                    "tokens": ids,
                    "request_id": capture.handle.request_id if capture else None,
                }
            )
            return observation, ids

        async def generate(prompt=None, messages=None, **kwargs):
            observation, ids = begin("json", prompt, messages, kwargs)
            if engine.fail_generation:
                if observation is not None:
                    observation.end("error")
                raise ValueError("synthetic generation failure")
            if observation is not None:
                observation.output(
                    SimpleNamespace(
                        new_text=ANSWER,
                        completion_tokens=3,
                        finished=True,
                        error=None,
                        finish_reason="stop",
                    )
                )
            return GenerationOutput(
                text=ANSWER,
                new_text=ANSWER,
                prompt_tokens=len(ids),
                completion_tokens=3,
            )

        async def stream(prompt=None, messages=None, **kwargs):
            observation, ids = begin("stream", prompt, messages, kwargs)
            accumulated = ""
            for count, fragment in enumerate(("route-", "answer-", "世界"), 1):
                accumulated += fragment
                finished = count == 3
                if engine.fail_generation and count == 2:
                    if observation is not None:
                        observation.end("error")
                    raise ValueError("synthetic generation failure")
                if observation is not None:
                    observation.output(
                        SimpleNamespace(
                            new_text=fragment,
                            completion_tokens=count,
                            finished=finished,
                            error=None,
                            finish_reason="stop" if finished else None,
                        )
                    )
                yield GenerationOutput(
                    text=accumulated,
                    new_text=fragment,
                    prompt_tokens=len(ids),
                    completion_tokens=count,
                    finished=finished,
                    finish_reason="stop" if finished else None,
                )

        engine.generate = engine.chat = generate
        engine.stream_generate = engine.stream_chat = stream
        # Reuse original APIRoute objects (including validated dependencies), not
        # copied endpoint declarations or authentication overrides. No real lifespan.
        app = FastAPI(exception_handlers=dict(server.app.exception_handlers))
        app.router.routes.extend(
            route
            for route in server.app.routes
            if getattr(route, "path", None) in PATHS
        )
        app.add_middleware(ActivityTransportMiddleware)
        with TestClient(app, raise_server_exceptions=False) as client:
            yield SimpleNamespace(
                client=client,
                recorder=recorder,
                engine=engine,
                calls=calls,
                pool=pool,
                settings=settings,
                entry=entry,
            )
    finally:
        configure_recorder(previous)
        assert recorder.close()


def records(api):
    assert api.recorder.flush()
    return api.recorder.reader.list_requests()


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("stream", [False, True])
def test_real_route_records_named_source_payload_prepared_output_and_exact_wire(
    api, path, stream
):
    body = payload(path, stream)
    response = api.client.post(
        path, json=body, headers={"authorization": "Bearer test-client"}
    )
    assert response.status_code == 200, response.text
    rows = records(api)
    assert len(rows) == 1
    row = rows[0]
    assert response.headers["x-omlx-request-id"] == row["id"]
    assert row["state"] == "completed"
    assert row["source_id"] == api.entry.id and row["source_name"] == "Example client"
    assert row["model_id"] == "resolved-model"
    assert row["endpoint"] == path
    assert row["observed_invocations"] == 1 and row["observed_generated_tokens"] == 3
    assert api.calls[0]["request_id"] == row["id"]
    reader = api.recorder.reader
    submitted = json.loads(reader.content(row["id"], "submitted")["data"])
    field = (
        "prompt"
        if path == "/v1/completions"
        else "input"
        if path == "/v1/responses"
        else "messages"
    )
    assert submitted[field] == body[field]
    assert submitted["model"] == "requested-alias"
    assert (
        reader.content(row["id"], "model_input", invocation_id="1")["data"].decode()
        == api.calls[0]["text"]
    )
    assert (
        json.loads(reader.content(row["id"], "model_tokens", invocation_id="1")["data"])
        == api.calls[0]["tokens"]
    )
    assert (
        reader.content(row["id"], "raw_output", invocation_id="1")["data"].decode()
        == ANSWER
    )
    assert reader.content(row["id"], "api_response")["data"] == response.content
    detail = reader.detail(row["id"])
    assert detail["coverage"]["backend_kinds"] == ["local_text"]
    assert detail["invocations"][0]["generated_tokens"] == 3
    assert api.pool.release_engine.await_count == 1
    retained = json.dumps(detail) + json.dumps(submitted)
    assert "test-client" not in retained and "test-owner" not in retained


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("mode", ["main", "anthropic_key", "anonymous_opt_in"])
def test_real_auth_source_attribution_modes(api, path, mode):
    if mode == "main":
        headers = {"authorization": "Bearer test-owner"}
        source = ("main", "Main key")
    elif mode == "anthropic_key":
        headers = {"x-api-key": "test-client"}
        source = (api.entry.id, "Example client")
    else:
        api.settings.auth.allow_unauthenticated_inference = True
        headers = {}
        source = ("anonymous", "Unauthenticated")
    response = api.client.post(path, json=payload(path), headers=headers)
    assert response.status_code == 200, response.text
    (row,) = records(api)
    assert (row["source_id"], row["source_name"]) == source


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer test-invalid"}])
def test_unauthorized_requests_never_start_capture_or_generation(api, path, headers):
    response = api.client.post(path, json=payload(path), headers=headers)
    assert response.status_code == 401
    assert records(api) == [] and api.calls == []
    assert "x-omlx-request-id" not in response.headers
    assert api.pool.release_engine.await_count == 0


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("malformed_json", [False, True])
def test_invalid_json_or_request_schema_never_enters_activity(
    api, path, malformed_json
):
    body = payload(path)
    field = (
        "prompt"
        if path == "/v1/completions"
        else "input"
        if path == "/v1/responses"
        else "messages"
    )
    body[field] = {} if field != "messages" else "not a message list"
    response = api.client.post(
        path,
        content=b'{"broken":' if malformed_json else json.dumps(body).encode(),
        headers={
            "authorization": "Bearer test-client",
            "content-type": "application/json",
        },
    )
    assert response.status_code == 422, response.text
    assert records(api) == [] and api.calls == []
    assert "x-omlx-request-id" not in response.headers


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("stream", [False, True])
def test_accepted_preflight_rejection_retains_input_and_exact_error_wire(
    api, path, stream
):
    error = HTTPException(status_code=413, detail="synthetic preflight rejection")
    api.engine.preflight_chat.side_effect = error
    api.engine.preflight_completion.side_effect = error
    response = api.client.post(
        path,
        json=payload(path, stream),
        headers={"authorization": "Bearer test-client"},
    )
    assert response.status_code == 413, response.text
    (row,) = records(api)
    assert row["state"] == "rejected" and row["source_name"] == "Example client"
    assert response.headers["x-omlx-request-id"] == row["id"]
    assert not row.get("observed_invocations") and api.calls == []
    assert api.recorder.reader.content(row["id"], "submitted")["data"]
    assert (
        api.recorder.reader.content(row["id"], "api_response")["data"]
        == response.content
    )
    assert api.pool.release_engine.await_count == 1


def test_nonstream_multiple_prompts_keep_each_invocation_and_prepared_input(api):
    body = payload("/v1/completions")
    body["prompt"] = ["first prompt", "second prompt"]
    response = api.client.post(
        "/v1/completions", json=body, headers={"authorization": "Bearer test-client"}
    )
    assert response.status_code == 200, response.text
    (row,) = records(api)
    assert row["observed_invocations"] == 2 and row["observed_generated_tokens"] == 6
    assert len(response.json()["choices"]) == 2
    assert len(api.calls) == 2
    for index, prompt in enumerate(body["prompt"], 1):
        assert (
            api.recorder.reader.content(
                row["id"], "model_input", invocation_id=str(index)
            )["data"].decode()
            == prompt
        )
    detail = api.recorder.reader.detail(row["id"])
    assert [item["id"] for item in detail["invocations"]] == ["1", "2"]
    assert (
        json.loads(api.recorder.reader.content(row["id"], "submitted")["data"])[
            "prompt"
        ]
        == body["prompt"]
    )


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("stream", [False, True])
def test_capture_off_preserves_production_route_responses_without_recording(
    api, path, stream
):
    from dataclasses import replace

    api.recorder.update_settings(replace(api.recorder.settings, enabled=False)).result(
        3
    )
    response = api.client.post(
        path,
        json=payload(path, stream),
        headers={"authorization": "Bearer test-client"},
    )
    assert response.status_code == 200, response.text
    assert "route-" in response.text
    assert records(api) == []
    assert len(api.calls) == 1 and api.calls[0]["request_id"] is None
    assert api.pool.release_engine.await_count == 1


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize("stream", [False, True])
def test_generation_failure_preserves_outcome_and_distinguishes_outer_error_wire(
    api, path, stream
):
    api.engine.fail_generation = True
    response = api.client.post(
        path,
        json=payload(path, stream),
        headers={"authorization": "Bearer test-client"},
    )
    (row,) = records(api)
    assert row["state"] == "error", response.text
    assert response.headers["x-omlx-request-id"] == row["id"]
    wire = api.recorder.reader.content(row["id"], "api_response")["data"]
    if stream:
        assert "synthetic generation failure" in response.text
        assert wire == response.content
    else:
        # Starlette's outer ServerErrorMiddleware response bypasses the user
        # middleware after it finalized the failed transport. Do not reopen it.
        assert response.status_code == 500
        assert "Internal server error" in response.text
        assert "synthetic generation failure" not in response.text
        assert wire == b""
        assert (
            api.recorder.reader.detail(row["id"])["coverage"]["evidence"]["api_wire"]
            == "not_observed"
        )
    assert api.recorder.reader.content(row["id"], "raw_output", invocation_id="1")[
        "data"
    ] == (b"route-" if stream else b"")
    assert api.recorder.reader.detail(row["id"])["invocations"][0]["state"] == "error"
    assert api.pool.release_engine.await_count == 1
