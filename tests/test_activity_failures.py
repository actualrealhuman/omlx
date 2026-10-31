# SPDX-License-Identifier: Apache-2.0
"""API semantic failure signals must survive a successful HTTP transport."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel
from test_activity_output import Engine

from omlx.activity import ActivityRecorder, ActivitySettings
from omlx.activity.middleware import ActivityTransportMiddleware
from omlx.activity.runtime import (
    RequestCapture,
    api_failure,
    configure_recorder,
    current_capture,
    observe_request,
)
from omlx.api.anthropic_models import MessagesRequest
from omlx.api.openai_models import ChatCompletionRequest, CompletionRequest
from omlx.api.responses_models import ResponsesRequest
from omlx.exceptions import (
    PrefillMemoryAbortedError,
    PrefillMemoryExceededError,
    RequestAbortedError,
)


class Payload(BaseModel):
    model: str = "example"


@pytest.fixture
def recorder(tmp_path):
    result = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    assert result.wait_ready()
    previous = configure_recorder(result)
    yield result
    configure_recorder(previous)
    assert result.close()


def formatted_failure(family):
    from omlx import server

    engine = Engine(family, broken=True)
    messages = [{"role": "user", "content": "hello"}]
    if family == "completions":
        return server.stream_completion(
            engine,
            "hello",
            CompletionRequest(model="example", prompt="hello", stream=True),
        )
    if family == "chat/completions":
        return server.stream_chat_completion(
            engine,
            messages,
            ChatCompletionRequest(model="example", messages=messages, stream=True),
        )
    if family == "messages":
        return server.stream_anthropic_messages(
            engine,
            messages,
            MessagesRequest(
                model="example", messages=messages, max_tokens=16, stream=True
            ),
        )
    return server.stream_responses_api(
        engine,
        messages,
        ResponsesRequest(model="example", input="hello", stream=True),
        store_response=False,
    )


@pytest.mark.parametrize(
    "family", ["completions", "chat/completions", "messages", "responses"]
)
@pytest.mark.parametrize("completed_invocation", [False, True])
def test_actual_formatter_error_under_observer_is_not_http_success(
    recorder, family, completed_invocation
):
    app = FastAPI()
    app.add_middleware(ActivityTransportMiddleware)

    @app.post("/v1/" + family)
    @observe_request
    async def endpoint(request: Payload, http_request: Request):
        if completed_invocation:
            current_capture().invocation("synthetic-completed").end("completed")
        return StreamingResponse(
            formatted_failure(family), media_type="text/event-stream"
        )

    with TestClient(app) as client:
        response = client.post("/v1/" + family, json={"model": "example"})
    assert response.status_code == 200
    assert "error" in response.text
    assert recorder.flush()
    row = recorder.reader.list_requests()[0]
    assert row["state"] == "error"
    assert row["api_failure_outcome"] == "error"
    assert response.headers["x-omlx-request-id"] == row["id"]


def exception_for(kind):
    return {
        "guard": PrefillMemoryExceededError("synthetic guard"),
        "mid_prefill": PrefillMemoryAbortedError("synthetic abort"),
        "aborted": RequestAbortedError("synthetic abort"),
        "rejected": HTTPException(400, "synthetic request"),
        "failed": HTTPException(500, "synthetic failure"),
    }[kind]


@pytest.mark.parametrize("wrapper", ["json", "json_grace", "sse"])
@pytest.mark.parametrize(
    "kind,expected",
    [
        ("guard", "rejected"),
        ("mid_prefill", "cancelled"),
        ("aborted", "cancelled"),
        ("rejected", "rejected"),
        ("failed", "error"),
    ],
)
def test_actual_keepalive_wrappers_preserve_protocol_outcome(
    recorder, monkeypatch, wrapper, kind, expected
):
    from omlx import server

    monkeypatch.setattr(server, "_JSON_KEEPALIVE_GRACE_S", 0)
    app = FastAPI()
    app.add_middleware(ActivityTransportMiddleware)

    @app.post("/v1/chat/completions")
    @observe_request
    async def endpoint(request: Payload, http_request: Request):
        async def result():
            await asyncio.sleep(0.001)
            raise exception_for(kind)

        async def stream():
            await result()
            yield "unreachable"

        if wrapper == "json_grace":
            return await server._json_response_or_keepalive(http_request, result())
        iterator = (
            server._with_json_keepalive(None, result())
            if wrapper == "json"
            else server._with_sse_keepalive(stream())
        )
        return StreamingResponse(iterator)

    with TestClient(app) as client:
        response = client.post("/v1/chat/completions", json={"model": "example"})
    assert response.status_code == 200
    assert "error" in response.text
    assert recorder.flush()
    row = recorder.reader.list_requests()[0]
    assert row["state"] == expected
    assert row["api_failure_outcome"] == expected
    assert row["api_failure_source"] == "api_formatter"


def test_protocol_failure_waits_for_engine_and_survives_content_capture_refusal(
    recorder, monkeypatch
):
    handle = recorder.begin(model_id="example", endpoint="synthetic")
    capture = RequestCapture(recorder, handle, require_transport=True)
    invocation = capture.invocation("still-running")
    # No error wire needs to fit for the protected failure metadata to survive.
    monkeypatch.setattr(recorder, "append_text", lambda *args, **kwargs: False)
    assert capture.api_failure("error")
    capture.api_done("completed")
    capture.transport_done("completed")
    assert recorder.flush()
    assert recorder.reader.get(handle.request_id)["finished_at"] is None
    invocation.end("completed")
    assert recorder.flush()
    row = recorder.reader.get(handle.request_id)
    assert row["state"] == "error" and row["api_failure_outcome"] == "error"
    assert not capture.api_failure("rejected")  # Do not reopen terminal records.


def test_signals_are_scoped_idempotent_and_do_not_revive_disabled_history(recorder):
    from dataclasses import replace

    assert api_failure("error") is False
    handle = recorder.begin(model_id="example", endpoint="synthetic")
    capture = RequestCapture(recorder, handle)
    assert not capture.api_failure("completed")
    assert capture.api_failure("error") and capture.api_failure("error")
    assert recorder.flush()
    assert recorder.reader.get(handle.request_id)["api_failure_outcome"] == "error"
    recorder.update_settings(replace(recorder.settings, enabled=False)).result(5)
    assert not capture.api_failure("rejected")
    capture.api_done("completed")
    assert recorder.flush()
    assert recorder.reader.get(handle.request_id)["state"] == "capture_stopped"


@pytest.mark.parametrize("family", ["chat", "anthropic", "responses"])
@pytest.mark.parametrize("bad", [False, True])
async def test_terminal_tool_parse_failure_is_signaled_but_recovered_prose_is_not(
    recorder, monkeypatch, family, bad
):
    from integration.test_e2e_streaming import _RECOVERY_PROSE, _recovery_stream

    from omlx import server
    from omlx.activity.runtime import _current

    monkeypatch.setattr(
        server,
        "get_server_metrics",
        lambda: SimpleNamespace(record_request_complete=lambda **kwargs: None),
    )
    handle = recorder.begin(model_id="example", endpoint="synthetic")
    capture = RequestCapture(recorder, handle)
    capture.invocation("synthetic-finished").end("completed")
    token = _current.set(capture)
    try:
        text = (
            "<tool_call><function=write><parameter=content>cut"
            if bad
            else _RECOVERY_PROSE
        )
        events = await _recovery_stream(text, family)
        assert (
            any(
                "error" in event or event.get("type") == "response.failed"
                for event in events
            )
            is bad
        )
        capture.api_done("completed")
    finally:
        _current.reset(token)
    assert recorder.flush()
    row = recorder.reader.get(handle.request_id)
    assert row["state"] == ("error" if bad else "completed")
    assert ("api_failure_outcome" in row) is bad


def test_concurrent_success_and_failure_keep_their_own_outcomes(recorder, monkeypatch):
    from omlx import server

    monkeypatch.setattr(
        server,
        "get_server_metrics",
        lambda: SimpleNamespace(record_request_complete=lambda **kwargs: None),
    )
    app = FastAPI()
    app.add_middleware(ActivityTransportMiddleware)
    waiting, arrived = asyncio.Event(), 0

    @app.post("/v1/chat/completions")
    @observe_request
    async def endpoint(request: Payload, http_request: Request):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            waiting.set()
        await asyncio.wait_for(waiting.wait(), 2)
        if request.model == "failed":
            iterator = formatted_failure("chat/completions")
        else:
            messages = [{"role": "user", "content": "hello"}]
            iterator = server.stream_chat_completion(
                Engine("chat/completions"),
                messages,
                ChatCompletionRequest(model="example", messages=messages, stream=True),
            )
        return StreamingResponse(iterator, media_type="text/event-stream")

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as executor:
        jobs = [
            executor.submit(client.post, "/v1/chat/completions", json={"model": model})
            for model in ["failed", "successful"]
        ]
        assert all(job.result(5).status_code == 200 for job in jobs)
    assert recorder.flush()
    rows = {row["model_id"]: row for row in recorder.reader.list_requests()}
    assert rows["failed"]["state"] == "error"
    assert rows["successful"]["state"] == "completed"
    assert "api_failure_outcome" not in rows["successful"]
