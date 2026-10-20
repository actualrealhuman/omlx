# SPDX-License-Identifier: Apache-2.0
"""Exercise real EngineCore worker ownership, including cancelled async callers."""

import asyncio
import threading
from unittest.mock import patch

import mlx.core as mx
import pytest

from omlx.engine_core import EngineConfig, EngineCore
from omlx.generation_gate import GenerationGate
from omlx.inference_pacing import InferencePacer
from omlx.inference_scheduling import ModelExecutionCoordinator
from omlx.model_pacing import ModelPacer
from omlx.scheduler import SchedulerOutput


@pytest.mark.asyncio
async def test_cancelled_preparation_keeps_worker_ownership_until_device_drain(
    mock_model, mock_tokenizer
):
    coordinator = ModelExecutionCoordinator(enabled=True, quantum_seconds=0.01)
    global_pacer = InferencePacer(0.8)
    with patch("omlx.engine_core.get_registry") as registry:
        registry.return_value.acquire.return_value = True
        first = EngineCore(
            mock_model,
            mock_tokenizer,
            EngineConfig(
                inference_coordinator=coordinator,
                inference_model_id="first",
                inference_pacer=ModelPacer(global_pacer),
            ),
        )
        second = EngineCore(
            mock_model,
            mock_tokenizer,
            EngineConfig(
                inference_coordinator=coordinator,
                inference_model_id="second",
                inference_pacer=ModelPacer(global_pacer),
            ),
        )
    first._generation_gate = second._generation_gate = GenerationGate()
    entered = threading.Event()
    release = threading.Event()
    peer_entered = threading.Event()
    events = []

    def held_operation():
        with mx.stream(first._mlx_stream):
            pending = mx.ones((8, 8)) + 1
            mx.async_eval(pending)
        events.append("first_started")
        entered.set()
        assert release.wait(3)
        events.append("first_finished")
        return pending

    def peer_operation():
        events.append("peer_started")
        peer_entered.set()
        with mx.stream(second._mlx_stream):
            return mx.sum(mx.ones((8, 8))).item()

    task = asyncio.create_task(first.run_model_preparation(held_operation))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        peer = asyncio.create_task(second.run_model_preparation(peer_operation))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.03)
        assert not peer_entered.is_set()
        # A waiting peer must not hold a global pacing permit; otherwise it
        # prevents the selected model's global controller from draining.
        assert global_pacer._active
        assert {permit.engine_id for permit in global_pacer._active.values()} == {
            first._engine_id
        }
        release.set()
        assert await asyncio.wait_for(peer, 3) == 64
        assert events.index("first_finished") < events.index("peer_started")
    finally:
        release.set()
        # Quiesce the dedicated worker before releasing registration/streams.
        await asyncio.to_thread(first._mlx_executor.submit(lambda: None).result)
        await asyncio.to_thread(first.close)
        await asyncio.to_thread(second.close)


def test_same_model_can_register_again_after_core_close(mock_model, mock_tokenizer):
    coordinator = ModelExecutionCoordinator(enabled=True)
    with patch("omlx.engine_core.get_registry") as registry:
        registry.return_value.acquire.return_value = True
        for _ in range(2):
            engine = EngineCore(
                mock_model,
                mock_tokenizer,
                EngineConfig(
                    inference_coordinator=coordinator, inference_model_id="reload"
                ),
            )
            # An actual worker operation initializes the model's thread streams.
            engine._mlx_executor.submit(lambda: mx.ones((1,)).item()).result()
            engine._execution_thread_id = engine._mlx_executor.submit(
                threading.get_ident
            ).result()
            engine.close()
            assert not coordinator.status_snapshot()["models"]["reload"]["registered"]


@pytest.mark.asyncio
async def test_parked_prefill_abort_reacquires_maintenance_while_battery_suspended(
    mock_model, mock_tokenizer
):
    coordinator = ModelExecutionCoordinator(enabled=True)
    with patch("omlx.engine_core.get_registry") as registry:
        registry.return_value.acquire.return_value = True
        engine = EngineCore(
            mock_model,
            mock_tokenizer,
            EngineConfig(
                inference_coordinator=coordinator,
                inference_model_id="prefill",
                decode_burst_max_steps=1,
            ),
        )
    entered = threading.Event()
    advance = threading.Event()
    cleaned = threading.Event()
    engine._generation_gate = GenerationGate()
    request_id = "cancel-this-prefill"

    def step():
        entered.set()
        assert advance.wait(2)
        engine._execution_checkpoint(request_id)
        assert request_id in engine.scheduler._pending_abort_ids
        assert engine._execution_work.kind == "maintenance"
        assert coordinator.status_snapshot()["owner"] == "prefill"
        engine.scheduler._pending_abort_ids.remove(request_id)
        cleaned.set()
        return SchedulerOutput(has_work=False)

    with patch.object(engine.scheduler, "step", side_effect=step):
        future = engine._mlx_executor.submit(engine._step_burst)
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            coordinator.set_suspended(True)
            engine._generation_gate.set_state(paused=True, restricted=True)
            advance.set()
            await asyncio.sleep(0.02)
            assert not future.done()
            engine.scheduler._pending_abort_ids.add(request_id)
            await asyncio.wait_for(asyncio.wrap_future(future), 2)
            assert cleaned.is_set()
            assert coordinator.status_snapshot()["suspended"] is True
        finally:
            advance.set()
            coordinator.set_suspended(False)
            engine._generation_gate.set_state(paused=False, restricted=False)
            await asyncio.to_thread(engine.close)
