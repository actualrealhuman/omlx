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


def _make_artifacts(root: Path, builds: list[int]) -> dict[int, Path]:
    apps: dict[int, Path] = {}
    for build in builds:
        directory = root / f"0.7.0.dev2-build{build}-{'a' * 12}"
        apps[build] = _fake_app(directory / "oMLX.app", build=build)
    return apps


def test_find_latest_artifact_picks_the_highest_build_number(tmp_path, monkeypatch):
    root = tmp_path / "Artifacts"
    apps = _make_artifacts(root, [2690, 2912, 2910])
    monkeypatch.setattr(install_build, "ARTIFACTS_DIR", root)
    monkeypatch.setattr(install_build, "verify_signature", lambda *_a, **_k: None)

    assert install_build.find_latest_artifact(canonical=False) == apps[2912].resolve()


def test_find_latest_artifact_validates_newest_first_and_stops(tmp_path, monkeypatch):
    # Deep signature verification is the expensive step (a full pass over every
    # embedded Mach-O in a ~1 GB bundle), so selection must not pay for it on
    # artifacts it is going to discard anyway. Ranking happens on the plist.
    root = tmp_path / "Artifacts"
    apps = _make_artifacts(root, [2690, 2691, 2912, 2910])
    verified: list[Path] = []

    monkeypatch.setattr(install_build, "ARTIFACTS_DIR", root)
    monkeypatch.setattr(
        install_build,
        "verify_signature",
        lambda app, *_a, **_k: verified.append(Path(app)),
    )

    assert install_build.find_latest_artifact(canonical=False) == apps[2912].resolve()
    assert verified == [apps[2912].resolve()]


def test_find_latest_artifact_falls_back_when_the_newest_is_invalid(
    tmp_path, monkeypatch
):
    root = tmp_path / "Artifacts"
    apps = _make_artifacts(root, [2910, 2912])
    broken = apps[2912].resolve()

    def verify(app, *_a, **_k):
        if Path(app) == broken:
            raise ValueError(f"invalid app signature for {app}")

    monkeypatch.setattr(install_build, "ARTIFACTS_DIR", root)
    monkeypatch.setattr(install_build, "verify_signature", verify)

    assert install_build.find_latest_artifact(canonical=False) == apps[2910].resolve()


def test_find_latest_artifact_reports_when_nothing_validates(tmp_path, monkeypatch):
    root = tmp_path / "Artifacts"
    _make_artifacts(root, [2910, 2912])

    def verify(*_a, **_k):
        raise ValueError("invalid app signature")

    monkeypatch.setattr(install_build, "ARTIFACTS_DIR", root)
    monkeypatch.setattr(install_build, "verify_signature", verify)

    with pytest.raises(ValueError, match="no valid oMLX artifacts found"):
        install_build.find_latest_artifact(canonical=False)


