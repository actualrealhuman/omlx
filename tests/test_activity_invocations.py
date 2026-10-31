# SPDX-License-Identifier: Apache-2.0
"""Structured invocation snapshots remain bounded and independent of content reads."""

import gc
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from omlx.activity import ActivityRecorder, ActivitySettings
from omlx.activity.invocations import MAX_INVOCATION_BYTES, InvocationSnapshot
from omlx.activity.runtime import RequestCapture
from omlx.activity.store import SCHEMA_VERSION, ActivityReader, ActivityStore


@pytest.fixture
def observed(tmp_path):
    recorder = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    assert recorder.wait_ready()
    handle = recorder.begin(model_id="resolved-example", endpoint="chat")
    capture = RequestCapture(recorder, handle)
    yield recorder, capture
    assert recorder.close()


def detail(recorder, capture):
    assert recorder.flush()
    return recorder.reader.detail(capture.handle.request_id)


def output(tokens=None, text="", finished=False, reason="stop", error=None):
    values = dict(new_text=text, finished=finished, finish_reason=reason, error=error)
    if tokens is not None:
        values["completion_tokens"] = tokens
    return SimpleNamespace(**values)


def test_overlapping_invocations_have_separate_counts_reasons_and_partial_input(
    observed,
):
    recorder, capture = observed
    first = capture.invocation("engine-one")
    second = capture.invocation("engine-two")
    first.prepared("prepared text", [10, 11])
    second.prepared([12], [12])
    first.output(output(3, "first", finished=True, reason="length"))
    assert detail(recorder, capture)["finished_at"] is None
    second.output(output(text="without counter", finished=True))
    capture.api_done("completed")
    result = detail(recorder, capture)
    one, two = result["invocations"]
    assert [one["engine_request_id"], two["engine_request_id"]] == [
        "engine-one",
        "engine-two",
    ]
    assert one["model_id"] == two["model_id"] == "resolved-example"
    assert (one["input_tokens"], two["input_tokens"]) == (2, 1)
    assert one["prepared_text"] is True and two["prepared_text"] is False
    assert one["generated_tokens"] == 3 and two["generated_tokens"] is None
    assert one["first_token_ms"] >= 0 and two["first_token_ms"] is None
    assert one["finish_reason"] == "length" and two["finish_reason"] == "stop"
    assert one["state"] == two["state"] == "completed"
    assert result["observed_input_tokens"] == 3
    assert result["observed_generated_tokens"] == 3
    assert result["invocation_detail_scope"] == "budgeted_snapshots_may_have_gaps"


def test_admission_options_are_frozen_bounded_and_never_serialize_objects(observed):
    recorder, capture = observed

    class Opaque:
        def __repr__(self):
            raise AssertionError("serialized opaque object")

    stops = ["end", "🙂" * 1500]
    ids = [42, 43]
    capture.invocation(
        "engine",
        options=(
            ("stop", stops),
            ("stop_token_ids", ids),
            ("temperature", float("nan")),
            ("max_tokens", 64),
            ("compiled_grammar", Opaque()),
            ("seed", 10**100),
            ("grammar_constrained", True),
            ("unrecognized", Opaque()),
        ),
    )
    stops[0] = "mutated"
    ids[0] = 99
    record = detail(recorder, capture)["invocations"][0]
    assert record["admission_options"] == {
        "stop": ["end"],
        "stop_token_ids": [42, 43],
        "max_tokens": 64,
        "grammar_constrained": True,
    }
    assert record["omitted_option_fields"] == ["seed", "stop", "temperature"]
    assert record["options_scope"] == "core_admission_not_final_backend_configuration"
    assert len(json.dumps(record)) < MAX_INVOCATION_BYTES


def test_empty_progress_does_not_invent_generating_or_zero_tokens(observed):
    recorder, capture = observed
    invocation = capture.invocation("engine")
    invocation.output(output())
    row = detail(recorder, capture)["invocations"][0]
    assert row["state"] == "admitted" and "generated_tokens" not in row
    invocation.prefill()
    assert detail(recorder, capture)["invocations"][0]["state"] == "prefilling"
    invocation.output(output(0, finished=True, reason="abort"))
    capture.api_done("completed")
    row = detail(recorder, capture)["invocations"][0]
    assert row["state"] == "cancelled" and row["generated_tokens"] == 0
    assert row["first_token_ms"] is None


