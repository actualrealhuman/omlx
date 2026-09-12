import argparse
import importlib.util
import plistlib
import sys
from pathlib import Path

import pytest

SCRIPT = Path("apps/omlx-mac/Scripts/install_build.py").resolve()
SPEC = importlib.util.spec_from_file_location("install_build", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
install_build = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = install_build
SPEC.loader.exec_module(install_build)


def _identity(build: int) -> install_build.BundleIdentity:
    return install_build.BundleIdentity(
        version="0.7.0.dev2",
        build_number=build,
        channel="private",
        revision="abcdef123456",
        branch="personal/main",
        features=("benchmark-upload-controls",),
    )


def _fake_app(path: Path, *, build: int = 10) -> Path:
    executable = path / "Contents/MacOS/oMLX"
    executable.parent.mkdir(parents=True)
    executable.write_text("binary", encoding="utf-8")
    resources = path / "Contents/Resources/omlx"
    resources.mkdir(parents=True)
    (resources / "_version.py").write_text(
        '__version__ = "0.7.0.dev2"\n', encoding="utf-8"
    )
    with (path / "Contents/Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "app.omlx",
                "CFBundleShortVersionString": "0.7.0.dev2",
                "CFBundleVersion": str(build),
                "OMLXBuildChannel": "private",
                "OMLXSourceRevision": "abcdef123456",
                "OMLXSourceBranch": "personal/main",
                "OMLXBuildFeatures": ["benchmark-upload-controls"],
            },
            handle,
        )
    return path


def test_bundle_identity_requires_matching_python_version(tmp_path):
    app = _fake_app(tmp_path / "oMLX.app")
    identity = install_build.bundle_identity(app)
    assert identity == _identity(10)

    (app / "Contents/Resources/omlx/_version.py").write_text(
        '__version__ = "0.6.4"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="bundle/Python version mismatch"):
        install_build.bundle_identity(app)


def test_downgrade_and_reinstall_require_explicit_override():
    with pytest.raises(ValueError, match="refusing build 10"):
        install_build.reject_downgrade(_identity(10), _identity(10), allowed=False)
    with pytest.raises(ValueError, match="refusing build 9"):
        install_build.reject_downgrade(_identity(9), _identity(10), allowed=False)

    install_build.reject_downgrade(_identity(9), _identity(10), allowed=True)
    install_build.reject_downgrade(_identity(11), _identity(10), allowed=False)


@pytest.mark.skipif(sys.platform != "darwin", reason="renamex_np is macOS-only")
def test_atomic_swap_exchanges_complete_bundles(tmp_path):
    first = tmp_path / "first.app"
    second = tmp_path / "second.app"
    first.mkdir()
    second.mkdir()
    (first / "marker").write_text("old", encoding="utf-8")
    (second / "marker").write_text("new", encoding="utf-8")

    install_build.atomic_swap(first, second)

    assert (first / "marker").read_text(encoding="utf-8") == "new"
    assert (second / "marker").read_text(encoding="utf-8") == "old"


def test_health_failure_swaps_the_previous_app_back(tmp_path, monkeypatch):
    artifact = _fake_app(tmp_path / "artifact/oMLX.app", build=11)
    live_app = _fake_app(tmp_path / "Applications/oMLX.app", build=10)
    incoming = _identity(11)
    installed = _identity(10)
    swaps: list[tuple[Path, Path]] = []
    opens: list[Path] = []

    monkeypatch.setattr(install_build, "validate_bundle", lambda *_a, **_k: incoming)
    monkeypatch.setattr(install_build, "bundle_identity", lambda _app: installed)
    monkeypatch.setattr(install_build, "running_app_processes", lambda: [])
    monkeypatch.setattr(
        install_build,
        "stop_old_instance",
        lambda *_a, **_k: (123, True),
    )
    monkeypatch.setattr(
        install_build,
        "_copy_bundle",
        lambda _source, destination: destination.mkdir(),
    )
    monkeypatch.setattr(
        install_build,
        "atomic_swap",
        lambda first, second: swaps.append((first, second)),
    )
    monkeypatch.setattr(
        install_build,
        "wait_for_replacement",
        lambda *_a, **_k: {"pid": 456, "host": "127.0.0.1", "port": 8000},
    )
    monkeypatch.setattr(
        install_build,
        "verify_running_identity",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("wrong revision")),
    )

    def fake_run(command, **_kwargs):
        if command[:2] == ["/usr/bin/open", "-n"]:
            opens.append(Path(command[2]))
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(install_build.subprocess, "run", fake_run)
    args = argparse.Namespace(
        app=artifact,
        live_app=live_app,
        backup_dir=tmp_path / "backups",
        control_socket=tmp_path / "control.sock",
        server_timeout=1.0,
        app_exit_timeout=1.0,
        allow_downgrade=False,
        allow_noncanonical=False,
        dry_run=False,
        yes=True,
    )

    with pytest.raises(RuntimeError, match="was rolled back"):
        install_build.install(args)

    assert len(swaps) == 2
    assert opens == [live_app, live_app]
