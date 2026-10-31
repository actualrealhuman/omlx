# SPDX-License-Identifier: Apache-2.0
"""Deterministic writer cadence, bounded merges and control/resource fences."""

import threading
import weakref
from dataclasses import replace
from unittest.mock import patch

import pytest

from omlx.activity import ActivityRecorder, ActivitySettings
from omlx.activity import recorder as module
from omlx.activity.store import ActivityStore


class NotifyingEvent:
    def __init__(self):
        self.event = threading.Event()
        self.entered = threading.Event()
        self.second_entered = threading.Event()
        self.wait_calls = 0

    def wait(self, timeout):
        self.wait_calls += 1
        self.entered.set()
        if self.wait_calls >= 2:
            self.second_entered.set()
        return self.event.wait(timeout)

    def set(self):
        self.event.set()

    def clear(self):
        self.event.clear()


@pytest.fixture
def writer(tmp_path, monkeypatch):
    # Amplify the normal 50ms collection delay: controls must interrupt it,
    # rather than tests racing against a short real-time sleep.
    monkeypatch.setattr(module, "_COLLECT_SECONDS", 30)
    active = []

    def create(settings=None):
        recorder = ActivityRecorder(
            tmp_path / f"activity-{len(active)}.sqlite3",
            settings or ActivitySettings(enabled=True),
        )
        active.append(recorder)
        assert recorder.wait_ready()
        recorder._urgent = NotifyingEvent()
        handle = recorder.begin(model_id="example", endpoint="chat")
        assert handle is not None and recorder._urgent.entered.wait(3)
        return recorder, handle

    yield create
    for recorder in active:
        assert recorder.close(3)


def test_interleaved_channels_coalesce_with_order_and_exact_logical_bytes(
    writer, monkeypatch
):
    recorder, handle = writer()
    writes = []
    append = ActivityStore.append

    def record(store, *args):
        writes.append(args)
        assert recorder.health()["writer_scratch_bytes"] == module._MERGE_SCRATCH_BYTES
        return append(store, *args)

    monkeypatch.setattr(ActivityStore, "append", record)
    text = [f"世界🙂{i}" for i in range(5)]
    wire = [b"data: " + str(i).encode() + b"\n\n" for i in range(5)]
    for one, two in zip(text, wire):
        assert recorder.append_text(handle, "raw_output", one, invocation_id="1")
        assert recorder.append(handle, "api_response", two)
    assert writes == []
    assert recorder.flush(3)
    assert len(writes) == 2
    assert (
        recorder.reader.content(handle.request_id, "raw_output", invocation_id="1")[
            "data"
        ]
        == "".join(text).encode()
    )
    assert recorder.reader.content(handle.request_id, "api_response")[
        "data"
    ] == b"".join(wire)
    detail = recorder.reader.detail(handle.request_id)
    assert detail["captured_bytes"] == sum(
        item["bytes"] for item in detail["representations"]
    )
    assert all(item["chunks"] == 1 for item in detail["representations"])
    assert (
        recorder.health()["pending_bytes"]
        == recorder.health()["writer_scratch_bytes"]
        == 0
    )
    assert (
        recorder._pending_count == 0
        and recorder._pending[handle.request_id].pending_chunks == 0
    )


@pytest.mark.parametrize(
    "kind,expected", [("items", 3), ("bytes", 2), ("mixed", 3), ("memory", 3)]
)
def test_merge_limits_or_memory_pressure_fall_back_without_losing_payload(
    writer, monkeypatch, kind, expected
):
    settings = (
        ActivitySettings(
            enabled=True,
            max_pending_bytes=262144,
            max_active_requests=1,
            max_memory_bytes=278528,
        )
        if kind == "memory"
        else None
    )
    recorder, handle = writer(settings)
    parts = (
        [b"x"] * 129
        if kind == "items"
        else [b"x" * 40000] * 2
        if kind == "bytes"
        else ["a", b"b", "c"]
        if kind == "mixed"
        else [b"a", b"b", b"c"]
    )
    for part in parts:
        method = recorder.append_text if type(part) is str else recorder.append
        assert method(handle, "api_response", part)
    append = ActivityStore.append
    writes = []

    def record(store, *args):
        writes.append(args)
        return append(store, *args)

    monkeypatch.setattr(ActivityStore, "append", record)
    assert recorder.flush(3)
    assert len(writes) == expected
    assert recorder.reader.content(
        handle.request_id, "api_response", limit=1024 * 1024
    )["data"] == b"".join(
        part.encode() if type(part) is str else part for part in parts
    )
    assert recorder.reader.get(handle.request_id)["capture_gaps"] == 0


