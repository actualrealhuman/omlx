# SPDX-License-Identifier: Apache-2.0
"""Generation integration with fake schedulers and no model inference."""

import asyncio
import concurrent.futures
import json
import threading
import time
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omlx.engine_core import EngineConfig, EngineCore
from omlx.engine_pool import EnginePool
from omlx.generation_gate import GenerationGate, GenerationSuspendedError
from omlx.inference_pacing import InferencePacer
from omlx.scheduler import Scheduler


def core():
    engine = EngineCore.__new__(EngineCore)
    engine._generation_gate = GenerationGate()
    engine._engine_id = "test-generation"
    engine._lifecycle_generation = 0
    engine.config = EngineConfig(decode_burst_max_steps=1)
    engine.scheduler = Mock()
    return engine


def test_closed_gate_prevents_scheduler_dispatch():
    engine = core()
    engine._generation_gate.set_state(paused=True, restricted=True)
    assert engine._step_burst() == []
    engine.scheduler.step.assert_not_called()
    engine._generation_gate.set_state(paused=False, restricted=False)
    assert engine._step_burst() == [engine.scheduler.step.return_value]


def test_user_maximum_is_routed_through_policy_owner():
    pool = EnginePool.__new__(EnginePool)
    pool._inference_controls_enabled = True
    pool._battery_protection = Mock()
    pool._inference_pacer = Mock()
    pool._model_scheduling = {}
    pool.configure_inference_share(0.75)
    pool._battery_protection.set_requested_share.assert_called_once_with(0.75)
    pool._inference_pacer.set_share.assert_not_called()


def test_suspended_maintenance_publishes_cancellation_to_admin_snapshot():
    scheduler = Scheduler.__new__(Scheduler)
    cancelled = SimpleNamespace(request_id="cancelled")
    waiting = SimpleNamespace(request_id="waiting")
    scheduler.running = {cancelled.request_id: cancelled}
    scheduler.waiting = deque([waiting])
    scheduler._publish_admin_snapshot()
    assert scheduler.snapshot_for_admin()["running_by_id"] == {"cancelled": cancelled}

    scheduler._process_pending_aborts = Mock(side_effect=scheduler.running.clear)
    scheduler._process_pending_reclaim = Mock()
    scheduler._drain_pending_async_removes = Mock(return_value=False)
    scheduler.step = Mock()
    scheduler.maintenance_step()

    snapshot = scheduler.snapshot_for_admin()
    assert snapshot["running_by_id"] == {}
    assert snapshot["waiting"] == [waiting]
    scheduler.step.assert_not_called()
    scheduler.waiting.clear()
    assert snapshot["waiting"] == [waiting]


def test_other_engines_are_not_declared_protected():
    pool = EnginePool.__new__(EnginePool)
    for kind in ("embedding", "reranker", "stt", "tts", "sts", "decision"):
        assert not pool._battery_protects_entry(SimpleNamespace(engine_type=kind))
    assert not pool._battery_protects_entry(
        SimpleNamespace(engine_type="batched", engine=object())
    )
    pool._distributed_deployment_for_entry = Mock(return_value=None)
    pool._settings_manager = Mock()
    pool._settings_manager.get_settings.return_value = SimpleNamespace(
        external_engine="splash"
    )
    pool._entry_is_diffusion_model = Mock(return_value=False)
    assert not pool._battery_protects_entry(
        SimpleNamespace(engine_type="batched", engine=None, model_id="external")
    )


@pytest.mark.asyncio
async def test_preparation_rejects_new_work_during_suspension():
    engine = core()
    engine._generation_gate.set_state(paused=True, restricted=True)
    operation = Mock()
    with pytest.raises(GenerationSuspendedError):
        await engine.run_model_preparation(operation)
    operation.assert_not_called()


