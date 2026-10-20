# SPDX-License-Identifier: Apache-2.0
"""Authenticated local admin usage endpoint, including degraded storage."""

import asyncio
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from omlx.admin.auth import require_admin
from omlx.admin.routes import router
from omlx.server_metrics import ServerMetrics
from omlx.usage_history import UsageHistory


@pytest.fixture
def client(tmp_path):
    metrics = ServerMetrics()
    metrics.usage_history = UsageHistory(tmp_path / "usage.sqlite3")
    metrics.record_request_complete(100, 20, 60, 0.5, 1.0, "canonical-model", 2.0)
    metrics.usage_history.flush()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: True
    with (
        patch("omlx.server_metrics.get_server_metrics", return_value=metrics),
        TestClient(app) as client,
    ):
        yield client, metrics
    metrics.close()


def test_usage_endpoint(client):
    client, _ = client
    response = client.get("/admin/api/usage?range=7d&model=canonical-model")
    assert response.status_code == 200
    data = response.json()
    assert data["totals"]["total_tokens"] == 120
    assert data["models"][0]["model_id"] == "canonical-model"
    assert len(data["heatmap"]) == 7
    assert not any(
        key in data for key in ("api_key", "host", "messages", "prompt", "response")
    )
    assert client.get("/admin/api/usage?range=invalid").status_code == 422
    assert (
        client.get("/admin/api/usage?model=unloaded-model").json()["totals"]["requests"]
        == 0
    )


def test_usage_requires_admin(client):
    client, _ = client
    client.app.dependency_overrides.clear()
    with (
        patch("omlx.admin.auth.verify_session", return_value=False),
        patch("omlx.admin.auth._get_global_settings", return_value=None),
    ):
        assert client.get("/admin/api/usage").status_code == 401


def test_storage_failure_is_generic_503(client):
    client, metrics = client
    with patch.object(
        metrics.usage_history, "query", side_effect=OSError("private path")
    ):
        response = client.get("/admin/api/usage")
    assert response.status_code == 503
    assert response.json() == {"detail": "Usage history unavailable"}
    metrics.record_request_complete(20, 10, model_id="canonical-model")
    assert metrics.get_snapshot()["total_requests"] == 2


@pytest.mark.asyncio
async def test_completed_stream_records_once_and_analytics_failure_preserves_response(
    tmp_path,
):
    import json
    from types import SimpleNamespace

    from omlx import server
    from omlx.api.openai_models import CompletionRequest
    from omlx.engine.base import GenerationOutput

    async def generate(**kwargs):
        yield GenerationOutput(
            text="private output",
            new_text="private output",
            prompt_tokens=100,
            completion_tokens=20,
            cached_tokens=60,
            finished=True,
        )

    engine = SimpleNamespace(tokenizer=None, stream_generate=generate)
    request = CompletionRequest(
        model="display-alias",
        prompt="private input",
        stream=True,
        stream_options={"include_usage": True},
    )
    metrics = ServerMetrics()
    metrics.usage_history = UsageHistory(tmp_path / "usage.sqlite3")
    try:
        with patch.object(server, "get_server_metrics", return_value=metrics):
            chunks = [
                chunk
                async for chunk in server.stream_completion(
                    engine, request.prompt, request, resolved_model="canonical-model"
                )
            ]
        assert chunks[-1] == "data: [DONE]\n\n"
        wire_usage = [
            json.loads(chunk[6:]) for chunk in chunks[:-1] if '"usage"' in chunk
        ][0]["usage"]
        metrics.usage_history.flush()
        result = metrics.usage_history.query()
        assert result["models"][0]["model_id"] == "canonical-model"
        assert result["totals"]["requests"] == 1
        assert result["totals"]["total_tokens"] == wire_usage["total_tokens"] == 120
        assert (
            result["totals"]["cached_tokens"]
            == wire_usage["prompt_tokens_details"]["cached_tokens"]
            == 60
        )
        assert result["totals"]["timed_requests"] == 1
        assert b"private input" not in metrics.usage_history.path.read_bytes()
        assert b"private output" not in metrics.usage_history.path.read_bytes()
        with (
            patch.object(server, "get_server_metrics", return_value=metrics),
            patch.object(
                metrics.usage_history, "record", side_effect=OSError("storage failed")
            ),
        ):
            chunks = [
                chunk
                async for chunk in server.stream_completion(
                    engine, request.prompt, request, resolved_model="canonical-model"
                )
            ]
        assert chunks[-1] == "data: [DONE]\n\n"
        assert metrics.get_snapshot()["total_requests"] == 2
    finally:
        metrics.close()


