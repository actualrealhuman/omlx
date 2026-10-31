# SPDX-License-Identifier: Apache-2.0
"""Live controls persist only Activity state and never cancel model inference."""

import asyncio
import json
import random
import threading
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omlx.activity import ActivityRecorder, ActivitySettings
from omlx.activity.routes import create_activity_router
from omlx.activity.runtime import RequestCapture, configure_recorder, get_recorder
from omlx.admin import auth
from omlx.settings import GlobalSettings, SubKeyEntry


@pytest.fixture
def controls(tmp_path):
    settings = GlobalSettings(base_path=tmp_path)
    settings.auth.api_key = "synthetic-admin"
    settings.auth.sub_keys = [SubKeyEntry(key="synthetic-client", name="Example")]
    settings.save()
    previous = configure_recorder(None)
    router = create_activity_router(
        get_settings=lambda: settings, require_admin=auth.require_admin
    )
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    with (
        patch.object(auth, "_get_global_settings", lambda: settings),
        patch.object(auth, "_key_failures", {}),
        TestClient(app) as client,
    ):
        yield settings, client, router.activity_service
    recorder = configure_recorder(previous)
    if recorder is not None:
        assert recorder.close()


def change(client, payload, **headers):
    return client.put(
        "/admin/api/activity/settings",
        json=payload,
        headers={
            "Authorization": "Bearer synthetic-admin",
            "X-Activity-Action": "settings",
            **headers,
        },
    )


def test_live_disable_preserves_partial_output_and_does_not_end_engine(controls):
    settings, client, service = controls
    assert change(client, {"enabled": True}).status_code == 200
    recorder = get_recorder()
    handle = recorder.begin(model_id="example", endpoint="chat")
    capture = RequestCapture(recorder, handle)
    invocation = capture.invocation("engine-id")
    recorder.append_text(handle, "raw_output", "partial answer")
    result = change(client, {"enabled": False})
    assert result.status_code == 200
    row = recorder.reader.detail(handle.request_id)
    assert row["state"] == "capture_stopped"
    assert row["capture_reason"] == "capture_disabled"
    assert row["finished_at"] is not None
    assert (
        recorder.reader.content(handle.request_id, "raw_output")["data"]
        == b"partial answer"
    )
    assert invocation.done is False
    assert capture._active == 1
    assert recorder.begin(model_id="example", endpoint="chat") is None
    assert recorder.append_text(handle, "raw_output", "late") is False
    assert change(client, {"enabled": True}).status_code == 200
    assert recorder.append_text(handle, "raw_output", "resurrect") is False
    assert recorder.begin(model_id="example", endpoint="chat") is not None
    invocation.end()
    assert recorder.flush()
    assert recorder.reader.detail(handle.request_id)["state"] == "capture_stopped"


def test_settings_save_preserves_disk_values_and_legacy_credential_ids(controls):
    settings, client, service = controls
    path = settings.base_path / "settings.json"
    disk = json.loads(path.read_text())
    disk["server"]["port"] = 12345
    disk["extension_example"] = {"preserve": True}
    disk["auth"]["sub_keys"][0].pop("id", None)
    path.write_text(json.dumps(disk))
    settings.server.port = 54321  # A runtime override must not leak into save.
    response = change(client, {"enabled": True, "max_requests": 7})
    assert response.status_code == 200
    saved = json.loads(path.read_text())
    assert saved["server"]["port"] == 12345
    assert saved["extension_example"] == {"preserve": True}
    assert saved["auth"]["sub_keys"][0]["id"] == settings.auth.sub_keys[0].id
    assert saved["activity"]["max_requests"] == 7
    assert saved["activity"]["max_storage_bytes"] == 5 * 1024**3


def test_invalid_or_stale_settings_never_change_saved_or_runtime_state(controls):
    settings, client, service = controls
    path = settings.base_path / "settings.json"
    before = path.read_bytes()
    for payload in (
        {"enabled": 1},
        {"max_requests": True},
        {"max_age_days": 0},
        {"max_storage_bytes": 1024},
        {"enabled": None},
        {"unexpected": True},
    ):
        assert change(client, payload).status_code in {400, 422}
    assert path.read_bytes() == before
    assert get_recorder() is None
    version = client.get(
        "/admin/api/activity/status",
        headers={"Authorization": "Bearer synthetic-admin"},
    ).json()["config_version"]
    assert (
        change(client, {"max_requests": 5}, **{"If-Match": version}).status_code == 200
    )
    assert (
        change(client, {"max_requests": 6}, **{"If-Match": version}).status_code == 409
    )
    assert settings.activity.max_requests == 5