def test_semantic_chunks_are_merge_barriers(writer, monkeypatch):
    recorder, handle = writer()
    for rep, data in [
        ("raw_output", "a"),
        ("events", '{"type":"boundary"}\n'),
        ("raw_output", "b"),
    ]:
        assert recorder.append_text(handle, rep, data)
    calls = []
    append = ActivityStore.append

    def record(store, *args):
        calls.append(args[2])
        return append(store, *args)

    monkeypatch.setattr(ActivityStore, "append", record)
    assert recorder.flush(3)
    assert calls == ["raw_output", "events", "raw_output"]
    assert recorder.reader.content(handle.request_id, "raw_output")["data"] == b"ab"


@pytest.mark.parametrize("action", ["flush", "clear", "delete", "disable", "close"])
def test_controls_and_shutdown_bypass_collection_window(writer, action):
    recorder, handle = writer()
    assert recorder.append_text(handle, "raw_output", "before")
    if action == "flush":
        assert recorder.flush(3)
    elif action == "clear":
        recorder.clear().result(3)
    elif action == "delete":
        recorder.delete(handle.request_id).result(3)
    elif action == "disable":
        recorder.update_settings(replace(recorder.settings, enabled=False)).result(3)
    else:
        assert recorder.close(3)
    row = recorder.reader.detail(handle.request_id)
    if action in {"clear", "delete"}:
        assert row is None
    else:
        assert (
            recorder.reader.content(handle.request_id, "raw_output")["data"]
            == b"before"
        )
        assert (
            row["state"]
            == {
                "flush": "received",
                "disable": "capture_stopped",
                "close": "interrupted",
            }[action]
        )
    assert recorder.health()["writer_scratch_bytes"] == 0
    if action != "flush":
        assert recorder.append_text(handle, "raw_output", "late") is False


def test_scratch_remains_charged_through_clear_and_inflight_write(writer, monkeypatch):
    recorder, handle = writer()
    entered, resume = threading.Event(), threading.Event()
    append = ActivityStore.append

    def block(store, *args):
        entered.set()
        assert resume.wait(3)
        return append(store, *args)

    monkeypatch.setattr(ActivityStore, "append", block)
    recorder.append_text(handle, "raw_output", "a")
    recorder.append_text(handle, "raw_output", "b")
    flush = recorder._control("flush")
    assert entered.wait(3)
    clear = recorder.clear()
    assert recorder.health()["writer_scratch_bytes"] == module._MERGE_SCRATCH_BYTES
    assert recorder.health()["pending_bytes"] > 0
    assert not recorder.append_text(handle, "raw_output", "late")
    resume.set()
    flush.result(3)
    clear.result(3)
    assert recorder.reader.detail(handle.request_id) is None
    assert (
        recorder.health()["pending_bytes"]
        == recorder.health()["writer_scratch_bytes"]
        == 0
    )


def test_scratch_participates_in_all_ingress_memory_admission(writer, monkeypatch):
    settings = ActivitySettings(
        enabled=True,
        max_pending_bytes=65536,
        max_active_requests=64,
        max_memory_bytes=1114112,
    )
    recorder, handle = writer(settings)
    entered, resume = threading.Event(), threading.Event()
    append = ActivityStore.append

    def block(store, *args):
        entered.set()
        assert resume.wait(3)
        return append(store, *args)

    monkeypatch.setattr(ActivityStore, "append", block)
    recorder.append(handle, "api_response", b"a")
    recorder.append(handle, "api_response", b"b")
    recorder.append(handle, "api_response", b"c" * 30000)
    flush = recorder._control("flush")
    try:
        assert entered.wait(3)
        admitted = 0
        while recorder.begin(model_id="extra", endpoint="chat") is not None:
            admitted += 1
        assert 0 < admitted < 63
        assert recorder.append_text(handle, "raw_output", "x" * 2000) is False
        with patch.object(
            module,
            "snapshot_request",
            side_effect=AssertionError("must reject before freezing"),
        ):
            assert recorder.capture_submitted(handle, {"messages": []}) is False
        reservations = 0
        while recorder.reserve_invocation(handle):
            reservations += 1
        assert reservations < 4
        for _ in range(reservations):
            recorder.release_invocation()
        with recorder._lock:
            assert recorder._memory_used() <= settings.max_memory_bytes
    finally:
        resume.set()
    flush.result(3)
    assert recorder.health()["writer_scratch_bytes"] == 0


