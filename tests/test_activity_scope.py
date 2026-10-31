# SPDX-License-Identifier: Apache-2.0
"""Background upkeep must not retain a foreground request's observation."""

from contextvars import ContextVar

import pytest

from omlx.activity.runtime import _current, current_capture, without_capture


async def test_actual_keep_warm_task_detaches_capture_without_running_gpu_work():
    from omlx.engine_pool import EnginePool

    pool = EnginePool.__new__(EnginePool)
    pool._gpu_keep_warm_interval = 1
    pool._shutting_down = False
    pool._gpu_keep_warm_task = None
    observed = []
    other = ContextVar("synthetic_background_context", default="missing")

    async def background():
        observed.append((current_capture(), other.get()))

    pool._gpu_keep_warm_loop = background
    capture = object()
    token, other_token = _current.set(capture), other.set("preserved")
    try:
        pool._ensure_gpu_keep_warm_task()
        assert current_capture() is capture
        await pool._gpu_keep_warm_task
        assert observed == [(None, "preserved")]
    finally:
        _current.reset(token)
        other.reset(other_token)


def test_nested_suppression_restores_foreground_context_after_errors():
    capture = object()
    token = _current.set(capture)
    try:
        with pytest.raises(RuntimeError), without_capture():
            assert current_capture() is None
            with without_capture():
                assert current_capture() is None
            raise RuntimeError("synthetic internal failure")
        assert current_capture() is capture
    finally:
        _current.reset(token)
    assert current_capture() is None
