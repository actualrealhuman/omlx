# SPDX-License-Identifier: Apache-2.0
"""Original image ownership, observer verification and admin-only delivery."""

import base64
import io
import random
import sqlite3
import struct
import threading
import zlib
from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image

from omlx.activity import ActivityRecorder, ActivitySettings
from omlx.activity.images import prepare_image
from omlx.activity.routes import create_activity_router
from omlx.activity.runtime import configure_recorder
from omlx.activity.snapshot import snapshot_request
from omlx.activity.store import SCHEMA_VERSION, ActivityReader, ActivityStore
from omlx.api.anthropic_models import MessagesRequest
from omlx.api.openai_models import ChatCompletionRequest
from omlx.api.responses_models import ResponsesRequest
from omlx.settings import GlobalSettings


def image_bytes(format="PNG"):
    target = io.BytesIO()
    Image.new("RGB", (16, 8), (40, 100, 160)).save(target, format=format)
    return target.getvalue()


def payload(data, family="chat", mime="image/png"):
    encoded = base64.b64encode(data).decode("ascii")
    uri = f"data:{mime};base64,{encoded}"
    parts = [
        {"type": "text", "text": "before"},
        {"type": "image_url", "image_url": {"url": uri}},
        {"type": "text", "text": "after"},
    ]
    if family == "messages":
        parts[1] = {
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": encoded},
        }
        return MessagesRequest(
            model="example",
            max_tokens=16,
            messages=[{"role": "user", "content": parts}],
        )
    if family == "responses":
        parts = [
            {"type": "input_text", "text": "before"},
            {"type": "input_image", "image_url": uri},
            {"type": "input_text", "text": "after"},
        ]
        return ResponsesRequest(
            model="example", input=[{"role": "user", "content": parts}]
        )
    return ChatCompletionRequest(
        model="example", messages=[{"role": "user", "content": parts}]
    )


def capture(recorder, request):
    handle = recorder.begin(model_id="example", endpoint="synthetic")
    assert recorder.capture_submitted(handle, request)
    recorder.finish(handle)
    assert recorder.flush()
    return handle


@pytest.fixture
def recorder(tmp_path):
    result = ActivityRecorder(
        tmp_path / "activity.sqlite3", ActivitySettings(enabled=True)
    )
    assert result.wait_ready()
    yield result
    assert result.close()