@pytest.mark.asyncio
async def test_queued_vision_work_rechecks_gate_before_execution():
    engine = core()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    engine._mlx_executor = executor
    engine.config.inference_pacer = InferencePacer(0.5)
    release = threading.Event()
    executor.submit(release.wait)
    operation = Mock(return_value="embeddings")
    task = asyncio.create_task(engine.run_model_preparation(operation))
    try:
        await asyncio.sleep(0)
        engine._generation_gate.set_state(paused=True, restricted=True)
        release.set()
        # A queued worker can drain, but must not execute the model callback.
        await asyncio.get_running_loop().run_in_executor(executor, lambda: None)
        operation.assert_not_called()
        engine._generation_gate.set_state(paused=False, restricted=False)
        assert await asyncio.wait_for(task, 2) == "embeddings"
        operation.assert_called_once()
        assert not engine.config.inference_pacer._engine_lifecycle
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_cancelled_queued_vision_does_not_run_or_leak_participants():
    engine = core()
    engine.config.inference_pacer = InferencePacer()
    release = threading.Event()
    operation = Mock()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        engine._mlx_executor = executor
        executor.submit(release.wait)
        task = asyncio.create_task(engine.run_model_preparation(operation))
        try:
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        await asyncio.get_running_loop().run_in_executor(executor, lambda: None)
    operation.assert_not_called()
    assert not engine.config.inference_pacer._engine_lifecycle


@pytest.mark.asyncio
async def test_retirement_keeps_cancelled_permit_until_worker_completion():
    pacer = InferencePacer(0.5)
    pacer.start_engine("vision")
    permit = await pacer.acquire("vision")
    pacer.retire_engine("vision")
    assert not pacer.begin_step(permit)
    assert "vision" in pacer._engine_lifecycle
    pacer.complete(permit, [])
    assert "vision" not in pacer._engine_lifecycle


@pytest.mark.asyncio
async def test_rejected_executor_submission_releases_permit():
    engine = core()
    engine.config.inference_pacer = InferencePacer(0.5)
    engine._mlx_executor = Mock()
    engine._mlx_executor.submit.side_effect = RuntimeError("executor stopped")
    with pytest.raises(RuntimeError, match="executor stopped"):
        await engine.run_model_preparation(Mock())
    assert not engine.config.inference_pacer._active
    assert not engine.config.inference_pacer._engine_lifecycle


@pytest.mark.asyncio
async def test_vlm_preparation_uses_real_generation_suspension_boundary():
    from omlx.engine.vlm import VLMBatchedEngine

    engine = VLMBatchedEngine.__new__(VLMBatchedEngine)
    controlled = core()
    controlled._generation_gate.set_state(paused=True, restricted=True)
    engine._engine = SimpleNamespace(engine=controlled)
    operation = Mock()
    with pytest.raises(GenerationSuspendedError):
        await engine._run_model_preparation(operation)
    operation.assert_not_called()


@pytest.mark.asyncio
async def test_suspension_rejection_has_machine_readable_openai_error(monkeypatch):
    from fastapi import HTTPException

    from omlx import server

    pool = Mock()
    pool.resolve_model_id.return_value = "model"
    pool.get_engine = AsyncMock(
        side_effect=GenerationSuspendedError("reserve_suspended")
    )
    monkeypatch.setattr(server._server_state, "engine_pool", pool)
    monkeypatch.setattr(server._server_state, "settings_manager", None)
    monkeypatch.setattr(server._server_state, "global_settings", None)
    monkeypatch.setattr(server._server_state, "process_memory_enforcer", None)
    with pytest.raises(HTTPException) as rejected:
        await server.get_engine("model")
    request = SimpleNamespace(
        method="POST", url=SimpleNamespace(path="/v1/chat/completions")
    )
    response = await server.http_exception_handler(request, rejected.value)
    error = json.loads(response.body)["error"]
    assert response.status_code == 503
    assert error["code"] == "inference_suspended"
    assert isinstance(error["message"], str)
    assert "reserve_suspended" in error["message"]


