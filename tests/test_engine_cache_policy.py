# SPDX-License-Identifier: Apache-2.0
"""Public engine wrappers preserve policy until runtime request construction."""

from types import SimpleNamespace

import pytest

from omlx.cache.policy import CachePolicy
from omlx.engine.batched import BatchedEngine
from omlx.engine.vlm import VLMBatchedEngine
from omlx.request import Request


class _RequestCapturingCore:
    def __init__(self):
        self.request = None

    async def add_request(self, **kwargs):
        self.request = Request(
            "policy-wrapper",
            kwargs["prompt"],
            kwargs["sampling_params"],
            cache_policy=kwargs["cache_policy"],
        )
        return self.request.request_id

    @staticmethod
    def _output():
        return SimpleNamespace(
            output_text="answer",
            new_text="answer",
            prompt_tokens=1,
            completion_tokens=1,
            finished=True,
            finish_reason="length",
            tool_calls=None,
            cached_tokens=0,
            first_token_at=None,
        )

    async def generate(self, **kwargs):
        await self.add_request(**kwargs)
        return self._output()

    async def stream_outputs(self, request_id):
        yield self._output()


@pytest.mark.parametrize("engine_type", [BatchedEngine, VLMBatchedEngine])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("mode", ["default", "memory", "none"])
@pytest.mark.asyncio
async def test_generation_wrapper_constructs_request_with_cache_policy(
    engine_type, stream, mode
):
    engine = engine_type(model_name="test-model")
    engine._loaded = True
    if engine_type is VLMBatchedEngine:
        engine._vlm_model = SimpleNamespace(
            config=SimpleNamespace(model_type="qwen3_5")
        )
    core = _RequestCapturingCore()
    engine._engine = core
    policy = {"kv": mode, "images": mode}
    if stream:
        outputs = [
            output async for output in engine.stream_generate(
                "prompt", max_tokens=1, cache_policy=policy
            )
        ]
        assert outputs[-1].finished
    else:
        output = await engine.generate("prompt", max_tokens=1, cache_policy=policy)
        assert output.completion_tokens == 1
    assert core.request.cache_policy == CachePolicy(kv=mode, images=mode)