def test_live_snapshots_coalesce_and_terminal_flush_preserves_regression(
    observed, monkeypatch
):
    recorder, capture = observed
    clock = [100.0]
    monkeypatch.setattr("omlx.activity.runtime.time.monotonic", lambda: clock[0])
    capture._started = 99.0
    snapshots = []
    original = recorder.invocation_snapshot

    def snapshot(*args, **kwargs):
        snapshots.append(kwargs.copy())
        return original(*args, **kwargs)

    monkeypatch.setattr(recorder, "invocation_snapshot", snapshot)
    invocation = capture.invocation("engine")
    for count in range(1, 101):
        invocation.output(output(count, "x"))
    assert len(snapshots) == 2  # admission and first live update
    clock[0] += 0.6
    invocation.output(output(100))
    assert len(snapshots) == 3
    invocation.output(output(50, finished=True))
    row = detail(recorder, capture)["invocations"][0]
    assert row["generated_tokens"] == 100 and row["counter_regressed"] is True
    assert row["first_token_ms"] == 1000 and row["state"] == "completed"
    assert row["admitted_at_ms"] == 1000 and row["finished_at_ms"] == 1600


def test_content_queue_refusal_keeps_protected_totals_and_marks_detail_gap(
    observed, monkeypatch
):
    recorder, capture = observed
    # The actual offer path must reject against its byte budget.
    with recorder._lock:
        recorder._pending_bytes = recorder.settings.max_pending_bytes
    invocation = capture.invocation("engine")
    assert invocation is not None
    invocation.prepared("unretained", [1, 2])
    invocation.output(output(3, finished=True))
    with recorder._lock:
        recorder._pending_bytes = 0
    capture.api_done("completed")
    result = detail(recorder, capture)
    assert (
        result["observed_input_tokens"] == 2
        and result["observed_generated_tokens"] == 3
    )
    assert result["capture_gaps"] and result["invocations"] == []


@pytest.mark.parametrize("action", ["clear", "delete", "disable"])
def test_live_observer_memory_survives_capture_controls_until_object_release(
    observed, action
):
    recorder, capture = observed
    invocation = capture.invocation("engine")
    assert detail(recorder, capture)["invocations"]
    assert recorder.health()["invocation_live_bytes"] == 2048
    if action == "clear":
        recorder.clear().result(3)
    elif action == "delete":
        recorder.delete(capture.handle.request_id).result(3)
    else:
        from dataclasses import replace

        recorder.update_settings(replace(recorder.settings, enabled=False)).result(3)
    assert recorder.health()["invocation_live_bytes"] == 2048
    invocation.output(output(8, "late", finished=True))
    result = detail(recorder, capture)
    if action != "disable":
        assert result is None
    else:
        assert result["invocations"][0]["state"] == "observation_ended"
        assert result["invocations"][0]["last_observed_state"] == "admitted"
        summary = recorder.reader.snapshot(request_id=capture.handle.request_id)[
            "detail"
        ]["invocations"][0]
        assert summary["state"] == "observation_ended"
        assert summary["last_observed_state"] == "admitted"
    del invocation
    gc.collect()
    assert recorder.health()["invocation_live_bytes"] == 0


def test_memory_bound_can_refuse_new_observers_and_new_requests_without_exceeding_cap(
    tmp_path,
):
    settings = ActivitySettings(
        enabled=True,
        max_pending_bytes=131072,
        max_memory_bytes=147456,
        max_active_requests=1,
    )
    recorder = ActivityRecorder(tmp_path / "activity.sqlite3", settings)
    try:
        assert recorder.wait_ready()
        handle = recorder.begin(model_id="example", endpoint="chat")
        capture = RequestCapture(recorder, handle)
        observers = []
        while (invocation := capture.invocation("engine")) is not None:
            observers.append(invocation)
        assert observers and len(observers) < 256
        with recorder._lock:
            assert recorder._memory_used() <= settings.max_memory_bytes
        assert recorder.begin(model_id="other", endpoint="chat") is None
        assert recorder.flush()
        assert recorder.reader.get(handle.request_id)["capture_gaps"] > 0
    finally:
        assert recorder.close()