@pytest.mark.asyncio
async def test_suspended_engine_services_cleanup_without_decode():
    engine = core()
    engine._generation_gate.set_state(paused=True, restricted=True)
    engine._running = True
    engine._last_reap = time.monotonic()
    engine._pacer_admission_waited = threading.Event()
    engine._wake_event = asyncio.Event()
    engine.scheduler.has_requests.return_value = True
    loop = asyncio.get_running_loop()

    def maintenance():
        engine._running = False
        loop.call_soon_threadsafe(engine._wake_event.set)

    engine.scheduler.maintenance_step.side_effect = maintenance
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        engine._mlx_executor = executor
        await asyncio.wait_for(engine._engine_loop(), 1)
    engine.scheduler.maintenance_step.assert_called_once()
    engine.scheduler.step.assert_not_called()


@pytest.mark.asyncio
async def test_settings_api_keeps_requested_and_effective_share_separate(
    tmp_path, monkeypatch
):
    from omlx.admin import routes
    from omlx.battery_protection import BatterySample
    from omlx.battery_service import BatteryProtectionService
    from omlx.server import _server_state
    from omlx.settings import GlobalSettings

    settings = GlobalSettings(base_path=tmp_path)
    gate = GenerationGate()
    applied = []
    service = BatteryProtectionService(
        enabled=False,
        requested_share=1,
        apply_share=applied.append,
        state_path=tmp_path / "battery-state.json",
        reader=Mock(),
        notifier=Mock(),
        gate=gate,
        clock=lambda: 0,
    )
    service._sample = BatterySample(
        0, battery_present=True, charge_percent=65, external_power=True
    )
    pool = EnginePool.__new__(EnginePool)
    pool._battery_protection = service
    pool._inference_controls_enabled = True
    pool._model_scheduling = {}
    monkeypatch.setattr(routes, "_get_global_settings", lambda: settings)
    monkeypatch.setattr(_server_state, "engine_pool", pool)
    monkeypatch.setattr(_server_state, "battery_protection", service)
    await routes.update_global_settings(
        request=routes.GlobalSettingsRequest(
            battery_protection_enabled=True, inference_share=1
        ),
        is_admin=True,
    )
    assert settings.server.inference_share == 1
    assert service.status()["effective_share"] == pytest.approx(0.64)
    loaded = GlobalSettings.load(base_path=tmp_path)
    assert loaded.server.battery_protection_enabled is True
    assert loaded.server.inference_share == 1
    await routes.update_global_settings(
        request=routes.GlobalSettingsRequest(inference_share=0.2),
        is_admin=True,
    )
    assert settings.server.inference_share == 0.2
    assert service.status()["effective_share"] == pytest.approx(0.16)


@pytest.mark.asyncio
async def test_suspended_preload_does_not_hold_up_other_engines(monkeypatch):
    gate = GenerationGate()
    gate.set_state(paused=True, restricted=True)
    monkeypatch.setattr("omlx.generation_gate.get_generation_gate", lambda: gate)
    pool = EnginePool.__new__(EnginePool)
    pool._entries = {
        "text": SimpleNamespace(is_pinned=True, engine_type="batched"),
        "audio": SimpleNamespace(is_pinned=True, engine_type="stt"),
    }
    pool._battery_protects_entry = lambda entry: entry.engine_type == "batched"
    pool._battery_deferred_pins = set()
    pool._battery_preload_task = None
    pool._shutting_down = False
    pool.get_engine = AsyncMock()
    await asyncio.wait_for(pool.preload_pinned_models(), 1)
    try:
        pool.get_engine.assert_awaited_once_with("audio")
        gate.set_state(paused=False, restricted=False)
        await asyncio.wait_for(pool._battery_preload_task, 1)
        assert pool.get_engine.await_args_list[-1].args == ("text",)
        assert not pool._battery_deferred_pins
    finally:
        if not pool._battery_preload_task.done():
            pool._battery_preload_task.cancel()
            await asyncio.gather(pool._battery_preload_task, return_exceptions=True)
