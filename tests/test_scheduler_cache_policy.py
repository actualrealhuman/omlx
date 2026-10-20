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


def test_memory_snapshot_budget_accounts_for_retained_hot_entries():
    scheduler = _snapshot_scheduler("memory", budget=16)
    scheduler.paged_ssd_cache_manager = SimpleNamespace(
        get_stats=lambda: SimpleNamespace(hot_cache_size_bytes=12)
    )
    scheduler._on_prefill_boundary_snapshot("r", [{"state": mx.ones((2,))}], 4)
    assert not scheduler._boundary_cache_snapshots["r"]
    scheduler._boundary_snapshot_store.save.assert_not_called()


@pytest.mark.parametrize("mode", ["memory", "none"])
def test_restrictive_request_cannot_use_live_ssd_requeue(mode):
    scheduler = Scheduler.__new__(Scheduler)
    request = _request(mode)
    assert (
        scheduler._memory_requeue_blocker(request)
        == "request prohibits SSD cache spill"
    )
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
    assert (
        scheduler.block_aware_cache.store_cache.call_args.kwargs["cache_mode"]
        == "memory"
    )


def test_async_none_store_skips_payload_and_releases_request_table():
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.block_aware_cache = MagicMock()
    scheduler.paged_cache_manager = MagicMock()
    scheduler.paged_cache_manager.get_block_table.return_value = SimpleNamespace(
        block_ids=[1]
    )
    scheduler._async_store_cache_worker(
        "r", [1, 2], [], None, None, None, None, None, False, "none"
    )
    scheduler.block_aware_cache.store_cache.assert_not_called()
    scheduler.block_aware_cache.clear_request_entry.assert_called_once_with("r")
    scheduler.paged_cache_manager.release_for_eviction.assert_called_once_with([1])


