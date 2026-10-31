# SPDX-License-Identifier: Apache-2.0
"""External prefill preserves KV and token progress across eviction pauses."""

from collections import deque
from contextlib import nullcontext
from types import SimpleNamespace

import mlx.core as mx
import pytest
from mlx_lm.models.cache import KVCache

from omlx.request import Request, RequestStatus, SamplingParams
from omlx.scheduler import (
    PrefillEvictionRequest,
    Scheduler,
    SchedulerConfig,
    _PrefillChunkPlan,
    _PrefillEvictionNeeded,
)
from omlx.server import _usage_timing_fields


class _RecordingModel:
    """Store input tokens in KV so retries expose duplication or recomputation."""

    def __init__(self):
        self.layers = [SimpleNamespace()]
        self.args = SimpleNamespace(num_hidden_layers=1)
        self.seen = []

    def __call__(self, inputs, cache=None, **kwargs):
        self.seen.extend(inputs[0].tolist())
        values = inputs[:, None, :, None].astype(mx.float32)
        cache[0].update_and_fetch(values, values)
        return mx.zeros((1, inputs.shape[1], 8))

    def make_cache(self):
        return [KVCache()]

    def parameters(self):
        return {}


class _ActivityProbe:
    def __init__(self):
        self.pauses = []
        self.resumes = 0
        self.prefills = 0

    def pause(self, reason):
        self.pauses.append(reason)

    def resume(self):
        self.resumes += 1

    def prefill(self):
        self.prefills += 1


def test_inflight_eviction_pause_resumes_only_after_chunk_progress():
    activity = _ActivityProbe()
    request = SimpleNamespace(request_id="req-progress", activity=activity)
    state = SimpleNamespace(
        request=request,
        tokens_processed=4,
        tokens_remaining=SimpleNamespace(shape=(1, 8)),
    )
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.prefilling = deque([request])
    scheduler._prefill_states = {request.request_id: state}
    scheduler._packed_prefill_disabled = True
    scheduler._prefill_gate_open = lambda: True
    scheduler._pending_prefill_eviction_request = None
    eviction = PrefillEvictionRequest(
        request_id=request.request_id,
        model_id="model",
        current_bytes=0,
        target_cap_bytes=1,
        predicted_transient_bytes=1,
        requested_tokens=1,
        reason="adaptive_prefill_throttle",
    )

    def pause_chunk(_state):
        raise _PrefillEvictionNeeded(eviction)

    scheduler._step_prefill_chunk = pause_chunk
    Scheduler._advance_chunked_prefills(scheduler, [], [])
    assert activity.pauses == ["prefill_eviction"]
    assert activity.resumes == 0
    assert scheduler.prefilling == deque([request])

    def advance_chunk(_state):
        state.tokens_processed += 2
        return False

    scheduler._step_prefill_chunk = advance_chunk
    Scheduler._advance_chunked_prefills(scheduler, [], [])
    assert activity.resumes == 1
    assert state.tokens_processed == 6


def test_inflight_prefill_with_no_new_progress_does_not_resume():
    activity = _ActivityProbe()
    request = SimpleNamespace(
        request_id="req-no-progress",
        activity=activity,
        status=RequestStatus.WAITING,
        batch_uid=1,
    )
    state = SimpleNamespace(
        request=request,
        tokens_processed=4,
        tokens_remaining=SimpleNamespace(shape=(1, 8)),
    )
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.prefilling = deque([request])
    scheduler._prefill_states = {request.request_id: state}
    scheduler._packed_prefill_disabled = True
    scheduler._prefill_gate_open = lambda: True
    scheduler._pending_prefill_eviction_request = None
    eviction = PrefillEvictionRequest(
        request_id=request.request_id,
        model_id="model",
        current_bytes=0,
        target_cap_bytes=1,
        predicted_transient_bytes=1,
        requested_tokens=1,
        reason="prefill_preflight",
    )

    def pause_chunk(_state):
        raise _PrefillEvictionNeeded(eviction)

    scheduler._step_prefill_chunk = pause_chunk
    Scheduler._advance_chunked_prefills(scheduler, [], [])
    assert activity.pauses == ["prefill_eviction"]
    scheduler._step_prefill_chunk = lambda _state: False
    Scheduler._advance_chunked_prefills(scheduler, [], [])
    assert activity.pauses == ["prefill_eviction"]
    assert activity.resumes == 0


