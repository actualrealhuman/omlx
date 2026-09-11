#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Activate a staged oMLX.app and wait for its server to become healthy."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_APP = SCRIPT_DIR.parent / "build" / "Stage" / "oMLX.app"
DEFAULT_CONTROL_SOCKET = (
    Path.home() / "Library" / "Application Support" / "oMLX" / "control.sock"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Gracefully stop the current oMLX instance, launch a staged build, "
            "and wait for its server to become healthy."
        )
    )
    parser.add_argument(
        "--app",
        type=Path,
        default=DEFAULT_APP,
        help="Staged oMLX.app bundle (default: %(default)s)",
    )
    parser.add_argument(
        "--control-socket",
        type=Path,
        default=DEFAULT_CONTROL_SOCKET,
        help="Menu-bar app control socket",
    )
    parser.add_argument(
        "--server-timeout",
        type=float,
        default=90.0,
        help="Seconds to wait for the replacement server (default: %(default)s)",
    )
    parser.add_argument(
        "--app-exit-timeout",
        type=float,
        default=10.0,
        help="Seconds before force-closing a stopped old app (default: %(default)s)",
    )
    return parser.parse_args()


def recv_json_line(client: socket.socket) -> dict[str, Any]:
    chunks: list[bytes] = []
    while True:
        chunk = client.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    if not chunks:
        raise RuntimeError("menu-bar control socket returned no response")
    return json.loads(b"".join(chunks).split(b"\n", 1)[0])


def control_command(
    socket_path: Path,
    command: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(json.dumps({"command": command}).encode("utf-8") + b"\n")
        return recv_json_line(client)


def try_control_status(socket_path: Path) -> dict[str, Any] | None:
    try:
        response = control_command(socket_path, "status", timeout=2)
    except (OSError, RuntimeError, json.JSONDecodeError):
        return None
    return response if response.get("ok") else None


def running_app_processes() -> list[tuple[int, str]]:
    output = subprocess.check_output(
        ["ps", "-axo", "pid=,command="],
        text=True,
        stderr=subprocess.DEVNULL,
    )
    processes: list[tuple[int, str]] = []
    suffix = "/oMLX.app/Contents/MacOS/oMLX"
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pid_text, separator, command = line.partition(" ")
        command = command.strip()
        if separator and command.endswith(suffix):
            processes.append((int(pid_text), command))
    return processes


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_old_instance(
    socket_path: Path,
    processes: list[tuple[int, str]],
    exit_timeout: float,
) -> tuple[int | None, bool]:
    status = try_control_status(socket_path)
    old_server_pid = status.get("pid") if status else None
    stopped_gracefully = False
    if status is not None and status.get("state") != "stopped":
        response = control_command(socket_path, "stop", timeout=20)
        if not response.get("ok") or response.get("state") != "stopped":
            raise RuntimeError(f"server refused graceful stop: {response}")
        stopped_gracefully = True

    for pid, _ in processes:
        if process_exists(pid):
            os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + exit_timeout
    remaining = [pid for pid, _ in processes]
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if process_exists(pid)]
        if remaining:
            time.sleep(0.1)

    # At this point the child server has already had its bounded graceful stop.
    # A wedged UI parent is safe to reap so it cannot retain the control socket.
    for pid in remaining:
        os.kill(pid, signal.SIGKILL)

    if remaining:
        force_deadline = time.monotonic() + 2
        while any(process_exists(pid) for pid in remaining):
            if time.monotonic() >= force_deadline:
                raise RuntimeError(f"old oMLX app processes did not exit: {remaining}")
            time.sleep(0.1)

    return old_server_pid if isinstance(old_server_pid, int) else None, stopped_gracefully


def is_healthy(host: str, port: int) -> bool:
    url = f"http://{host}:{port}/health"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def wait_for_replacement(
    socket_path: Path,
    old_server_pid: int | None,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_status: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        status = try_control_status(socket_path)
        if status is not None:
            last_status = status
            pid = status.get("pid")
            host = status.get("host")
            port = status.get("port")
            pid_changed = old_server_pid is None or pid != old_server_pid
            if (
                status.get("state") == "running"
                and isinstance(pid, int)
                and pid_changed
                and isinstance(host, str)
                and isinstance(port, int)
                and is_healthy(host, port)
            ):
                return status
        time.sleep(0.25)
    raise RuntimeError(f"replacement server did not become healthy: {last_status}")


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.server_timeout <= 0 or args.app_exit_timeout <= 0:
        raise ValueError("timeouts must be positive")

    app = args.app.expanduser().resolve()
    executable = app / "Contents" / "MacOS" / "oMLX"
    if app.suffix != ".app" or not executable.is_file():
        raise ValueError(f"not a runnable oMLX app bundle: {app}")

    old_processes = running_app_processes()
    old_server_pid, stopped_gracefully = stop_old_instance(
        args.control_socket.expanduser(),
        old_processes,
        args.app_exit_timeout,
    )
    subprocess.run(["open", "-n", str(app)], check=True)
    started = time.monotonic()
    replacement = wait_for_replacement(
        args.control_socket.expanduser(),
        old_server_pid,
        args.server_timeout,
    )
    return {
        "ok": True,
        "app": str(app),
        "old_app_pids": [pid for pid, _ in old_processes],
        "old_server_pid": old_server_pid,
        "graceful_server_stop": stopped_gracefully,
        "new_server_pid": replacement.get("pid"),
        "state": replacement.get("state"),
        "health_url": (
            f"http://{replacement.get('host')}:{replacement.get('port')}/health"
        ),
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def main() -> int:
    args = parse_args()
    try:
        result = run(args)
    except Exception as exc:
        print(f"activation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