@pytest.mark.parametrize("version", [1, 2])
def test_migration_is_transactional_preserves_old_history_and_restart_marks_observation_ended(
    tmp_path, version
):
    path = tmp_path / "activity.sqlite3"
    recorder = ActivityRecorder(path, ActivitySettings(enabled=True))
    assert recorder.wait_ready()
    handle = recorder.begin(model_id="example", endpoint="chat")
    capture = RequestCapture(recorder, handle)
    invocation = capture.invocation("engine")
    invocation.prepared("keep", [1])
    assert recorder.flush()
    assert recorder.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE invocations")
        db.execute(f"PRAGMA user_version={version}")
    assert ActivityReader(path).detail(handle.request_id)["invocations"] == []
    assert (
        ActivityReader(path).snapshot(request_id=handle.request_id)["detail"][
            "invocations"
        ]
        == []
    )
    connect = sqlite3.connect

    def reject_version(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_authorizer(
            lambda action, first, second, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_PRAGMA
                and first == "user_version"
                and second == str(SCHEMA_VERSION)
                else sqlite3.SQLITE_OK
            )
        )
        return db

    with (
        patch("omlx.activity.store.sqlite3.connect", reject_version),
        pytest.raises(sqlite3.DatabaseError),
    ):
        ActivityStore(path, ActivitySettings(enabled=True))
    with connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == version
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='invocations'"
            ).fetchone()
            is None
        )
    store = ActivityStore(path, ActivitySettings(enabled=True))
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 3
    assert (
        b"keep"
        in ActivityReader(path).content(
            handle.request_id, "model_input", invocation_id="1"
        )["data"]
    )
    store.close()


def test_restart_does_not_fabricate_an_engine_outcome_for_an_open_invocation(observed):
    recorder, capture = observed
    invocation = capture.invocation("engine")
    invocation.output(output(4, "partial"))
    assert recorder.flush()
    assert recorder.close()
    store = ActivityStore(recorder.path, recorder.settings)
    try:
        row = recorder.reader.detail(capture.handle.request_id)["invocations"][0]
        assert (
            row["state"] == "observation_ended"
            and row["last_observed_state"] == "generating"
        )
        assert "outcome" not in row
    finally:
        store.close()


def test_encoding_is_worker_only_and_inflight_snapshot_clear_is_fenced(
    observed, monkeypatch
):
    recorder, capture = observed
    assert recorder.flush()
    entered, resume = threading.Event(), threading.Event()
    original = InvocationSnapshot.encode

    def encode(snapshot):
        assert threading.current_thread().name == "omlx-activity"
        entered.set()
        assert resume.wait(3)
        return original(snapshot)

    monkeypatch.setattr(InvocationSnapshot, "encode", encode)
    invocation = capture.invocation("engine")
    assert entered.wait(3)
    assert recorder.health()["pending_bytes"] >= 65536
    clear = recorder.clear()
    resume.set()
    clear.result(3)
    assert recorder.flush()
    assert recorder.reader.detail(capture.handle.request_id) is None
    del invocation
    gc.collect()
    assert recorder.health()["pending_bytes"] == 0
    assert recorder.health()["invocation_live_bytes"] == 0


def test_writer_failure_preserves_live_memory_charge_until_release(
    observed, monkeypatch
):
    recorder, capture = observed
    assert recorder.flush()

    def fail(*args):
        raise OSError("synthetic storage failure")

    monkeypatch.setattr(ActivityStore, "save_invocation", fail)
    invocation = capture.invocation("engine")
    assert recorder.close()
    assert recorder.health()["error"] is not None
    assert recorder.health()["pending_bytes"] == 0
    assert recorder.health()["invocation_live_bytes"] == 2048
    invocation.end()
    del invocation
    gc.collect()
    assert recorder.health()["invocation_live_bytes"] == 0


def test_update_size_accounting_is_resident_and_delete_cascades(observed):
    recorder, capture = observed
    invocation = capture.invocation("engine")
    invocation.prepared("actual", [11, 12])
    invocation.output(output(2, "done", finished=True))
    capture.api_done("completed")
    result = detail(recorder, capture)
    with sqlite3.connect(recorder.path) as db:
        assert db.execute("SELECT count(*) FROM invocations").fetchone()[0] == 1
        size = db.execute("SELECT length(metadata) FROM invocations").fetchone()[0]
    assert result["captured_bytes"] == size + sum(
        item["bytes"] for item in result["representations"]
    )
    recorder.delete(capture.handle.request_id).result(3)
    with sqlite3.connect(recorder.path) as db:
        assert db.execute("SELECT count(*) FROM invocations").fetchone()[0] == 0
    assert not recorder.invocation_snapshot(capture.handle, "257", state="admitted")
    assert not recorder.invocation_snapshot(capture.handle, "001", state="admitted")