@pytest.mark.parametrize("family", ["chat", "messages", "responses"])
@pytest.mark.parametrize(
    "format,mime",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_actual_schemas_original_bytes_and_observer_provenance(
    recorder, family, format, mime
):
    original = image_bytes(format)
    handle = capture(recorder, payload(original, family, mime))
    detail = recorder.reader.detail(handle.request_id)
    info = detail["attachments"][0]
    assert info == {
        "id": "attachment-1",
        "status": "retained",
        "reason": None,
        "mime_type": mime,
        "provenance": "submitted_original",
        "validation": "observer_container_checked",
        "bytes": len(original),
        "width": 16,
        "height": 8,
    }
    result = recorder.reader.attachment(handle.request_id, info["id"])
    assert result["data"] == original
    assert result["size"] == len(original)
    assert detail["captured_bytes"] == len(original) + sum(
        item["bytes"] for item in detail["representations"]
    )
    submitted = recorder.reader.content(handle.request_id, "submitted")["data"]
    assert (
        b"before" in submitted
        and b"after" in submitted
        and b"submitted_unvalidated" in submitted
    )
    assert (
        b"see_attachment_index" in submitted
        and base64.b64encode(original) not in submitted
    )
    assert detail["capture_gaps"] == 0
    assert recorder.health()["pending_bytes"] == 0


@pytest.mark.parametrize(
    "data,mime",
    [
        (b"not an image", "image/png"),
        (image_bytes("JPEG"), "image/png"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml"),
        (b"<html>example</html>", "text/html"),
    ],
)
def test_invalid_or_active_formats_leave_text_and_explanatory_reference(
    recorder, data, mime
):
    handle = capture(recorder, payload(data, mime=mime))
    detail = recorder.reader.detail(handle.request_id)
    assert detail["attachments"][0]["status"] == "not_retained"
    assert detail["capture_gaps"] > 0
    assert recorder.reader.attachment(handle.request_id, "attachment-1") is None
    assert b"after" in recorder.reader.content(handle.request_id, "submitted")["data"]
    assert recorder.health()["error"] is None


def test_text_first_queue_and_request_limits_are_independent():
    request = payload(image_bytes() + bytes(16384))
    for options, reason in [
        ({"memory_bytes": 60000}, "attachment_queue_limit"),
        ({"max_attachment_bytes": 1}, "attachment_size_limit"),
        ({"max_bytes": 8500}, "attachment_request_limit"),
    ]:
        settings = dict(
            memory_bytes=1024**2,
            max_bytes=1024**2,
            max_attachment_bytes=1024**2,
            retain_images=True,
        )
        settings.update(options)
        snapshot = snapshot_request(request, **settings)
        assert snapshot.images[0].encoded is None
        assert snapshot.images[0].reason == reason
        assert b"after" in b"".join(snapshot.encoded_chunks(256 * 1024))
        assert snapshot.memory_bytes <= settings["memory_bytes"]
        assert snapshot.estimated_bytes <= settings["max_bytes"]


def test_retention_switch_and_external_references_never_decode(recorder):
    recorder.update_settings(replace(recorder.settings, retain_images=False)).result(5)
    with patch("base64.b64decode", side_effect=AssertionError("disabled media decode")):
        handle = capture(recorder, payload(image_bytes()))
    assert (
        recorder.reader.detail(handle.request_id)["attachments"][0]["reason"]
        == "image_retention_disabled"
    )
    request = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.invalid/image.png"},
                    }
                ],
            }
        ]
    }
    handle = capture(recorder, request)
    assert (
        recorder.reader.detail(handle.request_id)["attachments"][0]["reason"]
        == "reference_not_fetched"
    )


def test_verification_runs_only_on_writer_and_does_not_load_pixels(recorder):
    names = []
    original = prepare_image

    def observed(image):
        names.append(threading.current_thread().name)
        return original(image)

    request = payload(image_bytes())
    with (
        patch("omlx.activity.recorder.prepare_image", observed),
        patch.object(Image.Image, "load", side_effect=AssertionError("pixel decode")),
    ):
        handle = capture(recorder, request)
    assert names == ["omlx-activity"]
    assert (
        recorder.reader.detail(handle.request_id)["attachments"][0]["status"]
        == "retained"
    )


@pytest.mark.parametrize("operation", ["clear", "delete"])
def test_pending_verification_cannot_resurrect_after_deletion(recorder, operation):
    entered, resume = threading.Event(), threading.Event()
    original = prepare_image

    def blocked(image):
        entered.set()
        assert resume.wait(5)
        return original(image)

    with patch("omlx.activity.recorder.prepare_image", blocked):
        handle = recorder.begin(model_id="example", endpoint="synthetic")
        assert recorder.capture_submitted(handle, payload(image_bytes()))
        assert entered.wait(5)
        reserved = recorder.health()["pending_bytes"]
        future = (
            recorder.clear()
            if operation == "clear"
            else recorder.delete(handle.request_id)
        )
        assert reserved > 0 and recorder.health()["pending_bytes"] == reserved
        resume.set()
        future.result(5)
    assert recorder.flush()
    assert recorder.reader.detail(handle.request_id) is None
    assert recorder.reader.attachment(handle.request_id, "attachment-1") is None
    assert recorder.health()["pending_bytes"] == 0


def test_attachments_cascade_with_count_age_and_clear(recorder):
    recorder.update_settings(replace(recorder.settings, max_requests=1)).result(5)
    first = capture(recorder, payload(image_bytes()))
    second = capture(recorder, payload(image_bytes()))
    assert recorder.reader.attachment(first.request_id, "attachment-1") is None
    assert recorder.reader.attachment(second.request_id, "attachment-1") is not None
    recorder.clear().result(5)
    with sqlite3.connect(recorder.path) as db:
        assert db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0


