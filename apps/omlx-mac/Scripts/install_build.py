#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Safely install a canonical private oMLX build and verify its server.

The source artifact is never moved.  It is copied beside the live app,
validated again, atomically exchanged with the live bundle, and launched.
If identity or health verification fails, the exchange is reversed and the
previous application is relaunched.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import errno
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]
ARTIFACTS_DIR = SCRIPT_DIR.parent / "build" / "Artifacts"
DEFAULT_LIVE_APP = Path("/Applications/oMLX.app")
DEFAULT_BACKUP_DIR = (
    Path.home() / "Library" / "Application Support" / "oMLX" / "app-backups"
)
DEFAULT_CONTROL_SOCKET = (
    Path.home() / "Library" / "Application Support" / "oMLX" / "control.sock"
)
RENAME_SWAP = 0x00000002

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from activate_build import (  # noqa: E402
    running_app_processes,
    stop_old_instance,
    wait_for_replacement,
)


@dataclass(frozen=True)
class BundleIdentity:
    version: str
    build_number: int
    channel: str | None
    revision: str | None
    branch: str | None
    features: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Atomically install a canonical private oMLX artifact, restart it, "
            "verify server identity and health, and roll back on failure."
        )
    )
    parser.add_argument(
        "--app",
        type=Path,
        help="Artifact to install (default: highest canonical build in build/Artifacts)",
    )
    parser.add_argument(
        "--live-app",
        type=Path,
        default=DEFAULT_LIVE_APP,
        help="Installed app path (default: %(default)s)",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=DEFAULT_BACKUP_DIR,
        help="Directory that retains successful-install rollback bundles",
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
        help="Seconds before force-closing a stopped app (default: %(default)s)",
    )
    parser.add_argument(
        "--allow-downgrade",
        action="store_true",
        help="Permit a build number not newer than the installed build",
    )
    parser.add_argument(
        "--allow-noncanonical",
        action="store_true",
        help="Permit an artifact not built from the current canonical private source",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the installation plan without changing anything",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required confirmation for an actual installation",
    )
    return parser.parse_args()


def _read_plist(app: Path) -> dict[str, Any]:
    plist_path = app / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as handle:
            value = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise ValueError(f"cannot read app Info.plist: {plist_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"invalid app Info.plist: {plist_path}")
    return value


def bundle_identity(app: Path) -> BundleIdentity:
    plist = _read_plist(app)
    if plist.get("CFBundleIdentifier") != "app.omlx":
        raise ValueError(f"not an app.omlx bundle: {app}")

    version = plist.get("CFBundleShortVersionString")
    raw_build = plist.get("CFBundleVersion")
    if not isinstance(version, str) or not version:
        raise ValueError(f"bundle has no semantic version: {app}")
    try:
        build_number = int(raw_build)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"bundle has an invalid build number {raw_build!r}: {app}") from exc

    features_value = plist.get("OMLXBuildFeatures", [])
    if not isinstance(features_value, list) or not all(
        isinstance(item, str) for item in features_value
    ):
        raise ValueError(f"bundle has invalid OMLXBuildFeatures: {app}")

    identity = BundleIdentity(
        version=version,
        build_number=build_number,
        channel=plist.get("OMLXBuildChannel"),
        revision=plist.get("OMLXSourceRevision"),
        branch=plist.get("OMLXSourceBranch"),
        features=tuple(features_value),
    )

    version_file = app / "Contents" / "Resources" / "omlx" / "_version.py"
    if version_file.is_file():
        match = re.search(
            r'^__version__\s*=\s*["\']([^"\']+)["\']',
            version_file.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match is None or match.group(1) != identity.version:
            raise ValueError(
                f"bundle/Python version mismatch in {app}: "
                f"{identity.version!r} vs {match.group(1) if match else 'missing'!r}"
            )
    return identity


def verify_signature(app: Path) -> None:
    result = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "verification failed"
        raise ValueError(f"invalid app signature for {app}: {message}")


def _manifest() -> dict[str, Any]:
    path = REPO_ROOT / "omlx" / "_build_manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"invalid private build manifest: {path}")
    return value


def _git(*arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), *arguments],
        text=True,
        stderr=subprocess.DEVNULL,
    ).strip()


