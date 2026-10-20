# SPDX-License-Identifier: Apache-2.0
"""Model limits retain the global accounting and release cancelled admissions."""

import asyncio

import pytest

from omlx.inference_pacing import PACING_DISABLED, InferencePacer
from omlx.model_pacing import ModelPacer


@pytest.mark.asyncio
async def test_full_speed_model_still_accounts_global_work():
    now = [0.0]
    global_pacer = InferencePacer(0.8, clock=lambda: now[0])
    model = ModelPacer(global_pacer, 1.0)
    model.start_engine("prose")
    model.set_engine_busy("prose", True)
    permit = await model.acquire("prose")
    assert permit is not PACING_DISABLED
    assert permit.model_permit is None
    assert model.begin_step(permit)
    now[0] = 0.33
    model.complete(permit, [(0.0, 0.33)])
    assert global_pacer.snapshot()["covered_work_union_seconds"] == pytest.approx(0.33)
    assert not global_pacer._active
    assert model.share == 0.8


@pytest.mark.asyncio
async def test_live_model_change_does_not_reset_global_phase():
    global_pacer = InferencePacer(0.8)
    first = ModelPacer(global_pacer, 1.0)
    second = ModelPacer(global_pacer, 0.75)
    first.start_engine("first")
    first.set_engine_busy("first", True)
    generation = global_pacer.snapshot()["generation"]
    second.start_engine("second")
    second.set_engine_busy("second", True)
    second.set_share(0.5)
    assert global_pacer.snapshot()["generation"] == generation
    assert global_pacer.share == 0.8


@pytest.mark.asyncio
async def test_model_specific_rest_does_not_hold_global_admission():
    model = ModelPacer(InferencePacer(0.8), 0.5)
    model.start_engine("model")
    model.set_engine_busy("model", True)
    with model.model_pacer._lock:
        model.model_pacer._phase = "rest"
        model.model_pacer._rest_deadline = model.model_pacer._clock() + 10
    waiting = asyncio.Event()
    task = asyncio.create_task(model.acquire("model", on_admission_wait=waiting.set))
    await asyncio.wait_for(waiting.wait(), 1)
    assert not model.global_pacer._active
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not model.global_pacer._active
    assert not model.model_pacer._active


@pytest.mark.asyncio
async def test_model_limit_applies_when_global_is_full_speed():
    model = ModelPacer(InferencePacer(), 0.66)
    permit = await model.acquire("workhorse")
    assert permit.global_permit is None
    assert model.begin_step(permit)
    model.complete(permit, [])
    assert model.share == 0.66


@pytest.mark.asyncio
async def test_model_zero_pause_fences_inflight_permit_even_when_global_is_full_speed():
    model = ModelPacer(InferencePacer(), 0.5)
    permit = await model.acquire("paused")
    assert permit is not PACING_DISABLED
    model.set_share(0.0)
    assert not model.begin_step(permit)
    model.complete(permit, [])


@pytest.mark.asyncio
async def test_global_and_model_limits_compose_as_the_stricter_ceiling():
    now = [0.0]
    global_pacer = InferencePacer(0.5, clock=lambda: now[0])
    model = ModelPacer(global_pacer, 0.25)
    model.start_engine("workhorse")
    model.set_engine_busy("workhorse", True)

    first = await model.acquire("workhorse")
    assert model.begin_step(first)
    now[0] = 0.11
    model.complete(first, [(0.0, 0.11)])

    # The model's 0.25 ceiling requires 0.33 s of rest after 0.11 s of work.
    # That natural idle also satisfies the global pacer's looser 0.5 ceiling.
    now[0] = 0.44
    second = await model.acquire("workhorse")
    assert model.begin_step(second)
    model.complete(second, [])
