# SPDX-License-Identifier: Apache-2.0
"""Persistence and admin API coverage for the master inference control."""

import asyncio

import pytest
from fastapi import HTTPException

from omlx.settings import GlobalSettings, ServerSettings


def test_inference_controls_setting_round_trips_and_defaults_true():
    legacy = ServerSettings.from_dict({"inference_share": 0.4})
    assert legacy.inference_controls_enabled is True
    assert ServerSettings.from_dict(legacy.to_dict()).inference_controls_enabled is True

    disabled = ServerSettings.from_dict({"inference_controls_enabled": False})
    assert disabled.to_dict()["inference_controls_enabled"] is False


@pytest.mark.parametrize("value", [0, 1, "false"])
def test_master_control_request_requires_strict_boolean(value):
    from omlx.admin.routes import GlobalSettingsRequest

    with pytest.raises(ValueError):
        GlobalSettingsRequest.model_validate({"inference_controls_enabled": value})


def test_master_control_is_saved_before_runtime_application(tmp_path, monkeypatch):
    from omlx.admin import routes
    from omlx.server import _server_state

    settings = GlobalSettings(base_path=tmp_path)
    settings.server.inference_share = 0.35
    settings.server.inference_interactive_allowance = 0.7
    settings.server.inference_scheduling_enabled = True
    pool = _RecordingPool(settings)
    previous_pool = _server_state.engine_pool
    previous_battery = _server_state.battery_protection
    battery = _RecordingBattery()
    _server_state.engine_pool = pool
    _server_state.battery_protection = battery
    monkeypatch.setattr(routes, "_get_global_settings", lambda: settings)

    async def save_then_assert(gs):
        assert gs.server.inference_controls_enabled is False
        assert pool.calls == []
        gs.save()

    monkeypatch.setattr(routes, "_save_global_settings_async", save_then_assert)
    try:
        request = routes.GlobalSettingsRequest.model_validate(
            {"inference_controls_enabled": False}
        )
        result = asyncio.run(
            routes.update_global_settings(request=request, is_admin=True)
        )

        assert result["success"] is True
        assert settings.server.inference_share == 0.35
        assert settings.server.inference_interactive_allowance == 0.7
        assert settings.server.inference_scheduling_enabled is True
        assert pool.calls == [("controls", False)]
        assert battery.shares == [1.0]
        persisted = GlobalSettings.load(base_path=tmp_path)
        assert persisted.server.inference_controls_enabled is False
        assert persisted.server.inference_share == 0.35
    finally:
        _server_state.engine_pool = previous_pool
        _server_state.battery_protection = previous_battery


def test_combined_global_and_battery_edit_uses_candidate_active_maximum(
    tmp_path, monkeypatch
):
    from omlx.admin import routes
    from omlx.server import _server_state

    settings = GlobalSettings(base_path=tmp_path)
    settings.server.inference_share = 0.35
    pool = _RecordingPool(settings)
    battery = _RecordingBattery()
    previous_pool = _server_state.engine_pool
    previous_battery = _server_state.battery_protection
    _server_state.engine_pool = pool
    _server_state.battery_protection = battery
    monkeypatch.setattr(routes, "_get_global_settings", lambda: settings)

    async def save(gs):
        gs.save()

    monkeypatch.setattr(routes, "_save_global_settings_async", save)
    request = routes.GlobalSettingsRequest.model_validate(
        {"inference_share": 0.62, "battery_protection_enabled": False}
    )
    try:
        asyncio.run(routes.update_global_settings(request=request, is_admin=True))
        assert battery.configured[-1][1] == 0.62
        assert pool.calls == [("share", 0.62)]
    finally:
        _server_state.engine_pool = previous_pool
        _server_state.battery_protection = previous_battery


def test_master_control_save_failure_restores_setting_without_runtime_change(
    tmp_path, monkeypatch
):
    from omlx.admin import routes
    from omlx.server import _server_state

    settings = GlobalSettings(base_path=tmp_path)
    pool = _RecordingPool(settings)
    previous_pool = _server_state.engine_pool
    previous_battery = _server_state.battery_protection
    _server_state.engine_pool = pool
    _server_state.battery_protection = None
    monkeypatch.setattr(routes, "_get_global_settings", lambda: settings)

    async def fail_save(_):
        raise OSError("injected persistence failure")

    monkeypatch.setattr(routes, "_save_global_settings_async", fail_save)
    request = routes.GlobalSettingsRequest.model_validate(
        {"inference_controls_enabled": False}
    )
    try:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(routes.update_global_settings(request=request, is_admin=True))
        assert exc.value.status_code == 500
        assert settings.server.inference_controls_enabled is True
        assert pool.calls == []
    finally:
        _server_state.engine_pool = previous_pool
        _server_state.battery_protection = previous_battery


def test_global_settings_get_exposes_saved_and_default_control(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from omlx import settings as settings_module
    from omlx.admin import routes

    settings = GlobalSettings(base_path=tmp_path)
    settings.server.inference_controls_enabled = False
    runtime_pool = SimpleNamespace(
        _entries={},
        get_inference_controls_status=lambda: {
            "enabled": True,
            "configured_global_share": 0.35,
            "effective_global_share": 0.35,
        },
    )
    server_state = SimpleNamespace(engine_pool=runtime_pool)
    monkeypatch.setattr(routes, "_get_global_settings", lambda: settings)
    monkeypatch.setattr(routes, "_get_server_state", lambda: server_state)
    monkeypatch.setattr(routes, "_get_engine_pool", lambda: runtime_pool)
    monkeypatch.setattr(routes, "find_splash_binary", lambda _path: (None, None))
    memory_info = {
        "total_bytes": 100,
        "total_formatted": "100 B",
        "auto_limit_formatted": "80 B",
        "available_bytes": 50,
        "omlx_phys_footprint_bytes": 0,
        "free_memory_bytes": 0,
        "inactive_memory_bytes": 0,
        "active_memory_bytes": 0,
        "iogpu_wired_limit_bytes": 0,
        "omlx_wired_limit_request_bytes": 0,
        "memory_guard_preview": {},
    }
    monkeypatch.setattr(routes, "get_system_memory_info", lambda: memory_info)
    monkeypatch.setattr(
        routes,
        "get_ssd_disk_info",
        lambda _path: {"total_bytes": 100, "total_formatted": "100 B"},
    )
    monkeypatch.setattr(settings_module, "get_auto_ssd_cache_size", lambda _path: 0)

    current = asyncio.run(routes.get_global_settings(is_admin=True))
    defaults = asyncio.run(routes.get_global_settings_defaults(is_admin=True))
    assert current["server"]["inference_controls_enabled"] is False
    assert current["server"]["inference_controls_runtime"]["enabled"] is True
    assert defaults["server"]["inference_controls_enabled"] is True


class _RecordingPool:
    def __init__(self, settings):
        self.calls = []
        self.settings = settings

    def configure_inference_controls(self, enabled):
        self.calls.append(("controls", enabled))

    def configure_inference_share(self, share):
        self.calls.append(("share", share))


class _RecordingBattery:
    def __init__(self):
        self.shares = []
        self.configured = []

    def set_requested_share(self, share):
        self.shares.append(share)

    def configure(self, settings, requested_share):
        self.configured.append((settings, requested_share))