def test_usage_endpoint_reports_disabled_state(client):
    client, metrics = client
    metrics.usage_history.set_enabled(False)
    response = client.get("/admin/api/usage?range=7d")
    assert response.status_code == 200
    data = response.json()
    assert data["enabled"] is False
    assert data["totals"]["requests"] == 0
    assert data["models"] == []
    assert len(data["heatmap"]) == 7
    metrics.usage_history.set_enabled(True)
    data = client.get("/admin/api/usage?range=7d").json()
    assert data["enabled"] is True
    assert data["totals"]["requests"] == 1


def test_global_settings_toggle_applies_at_runtime(client, tmp_path, monkeypatch):
    from omlx.admin import routes as admin_routes
    from omlx.settings import GlobalSettings

    client, metrics = client
    base_path = tmp_path / "settings"
    gs = GlobalSettings(base_path=base_path)
    monkeypatch.setattr(admin_routes, "_get_global_settings", lambda: gs)

    current = asyncio.run(admin_routes.get_global_settings(is_admin=True))
    assert current["usage"] == {"usage_history": True}

    result = asyncio.run(
        admin_routes.update_global_settings(
            request=admin_routes.GlobalSettingsRequest(usage_history=False),
            is_admin=True,
        )
    )
    assert result["success"] is True
    assert "usage_history" in result["runtime_applied"]
    assert gs.usage.usage_history is False
    assert GlobalSettings.load(base_path=base_path).usage.usage_history is False
    assert metrics.usage_history.enabled is False

    # Serving statistics keep counting; history does not.
    metrics.record_request_complete(100, 20, 60, 0.5, 1.0, "canonical-model", 2.0)
    metrics.usage_history.flush()
    assert metrics.get_snapshot()["total_requests"] == 2
    assert client.get("/admin/api/usage").json()["enabled"] is False

    result = asyncio.run(
        admin_routes.update_global_settings(
            request=admin_routes.GlobalSettingsRequest(usage_history=True),
            is_admin=True,
        )
    )
    assert result["success"] is True
    assert gs.usage.usage_history is True

    metrics.record_request_complete(100, 20, 60, 0.5, 1.0, "canonical-model", 2.0)
    metrics.usage_history.flush()
    data = client.get("/admin/api/usage").json()
    assert data["enabled"] is True
    assert data["totals"]["requests"] == 2

    untouched = asyncio.run(
        admin_routes.update_global_settings(
            request=admin_routes.GlobalSettingsRequest(), is_admin=True
        )
    )
    assert "usage_history" not in untouched["runtime_applied"]
    assert gs.usage.usage_history is True


def test_inference_share_live_update_accepts_exact_fraction_and_persists(
    client, tmp_path, monkeypatch
):
    from omlx.admin import routes as admin_routes
    from omlx.server import _server_state
    from omlx.settings import GlobalSettings

    client, _ = client
    base_path = tmp_path / "settings"
    gs = GlobalSettings(base_path=base_path)
    monkeypatch.setattr(admin_routes, "_get_global_settings", lambda: gs)

    class Pool:
        def __init__(self):
            self.shares = []

        def configure_inference_share(self, share):
            self.shares.append(share)

    pool = Pool()
    monkeypatch.setattr(_server_state, "engine_pool", pool)
    result = asyncio.run(
        admin_routes.update_global_settings(
            request=admin_routes.GlobalSettingsRequest(inference_share=0.8325),
            is_admin=True,
        )
    )

    assert result["success"] is True
    assert "inference_share" in result["runtime_applied"]
    assert gs.server.inference_share == 0.8325
    assert GlobalSettings.load(base_path=base_path).server.inference_share == 0.8325
    assert pool.shares == [0.8325]


@pytest.mark.parametrize(
    "value", [None, True, "0.2", float("nan"), float("inf"), -float("inf")]
)
def test_battery_request_rejects_invalid_numeric_options(value):
    from omlx.admin.routes import GlobalSettingsRequest

    with pytest.raises(ValueError):
        GlobalSettingsRequest(battery_protection={"minimum_share": value})