def test_live_snapshots_exclude_options_and_engine_identifiers(observed):
    recorder, capture = observed
    invocation = capture.invocation(
        "private-engine-identifier", options=(("stop", ["private-stop-sequence"]),)
    )
    invocation.output(output(1, "partial"))
    result = detail(recorder, capture)
    assert result["invocation_options_included"] is True
    snapshot = recorder.reader.snapshot(request_id=capture.handle.request_id)
    assert snapshot["detail"]["invocation_options_included"] is False
    assert "private-stop-sequence" not in json.dumps(snapshot)
    assert "private-engine-identifier" not in json.dumps(snapshot)
    row = snapshot["detail"]["invocations"][0]
    assert row["generated_tokens"] == 1 and row["state"] == "generating"
    assert "admission_options" not in row


def test_summary_projection_skips_python_decode_and_preserves_scalar_semantics(
    observed, monkeypatch
):
    recorder, capture = observed
    capture.invocation("private-engine", options=(("stop", ["x" * 12000]),))
    assert recorder.flush()
    with sqlite3.connect(recorder.path) as db:
        metadata = db.execute("SELECT metadata FROM invocations").fetchone()[0]
        record = json.loads(metadata)
        record.update(
            {
                "state": "generating",
                "last_observed_state": "prefilling",
                "input_tokens": 0,
                "generated_tokens": None,
                "first_token_ms": False,
                "counter_regressed": True,
                "admission_options": {"stop": ["x" * 12000]},
            }
        )
        metadata = json.dumps(record)
        assert 12000 < len(metadata.encode()) <= MAX_INVOCATION_BYTES
        db.execute("UPDATE invocations SET metadata=?", (metadata,))

    original_loads = json.loads

    def reject_invocation_decode(value, *args, **kwargs):
        assert value != metadata
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr("omlx.activity.store.json.loads", reject_invocation_decode)
    snapshot = recorder.reader.snapshot(request_id=capture.handle.request_id)
    row = snapshot["detail"]["invocations"][0]
    assert row == {
        "id": "1",
        "state": "generating",
        "last_observed_state": "prefilling",
        "input_tokens": 0,
        "generated_tokens": None,
        "first_token_ms": False,
        "counter_regressed": True,
        "scope": "observed_scheduler_invocation_snapshot",
    }
    assert "admission_options" not in row


def test_summary_falls_back_when_sqlite_json_functions_are_unavailable(
    observed, monkeypatch
):
    recorder, capture = observed
    invocation = capture.invocation("engine")
    invocation.output(output(0, "partial"))
    assert recorder.flush()
    monkeypatch.setattr(
        ActivityReader, "_json_functions_available", staticmethod(lambda _db: False)
    )
    row = recorder.reader.snapshot(request_id=capture.handle.request_id)["detail"][
        "invocations"
    ][0]
    assert row["generated_tokens"] == 0
    assert row["state"] == "generating"
    assert row["id"] == "1"
    assert "admission_options" not in row


@pytest.mark.parametrize(
    ("message", "unavailable"),
    [("no such function: json_type", True), ("database is locked", False)],
)
def test_json_probe_falls_back_only_for_unavailable_functions(message, unavailable):
    class Connection:
        def execute(self, query):
            raise sqlite3.OperationalError(message)

    if unavailable:
        assert ActivityReader._json_functions_available(Connection()) is False
    else:
        with pytest.raises(sqlite3.OperationalError, match=message):
            ActivityReader._json_functions_available(Connection())


def test_gc_can_release_observer_memory_while_same_thread_owns_ledger_lock(observed):
    recorder, capture = observed
    invocation = capture.invocation("engine")
    assert detail(recorder, capture)["invocations"]
    invocation._cycle = invocation
    del invocation
    with recorder._lock:
        gc.collect()
        assert recorder._invocation_live_bytes == 0
