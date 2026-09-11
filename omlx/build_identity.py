"""Downstream build identity shared by CLI and status endpoints."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._version import __version__


def get_build_identity() -> dict[str, Any]:
    """Describe this build, with useful source-tree fallbacks for development."""
    manifest = json.loads(
        Path(__file__).with_name("_build_manifest.json").read_text(encoding="utf-8")
    )
    try:
        from ._build_info import (  # type: ignore[import-not-found]
            build_channel,
            build_features,
            build_number,
            source_branch,
            source_revision,
        )
    except ImportError:
        build_channel = f"{manifest['channel']}-dev"
        build_features = tuple(manifest["features"])
        build_number = None
        source_branch = None
        source_revision = None

    return {
        "version": __version__,
        "build_number": build_number,
        "channel": build_channel,
        "source_revision": source_revision,
        "source_branch": source_branch,
        "features": list(build_features),
    }


def build_label() -> str:
    """Compact human-readable identity for logs and terminal banners."""
    info = get_build_identity()
    parts = [info["version"]]
    if info["build_number"]:
        parts.append(f"build {info['build_number']}")
    if info["channel"]:
        parts.append(str(info["channel"]))
    if info["source_revision"]:
        parts.append(str(info["source_revision"]))
    return " · ".join(parts)
