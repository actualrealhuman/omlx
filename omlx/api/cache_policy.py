# SPDX-License-Identifier: Apache-2.0
"""HTTP request and acknowledgement helpers for per-request cache policy."""

from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict

from omlx.cache.policy import CachePolicy


class CachePolicyRequest(BaseModel):
    """Optional policy components supplied in an API request body."""

    kv: Literal["default", "memory", "none"] = "default"
    images: Literal["default", "memory", "none"] = "default"

    model_config = ConfigDict(extra="forbid")


class CachePolicyAcknowledgement(BaseModel):
    """Effective response policy, including unavailable cache subsystems."""

    kv: Literal["default", "memory", "none", "disabled"]
    images: Literal["default", "memory", "none", "disabled"]


_HEADER_COMPONENTS = {
    b"x-omlx-kv-cache": "kv",
    b"x-omlx-image-cache": "images",
}
_MODES = frozenset({"default", "memory", "none"})


def resolve_request_cache_policy(
    body_policy: CachePolicyRequest | None,
    request: Request,
) -> tuple[CachePolicy, frozenset[str]]:
    """Merge body and component headers, rejecting ambiguity or malformed input."""
    body_values = (
        body_policy.model_dump()
        if body_policy is not None
        else {"kv": "default", "images": "default"}
    )
    body_explicit = (
        set(body_policy.model_fields_set) if body_policy is not None else set()
    )
    header_values: dict[str, str] = {}
    raw_headers = request.scope.get("headers", ())
    for raw_name, raw_value in raw_headers:
        component = _HEADER_COMPONENTS.get(raw_name.lower())
        if component is None:
            continue
        try:
            value = raw_value.decode("ascii").strip(" \t")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400, detail="cache policy headers must be ASCII"
            ) from exc
        if value not in _MODES:
            raise HTTPException(
                status_code=400,
                detail=f"{component} cache header must be default, memory or none",
            )
        previous = header_values.get(component)
        if previous is not None and previous != value:
            header_name = (
                "X-oMLX-KV-Cache" if component == "kv" else "X-oMLX-Image-Cache"
            )
            raise HTTPException(
                status_code=400,
                detail=f"conflicting duplicate {header_name} headers",
            )
        header_values[component] = value

    for component, header_value in header_values.items():
        if component in body_explicit and body_values[component] != header_value:
            header_name = (
                "X-oMLX-KV-Cache" if component == "kv" else "X-oMLX-Image-Cache"
            )
            raise HTTPException(
                status_code=400,
                detail=f"cache_policy.{component} conflicts with {header_name} header",
            )

    merged = dict(body_values)
    merged.update(header_values)
    return CachePolicy(**merged), frozenset(body_explicit | set(header_values))


def ensure_cache_policy_supported(
    engine, policy: CachePolicy, explicit: frozenset[str]
) -> None:
    """Reject explicit restrictions before preprocessing when the engine cannot enforce them."""
    supports = getattr(engine, "supports_cache_policy_component", None)
    for component in explicit:
        if getattr(policy, component) == "default":
            continue
        if not callable(supports) or not supports(component):
            raise HTTPException(
                status_code=400,
                detail=f"{component} cache policy is not supported by this engine",
            )


def effective_cache_policy(
    engine, requested: CachePolicy
) -> CachePolicyAcknowledgement:
    """Report the effective retention tier based on the active runtime cache objects."""
    return CachePolicyAcknowledgement(
        kv=_effective_kv_mode(engine, requested.kv),
        images=_effective_image_mode(engine, requested.images),
    )


def cache_policy_response_headers(
    policy: CachePolicyAcknowledgement | CachePolicy,
) -> dict[str, str]:
    return {
        "X-oMLX-KV-Cache": policy.kv,
        "X-oMLX-Image-Cache": policy.images,
    }


def _effective_kv_mode(engine, requested: str) -> str:
    runtime = getattr(engine, "_engine", None)
    scheduler = getattr(getattr(runtime, "engine", None), "scheduler", None)
    prefix_cache = getattr(scheduler, "block_aware_cache", None)
    if prefix_cache is None or getattr(engine, "prefix_cache_enabled", True) is False:
        return "disabled"

    if requested == "none":
        return "none"

    ssd_manager = getattr(scheduler, "paged_ssd_cache_manager", None)
    if ssd_manager is None:
        return "disabled"

    ram_available = bool(getattr(ssd_manager, "_hot_cache_enabled", False))
    if requested == "memory":
        return "memory"

    # Runtime manager state is authoritative; configuration may have changed
    # after the manager was constructed or may describe an inactive cache.
    if bool(getattr(ssd_manager, "_hot_cache_only", False)):
        return "memory" if ram_available else "disabled"
    if getattr(ssd_manager, "_cache_dir", None) is not None:
        return "default"
    if ram_available:
        return "memory"
    return "disabled"


def _effective_image_mode(engine, requested: str) -> str:
    cache = getattr(engine, "_vision_cache", None)
    if cache is None:
        return "disabled"
    if requested == "none":
        return "none"
    if requested == "memory":
        return "memory"
    if getattr(cache, "_cache_dir", None) is not None:
        return "default"
    if getattr(cache, "_max_memory_bytes", 0) > 0:
        return "memory"
    return "disabled"
