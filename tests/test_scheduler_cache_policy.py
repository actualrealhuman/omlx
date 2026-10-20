# SPDX-License-Identifier: Apache-2.0
"""Request cache restrictions survive snapshots, pressure and async storage."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import mlx.core as mx
import pytest

from omlx.cache.policy import CachePolicy
from omlx.scheduler import Scheduler


def _request(mode):
    return SimpleNamespace(request_id="r", cache_policy=CachePolicy(kv=mode))


def _snapshot_scheduler(mode, budget=1024):
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.requests = {"r": _request(mode)}
    scheduler.config = SimpleNamespace(
        paged_cache_block_size=4, hot_cache_max_size=budget, hot_cache_budget=None
    )
    scheduler._boundary_snapshot_store = MagicMock()
    scheduler._boundary_cache_snapshots = {}
    scheduler._boundary_snapshot_diagnostics = MagicMock()
    scheduler.block_aware_cache = object()
    scheduler._model_has_unreconstructible_cache = lambda: False
    scheduler._cache_list_needs_boundary_snapshot = lambda cache: True
    scheduler._prefill_snapshot_value = lambda cache: cache
    scheduler._enable_mtp_boundary_alignment = lambda: None
    scheduler._stream = None
    return scheduler


def test_none_never_captures_prefill_or_decode_snapshot():
    scheduler = _snapshot_scheduler("none")
    scheduler._on_prefill_boundary_snapshot("r", [object()], 4)
    scheduler._maybe_capture_boundary_snapshot(scheduler.requests["r"], 7)
    scheduler._boundary_snapshot_store.save.assert_not_called()
    assert not scheduler._boundary_cache_snapshots


def test_memory_prefill_snapshot_never_uses_disk_store():
    scheduler = _snapshot_scheduler("memory")
    snapshot = [{"state": (mx.ones((1,)), mx.ones((1,)))}]
    scheduler._on_prefill_boundary_snapshot("r", snapshot, 4)
    scheduler._boundary_snapshot_store.save.assert_not_called()
    assert 4 in scheduler._boundary_cache_snapshots["r"]


@pytest.mark.parametrize("budget", [0, 4])
def test_memory_snapshot_over_budget_is_dropped_without_disk_fallback(budget):
    scheduler = _snapshot_scheduler("memory", budget)
    scheduler._on_prefill_boundary_snapshot("r", [{"state": mx.ones((4,))}], 4)
    scheduler._boundary_snapshot_store.save.assert_not_called()
    assert not scheduler._boundary_cache_snapshots["r"]


def test_memory_snapshot_budget_is_shared_by_live_requests():
    scheduler = _snapshot_scheduler("memory", budget=16)
    scheduler.requests["s"] = _request("memory")
    scheduler._boundary_cache_snapshots = {
        "r": {4: [{"state": mx.ones((4,))}]},
        "s": {8: [{"state": mx.ones((4,))}]},
    }
    scheduler._trim_memory_boundary_snapshots()
    assert not scheduler._boundary_cache_snapshots["r"]
    assert 8 in scheduler._boundary_cache_snapshots["s"]


@pytest.mark.parametrize("mode", ["memory", "none"])
def test_restrictive_request_cannot_use_live_ssd_requeue(mode):
    scheduler = Scheduler.__new__(Scheduler)
    request = _request(mode)
    assert scheduler._memory_requeue_blocker(request) == "request prohibits SSD cache spill"
    assert scheduler._store_for_memory_requeue(request) == 0


def test_async_memory_store_keeps_mode_after_request_removed():
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.requests = {}
    scheduler._stream = None
    scheduler._phase_timer = MagicMock()
    scheduler.block_aware_cache = MagicMock()
    scheduler.block_aware_cache.store_cache.return_value = None
    scheduler.paged_cache_manager = None
    scheduler._async_store_cache_worker(
        "r", [1, 2], [], None, None, None, None, None, False, "memory"
    )
    assert scheduler.block_aware_cache.store_cache.call_args.kwargs["cache_mode"] == "memory"


def test_async_none_store_skips_payload_and_releases_request_table():
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.block_aware_cache = MagicMock()
    scheduler.paged_cache_manager = MagicMock()
    scheduler.paged_cache_manager.get_block_table.return_value = SimpleNamespace(block_ids=[1])
    scheduler._async_store_cache_worker(
        "r", [1, 2], [], None, None, None, None, None, False, "none"
    )
    scheduler.block_aware_cache.store_cache.assert_not_called()
    scheduler.block_aware_cache.clear_request_entry.assert_called_once_with("r")
    scheduler.paged_cache_manager.release_for_eviction.assert_called_once_with([1])