def _dry_args(tmp_path, artifact, live_app, **overrides):
    base = {
        "app": artifact,
        "live_app": live_app,
        "backup_dir": tmp_path / "backups",
        "control_socket": tmp_path / "control.sock",
        "server_timeout": 1.0,
        "app_exit_timeout": 1.0,
        "allow_downgrade": False,
        "allow_noncanonical": False,
        "dry_run": False,
        "yes": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _record_mutators(monkeypatch, sink: list[str]) -> None:
    """Stub every step that touches the machine, recording the ones that run."""
    monkeypatch.setattr(install_build, "validate_bundle", lambda *_a, **_k: _identity(11))
    monkeypatch.setattr(install_build, "bundle_identity", lambda *_a, **_k: _identity(10))
    monkeypatch.setattr(install_build, "reject_downgrade", lambda *_a, **_k: None)
    monkeypatch.setattr(install_build, "running_app_processes", lambda: [])

    def copy(_artifact, stage):
        sink.append("copy")
        # The real copy leaves a complete bundle at the staging path, and the
        # backup step later moves that path, so the stub has to create it.
        Path(stage).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(install_build, "_copy_bundle", copy)
    monkeypatch.setattr(
        install_build,
        "stop_old_instance",
        lambda *_a, **_k: (sink.append("stop"), (123, True))[1],
    )
    monkeypatch.setattr(
        install_build, "atomic_swap", lambda *_a, **_k: sink.append("swap")
    )
    monkeypatch.setattr(
        install_build,
        "wait_for_replacement",
        lambda *_a, **_k: (sink.append("wait"), {"pid": 456})[1],
    )
    monkeypatch.setattr(
        install_build,
        "verify_running_identity",
        lambda *_a, **_k: (sink.append("verify"), {"status": "healthy"})[1],
    )

    def fake_run(command, **_kwargs):
        sink.append("open")
        return argparse.Namespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(install_build.subprocess, "run", fake_run)


def test_run_without_yes_is_a_dry_run_not_a_failure(tmp_path, monkeypatch):
    # Validating a promote is the common case. This path used to raise, so main()
    # printed "installation failed:" over a plan that had validated fine and exited
    # 1 -- every safe invocation looked like a broken promote.
    artifact = _fake_app(tmp_path / "artifact/oMLX.app", build=11)
    live_app = _fake_app(tmp_path / "Applications/oMLX.app", build=10)
    did: list[str] = []
    _record_mutators(monkeypatch, did)

    result = install_build.install(_dry_args(tmp_path, artifact, live_app))

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["would_do"], "a dry run must say what it would have done"
    assert did == [], f"dry run must touch nothing, but did: {did}"


def test_explicit_dry_run_matches_the_default(tmp_path, monkeypatch):
    artifact = _fake_app(tmp_path / "artifact/oMLX.app", build=11)
    live_app = _fake_app(tmp_path / "Applications/oMLX.app", build=10)
    _record_mutators(monkeypatch, [])

    default = install_build.install(_dry_args(tmp_path, artifact, live_app))
    explicit = install_build.install(
        _dry_args(tmp_path, artifact, live_app, dry_run=True)
    )

    assert default == explicit


def test_dry_run_lists_the_steps_an_install_would_take(tmp_path, monkeypatch):
    artifact = _fake_app(tmp_path / "artifact/oMLX.app", build=11)
    live_app = _fake_app(tmp_path / "Applications/oMLX.app", build=10)
    _record_mutators(monkeypatch, [])

    result = install_build.install(_dry_args(tmp_path, artifact, live_app))

    assert result["would_do"] == list(install_build.DRY_RUN_STEPS)
    # The plan has to name the paths a reader needs to check before saying yes.
    assert result["artifact"] == str(artifact.resolve())
    assert result["live_app"] == str(live_app)
    assert result["incoming"]["build_number"] == 11
    assert result["installed"]["build_number"] == 10


def test_yes_still_performs_the_steps_a_dry_run_only_lists(tmp_path, monkeypatch):
    artifact = _fake_app(tmp_path / "artifact/oMLX.app", build=11)
    live_app = _fake_app(tmp_path / "Applications/oMLX.app", build=10)
    did: list[str] = []
    _record_mutators(monkeypatch, did)

    result = install_build.install(
        _dry_args(tmp_path, artifact, live_app, yes=True)
    )

    assert "dry_run" not in result, "a real install must not report dry_run"
    assert did, "--yes must actually run the steps"
    assert "stop" in did and "swap" in did and "open" in did


def test_mode_is_announced_before_anything_is_touched(tmp_path, monkeypatch, capsys):
    artifact = _fake_app(tmp_path / "artifact/oMLX.app", build=11)
    live_app = _fake_app(tmp_path / "Applications/oMLX.app", build=10)
    stderr_at_first_mutation: list[str] = []

    def copy(_artifact, stage):
        stderr_at_first_mutation.append(capsys.readouterr().err)
        Path(stage).mkdir(parents=True, exist_ok=True)

    _record_mutators(monkeypatch, [])
    monkeypatch.setattr(install_build, "_copy_bundle", copy)

    install_build.install(_dry_args(tmp_path, artifact, live_app, yes=True))

    assert stderr_at_first_mutation, "expected the copy step to have run"
    assert "INSTALL" in stderr_at_first_mutation[0], (
        "the mode must be stated before the first mutating step, not inferred "
        "from how the run ended"
    )


def test_dry_run_announces_itself_on_stderr_and_leaves_stdout_parseable(
    tmp_path, monkeypatch, capsys
):
    artifact = _fake_app(tmp_path / "artifact/oMLX.app", build=11)
    live_app = _fake_app(tmp_path / "Applications/oMLX.app", build=10)
    _record_mutators(monkeypatch, [])

    install_build.install(_dry_args(tmp_path, artifact, live_app))

    captured = capsys.readouterr()
    assert "DRY RUN" in captured.err
    assert "--yes" in captured.err
    assert captured.out == "", "progress belongs on stderr; stdout stays JSON only"


def test_dry_run_and_yes_are_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["install_build.py", "--dry-run", "--yes"])
    with pytest.raises(SystemExit) as excinfo:
        install_build.parse_args()
    assert excinfo.value.code == 2


def test_find_latest_artifact_reports_why_nothing_validated(tmp_path, monkeypatch):
    # A bare "no valid artifacts" sent people rebuilding when the real answer was a
    # revision mismatch that --allow-noncanonical already resolves.
    root = tmp_path / "Artifacts"
    _make_artifacts(root, [2910, 2912])

    def verify(*_a, **_k):
        raise ValueError(
            "artifact revision is '37cd6689ed6d'; current personal/main is 36fd5114aa8d"
        )

    monkeypatch.setattr(install_build, "ARTIFACTS_DIR", root)
    monkeypatch.setattr(install_build, "verify_signature", verify)

    with pytest.raises(ValueError) as excinfo:
        install_build.find_latest_artifact(canonical=False)

    message = str(excinfo.value)
    assert "no valid oMLX artifacts found" in message
    assert "revision is '37cd6689ed6d'" in message, "the actual reason must survive"
    assert "build2912" in message, "the report must name which artifact failed"