def test_packed_companion_resume_survives_unobserved_head(monkeypatch):
    activity = _ActivityProbe()
    activity.pause("memory_requeue")
    head = SimpleNamespace(request_id="req-unobserved-head", activity=None)
    companion = SimpleNamespace(request_id="req-observed-companion", activity=activity)
    head_state = SimpleNamespace(
        request=head,
        tokens_processed=2,
        tokens_remaining=SimpleNamespace(shape=(1, 8)),
    )
    companion_state = SimpleNamespace(
        request=companion,
        tokens_processed=3,
        tokens_remaining=SimpleNamespace(shape=(1, 5)),
    )
    plans = [
        _PrefillChunkPlan(head_state, 2, 2, False, False, False, 0.0),
        _PrefillChunkPlan(
            companion_state,
            1,
            1,
            False,
            False,
            False,
            0.0,
            activity_observed=True,
        ),
    ]
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.prefilling = deque([head, companion])
    scheduler._prefill_states = {
        head.request_id: head_state,
        companion.request_id: companion_state,
    }
    scheduler._packed_prefill_disabled = None
    scheduler._packed_min_row_tokens = 1
    scheduler._prefill_gate_open = lambda: True
    scheduler._prefill_eviction = None
    monkeypatch.setattr(Scheduler, "_packed_prefill_ready", lambda _self: True)
    monkeypatch.setattr(Scheduler, "_packing_fits_contended_cap", lambda _self: True)
    monkeypatch.setattr(Scheduler, "_contended_prefill_cap", lambda _self: 0)
    scheduler._plan_prefill_rows = lambda _state, _candidates: plans

    def run_plans(_plans):
        for plan in _plans:
            plan.state.tokens_processed += plan.tokens
        return [False, False]

    scheduler._run_prefill_chunks = run_plans
    Scheduler._advance_chunked_prefills(scheduler, [], [])

    assert head_state.tokens_processed == 4
    assert companion_state.tokens_processed == 4
    assert activity.prefills == 1
    assert activity.resumes == 1


@pytest.mark.parametrize(
    "observer_fails", [False, True], ids=["no-observer", "observer-error"]
)
def test_activity_hook_is_best_effort_during_authoritative_pause(observer_fails):
    class BrokenActivity:
        def pause(self, _reason):
            raise RuntimeError("observer failure")

    request = SimpleNamespace(
        request_id="req-best-effort",
        activity=BrokenActivity() if observer_fails else None,
        status=RequestStatus.RUNNING,
        batch_uid=1,
    )
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.waiting = deque()
    scheduler._pending_prefill_eviction_request = None
    eviction = PrefillEvictionRequest(
        request_id=request.request_id,
        model_id="model",
        current_bytes=0,
        target_cap_bytes=1,
        predicted_transient_bytes=1,
        requested_tokens=1,
        reason="prefill_preflight",
    )
    Scheduler._pause_for_prefill_eviction(scheduler, request, eviction)
    assert scheduler.waiting == deque([request])
    assert scheduler._pending_prefill_eviction_request is eviction
    assert request.status is RequestStatus.WAITING


def test_oom_retry_requeues_the_same_observed_request_and_invocation():
    activity = _ActivityProbe()
    activity.id = "1"
    request = Request(
        request_id="req-oom-retry",
        prompt=[1, 2],
        sampling_params=SamplingParams(),
    )
    request.activity = activity
    scheduler = Scheduler.__new__(Scheduler)
    scheduler._specprefill_active_request_id = None
    scheduler.model = None
    scheduler.requests = {}
    scheduler.waiting = deque()

    Scheduler._requeue_prefill_retry(scheduler, request)

    assert scheduler.waiting == deque([request])
    assert scheduler.requests[request.request_id] is request
    assert scheduler.waiting[0].activity is activity
    assert scheduler.waiting[0].activity.id == "1"
    assert activity.pauses == ["oom_retry"]


