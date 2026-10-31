# SPDX-License-Identifier: Apache-2.0
"""Exercise observer hooks through production core/scheduler, with mock models."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from omlx.activity import ActivityRecorder, ActivitySettings
from omlx.activity.runtime import (
    configure_recorder,
    inference_request_id,
    observe_request,
)
from omlx.engine_core import EngineCore
from omlx.request import RequestOutput
from omlx.scheduler import SchedulerOutput


@pytest.fixture
def observed_engine(tmp_path, mock_model, mock_tokenizer):
    recorder = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    assert recorder.wait_ready()
    previous = configure_recorder(recorder)
    with patch("omlx.engine_core.get_registry") as registry:
        registry.return_value.acquire.return_value = True
        core = EngineCore(model=mock_model, tokenizer=mock_tokenizer)
        try:
            yield core, recorder
        finally:
            core.close()
            configure_recorder(previous)
            recorder.close()


class Payload(BaseModel):
    model: str


def arguments():
    return dict(
        request=Payload(model="synthetic-model"),
        http_request=SimpleNamespace(
            state=SimpleNamespace(), url=SimpleNamespace(path="/v1/chat/completions")
        ),
    )


@pytest.mark.asyncio
async def test_actual_prepared_tokens_and_core_output_are_observed(observed_engine):
    core, recorder = observed_engine
    # Supplied token IDs are already prepared: observing must not tokenize again.
    core.tokenizer.encode = Mock(
        side_effect=AssertionError("retokenized prepared input")
    )

    @observe_request
    async def endpoint(request, http_request):
        rid = inference_request_id(http_request)
        await core.add_request(
            [11, 12, 13], request_id=rid, activity_prompt="<user>actual<assistant>"
        )
        core.scheduler.uid_to_request_id[73] = rid
        with patch("omlx.scheduler.get_prefill_tracker"):
            core.scheduler._on_prompt_progress([(73, 1, 3)])
        assert recorder.flush()
        assert recorder.reader.get(rid)["state"] == "prefilling"

        def step():
            core._running = False
            return [
                SchedulerOutput(
                    outputs=[
                        RequestOutput(
                            request_id=rid,
                            new_text="<think>x</think>answer",
                            output_text="<think>x</think>answer",
                            completion_tokens=7,
                            finished=True,
                            finish_reason="stop",
                        )
                    ]
                )
            ]

        core._step_burst = step
        core._running = True
        await core._engine_loop()
        core._cleanup_request(rid)
        return JSONResponse({"answer": "answer"})

    response = await endpoint(**arguments())
    assert recorder.flush()
    rid = response.headers["x-omlx-request-id"]
    assert recorder.reader.get(rid)["state"] == "completed"
    detail = recorder.reader.detail(rid)
    assert detail["observed_input_tokens"] == 3
    assert detail["observed_generated_tokens"] == 7
    assert detail["observed_invocations"] == 1
    assert detail["prefilling_invocations"] == 1
    assert detail["first_observed_token_ms"] >= 0
    assert detail["observation"]["preparser_output_guaranteed"] is False
    events = [
        json.loads(line)
        for line in recorder.reader.content(rid, "events")["data"].splitlines()
    ]
    assert events[0]["sampling_subset"]["max_tokens"] == 256
    assert events[-1]["finish_reason"] == "stop"
    assert (
        recorder.reader.content(rid, "model_input", invocation_id="1")["data"]
        == b"<user>actual<assistant>"
    )
    assert json.loads(
        recorder.reader.content(rid, "model_tokens", invocation_id="1")["data"]
    ) == [11, 12, 13]
    assert (
        recorder.reader.content(rid, "raw_output", invocation_id="1")["data"]
        == b"<think>x</think>answer"
    )
    assert not core._activity_invocations


@pytest.mark.asyncio
async def test_deferred_abort_is_not_terminal_until_scheduler_applies_it(
    observed_engine,
):
    core, recorder = observed_engine

    @observe_request
    async def endpoint(request, http_request):
        await core.add_request([11, 12], request_id=inference_request_id(http_request))
        return JSONResponse({"transport": "ended before engine"})

    response = await endpoint(**arguments())
    rid = response.headers["x-omlx-request-id"]
    assert recorder.flush()
    assert recorder.reader.get(rid)["finished_at"] is None
    await core.abort_request(rid)
    assert recorder.flush()
    assert recorder.reader.get(rid)["finished_at"] is None
    await asyncio.get_running_loop().run_in_executor(
        core._mlx_executor, core.scheduler._process_pending_aborts
    )
    assert recorder.flush()
    assert recorder.reader.get(rid)["state"] == "cancelled"
    core._cleanup_request(rid)
    assert not core._activity_invocations


@pytest.mark.asyncio
async def test_scheduler_insert_failure_does_not_leak_activity_invocation(
    observed_engine,
):
    core, recorder = observed_engine

    @observe_request
    async def endpoint(request, http_request):
        await core.add_request([11], request_id=inference_request_id(http_request))
        return JSONResponse({})

    with (
        patch.object(
            core.scheduler,
            "add_request",
            side_effect=ValueError("synthetic insert failure"),
        ),
        pytest.raises(ValueError, match="synthetic insert failure"),
    ):
        await endpoint(**arguments())
    assert recorder.flush()
    assert recorder.reader.list_requests()[0]["state"] == "error"
    assert not core._activity_invocations
