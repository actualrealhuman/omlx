# SPDX-License-Identifier: Apache-2.0
"""Bounded Activity mutation and snapshot publication fences."""

import asyncio
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from activity_replay_test_helpers import replay_stream
from fastapi import HTTPException, Request

from omlx.activity.routes import ActivityAdmin, _MutationFence
from omlx.settings import GlobalSettings


class TimeoutFuture(Future):
    def result(self, timeout=None):
        raise TimeoutError("synthetic recorder timeout")


def test_fence_waits_for_actual_future_after_worker_timeout():
    fence = _MutationFence()
    token = fence.begin()
    future = Future()
    fence.track(token, future)
    fence.worker_done(token)
    assert fence.stamp() is None

    future.set_result(None)
    assert fence.stamp() is not None


def test_fence_handles_already_done_future_and_worker_failure_before_enqueue():
    fence = _MutationFence()
    token = fence.begin()
    future = Future()
    future.set_result(None)
    fence.track(token, future)
    assert fence.stamp() is None
    fence.worker_done(token)
    assert fence.stamp() is not None

    token = fence.begin()
    fence.worker_done(token)
    assert fence.stamp() is not None


def test_cancelled_actual_future_remains_fail_closed():
    fence = _MutationFence()
    token = fence.begin()
    future = Future()
    fence.track(token, future)
    future.cancel()
    fence.worker_done(token)
    assert fence.stamp() is None


def test_ambiguous_duplicate_future_registration_is_sticky():
    fence = _MutationFence()
    token = fence.begin()
    first, second = Future(), Future()
    fence.track(token, first)
    fence.track(token, second)
    fence.worker_done(token)
    first.set_result(None)
    second.set_result(None)
    assert fence.stamp() is None


def test_overlapping_timed_out_mutations_and_token_capacity():
    fence = _MutationFence()
    tokens, futures = [], []
    for _ in range(16):
        token, future = fence.begin(), Future()
        fence.track(token, future)
        fence.worker_done(token)
        tokens.append(token)
        futures.append(future)
    with pytest.raises(HTTPException) as error:
        fence.begin()
    assert error.value.status_code == 429

    for future in futures:
        future.set_result(None)
    assert fence.stamp() is not None


def test_control_caller_cancellation_does_not_release_mutation_gate(tmp_path):
    settings = GlobalSettings(base_path=tmp_path)
    service = ActivityAdmin(lambda: settings, lambda request: None)
    started, release = threading.Event(), threading.Event()

    def blocked():
        started.set()
        assert release.wait(3)

    async def check():
        task = asyncio.create_task(service.control(blocked, mutation=True))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert service.publication_stamp() is None
        finally:
            release.set()
        await asyncio.gather(*service._control_tasks)
        assert service.publication_stamp() is not None

    try:
        asyncio.run(check())
    finally:
        service.close()


def test_control_timeout_keeps_gate_until_writer_future_finishes(tmp_path):
    settings = GlobalSettings(base_path=tmp_path)
    service = ActivityAdmin(lambda: settings, lambda request: None)
    future = Future()

    def timed_out_worker():
        service._mutation_fences.track(service._mutation_context.token, future)
        raise TimeoutError("synthetic result timeout")

    async def check():
        with pytest.raises(HTTPException) as error:
            await service.control(timed_out_worker, mutation=True)
        assert error.value.status_code == 503
        assert service.publication_stamp() is None
        future.set_result(None)
        assert service.publication_stamp() is not None

    try:
        asyncio.run(check())
    finally:
        service.close()


def test_queued_control_task_cancellation_keeps_executor_job_fenced(tmp_path):
    settings = GlobalSettings(base_path=tmp_path)
    service = ActivityAdmin(lambda: settings, lambda request: None)
    first_started, second_started = threading.Event(), threading.Event()
    release_workers = threading.Event()
    mutation_started, writer_future = threading.Event(), Future()

    def occupy(started):
        started.set()
        assert release_workers.wait(3)

    def mutation_worker():
        service._mutation_fences.track(service._mutation_context.token, writer_future)
        mutation_started.set()

    async def check():
        readers = [
            asyncio.create_task(service.read(lambda started=started: occupy(started)))
            for started in (first_started, second_started)
        ]
        assert await asyncio.to_thread(first_started.wait, 2)
        assert await asyncio.to_thread(second_started.wait, 2)
        outer = asyncio.create_task(service.control(mutation_worker, mutation=True))
        try:
            while not service._control_tasks:
                await asyncio.sleep(0)
            while service._reads < 3:
                await asyncio.sleep(0)
            assert service._reads == 3
            control_task = next(iter(service._control_tasks))
            control_task.cancel()
            await asyncio.gather(control_task, return_exceptions=True)
            await asyncio.gather(outer, return_exceptions=True)
            assert service.publication_stamp() is None
        finally:
            release_workers.set()
        await asyncio.gather(*readers, return_exceptions=True)
        assert await asyncio.to_thread(mutation_started.wait, 2)
        while service._reads:
            await asyncio.sleep(0)
        assert service.publication_stamp() is None
        writer_future.set_result(None)
        assert service.publication_stamp() is not None

    try:
        asyncio.run(asyncio.wait_for(check(), 10))
    finally:
        release_workers.set()
        service.close()