def test_schema_one_upgrade_preserves_existing_text_and_is_transactional(tmp_path):
    path = tmp_path / "activity.sqlite3"
    recorder = ActivityRecorder(path, ActivitySettings(enabled=True))
    assert recorder.wait_ready()
    handle = capture(recorder, {"messages": [{"role": "user", "content": "keep"}]})
    assert recorder.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE attachments")
        db.execute("PRAGMA user_version=1")
    old = ActivityReader(path)
    assert old.detail(handle.request_id)["attachments"] == []
    assert old.attachment(handle.request_id, "attachment-1") is None
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
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='attachments'"
            ).fetchone()
            is None
        )
    with patch.object(ActivityStore, "prune"):
        store = ActivityStore(path, ActivitySettings(enabled=True))
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert b"keep" in old.content(handle.request_id, "submitted")["data"]
    store.close()
    # A newer schema is preserved rather than downgraded/erased.
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(ValueError):
        ActivityStore(path, ActivitySettings(enabled=True))
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


def test_admin_image_delivery_is_lazy_bounded_and_inert(recorder, tmp_path):
    settings = GlobalSettings(base_path=tmp_path)

    async def require_admin(request: Request):
        if request.headers.get("authorization") != "Bearer synthetic-admin":
            raise HTTPException(401)

    router = create_activity_router(
        get_settings=lambda: settings, require_admin=require_admin
    )
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    original = image_bytes()
    handle = capture(recorder, payload(original))
    path = f"/admin/api/activity/requests/{handle.request_id}/attachments/attachment-1"
    try:
        with TestClient(app) as client:
            assert client.get(path).status_code == 401
            assert (
                client.get(
                    path, headers={"authorization": "Bearer synthetic-inference"}
                ).status_code
                == 401
            )
            response = client.get(
                path, headers={"authorization": "Bearer synthetic-admin"}
            )
            assert response.status_code == 200 and response.content == original
            assert response.headers["content-type"] == "image/png"
            assert response.headers["cache-control"] == "no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert "sandbox" in response.headers["content-security-policy"]
            assert "activity-image.png" in response.headers["content-disposition"]
            assert router.activity_service._streams == set()
            recorder.delete(handle.request_id).result(5)
            assert (
                client.get(
                    path, headers={"authorization": "Bearer synthetic-admin"}
                ).status_code
                == 404
            )
            assert router.activity_service._streams == set()
    finally:
        router.activity_service.close()


def test_malformed_animated_and_huge_containers_are_omitted_without_codec_work(
    recorder,
):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    huge = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 16384, 16384, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", b"x")
        + chunk(b"IEND", b"")
    )
    animated = io.BytesIO()
    Image.new("RGB", (2, 2)).save(
        animated,
        format="PNG",
        save_all=True,
        append_images=[Image.new("RGB", (2, 2), "red")],
        duration=100,
    )
    webp = io.BytesIO()
    Image.new("RGB", (2, 2)).save(
        webp,
        format="WEBP",
        save_all=True,
        append_images=[Image.new("RGB", (2, 2), "red")],
        duration=100,
    )
    fixtures = [
        (huge, "image/png"),
        (animated.getvalue(), "image/png"),
        (webp.getvalue(), "image/webp"),
        (image_bytes()[:-5], "image/png"),
        (image_bytes("JPEG")[:-2], "image/jpeg"),
        (image_bytes("WEBP")[:-1], "image/webp"),
    ]
    with patch("PIL.Image.open", side_effect=AssertionError("observer invoked codec")):
        for data, mime in fixtures:
            handle = capture(recorder, payload(data, mime=mime))
            info = recorder.reader.detail(handle.request_id)["attachments"][0]
            assert info["reason"] == "image_verification_failed"
    assert recorder.health()["error"] is None


def test_repeated_images_have_distinct_ordered_references_and_budget_charges(recorder):
    request = payload(image_bytes(), "messages")
    block = request.messages[0].content[1]
    request.messages[0].content.append(block)
    handle = capture(recorder, request)
    detail = recorder.reader.detail(handle.request_id)
    assert {image["id"] for image in detail["attachments"]} == {
        "attachment-1",
        "attachment-2",
    }
    assert (
        detail["captured_bytes"]
        == sum(item["bytes"] for item in detail["representations"])
        + len(image_bytes()) * 2
    )
    assert (
        recorder.reader.attachment(handle.request_id, "attachment-1")["data"]
        == recorder.reader.attachment(handle.request_id, "attachment-2")["data"]
    )