def test_battery_config_and_maximum_reach_pool_without_service(
    client, tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from omlx.admin import routes as admin_routes
    from omlx.server import _server_state
    from omlx.settings import GlobalSettings

    settings = GlobalSettings(base_path=tmp_path / "settings")
    apply_share = Mock()
    monkeypatch.setattr(admin_routes, "_get_global_settings", lambda: settings)
    monkeypatch.setattr(_server_state, "battery_protection", None)
    monkeypatch.setattr(
        _server_state,
        "engine_pool",
        SimpleNamespace(configure_inference_share=apply_share),
    )
    asyncio.run(
        admin_routes.update_global_settings(
            admin_routes.GlobalSettingsRequest(
                battery_protection={"minimum_share": 0.2}, inference_share=0.65
            ),
            is_admin=True,
        )
    )
    apply_share.assert_called_once_with(0.65)
    saved = GlobalSettings.load(base_path=settings.base_path)
    assert saved.server.inference_share == 0.65
    assert saved.server.battery_protection["minimum_share"] == 0.2


def test_battery_settings_validate_atomically_and_apply_once(
    client, tmp_path, monkeypatch
):
    from omlx.admin import routes as admin_routes
    from omlx.server import _server_state
    from omlx.settings import GlobalSettings

    client, _ = client
    gs = GlobalSettings(base_path=tmp_path / "settings")
    gs.server.battery_protection = {
        **gs.server.battery_protection,
        "minimum_share": 0.2,
    }
    monkeypatch.setattr(admin_routes, "_get_global_settings", lambda: gs)
    monkeypatch.setattr(_server_state, "engine_pool", None)

    class Manager:
        def __init__(self):
            self.configurations = []

        def configure(self, settings, share):
            self.configurations.append((settings, share))

    manager = Manager()
    monkeypatch.setattr(_server_state, "battery_protection", manager)
    with pytest.raises(ValueError):
        admin_routes.GlobalSettingsRequest(battery_protection={"enabled": True})

    with pytest.raises(ValueError):
        admin_routes.GlobalSettingsRequest(battery_protection={"minimum_share": True})

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            admin_routes.update_global_settings(
                request=admin_routes.GlobalSettingsRequest(
                    host="example.com",
                    battery_protection={"suspend_percent": 75},
                ),
                is_admin=True,
            )
        )
    assert raised.value.status_code == 400
    assert gs.server.host == "127.0.0.1"
    assert manager.configurations == []

    no_change = asyncio.run(
        admin_routes.update_global_settings(
            request=admin_routes.GlobalSettingsRequest(battery_protection={}),
            is_admin=True,
        )
    )
    assert "battery_protection_enabled" not in no_change["runtime_applied"]
    assert manager.configurations == []

    result = asyncio.run(
        admin_routes.update_global_settings(
            request=admin_routes.GlobalSettingsRequest(
                battery_protection_enabled=True,
                battery_protection={"throttle_start_percent": 80},
                inference_share=0.8,
            ),
            is_admin=True,
        )
    )
    assert result["success"] is True
    assert "battery_protection" in result["runtime_applied"]
    assert "battery_protection_enabled" in result["runtime_applied"]
    assert gs.server.battery_protection_enabled is True
    assert gs.server.battery_protection["throttle_start_percent"] == 80
    assert gs.server.battery_protection["minimum_share"] == 0.2
    assert len(manager.configurations) == 1
    configured, maximum = manager.configurations[0]
    assert configured.enabled is True
    assert configured.throttle_start_percent == 80
    assert configured.minimum_share == 0.2
    assert maximum == 0.8
    saved = GlobalSettings.load(base_path=gs.base_path)
    assert saved.server.battery_protection_enabled is True
    assert saved.server.battery_protection == gs.server.battery_protection


@pytest.mark.parametrize("model", ["", "canonical-model", "missing"])
def test_usage_details_are_optional_and_preserve_summary(client, model):
    client, _ = client
    params = {"range": "90d", "model": model}
    compact = client.get("/admin/api/usage", params=params)
    detailed = client.get(
        "/admin/api/usage", params={**params, "include_details": "true"}
    )
    assert compact.status_code == detailed.status_code == 200
    compact_data, detailed_data = compact.json(), detailed.json()
    daily = detailed_data.pop("daily")
    hourly = detailed_data.pop("hourly")
    assert compact_data == detailed_data
    assert sum(row["requests"] for row in daily) == compact_data["totals"]["requests"]
    assert sum(row["requests"] for row in hourly) == compact_data["totals"]["requests"]
    assert len(compact.content) < len(detailed.content)
