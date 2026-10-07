# SPDX-License-Identifier: Apache-2.0
"""Deterministic contracts for shared inference submission pacing."""

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from omlx.engine_core import EngineConfig, EngineCore
from omlx.inference_pacing import PACING_DISABLED, InferencePacer
from omlx.scheduler import SchedulerOutput
from omlx.settings import GlobalSettings, ServerSettings


class FakeClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


def test_default_share_admits_without_a_pacing_wait():
    async def run():
        pacer = InferencePacer()
        pacer.set_engine_busy("one", True)
        assert await pacer.acquire("one") is PACING_DISABLED
        assert not pacer._active

    asyncio.run(run())


def test_work_deadline_closes_and_overlapping_activity_uses_interval_union():
    async def run():
        clock = FakeClock()
        pacer = InferencePacer(0.5, work_quantum_s=0.1, clock=clock)
        pacer.set_engine_busy("one", True)
        pacer.set_engine_busy("two", True)
        first = await pacer.acquire("one")
        second = await pacer.acquire("two")
        assert first and second

        clock.value = 0.051
        with pacer._lock:
            pacer._advance_locked(clock())
        assert pacer.snapshot()["phase"] == "drain"
        assert not pacer.step_allowed(first)
        assert not pacer.step_allowed(second)

        pacer.complete(first, [(0.0, 0.04)])
        pacer.complete(second, [(0.02, 0.05)])
        snapshot = pacer.snapshot()
        assert snapshot["phase"] == "rest"
        assert snapshot["covered_work_union_seconds"] == pytest.approx(0.05)

    asyncio.run(run())


def test_natural_idle_pays_rest_and_does_not_add_a_trailing_wait():
    async def run():
        clock = FakeClock()
        pacer = InferencePacer(0.5, work_quantum_s=0.1, clock=clock)
        pacer.set_engine_busy("one", True)
        permit = await pacer.acquire("one")
        assert permit is not None
        pacer.complete(permit, [(0.0, 0.01)])

        clock.value = 0.2
        with pacer._lock:
            pacer._advance_locked(clock())
        assert pacer.snapshot()["phase"] == "work"
        next_permit = await pacer.acquire("one")
        assert next_permit is not None and not next_permit.forced
        pacer.complete(next_permit, [])

    asyncio.run(run())


def test_rejection_or_maintenance_only_completion_creates_no_work_debt():
    async def run():
        clock = FakeClock()
        pacer = InferencePacer(0.25, work_quantum_s=0.1, clock=clock)
        pacer.set_engine_busy("one", True)
        permit = await pacer.acquire("one")
        pacer.complete(permit, [])
        clock.value = 0.5
        next_permit = await pacer.acquire("one")
        assert next_permit is not None and not next_permit.forced
        assert pacer.snapshot()["covered_work_union_seconds"] == 0
        pacer.complete(next_permit, [])

    asyncio.run(run())


def test_drain_deferral_deadline_grants_a_real_submission_permit():
    async def run():
        clock = FakeClock()
        pacer = InferencePacer(0.5, work_quantum_s=0.1, clock=clock)
        pacer.set_engine_busy("one", True)
        owner = await pacer.acquire("one")
        assert owner is not None
        clock.value = 0.051
        with pacer._lock:
            pacer._advance_locked(clock())
        assert pacer.snapshot()["phase"] == "drain"

        waiting = asyncio.create_task(pacer.acquire("two"))
        await asyncio.sleep(0)
        clock.value += 1.01
        with pacer._lock:
            pacer._wake_all_locked()
        forced = await waiting
        assert forced is not None and forced.forced
        assert pacer.step_allowed(forced)
        assert pacer.snapshot()["missed_targets"] == 1

        pacer.complete(forced, [])
        pacer.complete(owner, [])

    asyncio.run(run())