@pytest.mark.parametrize("corruption", [False, True], ids=["batch-reset", "corruption"])
def test_batch_reset_requeue_keeps_observed_request_identity(
    mock_tokenizer, corruption
):
    scheduler = Scheduler(model=_RecordingModel(), tokenizer=mock_tokenizer)
    request = Request(
        request_id="observed-reset", prompt=[1, 2], sampling_params=SamplingParams()
    )
    request.prompt_token_ids = [1, 2]
    request.status = RequestStatus.RUNNING
    activity = _ActivityProbe()
    request.activity = activity
    scheduler.requests[request.request_id] = request
    scheduler.running[request.request_id] = request

    assert scheduler._reschedule_running_requests(is_corruption=corruption) == []
    assert scheduler.waiting[0] is request
    assert request.activity is activity
    assert activity.pauses == ["cache_recovery" if corruption else "batch_reset"]
    assert activity.resumes == 0


@pytest.mark.parametrize("retry_limit", [0, 2], ids=["exhausted", "requeued"])
def test_overflow_annotation_requires_actual_requeue(mock_tokenizer, retry_limit):
    scheduler = Scheduler(model=_RecordingModel(), tokenizer=mock_tokenizer)
    request = Request(
        request_id="observed-overflow", prompt=[1, 2], sampling_params=SamplingParams()
    )
    request.prompt_token_ids = [1, 2]
    request.status = RequestStatus.RUNNING
    request.activity = _ActivityProbe()
    scheduler.requests[request.request_id] = request
    scheduler.running[request.request_id] = request

    failed = scheduler._reschedule_generation_overflow_requests(retry_limit)
    if retry_limit:
        assert failed == []
        assert scheduler.waiting[0] is request
        assert request.activity.pauses == ["generation_overflow"]
    else:
        assert failed == [request.request_id]
        assert not scheduler.waiting
        assert request.activity.pauses == []


def test_memory_pressure_requeues_observed_requests_without_resuming(
    mock_tokenizer, monkeypatch
):
    scheduler = Scheduler(model=_RecordingModel(), tokenizer=mock_tokenizer)
    requests = [
        Request(
            request_id=f"observed-memory-{i}",
            prompt=[1, 2],
            sampling_params=SamplingParams(),
        )
        for i in range(2)
    ]
    for request in requests:
        request.prompt_token_ids = [1, 2]
        request.activity = _ActivityProbe()
        scheduler.requests[request.request_id] = request
        scheduler.prefilling.append(request)
        scheduler._prefill_states[request.request_id] = SimpleNamespace(cache=[])
    scheduler._pending_memory_requeue = True
    scheduler.paged_ssd_cache_manager = SimpleNamespace(
        bounded_pending_writes=lambda _: nullcontext()
    )
    monkeypatch.setattr(scheduler, "_memory_requeue_storage_ready", lambda: True)
    monkeypatch.setattr(scheduler, "_memory_requeue_blocker", lambda _: None)
    monkeypatch.setattr(scheduler, "_store_for_memory_requeue", lambda _: 2)

    scheduler._process_pending_memory_requeue()
    assert set(id(request) for request in scheduler.waiting) == set(map(id, requests))
    for request in requests:
        assert request.activity.pauses == ["memory_requeue"]
        assert request.activity.resumes == 0