def test_large_original_is_read_in_bounded_ranges_and_stream_authority_rechecked(
    recorder, tmp_path
):
    output = io.BytesIO()
    Image.frombytes("RGB", (256, 256), random.Random(0).randbytes(256 * 256 * 3)).save(
        output, format="PNG"
    )
    original = output.getvalue()
    assert len(original) > 65536 * 2
    handle = capture(recorder, payload(original))
    collected = bytearray()
    for offset in range(0, len(original), 65536):
        page = recorder.reader.attachment(
            handle.request_id, "attachment-1", offset=offset
        )
        assert len(page["data"]) <= 65536
        collected.extend(page["data"])
    assert bytes(collected) == original
    checks = []

    async def require_admin(request: Request):
        checks.append(1)
        if len(checks) > 2:
            raise HTTPException(401)

    router = create_activity_router(
        get_settings=lambda: GlobalSettings(base_path=tmp_path),
        require_admin=require_admin,
    )
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    try:
        with TestClient(app) as client:
            response = client.get(
                f"/admin/api/activity/requests/{handle.request_id}/attachments/attachment-1"
            )
            assert len(checks) == 3
            assert len(response.content) < len(original)
            assert router.activity_service._streams == set()
    finally:
        router.activity_service.close()


def test_original_storage_obeys_physical_budget_and_age(tmp_path):
    path = tmp_path / "activity.sqlite3"
    settings = ActivitySettings(
        enabled=True,
        max_storage_bytes=2 * 1024**2,
        max_request_bytes=1024**2,
        max_attachment_bytes=1024**2,
    )
    recorder = ActivityRecorder(path, settings)
    assert recorder.wait_ready()
    output = io.BytesIO()
    Image.frombytes("RGB", (512, 256), random.Random(1).randbytes(512 * 256 * 3)).save(
        output, format="PNG"
    )
    try:
        ids = [
            capture(recorder, payload(output.getvalue())).request_id for _ in range(4)
        ]
        assert recorder.health()["error"] is None
        assert recorder.reader.get(ids[0]) is None
        assert recorder.reader.attachment(ids[-1], "attachment-1") is not None
        assert (
            sum(
                p.stat().st_size
                for p in tmp_path.iterdir()
                if p.name.startswith("activity.sqlite3")
            )
            <= settings.max_storage_bytes
        )
    finally:
        assert recorder.close()
    store = ActivityStore(path, settings)
    store.prune(now=10**12)
    assert store.db.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    store.close()


def test_live_retention_switch_persists_and_preserves_existing_originals(
    recorder, tmp_path
):
    settings = GlobalSettings(base_path=tmp_path)
    settings.activity = recorder.settings

    async def require_admin(request: Request):
        if request.headers.get("authorization") != "Bearer synthetic-admin":
            raise HTTPException(401)

    router = create_activity_router(
        get_settings=lambda: settings, require_admin=require_admin
    )
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    previous = configure_recorder(recorder)
    first = capture(recorder, payload(image_bytes()))
    try:
        with TestClient(app) as client:
            response = client.put(
                "/admin/api/activity/settings",
                headers={
                    "authorization": "Bearer synthetic-admin",
                    "X-Activity-Action": "settings",
                },
                json={"retain_images": False},
            )
            assert response.status_code == 200
            assert response.json()["settings"]["retain_images"] is False
            assert GlobalSettings.load(tmp_path).activity.retain_images is False
            assert (
                recorder.reader.attachment(first.request_id, "attachment-1") is not None
            )
            later = capture(recorder, payload(image_bytes()))
            assert (
                recorder.reader.detail(later.request_id)["attachments"][0]["reason"]
                == "image_retention_disabled"
            )
    finally:
        configure_recorder(previous)
        router.activity_service.close()
