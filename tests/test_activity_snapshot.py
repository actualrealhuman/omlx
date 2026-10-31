# SPDX-License-Identifier: Apache-2.0
"""Semantic snapshots keep useful text without retaining oversized media."""

import gc
import json
import threading
import weakref
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from omlx.activity import ActivityRecorder, ActivitySettings
from omlx.activity.snapshot import SemanticSnapshot, snapshot_request
from omlx.activity.store import MAX_CHUNK_BYTES, ActivityStore
from omlx.api.anthropic_models import MessagesRequest
from omlx.api.openai_models import ChatCompletionRequest, CompletionRequest
from omlx.api.responses_models import ResponsesRequest


def encoded(snapshot):
    chunks = list(snapshot.encoded_chunks(MAX_CHUNK_BYTES))
    assert all(len(chunk) <= MAX_CHUNK_BYTES for chunk in chunks)
    data = b"".join(chunks)
    assert len(data) <= snapshot.estimated_bytes
    return json.loads(data)


@pytest.mark.parametrize(
    "payload",
    [
        CompletionRequest(model="example", prompt=["one", "two"], temperature=None),
        ChatCompletionRequest(
            model="example",
            messages=[{"role": "user", "content": "世界🙂"}],
            logit_bias={"12": 0.5},
        ),
        MessagesRequest(
            model="example",
            max_tokens=16,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "custom_block", "data": {"keep": True}},
                    ],
                }
            ],
        ),
        ResponsesRequest(
            model="example",
            input=[
                {"role": "user", "content": "hello", "extra_example": {"data": "keep"}}
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "example",
                    "schema": {"type": "object"},
                    "strict": True,
                }
            },
        ),
    ],
)
def test_actual_request_families_preserve_semantics_unset_fields_and_extras(payload):
    expected = json.loads(payload.model_dump_json(exclude_unset=True))
    with patch.object(
        type(payload),
        "model_dump_json",
        side_effect=AssertionError("serialized at ingress"),
    ):
        snapshot = snapshot_request(
            payload, memory_bytes=16 * 1024**2, max_bytes=64 * 1024**2
        )
    assert encoded(snapshot) == expected
    assert not snapshot.reasons


@pytest.mark.parametrize("family", ["chat", "messages", "responses"])
def test_large_media_is_normalized_before_queue_admission_and_text_survives(
    tmp_path, family
):
    data = "A" * (5 * 1024**2)
    if family == "chat":
        request = ChatCompletionRequest(
            model="example",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "before"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64," + data},
                        },
                        {"type": "text", "text": "after"},
                    ],
                }
            ],
        )
    elif family == "messages":
        request = MessagesRequest(
            model="example",
            max_tokens=16,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "before"},
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": data,
                            },
                        },
                        {"type": "text", "text": "after"},
                    ],
                }
            ],
        )
    else:
        request = ResponsesRequest(
            model="example",
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "before"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64," + data,
                        },
                        {"type": "input_text", "text": "after"},
                    ],
                }
            ],
        )
    recorder = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    try:
        assert recorder.wait_ready()
        handle = recorder.begin(model_id="example", endpoint="synthetic")
        assert recorder.capture_submitted(handle, request)
        assert recorder.flush()
        saved = recorder.reader.content(handle.request_id, "submitted")["data"]
        assert len(saved) < 8192
        assert b"before" in saved and b"after" in saved
        assert b"AAAAA" not in saved
        assert b"submitted_unvalidated" in saved
        row = recorder.reader.get(handle.request_id)
        assert row["submitted_normalized"] is True
        assert row["submitted_media_references"] == 1
        assert row["capture_reason"] == "attachment_queue_limit"
        assert recorder.health()["pending_bytes"] == 0
    finally:
        assert recorder.close()


