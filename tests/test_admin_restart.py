# SPDX-License-Identifier: Apache-2.0
"""Tests for the admin server-restart route.

Covers the supervisor-gating contract: the endpoint refuses with 503 when
``OMLX_SUPERVISED`` is not set in the environment (plain ``omlx serve``),
uses the macOS app's explicit control channel for a menu-bar-managed server,
and retains delayed SIGTERM for other supervisors.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omlx.admin import routes as admin_routes


@pytest.fixture
def client(monkeypatch):
    """Build a TestClient with auth bypassed for the restart route."""
    async def _fake_require_admin():
        return True

    app = FastAPI()
    app.include_router(admin_routes.router)
    app.dependency_overrides[admin_routes.require_admin] = _fake_require_admin
    return TestClient(app)


class TestRestartServerRoute:
    def test_returns_503_when_unsupervised(self, client, monkeypatch):
        """No OMLX_SUPERVISED env var = no supervisor = no respawn path."""
        monkeypatch.delenv("OMLX_SUPERVISED", raising=False)

        r = client.post("/admin/api/server/restart")
        assert r.status_code == 503
        body = r.json()
        assert "detail" in body
        assert "supervisor" in body["detail"].lower()

    def test_returns_202_when_supervised(self, client, monkeypatch):
        """The macOS app is explicitly told to own the restart transaction."""
        monkeypatch.setenv("OMLX_SUPERVISED", "menubar")

        with (
            patch(
                "omlx.admin.routes._request_menubar_restart",
                new_callable=AsyncMock,
                return_value={"ok": True, "status": "restarting"},
            ) as request_restart,
            patch("omlx.admin.routes._schedule_self_terminate") as terminate,
        ):
            r = client.post("/admin/api/server/restart")

        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] == "restarting"
        assert body["supervisor"] == "menubar"
        assert body["expected_downtime_seconds"] > 0
        request_restart.assert_awaited_once_with()
        terminate.assert_not_called()

    def test_supervisor_label_round_trips(self, client, monkeypatch):
        """Whatever supervisor identifier is set in env comes back in
        the response — useful for the dashboard and for diagnosing
        which supervisor is responsible for the respawn."""
        monkeypatch.setenv("OMLX_SUPERVISED", "launchd")

        with patch("omlx.admin.routes._schedule_self_terminate") as terminate:
            r = client.post("/admin/api/server/restart")

        assert r.status_code == 202
        assert r.json()["supervisor"] == "launchd"
        terminate.assert_called_once_with(0.5)

    def test_menubar_control_failure_keeps_server_alive(self, client, monkeypatch):
        """A missing parent must not turn restart into a one-way shutdown."""
        monkeypatch.setenv("OMLX_SUPERVISED", "menubar")

        with (
            patch(
                "omlx.admin.routes._request_menubar_restart",
                new_callable=AsyncMock,
                side_effect=OSError("control socket unavailable"),
            ),
            patch("omlx.admin.routes._schedule_self_terminate") as terminate,
        ):
            r = client.post("/admin/api/server/restart")

        assert r.status_code == 503
        assert "did not accept" in r.json()["detail"]
        terminate.assert_not_called()

    def test_unsupervised_does_not_schedule_termination(self, client, monkeypatch):
        """503 path must not schedule a SIGTERM — otherwise plain
        ``omlx serve`` instances would die with no respawn after a
        single accidental click against an unsupervised server."""
        monkeypatch.delenv("OMLX_SUPERVISED", raising=False)

        with patch("omlx.admin.routes._schedule_self_terminate") as spy:
            r = client.post("/admin/api/server/restart")

        assert r.status_code == 503
        spy.assert_not_called()


@pytest.mark.asyncio
async def test_menubar_restart_control_socket_round_trip(monkeypatch):
    """The Python endpoint and Swift control server share one-line JSON."""
    # AF_UNIX paths are limited to roughly 104 bytes on macOS; pytest's nested
    # tmp_path can exceed that before the filename is added.
    socket_path = Path("/tmp") / f"omlx-restart-{uuid.uuid4().hex}.sock"
    received = []

    async def handle(reader, writer):
        received.append(json.loads((await reader.readline()).decode("utf-8")))
        writer.write(b'{"ok":true,"status":"restarting","state":"stopping"}\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=socket_path)
    monkeypatch.setenv("OMLX_CONTROL_SOCKET", str(socket_path))
    try:
        response = await admin_routes._request_menubar_restart()
    finally:
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)

    assert received == [{"command": "restart"}]
    assert response["status"] == "restarting"
