# SPDX-License-Identifier: Apache-2.0
"""Run the browser projection against actual production formatter output."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from omlx.api.anthropic_models import MessagesRequest
from omlx.api.anthropic_utils import convert_internal_to_anthropic_response
from omlx.api.openai_models import (
    AssistantMessage,
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    CompletionChoice,
    CompletionRequest,
    CompletionResponse,
    FunctionCall,
    ToolCall,
    Usage,
)
from omlx.api.responses_models import ResponseObject, ResponsesRequest
from omlx.api.responses_utils import (
    build_function_call_output_item,
    build_message_output_item,
    build_reasoning_output_item,
)
from omlx.engine.base import GenerationOutput

ROOT = Path(__file__).resolve().parents[1]


class Tokenizer:
    eos_token_id = 2

    def encode(self, text, **kwargs):
        return [1] * len(text.split())

    def apply_chat_template(self, messages, **kwargs):
        return "user: hello\nassistant:"


class Engine:
    tokenizer = Tokenizer()
    model_type = "llama"
    supports_early_tool_call_streaming = False
    prefix_cache_enabled = False

    def __init__(self, family, broken=False):
        self.family, self.broken = family, broken

    async def stream_generate(self, **kwargs):
        if self.broken:
            raise RuntimeError("synthetic formatter failure")
        yield GenerationOutput(
            text="answer 世界🙂", new_text="answer 世界🙂", completion_tokens=2
        )

    async def stream_chat(self, **kwargs):
        if self.broken:
            raise RuntimeError("synthetic formatter failure")
        yield GenerationOutput(
            text="<think>reason 世界🙂</think>",
            new_text="<think>reason 世界🙂</think>",
            completion_tokens=1,
            finished=False,
        )
        yield GenerationOutput(
            text="<think>reason 世界🙂</think>answer",
            new_text="answer",
            completion_tokens=2,
            finish_reason="tool_calls",
            tool_calls=[
                {"id": "call-example", "name": "lookup", "arguments": '{"x":1}'}
            ],
        )


def project(family, wire):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for actual browser projection checks")
    script = """
const fs = require('fs');
const {ApiOutput} = require('./apps/omlx-web/omlx_web/static/js/activity_output.js');
const {family, wire} = JSON.parse(fs.readFileSync(0, 'utf8'));
const results = [1, 7, 65536].map(stride => {
    const view = new ApiOutput('/v1/' + family);
    for (let n = 0; n < wire.length; n += stride) view.feed(wire.slice(n, n + stride));
    view.finish(true);
    return {status: view.status(), notes: [...view.notes], sections: view.sections()};
});
process.stdout.write(JSON.stringify(results));
"""
    result = subprocess.run(
        [node, "-e", script],
        cwd=ROOT,
        input=json.dumps({"family": family, "wire": wire}),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    results = json.loads(result.stdout)
    assert results[0] == results[1] == results[2]
    return results[0]


@pytest.mark.parametrize(
    "family", ["completions", "chat/completions", "messages", "responses"]
)
@pytest.mark.parametrize("streaming", [True, False])
async def test_projection_matches_actual_stream_formatters_and_response_builders(
    monkeypatch, family, streaming
):
    from omlx import server

    monkeypatch.setattr(
        server,
        "get_server_metrics",
        lambda: SimpleNamespace(record_request_complete=lambda **kwargs: None),
    )
    call = ToolCall(
        id="call-example", function=FunctionCall(name="lookup", arguments='{"x":1}')
    )
    messages = [{"role": "user", "content": "hello"}]
    if streaming:
        engine = Engine(family)
        kwargs = {"max_tokens": 16, "temperature": 0.7, "top_p": 0.9, "top_k": 40}
        if family == "completions":
            iterator = server.stream_completion(
                engine,
                "hello",
                CompletionRequest(model="example", prompt="hello", stream=True),
            )
        elif family == "chat/completions":
            iterator = server.stream_chat_completion(
                engine,
                messages,
                ChatCompletionRequest(model="example", messages=messages, stream=True),
                **kwargs,
            )
        elif family == "messages":
            iterator = server.stream_anthropic_messages(
                engine,
                messages,
                MessagesRequest(
                    model="example", messages=messages, max_tokens=16, stream=True
                ),
                **kwargs,
            )
        else:
            iterator = server.stream_responses_api(
                engine,
                messages,
                ResponsesRequest(model="example", input="hello", stream=True),
                store_response=False,
                **kwargs,
            )
        wire = "".join([event async for event in iterator])
    else:
        if family == "completions":
            response = CompletionResponse(
                model="example",
                choices=[CompletionChoice(index=0, text="answer 世界🙂")],
                usage=Usage(),
            )
        elif family == "chat/completions":
            response = ChatCompletionResponse(
                model="example",
                choices=[
                    ChatCompletionChoice(
                        index=0,
                        message=AssistantMessage(
                            content="answer",
                            reasoning_content="reason 世界🙂",
                            tool_calls=[call],
                        ),
                    )
                ],
                usage=Usage(),
            )
        elif family == "messages":
            response = convert_internal_to_anthropic_response(
                text="answer",
                thinking="reason 世界🙂",
                model="example",
                prompt_tokens=1,
                completion_tokens=2,
                finish_reason="tool_calls",
                tool_calls=[call],
            )
        else:
            response = ResponseObject(
                model="example",
                output=[
                    build_reasoning_output_item("reason 世界🙂"),
                    build_message_output_item("answer"),
                    build_function_call_output_item(
                        name="lookup", arguments='{"x":1}', call_id="call-example"
                    ),
                ],
            )
        # Non-streaming keepalive bodies may begin with whitespace.
        wire = " \n" + response.model_dump_json()
    result = project(family, wire)
    assert result["status"] == "complete", result
    answers = [
        part["text"] for part in result["sections"] if part["role"] == "assistant"
    ]
    assert answers == ["answer 世界🙂" if family == "completions" else "answer"]
    if family != "completions":
        assert [
            part["text"] for part in result["sections"] if part["role"] == "reasoning"
        ] == ["reason 世界🙂"]
        tools = [part for part in result["sections"] if part["role"] == "tool_calls"]
        assert len(tools) == 1 and tools[0]["name"] == "lookup"
        assert json.loads(tools[0]["text"][tools[0]["text"].index("{") :]) == {"x": 1}


@pytest.mark.parametrize(
    "family", ["completions", "chat/completions", "messages", "responses"]
)
async def test_actual_formatter_failures_are_visible_in_structured_projection(family):
    from omlx import server

    engine = Engine(family, broken=True)
    messages = [{"role": "user", "content": "hello"}]
    if family == "completions":
        iterator = server.stream_completion(
            engine,
            "hello",
            CompletionRequest(model="example", prompt="hello", stream=True),
        )
    elif family == "chat/completions":
        iterator = server.stream_chat_completion(
            engine,
            messages,
            ChatCompletionRequest(model="example", messages=messages, stream=True),
        )
    elif family == "messages":
        iterator = server.stream_anthropic_messages(
            engine,
            messages,
            MessagesRequest(
                model="example", messages=messages, max_tokens=16, stream=True
            ),
        )
    else:
        iterator = server.stream_responses_api(
            engine,
            messages,
            ResponsesRequest(model="example", input="hello", stream=True),
            store_response=False,
        )
    result = project(family, "".join([event async for event in iterator]))
    assert result["status"] == "failed"
    assert any(part["role"] == "output_error" for part in result["sections"])