@pytest.mark.parametrize("mode", ["default", "memory", "none"])
def test_mtp_prefix_history_respects_no_retention(
    mock_model, mock_tokenizer, monkeypatch, mode
):
    from omlx.patches.mlx_lm_mtp import prompt_priming
    from omlx.request import Request, SamplingParams

    scheduler = Scheduler(model=mock_model, tokenizer=mock_tokenizer)
    scheduler.block_aware_cache = MagicMock()
    scheduler._model_has_unreconstructible_cache = lambda: True
    scheduler._log_prefix_divergence = MagicMock()
    scheduler._try_specprefill_scoring = MagicMock()
    prepare = MagicMock()
    monkeypatch.setattr(prompt_priming, "prepare_prefix_context", prepare)
    request = Request(
        request_id="mtp-policy",
        prompt=[1, 2, 3],
        sampling_params=SamplingParams(),
        prompt_token_ids=[1, 2, 3],
        cache_policy=CachePolicy(kv=mode),
    )
    try:
        scheduler._prepare_prefix_cache_for_request(request)
        expected = None if mode == "none" else scheduler.block_aware_cache
        assert prepare.call_args.kwargs["prefix_cache"] is expected
    finally:
        scheduler.shutdown()


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("mode", ["memory", "none"])
def test_completed_hybrid_request_obeys_policy_and_reuses_correct_state(
    mock_tokenizer, tmp_path, split, mode
):
    from mlx_lm.generate import BatchGenerator
    from mlx_lm.models.qwen3_5 import Model, ModelArgs
    from omlx.cache.boundary_snapshot_store import BoundarySnapshotSSDStore
    from omlx.cache.paged_cache import PagedCacheManager
    from omlx.cache.paged_ssd_cache import PagedSSDCacheManager
    from omlx.cache.prefix_cache import BlockAwarePrefixCache
    from omlx.request import Request, SamplingParams
    from omlx.scheduler import SchedulerConfig

    mx.random.seed(7)
    model = Model(
        ModelArgs(
            model_type="qwen3_5",
            text_config=dict(
                hidden_size=32,
                intermediate_size=64,
                num_hidden_layers=2,
                num_attention_heads=2,
                num_key_value_heads=1,
                head_dim=16,
                full_attention_interval=2,
                vocab_size=32,
                linear_num_value_heads=2,
                linear_num_key_heads=2,
                linear_key_head_dim=16,
                linear_value_head_dim=16,
                linear_conv_kernel_dim=4,
                partial_rotary_factor=1.0,
            ),
        )
    )
    scheduler = Scheduler(
        model=model,
        tokenizer=mock_tokenizer,
        config=SchedulerConfig(
            paged_cache_block_size=4,
            gdn_ssd_split_enabled=split,
            hot_cache_max_size=8 * 1024**2,
        ),
    )
    paged = PagedCacheManager(
        block_size=4, max_blocks=16, initial_blocks=16, model_name="tiny"
    )
    ssd = PagedSSDCacheManager(
        cache_dir=tmp_path / "cache",
        max_size_bytes=16 * 1024**2,
        hot_cache_max_bytes=8 * 1024**2,
        hot_cache_write_through=True,
        expected_model_name="tiny",
        expected_num_layers=2,
        expected_block_size=4,
        expected_layer_cache_types=["ArraysCache", "KVCache"],
        gdn_ssd_split_enabled=split,
    )
    boundary = BoundarySnapshotSSDStore(tmp_path / "cache")
    boundary.save = MagicMock(side_effect=AssertionError("restrictive snapshot write"))
    prefix = BlockAwarePrefixCache(
        model=model,
        paged_cache_manager=paged,
        paged_ssd_cache_manager=ssd,
        gdn_ssd_split_enabled=split,
    )
    prefix.set_gdn_checkpoint_loader(boundary.load_file)
    scheduler.paged_cache_manager = paged
    scheduler.paged_ssd_cache_manager = ssd
    scheduler._boundary_snapshot_store = boundary
    scheduler.block_aware_cache = prefix
    generator = BatchGenerator(
        model,
        max_tokens=1,
        sampler=lambda logits: mx.zeros(logits.shape[:-1], dtype=mx.int32),
        stream=scheduler._stream,
    )
    scheduler.batch_generator = generator
    request = Request(
        request_id="finished",
        prompt=[3, 4, 5],
        sampling_params=SamplingParams(max_tokens=1),
    )
    request.cache_policy = CachePolicy(kv=mode)
    scheduler.add_request(request)
    scheduler.waiting.clear()
    scheduler.running[request.request_id] = request
    uid = generator.insert([[3, 4, 5]])[0]
    scheduler.request_id_to_uid[request.request_id] = uid
    scheduler.uid_to_request_id[uid] = request.request_id
    try:
        for _ in range(5):
            _, responses = generator.next()
            if responses:
                break
        _, finished = scheduler._process_batch_responses(responses)
        scheduler._cleanup_finished(finished)
        for future in list(scheduler._inflight_store_futures.values()):
            future.result(timeout=10)
        scheduler._drain_pending_async_removes()
        boundary.save.assert_not_called()
        table, _ = prefix.fetch_cache("next", [3, 4, 5, 0, 9], cache_mode="memory")
        if mode == "none":
            assert table is None
            assert not ssd._hot_cache
        else:
            assert table is not None and table.num_tokens == 4
            restored = prefix.reconstruct_cache(table, cache_mode="memory")
            assert restored is not None
            from mlx_lm.models.cache import make_prompt_cache

            fresh = make_prompt_cache(model)
            with mx.stream(scheduler._stream):
                expected = model(mx.array([[3, 4, 5, 0, 9]]), cache=fresh)[:, -1]
                actual = model(mx.array([[9]]), cache=restored)[:, -1]
                mx.eval(expected, actual)
            assert mx.allclose(actual, expected, atol=2e-5).item()
        assert not list((tmp_path / "cache").rglob("*.safetensors"))
    finally:
        generator.close()
        scheduler.shutdown()
        boundary.shutdown()
        ssd.close()
    assert not list((tmp_path / "cache").rglob("*.safetensors"))