def test_remove_clear_and_delete_keep_gate_until_recorder_future_finishes(
    tmp_path,
):
    settings = GlobalSettings(base_path=tmp_path)
    service = ActivityAdmin(lambda: settings, lambda request: None)

    class Writer:
        reader = SimpleNamespace(get=lambda _request_id: object())

        def clear(self):
            self.future = TimeoutFuture()
            return self.future

        def delete(self, _request_id):
            self.future = TimeoutFuture()
            return self.future

    writer = Writer()

    async def run_one(request_id):
        with (
            patch.object(service, "_writer", return_value=writer),
            pytest.raises(HTTPException) as error,
        ):
            await service.control(lambda: service.remove(request_id), mutation=True)
        assert error.value.status_code == 503
        assert service.publication_stamp() is None
        writer.future.set_result(None)
        assert service.publication_stamp() is not None

    try:
        asyncio.run(run_one(None))
        asyncio.run(run_one("request-id"))
    finally:
        service.close()


def test_apply_settings_tracks_recorder_future_after_timeout(tmp_path):
    settings = GlobalSettings(base_path=tmp_path)
    service = ActivityAdmin(lambda: settings, lambda request: None)
    future = TimeoutFuture()

    class Writer:
        def health(self):
            return {"stopping": False, "error": None}

        def update_settings(self, _candidate):
            return future

    writer = Writer()

    async def check():
        with (
            patch("omlx.activity.routes.get_recorder", return_value=writer),
            patch.object(settings, "save_activity_settings"),
            pytest.raises(HTTPException) as error,
        ):
            await service.control(
                lambda: service.apply_settings({"enabled": False}, None),
                mutation=True,
            )
        assert error.value.status_code == 503
        assert service.publication_stamp() is None
        future.set_result(None)
        assert service.publication_stamp() is not None

    try:
        asyncio.run(check())
    finally:
        service.close()


def test_snapshot_auth_await_invalidates_source_and_sends_no_snapshot(tmp_path):
    settings = GlobalSettings(base_path=tmp_path)
    service = ActivityAdmin(lambda: settings, lambda request: None)
    stamp = service.publication_stamp()
    request = Request({"type": "http", "headers": []})

    async def authorization(_request):
        settings.base_path = tmp_path / "new-source"

    service.require_admin = authorization

    async def check():
        stream = replay_stream(
            service,
            request,
            SimpleNamespace(),
            None,
            None,
            None,
            {"recent": [], "active": []},
            publication_stamp=stamp,
        )
        event = await anext(stream)
        assert event.startswith("event: reconnect")
        assert "event: snapshot" not in event
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    try:
        asyncio.run(check())
    finally:
        service.close()


def test_poll_read_that_straddles_mutation_reconnects_without_publishing(tmp_path):
    settings = GlobalSettings(base_path=tmp_path)
    service = ActivityAdmin(lambda: settings, lambda request: None, poll_seconds=0)
    request = Request({"type": "http", "headers": []})
    reader = SimpleNamespace()
    stamp = service.publication_stamp()

    async def stale_read(_operation):
        service._mutation_fences.begin()
        return {"recent": [{"id": "stale"}], "active": []}

    service.read = stale_read

    async def check():
        stream = replay_stream(
            service,
            request,
            reader,
            None,
            None,
            None,
            {"recent": [], "active": []},
            publication_stamp=stamp,
        )
        first = await anext(stream)
        assert b"event: snapshot" in first
        reconnect = await anext(stream)
        assert reconnect.startswith("event: reconnect")
        assert "stale" not in reconnect
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    try:
        asyncio.run(check())
    finally:
        service.close()
