"""Tests for downstream build identity reporting."""

import sys
from types import ModuleType

from omlx.build_identity import build_label, get_build_identity


def test_source_tree_reports_private_development_features():
    info = get_build_identity()

    assert info["version"]
    assert info["channel"] == "private-dev"
    assert "benchmark-upload-controls" in info["features"]


def test_generated_bundle_metadata_is_reported(monkeypatch):
    generated = ModuleType("omlx._build_info")
    generated.build_number = "4321"
    generated.build_channel = "private"
    generated.source_revision = "abc123def456"
    generated.source_branch = "personal/main"
    generated.build_features = ("benchmark-upload-controls", "custom-kernels")
    monkeypatch.setitem(sys.modules, "omlx._build_info", generated)

    info = get_build_identity()

    assert info == {
        "version": info["version"],
        "build_number": "4321",
        "channel": "private",
        "source_revision": "abc123def456",
        "source_branch": "personal/main",
        "features": ["benchmark-upload-controls", "custom-kernels"],
    }
    assert "private" in build_label()
    assert "abc123def456" in build_label()
