#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Exercise the dashboard's authenticated restart path end to end.

The check is deliberately local-only: it reads the configured admin key without
printing it, logs in over loopback, requests a restart, observes the server go
down and return, and confirms that the supervisor reports a replacement PID.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_SETTINGS = Path.home() / ".omlx" / "settings.json"
DEFAULT_CONTROL_SOCKET = (
    Path.home() / "Library" / "Application Support" / "oMLX" / "control.sock"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a supervised oMLX web restart on this Mac."
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Local oMLX base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=DEFAULT_SETTINGS,
        help="Settings file containing the admin API key",
    )
    parser.add_argument(
        "--control-socket",
        type=Path,
        default=DEFAULT_CONTROL_SOCKET,
        help="Menu-bar app control socket",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Seconds to wait for a down-then-up transition (default: %(default)s)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.25,
        help="Health polling interval in seconds (default: %(default)s)",
    )
    return parser.parse_args()


def require_loopback_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme != "http" or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("--base-url must be an HTTP loopback URL")
    return normalized


def read_api_key(settings_path: Path) -> str:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    api_key = settings.get("auth", {}).get("api_key")
    if not isinstance(api_key, str) or not api_key:
        raise ValueError(f"no admin API key configured in {settings_path}")
    return api_key


def control_status(socket_path: Path) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(5)
        client.connect(str(socket_path))
        client.sendall(b'{"command":"status"}\n')
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    response = json.loads(b"".join(chunks).split(b"\n", 1)[0])
    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "control status failed")
    return response


def request_json(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 5,
) -> tuple[int, dict[str, Any]]:
    data = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    with opener.open(request, timeout=timeout) as response:
        body = json.loads(response.read())
        return response.status, body


def is_healthy(base_url: str) -> bool:
    request = urllib.request.Request(base_url + "/health", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def run(args: argparse.Namespace) -> dict[str, Any]:
    base_url = require_loopback_url(args.base_url)
    if args.timeout <= 0 or args.poll_interval <= 0:
        raise ValueError("timeouts and polling intervals must be positive")

    api_key = read_api_key(args.settings)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )
    login_status, _ = request_json(
        opener,
        base_url + "/admin/api/login",
        payload={"api_key": api_key, "remember": False},
    )
    before = control_status(args.control_socket)
    restart_status, restart_body = request_json(
        opener,
        base_url + "/admin/api/server/restart",
    )
    if restart_status != 202:
        raise RuntimeError(f"restart endpoint returned HTTP {restart_status}")

    started = time.monotonic()
    saw_down = False
    healthy = False
    while time.monotonic() - started < args.timeout:
        healthy = is_healthy(base_url)
        if not healthy:
            saw_down = True
        elif saw_down:
            break
        time.sleep(args.poll_interval)

    after = control_status(args.control_socket)
    old_pid = before.get("pid")
    new_pid = after.get("pid")
    succeeded = (
        saw_down
        and healthy
        and after.get("state") == "running"
        and isinstance(old_pid, int)
        and isinstance(new_pid, int)
        and new_pid != old_pid
    )
    result = {
        "ok": succeeded,
        "login_status": login_status,
        "restart_status": restart_status,
        "restart_response": restart_body.get("status"),
        "old_pid": old_pid,
        "new_pid": new_pid,
        "final_state": after.get("state"),
        "saw_down_then_up": saw_down and healthy,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }
    if not succeeded:
        raise RuntimeError(f"restart verification failed: {json.dumps(result)}")
    return result


def main() -> int:
    args = parse_args()
    try:
        result = run(args)
    except Exception as exc:
        print(f"restart verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