def test_nested_media_preserves_order_plain_documents_and_ordinary_tool_json():
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": [
                            {"type": "text", "text": "result text"},
                            {
                                "type": "image",
                                "source": {"type": "base64", "data": "AAAA"},
                            },
                        ],
                    },
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "AAAA", "format": "wav"},
                    },
                    {
                        "type": "video_url",
                        "video_url": {"url": "data:video/mp4;base64,AAAA"},
                    },
                    {
                        "type": "file",
                        "file": {"data": "AAAA", "filename": "example.bin"},
                    },
                    {
                        "type": "document",
                        "source": {
                            "type": "text",
                            "media_type": "text/plain",
                            "data": "useful document text",
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "content": {
                    "data": "ordinary result",
                    "type": "image_url",
                    "url": "ordinary JSON",
                },
            },
        ]
    }
    snapshot = snapshot_request(payload, memory_bytes=1024**2, max_bytes=1024**2)
    frozen = encoded(snapshot)
    assert snapshot.media_count == 4
    assert frozen["messages"][1] == payload["messages"][1]
    assert (
        frozen["messages"][0]["content"][-1]["source"]["data"] == "useful document text"
    )
    references = []

    def visit(value):
        if isinstance(value, dict):
            if "_omlx_activity_attachment" in value:
                references.append(value["_omlx_activity_attachment"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(frozen)
    assert [ref["id"] for ref in references] == [f"attachment-{i}" for i in range(1, 5)]
    assert references[0]["path"] == [
        "messages",
        0,
        "content",
        0,
        "content",
        1,
        "source",
        "data",
    ]


def test_mutable_request_ownership_ends_at_snapshot_and_encoding_is_bounded():
    class Payload(BaseModel):
        model: str
        messages: list[dict]

    request = Payload(
        model="example", messages=[{"role": "user", "content": "世界🙂\\\n" * 50000}]
    )
    reference = weakref.ref(request)
    snapshot = snapshot_request(
        request, memory_bytes=16 * 1024**2, max_bytes=64 * 1024**2
    )
    request.messages[0]["content"] = "mutated"
    request.messages.clear()
    del request
    gc.collect()
    assert reference() is None
    assert encoded(snapshot)["messages"][0]["content"].startswith("世界🙂")


@pytest.mark.parametrize("budget", [8192, 16384, 32768, 131072])
def test_snapshot_limits_leave_valid_explicit_partial_json(budget):
    payload = {"messages": [{"role": "user", "content": "x" * 100000}] * 10000}
    snapshot = snapshot_request(payload, memory_bytes=budget, max_bytes=budget)
    assert snapshot.memory_bytes <= budget
    assert snapshot.estimated_bytes <= budget
    assert snapshot.reasons
    assert "_omlx_activity_omitted" in json.dumps(encoded(snapshot))


def test_cycles_and_unknown_objects_cannot_escape_into_the_writer():
    payload = {"unknown": object()}
    payload["cycle"] = payload
    snapshot = snapshot_request(payload, memory_bytes=1024**2, max_bytes=1024**2)
    assert "snapshot_structure_limit" in snapshot.reasons
    assert "snapshot_value_unavailable" in snapshot.reasons
    assert "_omlx_activity_omitted" in json.dumps(encoded(snapshot))


@pytest.mark.parametrize("operation", ["clear", "delete", "disable", "failure"])
def test_snapshot_reservation_survives_clear_or_writer_failure_and_releases_once(
    tmp_path, operation
):
    recorder = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    entered, release = threading.Event(), threading.Event()
    result = []

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return snapshot_request(*args, **kwargs)

    try:
        assert recorder.wait_ready()
        handle = recorder.begin(model_id="example", endpoint="synthetic")
        with patch("omlx.activity.recorder.snapshot_request", blocked):
            thread = threading.Thread(
                target=lambda: result.append(
                    recorder.capture_submitted(handle, {"prompt": "hello"})
                )
            )
            thread.start()
            assert entered.wait(2)
            try:
                if operation == "clear":
                    recorder.clear().result(2)
                elif operation == "delete":
                    recorder.delete(handle.request_id).result(2)
                elif operation == "disable":
                    recorder.update_settings(ActivitySettings(enabled=False)).result(2)
                else:
                    with patch.object(
                        ActivityStore,
                        "prune",
                        side_effect=RuntimeError("synthetic failure"),
                    ):
                        assert not recorder.flush()
                assert recorder.health()["pending_bytes"] > 0
            finally:
                release.set()
                thread.join(2)
            assert not thread.is_alive()
        assert result == [False]
        assert recorder.health()["pending_bytes"] == 0
        assert recorder._pending_count == 0
        assert recorder._freezing_count == 0
        if operation in {"clear", "delete"}:
            assert recorder.flush()
            assert recorder.reader.list_requests() == []
        elif operation == "disable":
            assert recorder.reader.get(handle.request_id)["state"] == "capture_stopped"
    finally:
        release.set()
        assert recorder.close()


def test_disabled_snapshot_does_no_traversal_or_encoding(tmp_path):
    recorder = ActivityRecorder(tmp_path / "activity.sqlite3", ActivitySettings())
    with patch(
        "omlx.activity.recorder.snapshot_request",
        side_effect=AssertionError("snapshot while off"),
    ) as freeze:
        assert recorder.capture_submitted(None, object()) is False
        freeze.assert_not_called()
    assert not recorder.path.exists()
    assert recorder.close()


def test_snapshot_walk_failure_releases_admission_and_preserves_serving(tmp_path):
    recorder = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    try:
        assert recorder.wait_ready()
        handle = recorder.begin(model_id="example", endpoint="synthetic")
        with patch(
            "omlx.activity.recorder.snapshot_request",
            side_effect=ValueError("synthetic failure"),
        ):
            assert not recorder.capture_submitted(handle, {"prompt": "hello"})
        assert recorder.health()["pending_bytes"] == 0
        assert recorder._pending_count == 0
        assert recorder.capture_submitted(handle, {"prompt": "retry"})
        assert recorder.flush()
        assert (
            recorder.reader.get(handle.request_id)["capture_reason"]
            == "submitted_snapshot_unavailable"
        )
    finally:
        assert recorder.close()


def test_snapshot_json_encoding_runs_only_on_the_writer_thread(tmp_path):
    recorder = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    observed = []
    original = SemanticSnapshot.encoded_chunks

    def record_thread(snapshot, limit):
        observed.append(threading.get_ident())
        yield from original(snapshot, limit)

    try:
        assert recorder.wait_ready()
        handle = recorder.begin(model_id="example", endpoint="synthetic")
        with patch.object(SemanticSnapshot, "encoded_chunks", record_thread):
            assert recorder.capture_submitted(handle, {"prompt": "hello"})
            assert recorder.flush()
        assert observed and all(thread != threading.get_ident() for thread in observed)
    finally:
        assert recorder.close()
