# SPDX-License-Identifier: Apache-2.0
"""Immutable request-scoped choices for reusable inference caches."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

CacheMode = Literal["default", "memory", "none"]
_MODES = ("default", "memory", "none")


@dataclass(frozen=True)
class CachePolicy:
    """Server defaults, bounded RAM retention, or no new reusable retention."""

    kv: CacheMode = "default"
    images: CacheMode = "default"

    def __post_init__(self) -> None:
        for name in ("kv", "images"):
            if getattr(self, name) not in _MODES:
                raise ValueError(f"cache_policy.{name} must be default, memory or none")


def coerce_cache_policy(value=None) -> CachePolicy:
    """Normalize API or internal input without silently discarding mistakes."""
    if value is None:
        return CachePolicy()
    if isinstance(value, CachePolicy):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if not isinstance(value, Mapping):
        raise ValueError("cache_policy must be an object")
    if set(value) - {"kv", "images"}:
        raise ValueError("cache_policy accepts only kv and images")
    return CachePolicy(**value)
