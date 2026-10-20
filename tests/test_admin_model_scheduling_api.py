# SPDX-License-Identifier: Apache-2.0
"""Route-level tests for model inference scheduling settings."""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import omlx.server  # noqa: F401 - ensure server module is imported first
from omlx.admin import routes as admin_routes
from omlx.engine_pool import EngineEntry, EnginePool
from omlx.model_settings import ModelSettings, ModelSettingsManager


@pytest.fixture
def scheduling_client(tmp_path, monkeypatch):
    manager = ModelSettingsManager(tmp_path)
    manager.set_settings(
        "ling",
        ModelSettings(
            inference_share=0.65,
            inference_priority=4,
            inference_exclusive=False,
        ),
    )
    manager.migrate_inference_allowances(1.0)
    pool = EnginePool()
    # The entry exists but has no engine, representing a discovered, unloaded model.
    pool._entries["ling"] = EngineEntry(
        model_id="ling",
        model_path="/tmp/ling",
        model_type="llm",
        engine_type="batched",
        estimated_size=1,
    )
    pool.configure_model_scheduling = MagicMock()
    pool.get_inference_scheduling_status = MagicMock(
        return_value={"enabled": True, "quantum_seconds": 5.0, "models": []}
    )
    monkeypatch.setattr(admin_routes, "_get_settings_manager", lambda: manager)
    monkeypatch.setattr(admin_routes, "_get_engine_pool", lambda: pool)

    async def allow_admin():
        return True

    app = FastAPI()
    app.include_router(admin_routes.router)
    app.dependency_overrides[admin_routes.require_admin] = allow_admin
    return TestClient(app), manager, pool


def test_patch_route_saves_unloaded_model_policy_and_supports_partial_inherit(
    scheduling_client,
):
    client, manager, pool = scheduling_client

    response = client.patch(
        "/admin/api/models/ling/scheduling",
        json={"inference_priority": 9},
    )
    assert response.status_code == 200, response.text
    assert response.json()["settings"] == {
        "inference_share": 0.65,
        "inference_allowance": 0.65,
        "inference_priority": 9,
        "inference_exclusive": False,
    }
    assert pool.configure_model_scheduling.call_args_list[-1].args == ("ling",)
    assert pool.configure_model_scheduling.call_args_list[-1].kwargs == {
        "share": 0.65,
        "allowance": 0.65,
        "priority": 9,
        "exclusive": False,
    }
    saved = ModelSettingsManager(manager.base_path).get_settings("ling")
    assert (
        saved.inference_share,
        saved.inference_priority,
        saved.inference_exclusive,
    ) == (
        0.65,
        9,
        False,
    )

    response = client.patch(
        "/admin/api/models/ling/scheduling", json={"inference_share": None}
    )
    assert response.status_code == 200, response.text
    saved = ModelSettingsManager(manager.base_path).get_settings("ling")
    assert saved.inference_share == 0.65
    assert saved.inference_allowance == 1.0
    assert saved.inference_priority == 9
    assert response.json()["settings"]["inference_share"] == 1.0
    assert response.json()["settings"]["inference_allowance"] == 1.0


@pytest.mark.parametrize(
    "body",
    [
        {"inference_priority": None},
        {"inference_exclusive": None},
        {"inference_share": True},
        {"inference_share": 1.01},
        {"inference_allowance": -0.01},
        {"inference_allowance": 1.01},
        {"inference_share": 0.6, "inference_allowance": 0.5},
        {"inference_priority": True},
        {"inference_priority": 101},
        {"inference_exclusive": 1},
        {"unknown": 1},
    ],
)
def test_patch_route_rejects_invalid_policy_payloads(scheduling_client, body):
    client, _manager, pool = scheduling_client
    response = client.patch("/admin/api/models/ling/scheduling", json=body)
    assert response.status_code == 422, response.text
    pool.configure_model_scheduling.assert_not_called()


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity"])
def test_patch_route_rejects_nonfinite_share(scheduling_client, number):
    client, _manager, pool = scheduling_client
    response = client.patch(
        "/admin/api/models/ling/scheduling",
        content=f'{{"inference_share": {number}}}',
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422, response.text
    pool.configure_model_scheduling.assert_not_called()


def test_legacy_absolute_api_accepts_zero_projected_share(scheduling_client):
    client, manager, _pool = scheduling_client
    response = client.patch(
        "/admin/api/models/ling/scheduling", json={"inference_share": 0.0}
    )
    assert response.status_code == 200, response.text
    saved = manager.get_settings("ling")
    assert saved.inference_allowance == 0.0
    assert response.json()["settings"]["inference_share"] == 0.0


def test_legacy_projection_honors_class_ceiling_before_migration(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        admin_routes,
        "_get_global_settings",
        lambda: SimpleNamespace(
            server=SimpleNamespace(
                inference_share=0.8,
                inference_background_allowance=0.25,
                inference_interactive_allowance=0.5,
            )
        ),
    )
    settings = ModelSettings(inference_share=0.12, inference_exclusive=False)
    assert admin_routes._configured_model_share(settings) == 0.12
    assert admin_routes._legacy_share_to_allowance(0.12, False) == 0.6


def test_patch_persistence_failure_does_not_mutate_returned_cached_settings(
    scheduling_client, monkeypatch
):
    client, manager, pool = scheduling_client
    original = manager.get_settings("ling")
    monkeypatch.setattr(manager, "get_settings", lambda _model_id: original)

    def fail_save(_model_id, _settings):
        raise OSError("disk full")

    monkeypatch.setattr(manager, "set_settings", fail_save)
    response = client.patch(
        "/admin/api/models/ling/scheduling", json={"inference_priority": 20}
    )
    assert response.status_code == 500
    assert original.inference_priority == 4
    pool.configure_model_scheduling.assert_not_called()


def test_patch_disk_failure_rolls_back_settings_manager_cache(
    scheduling_client, monkeypatch
):
    client, manager, pool = scheduling_client
    monkeypatch.setattr(
        manager,
        "_save",
        lambda: (_ for _ in ()).throw(OSError("disk full")),
    )
    response = client.patch(
        "/admin/api/models/ling/scheduling", json={"inference_priority": 20}
    )
    assert response.status_code == 500
    saved = manager.get_settings("ling")
    assert saved.inference_priority == 4
    assert saved.inference_share == 0.65
    pool.configure_model_scheduling.assert_not_called()
