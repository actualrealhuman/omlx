# SPDX-License-Identifier: Apache-2.0
"""Scalar evidence stays truthful across cumulative updates and capture loss."""

import json
from types import SimpleNamespace

from omlx.activity import ActivityRecorder, ActivitySettings
from omlx.activity.runtime import RequestCapture


def output(tokens=None, *, text="", finished=False):
    values = dict(new_text=text, error=None, finished=finished, finish_reason="stop")
    if tokens is not None:
        values["completion_tokens"] = tokens
    return SimpleNamespace(**values)


def test_multiple_invocations_sum_counter_increases_without_recounting_snapshots(
    tmp_path,
):
    recorder = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    try:
        assert recorder.wait_ready()
        handle = recorder.begin(model_id="example", endpoint="chat")
        capture = RequestCapture(recorder, handle)
        first, second = capture.invocation("one"), capture.invocation("two")
        first.prepared("a", [1, 2, 3])
        second.prepared("b", [4, 5])
        first.output(output(3, text="x"))
        assert recorder.flush()
        earliest = recorder.reader.get(handle.request_id)["first_observed_token_ms"]
        first.output(output(3))
        first.output(output(8, finished=True))
        second.output(output(4, finished=True))
        capture.api_done("completed")
        assert recorder.flush()
        row = recorder.reader.get(handle.request_id)
        assert row["observed_invocations"] == 2
        assert row["observed_input_tokens"] == 5
        assert row["observed_generated_tokens"] == 12
        assert row["first_observed_token_ms"] == earliest
    finally:
        assert recorder.close()


def test_text_without_an_engine_counter_does_not_invent_tokens_or_token_latency(
    tmp_path,
):
    recorder = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    try:
        assert recorder.wait_ready()
        handle = recorder.begin(model_id="example", endpoint="chat")
        capture = RequestCapture(recorder, handle)
        invocation = capture.invocation("one")
        invocation.output(
            output(text="many words still provide no token count", finished=True)
        )
        capture.api_done("completed")
        assert recorder.flush()
        detail = recorder.reader.detail(handle.request_id)
        assert "observed_generated_tokens" not in detail
        assert "first_observed_token_ms" not in detail
        assert detail["first_observed_text_ms"] >= 0
        assert detail["observation"]["generated_count_source"] == "unavailable"
        events = [
            json.loads(line)
            for line in recorder.reader.content(handle.request_id, "events")[
                "data"
            ].splitlines()
        ]
        assert events[-1]["generated_tokens"] is None
    finally:
        assert recorder.close()


def test_counter_regression_is_flagged_and_late_updates_cannot_reopen_closed_capture(
    tmp_path,
):
    recorder = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    try:
        assert recorder.wait_ready()
        handle = recorder.begin(model_id="example", endpoint="chat")
        capture = RequestCapture(recorder, handle)
        invocation = capture.invocation("one")
        for tokens in (8, 3, 10):
            invocation.output(output(tokens))
        recorder.update_settings(ActivitySettings(enabled=False)).result(2)
        invocation.output(output(100, finished=True))
        capture.api_done("completed")
        assert recorder.flush()
        row = recorder.reader.get(handle.request_id)
        assert row["state"] == "capture_stopped"
        assert row["observed_generated_tokens"] == 10
        assert row["capture_gaps"] >= 2
    finally:
        assert recorder.close()


def test_scalar_evidence_survives_content_queue_loss_without_claiming_retained_ids(
    tmp_path,
):
    recorder = ActivityRecorder(
        tmp_path / "activity.sqlite3",
        ActivitySettings(enabled=True, max_pending_bytes=256),
    )
    try:
        assert recorder.wait_ready()
        handle = recorder.begin(model_id="example", endpoint="chat")
        capture = RequestCapture(recorder, handle)
        invocation = capture.invocation("one")
        invocation.prepared("input", [1, 2])
        invocation.output(output(4, finished=True))
        capture.api_done("completed")
        assert recorder.flush()
        detail = recorder.reader.detail(handle.request_id)
        assert detail["observed_input_tokens"] == 2
        assert detail["observed_generated_tokens"] == 4
        assert detail["observation"]["prepared_token_ids_retained"] is False
        assert detail["capture_status"] == "gaps_recorded"
    finally:
        assert recorder.close()


def test_sampling_evidence_excludes_non_scalar_and_nonfinite_values(tmp_path):
    recorder = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    try:
        assert recorder.wait_ready()
        handle = recorder.begin(model_id="example", endpoint="chat")
        capture = RequestCapture(recorder, handle)
        capture.invocation(
            "one",
            options=(
                ("temperature", float("nan")),
                ("seed", 10**5000),
                ("grammar", object()),
                ("max_tokens", 64),
            ),
        )
        assert recorder.flush()
        event = json.loads(recorder.reader.content(handle.request_id, "events")["data"])
        assert event["sampling_subset"] == {"max_tokens": 64}
        assert recorder.metrics(handle, counts={"observed_input_tokens": True}) is False
        assert (
            recorder.metrics(handle, times={"first_observed_token_ms": float("inf")})
            is False
        )
        assert recorder.metrics(handle, counts={"unbounded_field": 1}) is False
    finally:
        assert recorder.close()