def test_failed_batch_releases_owned_reservations_and_preserves_fail_open(
    writer, monkeypatch
):
    recorder, handle = writer()
    refs = []
    next_chunk = recorder._next_chunk

    def track(chunks):
        chunk = next_chunk(chunks)
        refs.append(weakref.ref(chunk))
        return chunk

    monkeypatch.setattr(recorder, "_next_chunk", track)
    monkeypatch.setattr(
        ActivityStore,
        "append",
        lambda *args: (_ for _ in ()).throw(OSError("synthetic writer failure")),
    )
    recorder.append_text(handle, "raw_output", "a")
    recorder.append_text(handle, "raw_output", "b")
    recorder.append_text(handle, "raw_output", "c", invocation_id="2")
    recorder.append_text(handle, "raw_output", "d", invocation_id="2")
    with recorder._lock:
        refs.extend(weakref.ref(chunk) for chunk in recorder._chunks)
    assert not recorder.flush(3)
    assert recorder.close(3)
    assert recorder.health()["error"] is not None
    assert (
        recorder.health()["pending_bytes"]
        == recorder.health()["writer_scratch_bytes"]
        == 0
    )
    assert all(ref() is None for ref in refs)
    assert recorder.begin(model_id="later", endpoint="chat") is None


def test_coalescing_keeps_requests_and_invocations_separate(writer):
    recorder, first = writer()
    second = recorder.begin(model_id="other", endpoint="chat")
    assert second is not None
    for handle, invocation, text in [
        (first, "1", "a"),
        (second, "1", "B"),
        (first, "2", "c"),
        (first, "1", "d"),
        (second, "1", "E"),
    ]:
        assert recorder.append_text(
            handle, "raw_output", text, invocation_id=invocation
        )
    assert recorder.flush(3)
    assert (
        recorder.reader.content(first.request_id, "raw_output", invocation_id="1")[
            "data"
        ]
        == b"ad"
    )
    assert (
        recorder.reader.content(first.request_id, "raw_output", invocation_id="2")[
            "data"
        ]
        == b"c"
    )
    assert (
        recorder.reader.content(second.request_id, "raw_output", invocation_id="1")[
            "data"
        ]
        == b"BE"
    )
    assert (
        recorder._pending[first.request_id].pending_chunks
        == recorder._pending[second.request_id].pending_chunks
        == 0
    )


def test_storage_refusal_drops_group_with_gap_but_preserves_terminal_metadata(
    writer, monkeypatch
):
    recorder, handle = writer()
    monkeypatch.setattr(ActivityStore, "append", lambda *args: False)
    recorder.append_text(handle, "raw_output", "a")
    recorder.append_text(handle, "raw_output", "b")
    assert recorder.finish(handle, "completed")
    assert recorder.flush(3)
    row = recorder.reader.detail(handle.request_id)
    assert row["state"] == "completed" and row["capture_gaps"] == 1
    assert row["captured_bytes"] == 0 and row["representations"] == []
    assert recorder.health()["error"] is None
    assert (
        recorder.health()["pending_bytes"]
        == recorder.health()["writer_scratch_bytes"]
        == 0
    )


@pytest.mark.parametrize("terminal", [False, True])
def test_metadata_freshness_does_not_wait_for_continuous_payload_drain(
    writer, monkeypatch, terminal
):
    recorder, handle = writer()
    entered, resume = threading.Event(), threading.Event()
    append = ActivityStore.append

    def block_first_append(store, *args):
        entered.set()
        assert resume.wait(3)
        return append(store, *args)

    monkeypatch.setattr(ActivityStore, "append", block_first_append)
    assert recorder.append_text(handle, "raw_output", "first")
    flush = recorder._control("flush")
    assert entered.wait(3)

    # The initial snapshot is already committed, while the first content write
    # is blocked. Create a new phase/counter version and keep another piece of
    # content pending during the same drain.
    initial = recorder.reader.get(handle.request_id)
    assert initial["state"] == "received"
    assert recorder.append_text(handle, "raw_output", "second")
    assert recorder.metrics(handle, counts={"observed_generated_tokens": 7})
    assert recorder.state(handle, "generating")
    if terminal:
        assert recorder.finish(handle, "completed")

    resume.set()
    flush.result(3)
    assert recorder._urgent.second_entered.wait(3)
    assert not recorder._urgent.event.is_set()
    with recorder._lock:
        assert recorder._pending[handle.request_id].pending_chunks == 1

    persisted = recorder.reader.get(handle.request_id)
    if terminal:
        # Terminal state and removal remain behind the full payload barrier.
        assert persisted["state"] == "received"
        assert persisted["finished_at"] is None
        assert persisted.get("observed_generated_tokens") is None
    else:
        assert persisted["state"] == "generating"
        assert persisted["finished_at"] is None
        assert persisted["observed_generated_tokens"] == 7

    assert recorder.flush(3)
    persisted = recorder.reader.get(handle.request_id)
    assert persisted["state"] == ("completed" if terminal else "generating")
    assert persisted["observed_generated_tokens"] == 7