def test_cancelled_acquisition_cleans_async_waiter_and_child_tasks():
    async def run():
        clock = FakeClock()
        pacer = InferencePacer(0.5, work_quantum_s=0.1, clock=clock)
        pacer.set_engine_busy("one", True)
        owner = await pacer.acquire("one")
        clock.value = 0.051
        with pacer._lock:
            pacer._advance_locked(clock())
        waiting = asyncio.create_task(pacer.acquire("two"))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert "two" not in pacer._waiters
        assert not [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        pacer.complete(owner, [])

    asyncio.run(run())


def test_live_policy_change_invalidates_inflight_permit_and_old_work_interval():
    async def run():
        clock = FakeClock()
        pacer = InferencePacer(0.5, clock=clock)
        pacer.set_engine_busy("one", True)
        old = await pacer.acquire("one")
        assert old is not None
        pacer.set_share(0.25)
        assert not pacer.step_allowed(old)
        pacer.complete(old, [(0.0, 0.2)])
        assert pacer.snapshot()["covered_work_union_seconds"] == 0

    asyncio.run(run())


def test_full_speed_wakes_waiter_without_issuing_a_paced_permit_and_reenables_fresh():
    async def run():
        clock = FakeClock()
        pacer = InferencePacer(0.5, work_quantum_s=0.1, clock=clock)
        pacer.set_engine_busy("owner", True)
        owner = await pacer.acquire("owner")
        assert owner is not None
        clock.value = 0.051
        with pacer._lock:
            pacer._advance_locked(clock())

        waiter = asyncio.create_task(pacer.acquire("waiting"))
        await asyncio.sleep(0)
        assert "waiting" in pacer._waiters
        pacer._deferred_since["waiting"] = -2.0
        pacer.set_share(1.0)
        assert await waiter is PACING_DISABLED
        assert pacer.full_speed_snapshot is True
        assert set(pacer._active) == {owner.permit_id}
        assert pacer._deferred_since == {}

        pacer.complete(owner, [(0.0, 0.2)])
        assert pacer.snapshot()["covered_work_union_seconds"] == 0
        pacer.set_share(0.8)
        assert pacer.full_speed_snapshot is False
        fresh = await pacer.acquire("fresh")
        assert fresh is not None and not fresh.forced
        pacer.complete(fresh, [])

    asyncio.run(run())


def test_full_speed_transition_releases_rest_waiter_without_admission_delay():
    async def run():
        clock = FakeClock()
        pacer = InferencePacer(0.5, work_quantum_s=0.1, clock=clock)
        owner = await pacer.acquire("owner")
        assert owner is not None
        clock.value = 0.051
        pacer.complete(owner, [(0.0, 0.04)])
        assert pacer.snapshot()["phase"] == "rest"

        waiter = asyncio.create_task(pacer.acquire("waiting"))
        await asyncio.sleep(0)
        assert "waiting" in pacer._waiters
        pacer.set_share(1.0)
        assert await waiter is PACING_DISABLED
        assert not pacer._active
        assert pacer._deferred_since == {}

    asyncio.run(run())


def test_full_mode_overrides_old_forced_permit_until_policy_is_reenabled():
    async def run():
        clock = FakeClock()
        pacer = InferencePacer(0.8, work_quantum_s=0.1, clock=clock)
        owner = await pacer.acquire("owner")
        assert owner is not None
        clock.value = 0.081
        with pacer._lock:
            pacer._advance_locked(clock())
        waiting = asyncio.create_task(pacer.acquire("waiting"))
        await asyncio.sleep(0)
        clock.value += 1.01
        with pacer._lock:
            pacer._wake_all_locked()
        forced = await waiting
        assert forced is not None and forced.forced

        pacer.set_share(1.0)
        assert pacer.begin_step(forced)
        assert pacer.begin_step(forced)
        pacer.set_share(0.8)
        assert not pacer.begin_step(forced)
        assert pacer._deferred_since == {}

        fresh = await pacer.acquire("fresh")
        assert fresh is not None and not fresh.forced
        pacer.complete(forced, [(0.0, 0.2)])
        pacer.complete(owner, [(0.0, 0.1)])
        pacer.complete(fresh, [])
        assert pacer.snapshot()["covered_work_union_seconds"] == 0

    asyncio.run(run())


def test_waiter_reenabled_after_rapid_full_speed_bounce_keeps_bounded_deferral():
    async def run():
        clock = FakeClock()
        pacer = InferencePacer(0.8, work_quantum_s=0.1, clock=clock)
        owner = await pacer.acquire("owner")
        assert owner is not None
        clock.value = 0.081
        with pacer._lock:
            pacer._advance_locked(clock())
        waiter = asyncio.create_task(pacer.acquire("waiting"))
        await asyncio.sleep(0)
        assert pacer.snapshot()["phase"] == "drain"

        pacer.set_share(1.0)
        pacer.set_share(0.8)
        clock.value = 1.2
        with pacer._lock:
            pacer._wake_all_locked()
        forced = await waiter
        assert forced is not None and forced.forced
        assert pacer.begin_step(forced)
        pacer.complete(forced, [])
        pacer.complete(owner, [])

    asyncio.run(run())


def test_cancelled_engine_keeps_executing_permit_until_worker_completion():
    async def run():
        clock = FakeClock()
        pacer = InferencePacer(0.5, clock=clock)
        pacer.set_engine_busy("one", True)
        permit = await pacer.acquire("one")
        assert permit is not None
        pacer.cancel_engine("one")
        assert not pacer.step_allowed(permit)
        assert pacer._active.get(permit.permit_id) == permit
        pacer.complete(permit, [])
        assert permit.permit_id not in pacer._active

    asyncio.run(run())


def test_restart_does_not_revive_or_release_an_old_worker_permit():
    async def run():
        pacer = InferencePacer(0.5)
        pacer.start_engine("one")
        old = await pacer.acquire("one")
        assert old is not None
        pacer.cancel_engine("one")
        pacer.start_engine("one")
        new = await pacer.acquire("one")
        assert new is not None and new.permit_id != old.permit_id
        assert not pacer.step_allowed(old)
        assert pacer.step_allowed(new)

        pacer.complete(old, [])
        assert pacer._active.get(new.permit_id) == new
        pacer.complete(new, [])

    asyncio.run(run())


def test_repeated_stale_executor_permits_keep_one_deferral_deadline():
    async def run():
        clock = FakeClock()
        pacer = InferencePacer(0.5, work_quantum_s=0.1, clock=clock)
        pacer.set_engine_busy("one", True)
        for _ in range(10):
            permit = await pacer.acquire("one")
            assert permit is not None
            clock.value += 0.11  # Owner executor starts after WORK has closed.
            assert not pacer.begin_step(permit)
            pacer.complete(permit, [])

        clock.value += 0.02
        forced = await pacer.acquire("one")
        assert forced is not None and forced.forced
        assert pacer.begin_step(forced)
        pacer.complete(forced, [(clock(), clock() + 0.01)])

    asyncio.run(run())


def test_settings_round_trip_and_share_validation():
    settings = ServerSettings.from_dict({"inference_share": 0.6})
    assert settings.to_dict()["inference_share"] == 0.6
    assert ServerSettings.from_dict({}).inference_share == 1.0
    with pytest.raises(ValueError):
        ServerSettings.from_dict({"inference_share": float("nan")})
    with pytest.raises(ValueError):
        ServerSettings.from_dict({"inference_share": 0.0})
    with pytest.raises(ValueError):
        ServerSettings.from_dict({"inference_share": True})


def test_admin_request_rejects_invalid_shares():
    from pydantic import ValidationError

    from omlx.admin.routes import GlobalSettingsRequest

    assert GlobalSettingsRequest(inference_share=0.4).inference_share == 0.4
    for invalid in (0.0, 1.1, float("nan"), float("inf"), True):
        with pytest.raises(ValidationError):
            GlobalSettingsRequest(inference_share=invalid)


def test_admin_settings_exposes_and_hot_applies_inference_share(tmp_path, monkeypatch):
    from omlx import server as server_module
    from omlx.admin import routes as admin_routes
    from omlx.server import ServerState

    class Pool:
        def __init__(self):
            self.applied = []

        def configure_inference_share(self, share):
            self.applied.append(share)

    pool = Pool()
    settings = GlobalSettings(base_path=tmp_path)
    monkeypatch.setattr(admin_routes, "_get_global_settings", lambda: settings)
    monkeypatch.setattr(server_module, "_server_state", ServerState(engine_pool=pool))

    before = asyncio.run(admin_routes.get_global_settings(is_admin=True))
    assert before["server"]["inference_share"] == 1.0

    request = admin_routes.GlobalSettingsRequest(inference_share=0.4)
    result = asyncio.run(
        admin_routes.update_global_settings(request=request, is_admin=True)
    )
    assert result["success"] is True
    assert "inference_share" in result["runtime_applied"]
    assert settings.server.inference_share == 0.4
    assert pool.applied == [0.4]
    assert GlobalSettings.load(base_path=tmp_path).server.inference_share == 0.4


@pytest.mark.asyncio
async def test_full_speed_engine_active_and_idle_paths_do_not_touch_pacer_lock(
    mock_model, mock_tokenizer, monkeypatch
):
    pacer = InferencePacer()
    with patch("omlx.engine_core.get_registry") as registry:
        registry.return_value.acquire.return_value = True
        engine = EngineCore(
            mock_model,
            mock_tokenizer,
            config=EngineConfig(inference_pacer=pacer, decode_burst_max_steps=1),
        )

    loop = asyncio.get_running_loop()
    engine._loop = loop
    engine._wake_event = asyncio.Event()
    engine._lifecycle_generation += 1
    engine._running = True
    pacer.start_engine(engine._engine_id)

    step_entered = threading.Event()
    idle_checked = threading.Event()
    scheduler = engine.scheduler

    def has_requests():
        if not step_entered.is_set():
            return True
        idle_checked.set()
        return False

    def step():
        step_entered.set()
        return SchedulerOutput(has_work=True)

    scheduler.has_requests = has_requests
    scheduler.step = MagicMock(side_effect=step)

    release_lock = threading.Event()
    lock_acquired = threading.Event()

    def hold_lock():
        with pacer._lock:
            lock_acquired.set()
            release_lock.wait(5.0)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    try:
        assert await asyncio.to_thread(lock_acquired.wait, 1.0)
        with monkeypatch.context() as patch_methods:
            for name in ("set_engine_busy", "acquire", "begin_step", "complete"):
                patch_methods.setattr(
                    pacer,
                    name,
                    MagicMock(side_effect=AssertionError(f"full mode called {name}")),
                )
            engine._task = asyncio.create_task(engine._engine_loop())
            assert await asyncio.to_thread(step_entered.wait, 1.0)
            assert await asyncio.to_thread(idle_checked.wait, 1.0)
            engine._running = False
            engine._wake_event.set()
            await asyncio.wait_for(engine._task, timeout=1.0)
            assert scheduler.step.call_count == 1
            assert not release_lock.is_set()
    finally:
        release_lock.set()
        await asyncio.to_thread(holder.join, 1.0)
        if engine._running:
            engine._running = False
            if engine._wake_event is not None:
                engine._wake_event.set()
            if engine._task is not None:
                await asyncio.gather(engine._task, return_exceptions=True)
        await engine.stop()
        engine.close()


@pytest.mark.asyncio
async def test_full_speed_transition_preserves_queued_permit_until_worker_runs(
    mock_model, mock_tokenizer
):
    pacer = InferencePacer(0.5)
    with patch("omlx.engine_core.get_registry") as registry:
        registry.return_value.acquire.return_value = True
        engine = EngineCore(
            mock_model,
            mock_tokenizer,
            config=EngineConfig(
                inference_pacer=pacer,
                decode_burst_max_steps=1,
            ),
        )

    scheduler = engine.scheduler
    idle_checked = threading.Event()

    def has_requests():
        if scheduler.step.call_count == 0:
            return True
        idle_checked.set()
        return False

    scheduler.has_requests = has_requests
    step_started = threading.Event()
    scheduler.step = MagicMock(
        side_effect=lambda: (
            step_started.set(),
            SchedulerOutput(has_work=True),
        )[1]
    )
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    blocker = engine._mlx_executor.submit(
        lambda: (blocker_started.set(), release_blocker.wait(2.0))
    )
    try:
        assert await asyncio.to_thread(blocker_started.wait, 1.0)
        await engine.start()
        deadline = time.monotonic() + 1.0
        while not pacer._active and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert len(pacer._active) == 1

        pacer.set_share(1.0)
        assert len(pacer._active) == 1
        release_blocker.set()
        await asyncio.to_thread(blocker.result, 1.0)
        assert await asyncio.to_thread(step_started.wait, 1.0)
        assert await asyncio.to_thread(idle_checked.wait, 1.0)
        deadline = time.monotonic() + 1.0
        while pacer._active and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert not pacer._active
        scheduler.step.assert_called_once()
    finally:
        release_blocker.set()
        if engine._running:
            await engine.stop()
        engine.close()


@pytest.mark.asyncio
async def test_full_speed_transition_does_not_truncate_executing_burst(
    mock_model, mock_tokenizer
):
    pacer = InferencePacer(0.5)
    with patch("omlx.engine_core.get_registry") as registry:
        registry.return_value.acquire.return_value = True
        engine = EngineCore(
            mock_model,
            mock_tokenizer,
            config=EngineConfig(
                inference_pacer=pacer,
                decode_burst_max_steps=2,
                decode_burst_budget_single_s=5.0,
                decode_burst_budget_s=5.0,
            ),
        )

    scheduler = engine.scheduler
    idle_checked = threading.Event()

    def has_requests():
        if scheduler.step.call_count < 2:
            return True
        idle_checked.set()
        return False

    scheduler.has_requests = has_requests
    first_step_entered = threading.Event()
    release_first_step = threading.Event()
    second_step_entered = threading.Event()

    def step():
        scheduler._step_inference_work_started = True
        call_count = scheduler.step.call_count
        if call_count == 1:
            first_step_entered.set()
            release_first_step.wait(2.0)
        elif call_count == 2:
            second_step_entered.set()
        return SchedulerOutput(has_work=True)

    scheduler.step = MagicMock(side_effect=step)
    try:
        await engine.start()
        assert await asyncio.to_thread(first_step_entered.wait, 1.0)
        assert len(pacer._active) == 1
        pacer.set_share(1.0)
        assert len(pacer._active) == 1
        release_first_step.set()

        assert await asyncio.to_thread(second_step_entered.wait, 1.0)
        assert await asyncio.to_thread(idle_checked.wait, 1.0)
        deadline = time.monotonic() + 1.0
        while pacer._active and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert not pacer._active
        assert scheduler.step.call_count == 2
    finally:
        release_first_step.set()
        if engine._running:
            await engine.stop()
        engine.close()


@pytest.mark.asyncio
async def test_queued_full_speed_burst_cannot_cross_engine_restart(
    mock_model, mock_tokenizer
):
    pacer = InferencePacer()
    with patch("omlx.engine_core.get_registry") as registry:
        registry.return_value.acquire.return_value = True
        engine = EngineCore(
            mock_model,
            mock_tokenizer,
            config=EngineConfig(inference_pacer=pacer, decode_burst_max_steps=1),
        )

    scheduler = engine.scheduler
    scheduler.has_requests = lambda: scheduler.step.call_count == 0
    scheduler.step = MagicMock(return_value=SchedulerOutput(has_work=True))
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    blocker = engine._mlx_executor.submit(
        lambda: (blocker_started.set(), release_blocker.wait(2.0))
    )
    try:
        assert await asyncio.to_thread(blocker_started.wait, 1.0)
        await engine.start()
        deadline = time.monotonic() + 1.0
        while (
            engine._mlx_executor._work_queue.qsize() < 1 and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.001)
        assert engine._mlx_executor._work_queue.qsize() >= 1

        await engine.stop()
        assert scheduler.step.call_count == 0
        await engine.start()
        deadline = time.monotonic() + 1.0
        while (
            engine._mlx_executor._work_queue.qsize() < 1 and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.001)
        assert engine._mlx_executor._work_queue.qsize() >= 1
        release_blocker.set()
        await asyncio.to_thread(blocker.result, 1.0)

        deadline = time.monotonic() + 1.0
        while scheduler.step.call_count == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert scheduler.step.call_count == 1
    finally:
        release_blocker.set()
        if engine._running:
            await engine.stop()
        engine.close()


@pytest.mark.asyncio
async def test_queued_executor_permit_survives_stop_until_worker_finally(
    mock_model, mock_tokenizer, monkeypatch
):
    pacer = InferencePacer(0.5)
    with patch("omlx.engine_core.get_registry") as registry:
        registry.return_value.acquire.return_value = True
        engine = EngineCore(
            mock_model,
            mock_tokenizer,
            config=EngineConfig(inference_pacer=pacer),
        )

    scheduler = engine.scheduler
    monkeypatch.setattr(scheduler, "has_requests", lambda: True)
    scheduler.step = MagicMock(return_value=SchedulerOutput(has_work=True))

    blocker_started = threading.Event()
    unblock_owner = threading.Event()
    try:
        blocker = engine._mlx_executor.submit(
            lambda: (blocker_started.set(), unblock_owner.wait(2.0))
        )
        assert await asyncio.to_thread(blocker_started.wait, 1.0)
        await engine.start()

        deadline = time.monotonic() + 1.0
        while not pacer._active and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert pacer._active  # the burst is queued behind the blocker

        await engine.stop()
        assert pacer._active  # stop did not release work the worker has not run
        unblock_owner.set()
        await asyncio.to_thread(blocker.result, 1.0)

        deadline = time.monotonic() + 1.0
        while pacer._active and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert not pacer._active
        scheduler.step.assert_not_called()
    finally:
        unblock_owner.set()
        if engine._running:
            await engine.stop()
        engine.close()


@pytest.mark.asyncio
async def test_executing_permit_is_not_released_by_stop(
    mock_model, mock_tokenizer, monkeypatch
):
    pacer = InferencePacer(0.5)
    with patch("omlx.engine_core.get_registry") as registry:
        registry.return_value.acquire.return_value = True
        engine = EngineCore(
            mock_model,
            mock_tokenizer,
            config=EngineConfig(
                inference_pacer=pacer,
                decode_burst_max_steps=1,
            ),
        )

    scheduler = engine.scheduler
    monkeypatch.setattr(scheduler, "has_requests", lambda: True)
    entered_step = threading.Event()
    finish_step = threading.Event()

    def slow_step():
        scheduler._step_inference_work_started = True
        entered_step.set()
        finish_step.wait(2.0)
        return SchedulerOutput(has_work=True)

    scheduler.step = MagicMock(side_effect=slow_step)
    try:
        await engine.start()
        assert await asyncio.to_thread(entered_step.wait, 1.0)
        assert pacer._active
        await engine.stop()
        assert pacer._active  # the atomic scheduler call still owns its permit

        finish_step.set()
        deadline = time.monotonic() + 1.0
        while pacer._active and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert not pacer._active
        scheduler.step.assert_called_once()
    finally:
        finish_step.set()
        if engine._running:
            await engine.stop()
        engine.close()


@pytest.mark.asyncio
async def test_stop_then_restart_does_not_revive_old_queued_permit(
    mock_model, mock_tokenizer, monkeypatch
):
    pacer = InferencePacer(0.5)
    with patch("omlx.engine_core.get_registry") as registry:
        registry.return_value.acquire.return_value = True
        engine = EngineCore(
            mock_model,
            mock_tokenizer,
            config=EngineConfig(
                inference_pacer=pacer,
                decode_burst_max_steps=1,
            ),
        )

    scheduler = engine.scheduler
    scheduler.step = MagicMock(side_effect=lambda: SchedulerOutput(has_work=True))
    monkeypatch.setattr(
        scheduler, "has_requests", lambda: scheduler.step.call_count == 0
    )
    blocker_started = threading.Event()
    unblock_owner = threading.Event()
    try:
        blocker = engine._mlx_executor.submit(
            lambda: (blocker_started.set(), unblock_owner.wait(2.0))
        )
        assert await asyncio.to_thread(blocker_started.wait, 1.0)
        await engine.start()
        deadline = time.monotonic() + 1.0
        while not pacer._active and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert len(pacer._active) == 1

        await engine.stop()
        await engine.start()
        deadline = time.monotonic() + 1.0
        while len(pacer._active) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert len(pacer._active) == 2

        unblock_owner.set()
        await asyncio.to_thread(blocker.result, 1.0)
        deadline = time.monotonic() + 1.0
        while scheduler.step.call_count == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert scheduler.step.call_count == 1
        assert not pacer._active
    finally:
        unblock_owner.set()
        if engine._running:
            await engine.stop()
        engine.close()


@pytest.mark.asyncio
async def test_rest_wakeup_runs_only_maintenance_on_owner_executor(
    mock_model, mock_tokenizer, monkeypatch
):
    pacer = InferencePacer(0.5, work_quantum_s=0.04)
    with patch("omlx.engine_core.get_registry") as registry:
        registry.return_value.acquire.return_value = True
        engine = EngineCore(
            mock_model,
            mock_tokenizer,
            config=EngineConfig(
                inference_pacer=pacer,
                decode_burst_max_steps=1,
            ),
        )

    scheduler = engine.scheduler
    monkeypatch.setattr(scheduler, "has_requests", lambda: True)
    maintenance_called = threading.Event()

    def active_step():
        scheduler._step_inference_work_started = True
        time.sleep(0.01)
        return SchedulerOutput(has_work=True)

    scheduler.step = MagicMock(side_effect=active_step)
    scheduler.maintenance_step = MagicMock(side_effect=maintenance_called.set)
    try:
        await engine.start()
        deadline = time.monotonic() + 1.0
        while engine.engine_id not in pacer._waiters and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert engine.engine_id in pacer._waiters
        steps_at_rest = scheduler.step.call_count

        assert engine._wake_event is not None
        engine._wake_event.set()
        assert await asyncio.to_thread(maintenance_called.wait, 1.0)
        assert scheduler.step.call_count == steps_at_rest
        scheduler.maintenance_step.assert_called_once()
    finally:
        if engine._running:
            await engine.stop()
        engine.close()


@pytest.mark.asyncio
async def test_first_chunk_is_published_before_the_next_pacing_wait(
    mock_model, mock_tokenizer, monkeypatch
):
    from omlx.output_collector import RequestOutputCollector
    from omlx.request import RequestOutput

    pacer = InferencePacer(0.5, work_quantum_s=0.04)
    with patch("omlx.engine_core.get_registry") as registry:
        registry.return_value.acquire.return_value = True
        engine = EngineCore(
            mock_model,
            mock_tokenizer,
            config=EngineConfig(
                inference_pacer=pacer,
                decode_burst_max_steps=1,
            ),
        )

    scheduler = engine.scheduler
    first_collector = RequestOutputCollector()
    engine._output_collectors["request"] = first_collector
    calls = 0

    def step():
        nonlocal calls
        calls += 1
        scheduler._step_inference_work_started = True
        time.sleep(0.03)
        if calls == 1:
            return SchedulerOutput(
                outputs=[
                    RequestOutput(
                        request_id="request",
                        new_token_ids=[7],
                        new_text="a",
                        completion_tokens=1,
                    )
                ],
                has_work=True,
            )
        return SchedulerOutput(
            outputs=[
                RequestOutput(
                    request_id="request",
                    new_token_ids=[8],
                    new_text="b",
                    completion_tokens=2,
                    finished=True,
                )
            ],
            has_work=False,
        )

    scheduler.step = MagicMock(side_effect=step)
    monkeypatch.setattr(scheduler, "has_requests", lambda: calls < 2)
    try:
        await engine.start()
        first = await asyncio.wait_for(first_collector.get(), timeout=1.0)
        assert first.new_token_ids == [7]

        deadline = time.monotonic() + 1.0
        while engine.engine_id not in pacer._waiters and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert engine.engine_id in pacer._waiters
        assert scheduler.step.call_count == 1

        final = await asyncio.wait_for(first_collector.get(), timeout=1.0)
        assert final.finished
        assert final.new_token_ids == [8]
        assert scheduler.step.call_count == 2
    finally:
        if engine._running:
            await engine.stop()
        engine.close()