def validate_canonical_identity(identity: BundleIdentity) -> None:
    manifest = _manifest()
    release_branch = str(manifest["release_branch"])
    expected_revision = _git("rev-parse", release_branch)
    expected_build = int(_git("rev-list", "--count", expected_revision))
    expected_features = set(manifest["features"])
    actual_features = set(identity.features)
    optional_features = {"custom-kernels"}

    if identity.channel != manifest["channel"]:
        raise ValueError(
            f"artifact channel is {identity.channel!r}; expected {manifest['channel']!r}"
        )
    if identity.branch != release_branch:
        raise ValueError(
            f"artifact branch is {identity.branch!r}; expected {release_branch!r}"
        )
    if not identity.revision or not expected_revision.startswith(identity.revision):
        raise ValueError(
            f"artifact revision is {identity.revision!r}; current {release_branch} "
            f"is {expected_revision[:12]}"
        )
    if identity.build_number != expected_build:
        raise ValueError(
            f"artifact build is {identity.build_number}; current source build is "
            f"{expected_build}"
        )
    if not expected_features.issubset(actual_features) or not actual_features.issubset(
        expected_features | optional_features
    ):
        raise ValueError(
            "artifact feature IDs do not match omlx/_build_manifest.json: "
            f"expected={sorted(expected_features)}, actual={sorted(actual_features)}"
        )


def validate_bundle(app: Path, *, canonical: bool) -> BundleIdentity:
    app = app.expanduser().resolve()
    executable = app / "Contents" / "MacOS" / "oMLX"
    if app.suffix != ".app" or not executable.is_file():
        raise ValueError(f"not a runnable oMLX app bundle: {app}")
    identity = bundle_identity(app)
    verify_signature(app)
    if canonical:
        validate_canonical_identity(identity)
    return identity


def find_latest_artifact(*, canonical: bool) -> Path:
    candidates: list[tuple[int, Path]] = []
    for app in ARTIFACTS_DIR.glob("*/oMLX.app"):
        try:
            identity = validate_bundle(app, canonical=canonical)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        candidates.append((identity.build_number, app.resolve()))
    if not candidates:
        raise ValueError(f"no valid oMLX artifacts found under {ARTIFACTS_DIR}")
    return max(candidates, key=lambda item: item[0])[1]


def reject_downgrade(
    incoming: BundleIdentity,
    installed: BundleIdentity | None,
    *,
    allowed: bool,
) -> None:
    if (
        installed is not None
        and incoming.build_number <= installed.build_number
        and not allowed
    ):
        raise ValueError(
            f"refusing build {incoming.build_number} over installed build "
            f"{installed.build_number}; pass --allow-downgrade for an intentional "
            "downgrade or reinstall"
        )


def atomic_swap(first: Path, second: Path) -> None:
    """Atomically exchange two same-volume paths using macOS renamex_np."""
    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = libc.renamex_np
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    result = renamex_np(os.fsencode(first), os.fsencode(second), RENAME_SWAP)
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), f"{first} <-> {second}")