def test_save_failure_does_not_enable_new_capture_or_rewrite_config(controls):
    settings, client, service = controls
    before = (settings.base_path / "settings.json").read_bytes()
    with patch.object(
        settings, "save_activity_settings", side_effect=OSError("synthetic failure")
    ):
        result = change(client, {"enabled": True})
    assert result.status_code == 503
    assert get_recorder() is None
    assert settings.activity.enabled is False
    assert (settings.base_path / "settings.json").read_bytes() == before


def test_retention_reduction_prunes_terminal_records_and_keeps_active_capture(controls):
    settings, client, service = controls
    assert change(client, {"enabled": True}).status_code == 200
    recorder = get_recorder()
    for _ in range(4):
        handle = recorder.begin(model_id="example", endpoint="chat")
        recorder.finish(handle)
    active = recorder.begin(model_id="example", endpoint="chat")
    assert recorder.flush()
    assert change(client, {"max_requests": 1, "max_age_days": 1}).status_code == 200
    rows = recorder.reader.list_requests()
    assert len(rows) == 2
    assert any(
        row["id"] == active.request_id and row["finished_at"] is None for row in rows
    )


def test_clear_and_delete_work_while_disabled_and_fence_late_output(controls):
    settings, client, service = controls
    assert change(client, {"enabled": True}).status_code == 200
    recorder = get_recorder()
    handle = recorder.begin(model_id="example", endpoint="chat")
    recorder.append_text(handle, "submitted", "retained")
    assert recorder.flush()
    assert change(client, {"enabled": False}).status_code == 200
    path = f"/admin/api/activity/requests/{handle.request_id}"
    headers = {"Authorization": "Bearer synthetic-admin", "X-Activity-Action": "delete"}
    assert (
        client.request(
            "DELETE", path, json={"confirm": "delete"}, headers=headers
        ).status_code
        == 200
    )
    assert recorder.reader.detail(handle.request_id) is None
    assert recorder.append_text(handle, "raw_output", "late") is False
    assert change(client, {"enabled": True}).status_code == 200
    active = recorder.begin(model_id="example", endpoint="chat")
    assert recorder.flush()
    result = client.post(
        "/admin/api/activity/clear",
        json={"confirm": "clear"},
        headers={**headers, "X-Activity-Action": "clear"},
    )
    assert result.status_code == 200
    assert recorder.reader.list_requests() == []
    assert recorder.append_text(active, "raw_output", "late") is False
    assert recorder.begin(model_id="example", endpoint="chat") is not None


def test_empty_disabled_controls_do_not_create_a_history_store(controls):
    settings, client, service = controls
    assert change(client, {"max_requests": 99}).status_code == 200
    result = client.post(
        "/admin/api/activity/clear",
        json={"confirm": "clear"},
        headers={
            "Authorization": "Bearer synthetic-admin",
            "X-Activity-Action": "clear",
        },
    )
    assert result.status_code == 200
    assert not (settings.base_path / "activity.sqlite3").exists()
    assert get_recorder() is None


def test_controls_require_admin_identity_and_explicit_action_confirmation(controls):
    settings, client, service = controls
    for headers in ({}, {"Authorization": "Bearer synthetic-client"}):
        assert (
            client.put(
                "/admin/api/activity/settings", json={"enabled": True}, headers=headers
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/admin/api/activity/clear", json={"confirm": "clear"}, headers=headers
            ).status_code
            == 401
        )
        assert (
            client.request(
                "DELETE",
                "/admin/api/activity/requests/id",
                json={"confirm": "delete"},
                headers=headers,
            ).status_code
            == 401
        )
    headers = {"Authorization": "Bearer synthetic-admin"}
    assert (
        client.put(
            "/admin/api/activity/settings", json={"enabled": True}, headers=headers
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/admin/api/activity/clear", json={"confirm": "clear"}, headers=headers
        ).status_code
        == 400
    )