@pytest.mark.parametrize("cached_tokens", [0, 4], ids=["cold", "warm"])
@pytest.mark.parametrize("pause_after_chunks", [0, 1, 2])
@pytest.mark.parametrize("pause_count", [1, 2])
@pytest.mark.parametrize("route", ["adaptive", "guard"])
def test_external_prefill_resumes_without_replaying_tokens(
    mock_tokenizer, monkeypatch, cached_tokens, pause_after_chunks, pause_count, route
):
    model = _RecordingModel()
    scheduler = Scheduler(
        model=model,
        tokenizer=mock_tokenizer,
        config=SchedulerConfig(prefill_step_size=4),
    )
    prompt = list(range(100, 132))
    request = Request(
        request_id="req-pause", prompt=prompt, sampling_params=SamplingParams()
    )
    request.prompt_token_ids = prompt
    request.num_prompt_tokens = len(prompt)
    request.cached_tokens = cached_tokens
    request.remaining_tokens = prompt[cached_tokens:]
    if cached_tokens:
        request.prompt_cache = model.make_cache()
        model(mx.array(prompt[:cached_tokens])[None], cache=request.prompt_cache)
    scheduler.requests[request.request_id] = request

    scheduler._memory_limit_bytes = 80
    scheduler._memory_hard_limit_bytes = 100
    scheduler._memory_abort_limit_bytes = 100
    scheduler._prefill_abort_margin = 0.9
    scheduler._prefill_min_chunk_tokens = 4
    pause_at = cached_tokens + 4 * pause_after_chunks

    def current_usage():
        return 60 if len(model.seen) >= pause_at else 0

    monkeypatch.setattr(scheduler, "_current_usage_bytes", current_usage)
    monkeypatch.setattr(scheduler, "_reclaim_prefill_headroom", current_usage)
    # The guard also charges observed peaks, which can exceed the throttle's estimate.
    monkeypatch.setattr(
        scheduler,
        "_predicted_chunk_transient",
        lambda *args, **kwargs: 50 if route == "adaptive" else 4,
    )
    monkeypatch.setattr(scheduler, "_admission_transient_bound", lambda *a, **kw: 50)
    original_guard = scheduler._guard_prefill_chunk

    def guard(n, **kwargs):
        assert kwargs["kv_len"] == len(model.seen)
        return original_guard(n, **kwargs)

    monkeypatch.setattr(scheduler, "_guard_prefill_chunk", guard)

    for _ in range(pause_count):
        with pytest.raises(_PrefillEvictionNeeded) as exc:
            scheduler._do_external_prefill(
                request, request.remaining_tokens, request.prompt_cache
            )
        expected_reason = (
            "adaptive_prefill_throttle" if route == "adaptive" else "prefill_safety_cap"
        )
        assert exc.value.request.reason == expected_reason
        assert request.cached_tokens == pause_at
        assert request.remaining_tokens == prompt[pause_at:]
        if pause_at:
            assert request.prompt_cache[0].offset == pause_at
        else:
            assert request.prompt_cache is None
        scheduler._pause_for_prefill_eviction(request, exc.value.request)
        assert scheduler.waiting.popleft() is request
        pause_at += 4

    pause_at = len(prompt) + 1
    cache, last_token = scheduler._do_external_prefill(
        request, request.remaining_tokens, request.prompt_cache
    )
    assert last_token == prompt[-1:]
    assert cache[0].offset == len(prompt) - 1
    model(mx.array(last_token)[None], cache=cache)
    assert model.seen == prompt
    assert cache[0].keys[0, 0, : cache[0].offset, 0].tolist() == prompt

    request.status = RequestStatus.RUNNING
    scheduler.running[request.request_id] = request
    scheduler.uid_to_request_id[1] = request.request_id
    outputs, finished = scheduler._process_batch_responses(
        [
            SimpleNamespace(uid=1, token=3, finish_reason=None),
            SimpleNamespace(uid=1, token=4, finish_reason="length"),
        ]
    )
    assert finished == {request.request_id}
    assert [output.cached_tokens for output in outputs] == [cached_tokens] * 2

    usage = _usage_timing_fields(
        outputs[-1].prompt_tokens,
        outputs[-1].completion_tokens,
        ttft=1.0,
        prefill_duration=1.0,
        generation_duration=0.1,
        cached_tokens=outputs[-1].cached_tokens,
    )
    assert usage["prompt_tokens_per_second"] == len(prompt) - cached_tokens