def _copy_bundle(source: Path, destination: Path) -> None:
    result = subprocess.run(
        ["/usr/bin/ditto", str(source), str(destination)],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "ditto failed"
        raise RuntimeError(f"could not stage app in /Applications: {message}")


def _health_json(status: dict[str, Any]) -> dict[str, Any]:
    host = status.get("host")
    port = status.get("port")
    if not isinstance(host, str) or not isinstance(port, int):
        raise RuntimeError(f"server status has no usable address: {status}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://{host}:{port}/health", timeout=3) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise RuntimeError("health endpoint did not return an object")
    return value


def verify_running_identity(
    status: dict[str, Any], expected: BundleIdentity
) -> dict[str, Any]:
    health = _health_json(status)
    build = health.get("build")
    if not isinstance(build, dict):
        raise RuntimeError("replacement health response has no build identity")
    revision = build.get("source_revision")
    if revision != expected.revision:
        raise RuntimeError(
            f"replacement server revision is {revision!r}; expected {expected.revision!r}"
        )
    if build.get("version") != expected.version:
        raise RuntimeError(
            f"replacement server version is {build.get('version')!r}; "
            f"expected {expected.version!r}"
        )
    return health


def _backup_destination(root: Path, identity: BundleIdentity) -> Path:
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    revision = identity.revision or "unidentified"
    leaf = f"{timestamp}-{identity.version}-build{identity.build_number}-{revision}"
    return root.expanduser() / leaf / "oMLX.app"


def _identity_dict(identity: BundleIdentity | None) -> dict[str, Any] | None:
    return asdict(identity) if identity is not None else None


def install(args: argparse.Namespace) -> dict[str, Any]:
    if args.server_timeout <= 0 or args.app_exit_timeout <= 0:
        raise ValueError("timeouts must be positive")

    canonical = not args.allow_noncanonical
    artifact = (
        args.app.expanduser().resolve()
        if args.app is not None
        else find_latest_artifact(canonical=canonical)
    )
    live_app = args.live_app.expanduser().absolute()
    if artifact == live_app.resolve():
        raise ValueError("artifact and live app paths must differ")

    incoming = validate_bundle(artifact, canonical=canonical)
    installed = bundle_identity(live_app) if live_app.exists() else None
    reject_downgrade(incoming, installed, allowed=args.allow_downgrade)

    plan = {
        "artifact": str(artifact),
        "live_app": str(live_app),
        "incoming": _identity_dict(incoming),
        "installed": _identity_dict(installed),
        "backup_root": str(args.backup_dir.expanduser()),
    }
    if args.dry_run:
        return {"ok": True, "dry_run": True, **plan}
    if not args.yes:
        raise ValueError(
            "installation plan validated; rerun with --yes to stop, install, and restart"
        )

    stage = live_app.parent / f".oMLX-install-{uuid.uuid4().hex}.app"
    had_live_app = live_app.exists()
    swapped = False
    installed_new_app = False
    old_server_pid: int | None = None
    stopped_gracefully = False
    backup_app: Path | None = None

    try:
        _copy_bundle(artifact, stage)
        staged_identity = validate_bundle(stage, canonical=canonical)
        if staged_identity != incoming:
            raise RuntimeError("staged bundle identity changed during copy")

        old_processes = running_app_processes()
        old_server_pid, stopped_gracefully = stop_old_instance(
            args.control_socket.expanduser(),
            old_processes,
            args.app_exit_timeout,
        )

        if had_live_app:
            atomic_swap(live_app, stage)
            swapped = True
        else:
            os.replace(stage, live_app)
        installed_new_app = True

        subprocess.run(["/usr/bin/open", "-n", str(live_app)], check=True)
        replacement = wait_for_replacement(
            args.control_socket.expanduser(),
            old_server_pid,
            args.server_timeout,
        )
        health = verify_running_identity(replacement, incoming)

        if swapped:
            assert installed is not None
            backup_app = _backup_destination(args.backup_dir, installed)
            backup_app.parent.mkdir(parents=True, exist_ok=False)
            shutil.move(str(stage), str(backup_app))

        return {
            "ok": True,
            **plan,
            "graceful_server_stop": stopped_gracefully,
            "old_server_pid": old_server_pid,
            "new_server_pid": replacement.get("pid"),
            "health_status": health.get("status"),
            "backup_app": str(backup_app) if backup_app else None,
        }
    except Exception as install_error:
        rollback_error: Exception | None = None
        if installed_new_app:
            try:
                stop_old_instance(
                    args.control_socket.expanduser(),
                    running_app_processes(),
                    args.app_exit_timeout,
                )
                if swapped:
                    atomic_swap(live_app, stage)
                    if stage.exists():
                        shutil.rmtree(stage)
                    subprocess.run(["/usr/bin/open", "-n", str(live_app)], check=True)
                    wait_for_replacement(
                        args.control_socket.expanduser(),
                        old_server_pid,
                        args.server_timeout,
                    )
                elif live_app.exists():
                    shutil.rmtree(live_app)
            except Exception as exc:
                rollback_error = exc
        elif stage.exists():
            shutil.rmtree(stage)

        if rollback_error is not None:
            raise RuntimeError(
                f"installation failed ({install_error}); rollback also failed "
                f"({rollback_error})"
            ) from install_error
        raise RuntimeError(f"installation failed and was rolled back: {install_error}") from install_error


def main() -> int:
    args = parse_args()
    try:
        result = install(args)
    except Exception as exc:
        if isinstance(exc, OSError) and exc.errno in {errno.EACCES, errno.EPERM}:
            print(
                f"installation failed: no permission to update {args.live_app}: {exc}",
                file=sys.stderr,
            )
        else:
            print(f"installation failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