@pytest.mark.parametrize("count", [64, 256])
def test_unique_channels_have_linear_planning_work_not_repeated_window_scans(
    writer, monkeypatch, count
):
    accesses = []
    original = module._Chunk

    class CountedChunk(original):
        def __getattribute__(self, name):
            if name == "representation":
                accesses.append(1)
            return super().__getattribute__(name)

    monkeypatch.setattr(module, "_Chunk", CountedChunk)
    recorder, handle = writer()
    for index in range(1, count + 1):
        assert recorder.append_text(
            handle, "raw_output", str(index), invocation_id=str(index)
        )
    accesses.clear()
    assert recorder.flush(3)
    # Includes planner and persistence property access; independent of a timer.
    assert len(accesses) < count * 12
    for index in (1, count // 2, count):
        assert recorder.reader.content(
            handle.request_id, "raw_output", invocation_id=str(index)
        )["data"].decode() == str(index)


def test_prepared_groups_fall_back_if_memory_is_consumed_after_planning(
    writer, monkeypatch
):
    settings = ActivitySettings(
        enabled=True,
        max_pending_bytes=65536,
        max_active_requests=64,
        max_memory_bytes=1114112,
    )
    recorder, handle = writer(settings)
    entered, resume = threading.Event(), threading.Event()
    next_chunk = recorder._next_chunk
    steps = []

    def pause(chunks):
        steps.append(1)
        if len(steps) == 2:
            entered.set()
            assert resume.wait(3)
        return next_chunk(chunks)

    monkeypatch.setattr(recorder, "_next_chunk", pause)
    recorder.append_text(handle, "raw_output", "a", invocation_id="1")
    recorder.append_text(handle, "raw_output", "B", invocation_id="2")
    recorder.append_text(handle, "raw_output", "C", invocation_id="2")
    append = ActivityStore.append
    writes = []

    def record(store, *args):
        writes.append(args)
        return append(store, *args)

    monkeypatch.setattr(ActivityStore, "append", record)
    flush = recorder._control("flush")
    try:
        assert entered.wait(3)
        assert recorder.health()["writer_scratch_bytes"] == 0
        assert recorder.health()["prepared_group_bytes"] == module._GROUP_METADATA_BYTES
        while recorder.begin(model_id="extra", endpoint="chat") is not None:
            pass
        with recorder._lock:
            assert (
                recorder._memory_used() + module._MERGE_SCRATCH_BYTES
                > settings.max_memory_bytes
            )
    finally:
        resume.set()
    flush.result(3)
    assert len(writes) == 3
    assert recorder.health()["prepared_group_bytes"] == 0
    assert (
        recorder.reader.content(handle.request_id, "raw_output", invocation_id="1")[
            "data"
        ]
        == b"a"
    )
    assert (
        recorder.reader.content(handle.request_id, "raw_output", invocation_id="2")[
            "data"
        ]
        == b"BC"
    )
    assert (
        recorder.health()["pending_bytes"]
        == recorder.health()["writer_scratch_bytes"]
        == 0
    )


def test_largest_prepared_descriptor_fits_reserved_metadata_without_instance_dict():
    import sys

    handle = module.CaptureHandle("example", "epoch")
    parts = tuple(
        module._Chunk(handle, "raw_output", "1", "x")
        for _ in range(module._COALESCE_ITEMS)
    )
    group = module._ChunkBatch(parts)
    assert not hasattr(group, "__dict__")
    assert sys.getsizeof(group) + sys.getsizeof(parts) < module._GROUP_METADATA_BYTES
    assert weakref.ref(group)() is group