def test_cancelled_control_retains_shared_save_lock_until_worker_completes(controls):
    settings, client, service = controls
    started, release = threading.Event(), threading.Event()

    def blocked():
        started.set()
        assert release.wait(3)

    async def check():
        task = asyncio.create_task(service.control(blocked))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert service._settings_lock.locked()
            assert service._controls == 1
        finally:
            release.set()
        await asyncio.gather(*service._control_tasks)
        assert not service._settings_lock.locked()
        assert service._controls == 0

    asyncio.run(check())


def test_disabled_maintenance_prunes_existing_history_without_new_admission(tmp_path):
    path = tmp_path / "activity.sqlite3"
    recorder = ActivityRecorder(path, ActivitySettings(enabled=True))
    assert recorder.wait_ready()
    for _ in range(3):
        handle = recorder.begin(model_id="example", endpoint="chat")
        recorder.finish(handle)
    assert recorder.flush()
    assert recorder.close()
    recorder = ActivityRecorder(
        path, ActivitySettings(enabled=False, max_requests=1), maintenance=True
    )
    try:
        assert recorder.wait_ready()
        assert recorder.flush()
        assert len(recorder.reader.list_requests()) == 1
        assert recorder.begin(model_id="example", endpoint="chat") is None
        assert recorder.clear().result(2) is None
    finally:
        assert recorder.close()


def test_lowered_physical_budget_stops_capture_and_reclaims_terminal_history(tmp_path):
    path = tmp_path / "activity.sqlite3"
    recorder = ActivityRecorder(
        path,
        ActivitySettings(
            enabled=True,
            max_storage_bytes=8 * 1024**2,
            max_request_bytes=512 * 1024,
            max_attachment_bytes=128 * 1024,
        ),
    )
    try:
        assert recorder.wait_ready()
        handles = []
        random_bytes = random.Random(42)
        for _ in range(8):
            handle = recorder.begin(model_id="example", endpoint="chat")
            handles.append(handle)
            assert recorder.append(
                handle, "raw_output", random_bytes.randbytes(200 * 1024)
            )
        assert recorder.flush()
        assert len(recorder.reader.list_requests()) == 8
        small = ActivitySettings(
            enabled=True,
            max_storage_bytes=1024**2,
            max_request_bytes=512 * 1024,
            max_attachment_bytes=128 * 1024,
        )
        recorder.update_settings(small).result(5)
        rows = recorder.reader.list_requests()
        assert all(row["state"] == "capture_stopped" for row in rows)
        assert all(row["capture_reason"] == "storage_budget_reduced" for row in rows)
        assert all(
            recorder.append_text(handle, "raw_output", "late") is False
            for handle in handles
        )
        physical = sum(
            file.stat().st_size for file in tmp_path.glob("activity.sqlite3*")
        )
        assert physical <= small.max_storage_bytes
    finally:
        assert recorder.close()


def test_slow_failed_start_is_retained_until_shutdown_before_another_writer(controls):
    settings, client, service = controls

    class SlowStart:
        def wait_ready(self):
            return False

        def close(self):
            return False

        def health(self):
            return {"stopping": True, "error": None}

    slow = SlowStart()
    try:
        with patch(
            "omlx.activity.routes.ActivityRecorder", return_value=slow
        ) as create:
            for _ in range(2):
                result = change(client, {"enabled": True})
                assert result.status_code == 503
            assert create.call_count == 1
            assert get_recorder() is slow
            assert settings.activity.enabled is False
    finally:
        configure_recorder(None)  # Synthetic object has no actual worker.


def test_failed_save_does_not_prune_history_under_proposed_retention(controls):
    settings, client, service = controls
    assert change(client, {"enabled": True}).status_code == 200
    recorder = get_recorder()
    for _ in range(3):
        handle = recorder.begin(model_id="example", endpoint="chat")
        recorder.finish(handle)
    assert recorder.flush()
    assert recorder.close()
    configure_recorder(None)
    with patch.object(
        settings, "save_activity_settings", side_effect=OSError("synthetic failure")
    ):
        response = change(client, {"enabled": False, "max_requests": 1})
    assert response.status_code == 503
    assert settings.activity.max_requests == 1000
    assert len(recorder.reader.list_requests()) == 3
