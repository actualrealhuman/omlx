# SPDX-License-Identifier: Apache-2.0
"""Policy saves apply to runtime only after their preferences are durable."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omlx.admin import routes
from omlx.settings import GlobalSettings


@pytest.fixture
def allowance_settings_client(tmp_path, monkeypatch):
    from omlx import server

    settings = GlobalSettings(base_path=tmp_path)
    settings.server.host = "127.0.0.1"
    settings.server.inference_share = 0.8
    settings.server.inference_interactive_allowance = 0.75
    settings.server.inference_background_allowance = 0.5
    pool = MagicMock()
    battery = MagicMock()
    monkeypatch.setattr(routes, "_get_global_settings", lambda: settings)
    monkeypatch.setattr(
        server,
        "_server_state",
        SimpleNamespace(engine_pool=pool, battery_protection=battery),
    )
    save = AsyncMock()
    monkeypatch.setattr(routes, "_save_global_settings_async", save)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.require_admin] = lambda: True
    return TestClient(app), settings, pool, battery, save


def test_failed_allowance_save_preserves_cached_and_runtime_policy(
    allowance_settings_client,
):
    client, settings, pool, battery, save = allowance_settings_client
    save.side_effect = OSError("disk full")
    response = client.post(
        "/admin/api/global-settings",
        json={"inference_share": 0.4, "inference_background_allowance": 0.0},
    )
    assert response.status_code == 500, response.text
    assert settings.server.inference_share == 0.8
    assert settings.server.inference_background_allowance == 0.5
    pool.configure_inference_share.assert_not_called()
    pool.configure_inference_allowances.assert_not_called()
    battery.configure.assert_not_called()


def test_combined_battery_and_global_save_updates_pool_requested_ceiling(
    allowance_settings_client,
):
    client, settings, pool, battery, save = allowance_settings_client
    response = client.post(
        "/admin/api/global-settings",
        json={
            "inference_share": 0.4,
            "inference_background_allowance": 0.25,
            "battery_protection_enabled": True,
        },
    )
    assert response.status_code == 200, response.text
    save.assert_awaited_once_with(settings)
    battery.configure.assert_called_once()
    pool.configure_inference_share.assert_called_once_with(0.4)
    pool.configure_inference_allowances.assert_called_once_with(
        interactive=0.75, background=0.25
    )
