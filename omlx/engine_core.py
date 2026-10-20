# SPDX-License-Identifier: Apache-2.0
# Adapted from vllm-mlx (https://github.com/vllm-project/vllm-mlx).
"""
Engine Core for oMLX continuous batching.

This module provides the EngineCore class that coordinates:
- Model loading and management
- Request scheduling via Scheduler
- Async request processing
- Output streaming

The design follows vLLM's engine architecture adapted for MLX.
"""

import asyncio
import concurrent.futures
import gc
import logging
import os
import threading
import time
import uuid
import weakref
from contextlib import ExitStack, nullcontext, suppress
from dataclasses import dataclass, field
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)

import mlx.core as mx

from .custom_kernels.quant_matmul.fast import share_command_queue
from .exceptions import (
    PrefillMemoryAbortedError,
    PrefillMemoryExceededError,
    RequestAbortedError,
    describe_ceiling_binding,
)
from .generation_gate import get_generation_gate
from .inference_pacing import PACING_DISABLED, InferencePacer, PacingPermit
from .inference_scheduling import (
    ModelExecutionCancelledError,
    ModelExecutionCoordinator,
)
from .model_registry import get_registry
from .output_collector import RequestOutputCollector, RequestStreamState
from .request import Request, RequestOutput, SamplingParams
from .scheduler import Scheduler, SchedulerConfig, _sync_and_clear_cache
from .utils.fatal import (
    FATAL_TEARDOWN_TIMEOUT_S,
    exit_if_gpu_submissions_ignored,
    fatal_exit,
    is_command_buffer_error,
)
from .utils.hardware import format_bytes
from .utils.metal_sync import clear_thread_streams

logger = logging.getLogger(__name__)


class _EngineTeardown:
    """Allow one extra minute only for recently successful SSD persistence."""

    def __init__(self, engine_id: str, timeout: float = 60.0):
        self.engine_id = engine_id
        self.started = time.monotonic()
        self.timeout = timeout
        self.deadline = self.started + 2 * timeout
        self.extended = False
        self._phase = "shutdown"
        self._progress: Callable[[], float | None] | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._watch, name=f"engine-close-{engine_id[:8]}", daemon=True
        )

    def set_phase(
        self, phase: str, progress: Callable[[], float | None] | None = None
    ) -> None:
        with self._lock:
            self._phase = phase
            self._progress = progress

    def remaining(self, *, mlx: bool = False) -> float:
        remaining = max(0.0, self.deadline - time.monotonic())
        return min(self.timeout, remaining) if mlx else remaining

    def check(self, now: float) -> str | None:
        """Return a fatal reason, or grant the single persistence extension."""
        with self._lock:
            phase, progress = self._phase, self._progress
            if now < self.started + self.timeout:
                return None
            if now >= self.deadline:
                return f"Engine {self.engine_id}: teardown exceeded {2 * self.timeout:g}s ({phase})"
            if self.extended:
                return None
            last_success = progress() if progress is not None else None
            # A write can finish between the time sample and this snapshot.
            if (
                last_success is not None
                and last_success >= self.started
                and now - last_success <= self.timeout / 2
            ):
                self.extended = True
                logger.warning(
                    "Engine %s: extending teardown to %gs for progressing SSD "
                    "persistence (%s, last completed write %.1fs ago)",
                    self.engine_id,
                    2 * self.timeout,
                    phase,
                    now - last_success,
                )
                return None
            return f"Engine {self.engine_id}: teardown timed out after {self.timeout:g}s ({phase}); no recent SSD persistence progress"

    def _watch(self) -> None:
        if self._done.wait(max(0.0, self.started + self.timeout - time.monotonic())):
            return
        try:
            reason = self.check(time.monotonic())
        except Exception:
            logger.exception("Failed to inspect engine teardown progress")
            reason = f"Engine {self.engine_id}: teardown progress check failed"
        if reason is not None:
            fatal_exit(reason)
        while not self._done.wait(min(10.0, self.remaining())):
            now = time.monotonic()
            if now >= self.deadline:
                fatal_exit(
                    f"Engine {self.engine_id}: teardown exceeded {2 * self.timeout:g}s"
                )
                return
            with self._lock:
                phase, progress = self._phase, self._progress
                try:
                    last_success = progress() if progress is not None else None
                except Exception:
                    logger.exception("Failed to inspect engine teardown progress")
                    last_success = None
            detail = ""
            if progress is not None:
                detail = (
                    f", last completed write {max(0.0, now - last_success):.1f}s ago"
                    if last_success is not None
                    else ", no recent successful SSD write confirmed"
                )
            logger.info(
                "Engine %s: waiting for teardown (elapsed %.0fs/%gs, phase=%s%s)",
                self.engine_id,
                now - self.started,
                2 * self.timeout,
                phase,
                detail,
            )

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._done.set()
        self._thread.join()
        self.set_phase("closed")


def _raise_request_output_error(output: RequestOutput) -> None:
    if output.error_code in ("prefill_memory_exceeded", "prefill_memory_aborted"):
        metadata = output.error_metadata or {}
        request_id = metadata.get("request_id")
        estimated_bytes = metadata.get("estimated_bytes")
        limit_bytes = metadata.get("limit_bytes")
        # Both are memory-guard outcomes and share the HTTP 400 mapping; the
        # aborted subclass only changes the wording (admitted then killed
        # mid-prefill vs rejected before it started).
        error_type = (
            PrefillMemoryAbortedError
            if output.error_code == "prefill_memory_aborted"
            else PrefillMemoryExceededError
        )
        raise error_type(
            message=output.error or "Prefill memory exceeded",
            request_id=str(request_id) if request_id is not None else output.request_id,
            estimated_bytes=(
                int(estimated_bytes) if estimated_bytes is not None else None
            ),
            limit_bytes=int(limit_bytes) if limit_bytes is not None else None,
        )
    if output.error_code == "model_unloading":
        raise RequestAbortedError(output.error, request_id=output.request_id)
    raise RuntimeError(output.error)


_global_mlx_executor: concurrent.futures.ThreadPoolExecutor | None = None
_engines_on_own_queues: "weakref.WeakSet[EngineCore]" = weakref.WeakSet()
_warned_own_queues = False


def _note_engine_on_own_queue(engine: "EngineCore") -> None:
    """Warn once when two live engines submit on separate Metal queues."""
    global _warned_own_queues
    _engines_on_own_queues.add(engine)
    live = sum(1 for e in _engines_on_own_queues if not e._closed)
    if _warned_own_queues or live < 2:
        return
    _warned_own_queues = True
    logger.warning(
        "Loaded models submit GPU work on separate Metal command queues "
        "because the native quant_matmul extension is missing or predates "
        "share_command_queue. Running several models at once can then hang "
        "the GPU (#4224). Rebuild the native kernels with `make kernels`."
    )


def _teardown_sync_and_clear_cache(stream: Any = None) -> None:
    """Drain and clear the Metal pool past errors a GPU reset left behind.

    MLX raises an encoder's stored command-buffer error once and then clears
    it, so the given stream and the thread's default stream need at most one
    retry each.
    """
    for attempt in range(3):
        try:
            _sync_and_clear_cache(stream)
            return
        except RuntimeError as exc:
            if attempt == 2 or not is_command_buffer_error(exc):
                raise
            logger.warning("Dropping a pending Metal error during teardown: %s", exc)


def _drop_pending_gpu_errors(stream: Any) -> None:
    """Raise and log errors a GPU reset left on this thread's streams.

    Later teardown syncs then run to completion. No buffer lock is taken, so
    this does not wait on an in-flight store worker.
    """
    for sync in (lambda: mx.synchronize(stream), mx.synchronize):
        try:
            sync()
        except RuntimeError as exc:
            exit_if_gpu_submissions_ignored(exc)
            if "no Stream" in str(exc):
                continue
            if not is_command_buffer_error(exc):
                raise
            logger.warning("Dropping a pending Metal error during teardown: %s", exc)


def _final_engine_thread_reclaim(stream: Any) -> None:
    """Drop Python cycles and reclaim MLX buffers on the engine worker thread."""
    gc.collect()
    _teardown_sync_and_clear_cache(stream)
    gc.collect()
    clear_thread_streams()


def _final_global_mlx_thread_reclaim() -> None:
    """Reclaim MLX state before the process-wide worker thread exits."""
    gc.collect()
    _teardown_sync_and_clear_cache()
    gc.collect()
    clear_thread_streams()


def get_mlx_executor() -> concurrent.futures.ThreadPoolExecutor:
    """Get or create the global MLX executor (lazy singleton).

    ALL MLX GPU operations across all models that share this executor are
    serialized onto one thread to prevent Metal command buffer races that
    cause segfaults. See issue #85. mlx-lm's ``generation_stream`` is a
    thread-local stream, so work submitted here needs no stream setup.
    """
    global _global_mlx_executor
    if _global_mlx_executor is None:
        _global_mlx_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="mlx-global",
        )
    return _global_mlx_executor


def shutdown_mlx_executor() -> None:
    """Shut down the global MLX worker after clearing its thread-local streams."""
    global _global_mlx_executor
    executor = _global_mlx_executor
    if executor is None:
        return

    try:
        future = executor.submit(_final_global_mlx_thread_reclaim)
    except RuntimeError:
        # An embedding application may have shut the executor down directly.
        future = None

    if future is not None:
        try:
            future.result(timeout=FATAL_TEARDOWN_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            fatal_exit(
                "Global MLX executor teardown timed out after "
                f"{FATAL_TEARDOWN_TIMEOUT_S:.0f}s"
            )
        except Exception as exc:
            fatal_exit(f"Global MLX executor teardown failed: {exc!r}")

    executor.shutdown(wait=False)
    _global_mlx_executor = None


@dataclass
class EngineConfig:
    """Configuration for the engine."""

    model_name: str = ""
    scheduler_config: Optional[SchedulerConfig] = None
    step_interval: float = 0.05  # Idle wait timeout; requests wake the loop
    stream_interval: int = 1  # Tokens to batch before streaming (1=every token)
    prefill_eviction_callback: Optional[Callable[[Any], Awaitable[bool]]] = None
    # Shared across local batched engines. None preserves the original path.
    inference_pacer: InferencePacer | None = None
    inference_coordinator: ModelExecutionCoordinator | None = None
    inference_model_id: str | None = None
    inference_priority: int = 0
    inference_exclusive: bool = False
    # Decode burst: run several scheduler.step() calls per run_in_executor
    # hand-off instead of one. Each decode token otherwise bounces back to the
    # event loop, ping-ponging the GIL with the asyncio loop + uvicorn on the
    # main thread; bursting keeps the MLX thread holding the GIL continuously.
    # scheduler.step() services aborts/admission/finish every step, so
    # correctness is unchanged and memory is identical (same tokens decoded,
    # same KV cache; only a small list of K SchedulerOutputs is held per
    # burst). The budget is a TIME ceiling so the event-loop pause (and thus
    # new-request admission / abort / HTTP latency) is bounded consistently
    # across hardware, and a slow prefill-chunk step ends the burst.
    #
    # Adaptive: with a single active request (the common local/single-user
    # case) there is no concurrent request to stay responsive to, so we burst
    # aggressively (decode_burst_budget_single_s). Once concurrent, we use the
    # tight decode_burst_budget_s to keep admission/abort latency low.
    # A request's first generated chunk always ends the burst so buffering
    # later decode steps does not add to its time to first token.
    # max_steps is a safety cap (bounds the host-side output list), NOT a
    # memory knob. Set both budgets <= 0, or max_steps <= 1, to disable.
    decode_burst_max_steps: int = field(
        default_factory=lambda: int(os.environ.get("OMLX_DECODE_BURST_MAX_STEPS", "64"))
    )
    decode_burst_budget_single_s: float = field(
        default_factory=lambda: float(
            os.environ.get("OMLX_DECODE_BURST_BUDGET_SINGLE_S", "0.1")
        )
    )
    decode_burst_budget_s: float = field(
        default_factory=lambda: float(
            os.environ.get("OMLX_DECODE_BURST_BUDGET_S", "0.03")
        )
    )


class EngineCore:
    """
    Core engine for oMLX inference with continuous batching.

    This engine runs the generation loop and manages request lifecycle.
    It provides both sync and async interfaces for request handling.
    """

    def __init__(
        self,
        model: Any,
        tokenizer: Any,
        config: Optional[EngineConfig] = None,
        engine_id: Optional[str] = None,
        force_model_ownership: bool = True,
    ):
        """
        Initialize the engine.

        Args:
            model: The MLX model
            tokenizer: The tokenizer
            config: Engine configuration
            engine_id: Optional unique ID for this engine (auto-generated if None)
            force_model_ownership: If True (default), forcibly take model ownership
                                   from any existing engine. If False, raises
                                   ModelOwnershipError if model is in use.
        """
        self.model = model
        self.tokenizer = tokenizer
        self.config = config or EngineConfig()
        self._generation_gate = get_generation_gate()
        self._engine_id = engine_id or str(uuid.uuid4())
        self._owns_model = False
        self._closed = False
        self._close_lock = threading.Lock()
        self._closing = False

        # Acquire model ownership
        registry = get_registry()
        registry.acquire(
            model=model,
            engine=self,
            engine_id=self._engine_id,
            force=force_model_ownership,
        )
        self._owns_model = True

        # Per-engine executor with dedicated mx.Stream (#1248).
        # Each EngineCore gets its own thread + GPU stream so different
        # models can run scheduler.step() concurrently.
        self._mlx_stream = mx.new_thread_local_stream(mx.default_device())
        self._mlx_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"mlx-engine-{self._engine_id[:8]}",
        )

        # Create scheduler with per-engine stream
        scheduler_config = self.config.scheduler_config or SchedulerConfig()
        self.scheduler = Scheduler(
            model=model,
            tokenizer=tokenizer,
            config=scheduler_config,
            stream=self._mlx_stream,
        )
        self.scheduler._generation_gate = self._generation_gate
        self.scheduler._generation_pacer = self.config.inference_pacer
        self._generation_stop_event = threading.Event()
        self.scheduler._generation_stop_event = self._generation_stop_event
        self._execution_work = None
        self._execution_thread_id = None
        self._execution_registration = None
        coordinator = self.config.inference_coordinator
        if coordinator is not None:
            self._execution_registration = coordinator.register_model(
                self.config.inference_model_id or self._engine_id,
                priority=self.config.inference_priority,
                exclusive=self.config.inference_exclusive,
                drain=self._drain_model_execution,
            )
            configured_pacer = self.config.inference_pacer
            if configured_pacer is not None:
                coordinator.set_model_admission(
                    self.config.inference_model_id or self._engine_id,
                    configured_pacer.share > 0.0,
                )
        self.scheduler._execution_checkpoint = self._execution_checkpoint
        self.scheduler._model_execution_coordinator = coordinator

        # Output collectors for low-latency streaming (vLLM pattern)
        self._output_collectors: Dict[str, RequestOutputCollector] = {}
        self._stream_states: Dict[str, RequestStreamState] = {}
        self._finished_events: Dict[str, asyncio.Event] = {}
        # Finish timestamps for orphan-collector reaping (#1154).
        # Normally a consumer drains and removes its own collector, but if the
        # client disconnects mid-stream the SSE generator chain is abandoned and
        # its cleanup finally only runs at GC time, so the collector lingers and
        # the dashboard keeps showing the request as "Generating".
        self._finished_at: Dict[str, float] = {}
        self._last_reap = 0.0

        # Engine state
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake_event: Optional[asyncio.Event] = None
        self._start_time: Optional[float] = None
        self._steps_executed = 0
        # Executor callables keep this token so a stop/restart cannot revive
        # work that was queued by an earlier engine lifecycle.
        self._lifecycle_generation = 0
        # Set by the asyncio loop only when the pacer actually closes
        # admission; consumed by the next scheduler burst on its owner thread.
        self._pacer_admission_waited = threading.Event()

        # Drop transient aliases after ownership moves to the engine/scheduler
        # graph, so close()/deep_reset() can make that graph unreachable.
        model = None
        tokenizer = None

        # Engine streams submit to one Metal command queue. Busy engines on
        # separate queues can trip a GPU firmware lockup (#4224).
        queue = self._mlx_executor.submit(
            share_command_queue, self._mlx_stream
        ).result()
        if queue is None:
            _note_engine_on_own_queue(self)

        logger.debug(f"Engine {self._engine_id} initialized")

    async def start(self) -> None:
        """Start the engine loop."""
        if self._running:
            return

        self._loop = asyncio.get_running_loop()
        self._wake_event = asyncio.Event()
        self._generation_stop_event = threading.Event()
        self.scheduler._generation_stop_event = self._generation_stop_event
        self._lifecycle_generation += 1
        pacer = self.config.inference_pacer
        if pacer is not None:
            pacer.start_engine(self._engine_id)
        self._running = True
        self._start_time = time.time()
        self._task = asyncio.create_task(self._engine_loop())
        logger.info("Engine started")

    async def stop(self) -> None:
        """Stop the engine loop."""
        self._generation_stop_event.set()
        if self.config.inference_coordinator is not None:
            self.config.inference_coordinator.set_runnable(
                self.config.inference_model_id or self._engine_id, False
            )
        self._lifecycle_generation += 1
        self._running = False
        pacer = self.config.inference_pacer
        if pacer is not None:
            pacer.cancel_engine(self._engine_id)
            pacer.set_engine_busy(self._engine_id, False)
        if self._wake_event is not None:
            self._wake_event.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._wake_event = None
        self._loop = None
        logger.info("Engine stopped")

    def is_running(self) -> bool:
        """Check if engine is running."""
        return self._running

    def _wake_engine_loop(self) -> None:
        """Wake the idle engine loop after scheduler-visible state changes."""
        event = getattr(self, "_wake_event", None)
        loop = getattr(self, "_loop", None)
        if event is None or loop is None or loop.is_closed():
            return
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            event.set()
        else:
            loop.call_soon_threadsafe(event.set)

    def _drain_model_execution(self) -> None:
        """Fence this worker's submitted device work; errors fail handover closed."""

        def drain():
            from .decode_activity import get_decode_activity

            key = getattr(self.scheduler, "_decode_activity_key", None)
            if key is not None:
                get_decode_activity().remove(key)
            mx.synchronize(self._mlx_stream)
            # No tolerant teardown helper: a missing stream is not a drain receipt.
            mx.synchronize()

        if threading.get_ident() == self._execution_thread_id:
            drain()
        else:
            self._mlx_executor.submit(drain).result(timeout=60.0)

    def _execution_context(self, kind="inference", cancelled=None):
        registration = getattr(self, "_execution_registration", None)
        if registration is None:
            return nullcontext(None)
        submission = getattr(self, "_queued_execution_submission", None)
        if submission is not None:
            self._queued_execution_submission = None
            return submission.work(
                stop_event=(
                    None if kind == "maintenance" else self._generation_stop_event
                ),
                cancelled=cancelled,
                kind=kind,
            )
        return registration.work(
            stop_event=None if kind == "maintenance" else self._generation_stop_event,
            cancelled=cancelled,
            kind=kind,
        )

    def _finish_execution_pacing(self) -> None:
        permit = getattr(self, "_execution_pacing_permit", None)
        if permit is None:
            return
        self._execution_pacing_permit = None
        started = getattr(self, "_execution_pacing_started", None)
        intervals = []
        if started is not None and getattr(
            self.scheduler, "_step_inference_work_started", False
        ):
            intervals.append((started, time.monotonic()))
        self.config.inference_pacer.complete(permit, intervals)

    def _acquire_execution_pacing(self, work, cancelled) -> None:
        if (
            not getattr(self, "_execution_controls_pacing", False)
            or work.kind == "maintenance"
        ):
            return
        pacer = self.config.inference_pacer
        if pacer is None or pacer.full_speed_snapshot:
            return

        def waited():
            # No global permit has been granted while its admission callback
            # runs. Model-local-first acquisition cannot hold a global drain.
            self.scheduler.interrupt_inference_timing()

        while True:
            work.raise_if_cancelled()
            while self._generation_gate.paused_snapshot:
                work.checkpoint(cancelled=cancelled)
                self._generation_stop_event.wait(0.01)
            permit = asyncio.run(
                pacer.acquire(
                    self._engine_id,
                    on_admission_wait=waited,
                    cancelled=lambda: work.checkpoint(cancelled=cancelled),
                )
            )
            if permit is PACING_DISABLED:
                return
            if permit is None:
                raise ModelExecutionCancelledError("engine pacing stopped")
            if self._generation_gate.paused_snapshot:
                pacer.complete(permit, [])
                continue
            if pacer.begin_step(permit):
                self._execution_pacing_permit = permit
                self._execution_pacing_started = time.monotonic()
                return
            pacer.complete(permit, [])

    def _execution_checkpoint(self, request_id=None) -> None:
        work = getattr(self, "_execution_work", None)
        if work is None:
            return
        stack = getattr(self, "_execution_stack", None)
        pending = lambda: bool(self.scheduler._pending_abort_ids)
        self._finish_execution_pacing()
        while True:
            try:
                if work.kind == "maintenance" and stack is not None:
                    stack.close()
                    work = stack.enter_context(
                        self._execution_context(cancelled=pending)
                    )
                    work.cancelled = None
                    self._execution_work = work
                    self._execution_contexts.append(work)
                waited = work.checkpoint(cancelled=pending)
                if waited:
                    self.scheduler.interrupt_inference_timing()
                self._acquire_execution_pacing(work, pending)
                return
            except ModelExecutionCancelledError:
                if (
                    stack is None
                    or not pending()
                    or self._generation_stop_event.is_set()
                ):
                    raise
                # Do not run abort/store operations under another model's
                # ownership. Retire this inference scope and reacquire cleanup.
                stack.close()
                work = stack.enter_context(self._execution_context("maintenance"))
                self._execution_work = work
                self._execution_contexts.append(work)
                self.scheduler.interrupt_inference_timing()
                if (
                    request_id is not None
                    and request_id in self.scheduler._pending_abort_ids
                ):
                    return  # the external-prefill abort/store path owns its cache
                self.scheduler.maintenance_step()
                if request_id is None:
                    return  # leave this burst; the next burst requests inference
                # A different request was cancelled during this prefill. Keep
                # its stack/KV state, release cleanup, and await inference again.

    def _run_device_cleanup(self, operation, *args):
        self._execution_thread_id = threading.get_ident()
        with self._execution_context("maintenance"):
            return operation(*args)

    def _run_maintenance(self):
        return self._run_device_cleanup(self.scheduler.maintenance_step)

    def _step_burst(
        self,
        permit=None,
        activity_intervals=None,
        lifecycle_generation=None,
        interrupt_mtp_timing=False,
    ):
        if (
            lifecycle_generation is not None
            and lifecycle_generation != self._lifecycle_generation
        ):
            return []
        self._execution_thread_id = threading.get_ident()
        with ExitStack() as stack:
            try:
                work = stack.enter_context(
                    self._execution_context(
                        cancelled=lambda: bool(self.scheduler._pending_abort_ids)
                    )
                )
            except ModelExecutionCancelledError:
                if self._generation_stop_event.is_set():
                    return []
                self._run_maintenance()
                return []
            if work is not None:
                work.cancelled = None
            self._execution_controls_pacing = work is not None and (
                not work._passthrough
                or (
                    permit is None
                    and self.config.inference_pacer is not None
                    and not self.config.inference_pacer.full_speed_snapshot
                )
            )
            if self._execution_controls_pacing and permit is not None:
                # An opt-in change may race legacy async admission. Retire that
                # admission before ownership can yield; actual work uses fresh
                # owner-local admissions below.
                self.config.inference_pacer.complete(permit, [])
                permit = None
                activity_intervals = None
            self._execution_stack = stack
            self._execution_contexts = [work] if work is not None else []
            self._execution_work = work
            try:
                if work is not None and work.waited_seconds:
                    interrupt_mtp_timing = True
                result = self._step_burst_uncoordinated(
                    permit,
                    activity_intervals,
                    lifecycle_generation,
                    interrupt_mtp_timing,
                )
            finally:
                self._finish_execution_pacing()
                self._execution_controls_pacing = False
                self._execution_work = None
                self._execution_stack = None
                if work is not None and activity_intervals is not None:
                    # A step can park mid-prefill. Charge only the pieces outside
                    # coordinator waits to both global and per-model controllers.
                    waits = [
                        interval
                        for context in self._execution_contexts
                        for interval in context.wait_intervals
                    ]
                    for start, end in waits:
                        remaining = []
                        for left, right in activity_intervals:
                            if end <= left or start >= right:
                                remaining.append((left, right))
                            else:
                                if left < start:
                                    remaining.append((left, start))
                                if end < right:
                                    remaining.append((end, right))
                        activity_intervals[:] = remaining
            return result

    def _step_burst_uncoordinated(
        self,
        permit: PacingPermit | None = None,
        activity_intervals: list[tuple[float, float]] | None = None,
        lifecycle_generation: int | None = None,
        interrupt_mtp_timing: bool = False,
    ) -> list:
        """Run scheduler.step() several times in one executor hand-off.

        Each decode token otherwise bounces back to the event loop, which
        ping-pongs the GIL with the asyncio loop + uvicorn on the main thread
        (~1ms/token of contention). Chaining a few steps lets the MLX thread
        hold the GIL continuously (in-process sync loop hits ~80 tok/s vs ~74
        through the per-token async hand-off).

        scheduler.step() services aborts/admission/finish every step, so
        correctness is unchanged; the only cost is event-loop responsiveness,
        bounded by decode_burst_budget_s. Stops early when no work remains, a
        request produces its first chunk, a prefill eviction needs the (async)
        callback, or the budget elapses —
        the budget also ends the burst when a slow prefill-chunk step lands.

        Runs on the MLX executor thread. Returns the SchedulerOutputs in order.
        """
        pacer = self.config.inference_pacer

        def step_once():
            self._execution_checkpoint()
            work = getattr(self, "_execution_work", None)
            if work is not None and work.kind == "maintenance":
                return None
            if self._generation_gate.paused_snapshot:
                return None
            nonlocal activity_intervals, interrupt_mtp_timing
            if (
                lifecycle_generation is not None
                and lifecycle_generation != self._lifecycle_generation
            ):
                return None
            if pacer is not None and pacer.full_speed_snapshot:
                # A live switch to full speed ends old-generation accounting
                # immediately. Lifecycle cancellation remains checked above.
                activity_intervals = None
            elif (
                permit is not None
                and pacer is not None
                and not pacer.begin_step(permit)
            ):
                return None
            if interrupt_mtp_timing:
                interrupt_timing = getattr(
                    self.scheduler, "interrupt_inference_timing", None
                )
                if interrupt_timing is not None:
                    interrupt_timing()
                self._pacer_admission_waited.clear()
                interrupt_mtp_timing = False
            started = time.monotonic() if activity_intervals is not None else 0.0
            try:
                output = self.scheduler.step()
                return output
            finally:
                self._finish_execution_pacing()
                if activity_intervals is not None and bool(
                    getattr(self.scheduler, "_step_inference_work_started", False)
                ):
                    activity_intervals.append((started, time.monotonic()))

        first = step_once()
        if first is None:
            return []
        max_steps = self.config.decode_burst_max_steps
        outputs = [first]
        if max_steps <= 1:
            return outputs
        # Adaptive budget: single active request -> aggressive (nothing else to
        # stay responsive to); concurrent -> tight to keep admission/abort low.
        running = getattr(self.scheduler, "running", None)
        single = running is None or len(running) <= 1
        budget = (
            self.config.decode_burst_budget_single_s
            if single
            else self.config.decode_burst_budget_s
        )
        if budget <= 0:
            return outputs
        deadline = time.monotonic() + budget
        while len(outputs) < max_steps:
            last = outputs[-1]
            # Also release the first chunk of a request admitted mid-burst.
            # Comparing cumulative and new tokens covers multi-token steps
            # without per-request tracking. Later chunks retain normal bursts.
            if any(
                item.new_token_ids
                and item.completion_tokens == len(item.new_token_ids)
                for item in last.outputs
            ):
                break
            if (
                not last.has_work  # throttled/idle: stop and let the loop wait
                or not self.scheduler.has_requests()
                or last.prefill_eviction_request is not None
                or time.monotonic() >= deadline
            ):
                break
            next_output = step_once()
            if next_output is None:
                break
            outputs.append(next_output)
        return outputs

    def _enqueue_execution(self, operation, args, submission):
        def worker():
            self._execution_thread_id = threading.get_ident()
            self._queued_execution_submission = submission
            try:
                return operation(*args)
            finally:
                self._queued_execution_submission = None
                if submission is not None:
                    submission.release()

        try:
            future = self._mlx_executor.submit(worker)
        except BaseException:
            if submission is not None:
                submission.release()
            raise
        # If the queued future is cancelled before the callable begins, there
        # is no worker finally to retire its reservation.
        if submission is not None:
            future.add_done_callback(
                lambda done: submission.release() if done.cancelled() else None
            )
        return future

    async def _submit_model_worker(self, operation, *args):
        registration = getattr(self, "_execution_registration", None)
        submission = None
        if registration is not None:
            while submission is None:
                submission = registration.reserve_submission()
                if submission is None:
                    await asyncio.sleep(0.001)
        return self._enqueue_execution(operation, args, submission)

    def _submit_model_worker_sync(self, operation, *args):
        registration = getattr(self, "_execution_registration", None)
        submission = None
        if registration is not None:
            while submission is None:
                submission = registration.reserve_submission()
                if submission is None:
                    time.sleep(0.001)
        return self._enqueue_execution(operation, args, submission)

    async def _await_model_worker(self, loop, operation, *args):
        concurrent_future = await self._submit_model_worker(operation, *args)
        future = asyncio.wrap_future(concurrent_future, loop=loop)
        future.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        return await asyncio.shield(future)

    async def _submit_execution_burst(self, loop, *args):
        return await self._await_model_worker(loop, self._step_burst, *args)

    async def _engine_loop(self) -> None:
        """Main engine loop - runs scheduler steps on the MLX executor.

        All scheduler steps run on _mlx_executor (single-worker thread), so
        this engine's MLX GPU operations are never concurrent.  VLM vision
        encoding also runs on the same executor, so inline scheduler.step()
        on the event loop would race with vision mx.eval() and segfault.
        """
        loop = asyncio.get_running_loop()

        step_interval = self.config.step_interval
        stream_interval = self.config.stream_interval
        use_simple_streaming = stream_interval == 1
        admission_waited = self._pacer_admission_waited.is_set()

        def note_admission_wait() -> None:
            nonlocal admission_waited
            admission_waited = True
            self._pacer_admission_waited.set()

        while self._running:
            try:
                # Sweep collectors orphaned by client disconnects (throttled).
                now = time.monotonic()
                if now - self._last_reap >= 1.0:
                    self._last_reap = now
                    self._reap_orphaned_collectors(now)

                coordinator = self.config.inference_coordinator
                if coordinator is not None:
                    coordinator.set_runnable(
                        self.config.inference_model_id or self._engine_id,
                        bool(
                            getattr(self.scheduler, "waiting", None)
                            or getattr(self.scheduler, "running", None)
                        ),
                    )
                if self.scheduler.has_requests():
                    gate = self._generation_gate
                    if gate.paused_snapshot:
                        event = self._wake_event
                        if event is not None:
                            event.clear()
                        await self._await_model_worker(loop, self._run_maintenance)
                        await gate.wait(event)
                        continue
                    pacer = self.config.inference_pacer
                    permit = None
                    if (
                        pacer is not None
                        and not pacer.full_speed_snapshot
                        and pacer.share <= 0.0
                    ):
                        # A model/user pause stops generation before queueing a
                        # worker. The pacer's wake event also lets request
                        # cancellation and maintenance continue promptly.
                        event = self._wake_event
                        if event is not None:
                            event.clear()
                        paused_admission = await pacer.acquire(
                            self._engine_id, wake_event=event
                        )
                        if paused_admission is None:
                            if not self._running:
                                break
                            await self._await_model_worker(loop, self._run_maintenance)
                            continue
                        if paused_admission is not PACING_DISABLED:
                            pacer.complete(paused_admission, [])
                        if pacer.share <= 0.0:
                            continue
                    if pacer is not None and not pacer.full_speed_snapshot:
                        pacer.set_engine_busy(self._engine_id, True)
                        if coordinator is not None:
                            acquisition = PACING_DISABLED
                        else:
                            acquisition = await pacer.acquire(
                                self._engine_id,
                                wake_event=self._wake_event,
                                on_admission_wait=note_admission_wait,
                            )
                        if acquisition is PACING_DISABLED:
                            # Full mode was published while we waited. Submit
                            # the ordinary scheduler burst, not maintenance.
                            if not self._running:
                                break
                            permit = None
                        elif acquisition is None:
                            if not self._running:
                                break
                            event = self._wake_event
                            if event is not None:
                                event.clear()
                            await self._await_model_worker(loop, self._run_maintenance)
                            continue
                        else:
                            permit = acquisition

                    lifecycle_generation = self._lifecycle_generation

                    if permit is None:
                        # Preserve the ordinary executor path at full speed:
                        # no permit wrapper, timing list, or completion work.
                        if pacer is None:
                            step_outputs = await self._submit_execution_burst(loop)
                        elif admission_waited:
                            step_outputs = await self._submit_execution_burst(
                                loop, None, None, lifecycle_generation, True
                            )
                        else:
                            step_outputs = await self._submit_execution_burst(
                                loop, None, None, lifecycle_generation
                            )
                    else:

                        def run_burst(
                            burst_permit=permit,
                            burst_pacer=pacer,
                            burst_lifecycle_generation=lifecycle_generation,
                            burst_interrupt_mtp_timing=admission_waited,
                        ):
                            intervals: list[tuple[float, float]] = []
                            try:
                                return self._step_burst(
                                    burst_permit,
                                    intervals,
                                    burst_lifecycle_generation,
                                    burst_interrupt_mtp_timing,
                                )
                            finally:
                                if burst_pacer is not None:
                                    burst_pacer.complete(burst_permit, intervals)

                        # Shield the executor future: cancelling this loop task
                        # cannot cancel a queued callable before its `finally`
                        # releases the registered permit.
                        concurrent_future = await self._submit_model_worker(run_burst)
                        wrapped_future = asyncio.wrap_future(
                            concurrent_future, loop=loop
                        )
                        # If shutdown cancels this loop task, still retrieve a
                        # later worker exception after the shielded job completes.
                        wrapped_future.add_done_callback(
                            lambda future: (
                                None if future.cancelled() else future.exception()
                            )
                        )
                        step_outputs = await asyncio.shield(wrapped_future)
                    if not step_outputs:
                        continue
                    admission_waited = False
                    self._steps_executed += len(step_outputs)

                    # Distribute every step's outputs to collectors (one or
                    # more decode tokens per burst). collector.put() runs on the
                    # loop thread, keeping the asyncio.Event signalling
                    # thread-safe and per-token streaming intact.
                    collectors = self._output_collectors
                    states = self._stream_states
                    eviction_request = None
                    distributed = False

                    for output in step_outputs:
                        if (
                            eviction_request is None
                            and output.prefill_eviction_request is not None
                        ):
                            eviction_request = output.prefill_eviction_request

                        outputs = output.outputs
                        if not outputs:
                            continue
                        distributed = True

                        for req_output in outputs:
                            rid = req_output.request_id
                            collector = collectors.get(rid)

                            if collector is not None:
                                # Optimized: skip stream_interval check when interval=1
                                if use_simple_streaming:
                                    collector.put(req_output)
                                else:
                                    state = states.get(rid)
                                    if state and state.should_send(
                                        req_output.completion_tokens,
                                        req_output.finished,
                                    ):
                                        collector.put(req_output)
                                        state.mark_sent(req_output.completion_tokens)

                            if req_output.finished:
                                self._mark_request_finished(rid)
                                # Cleanup normally happens in the consumer
                                # (stream_outputs()/generate()); collectors left
                                # behind by a disconnected client are swept by
                                # _reap_orphaned_collectors() via _finished_at.

                    if distributed:
                        # Always yield to prevent event loop starvation.
                        # Without this, orphaned requests (client disconnected but
                        # request still in scheduler) block the entire event loop,
                        # making the server unresponsive to all HTTP requests.
                        await asyncio.sleep(0)

                    if eviction_request is not None:
                        callback = self.config.prefill_eviction_callback
                        if callback is not None:
                            logger.info(
                                "Running prefill LRU eviction for request %s",
                                eviction_request.request_id,
                            )
                            evicted = await callback(eviction_request)
                            if evicted:
                                logger.info(
                                    "Prefill LRU eviction completed for request %s",
                                    eviction_request.request_id,
                                )
                            else:
                                logger.info(
                                    "No idle model evicted for request %s; "
                                    "scheduler will fall back to throttling",
                                    eviction_request.request_id,
                                )
                        else:
                            logger.debug(
                                "Prefill eviction requested for %s but no callback "
                                "is configured",
                                eviction_request.request_id,
                            )
                        continue
                    if not step_outputs[-1].has_work:
                        # Requests may be queued while scheduler admission is
                        # intentionally throttled by async cache cleanup. Avoid
                        # spinning the engine loop, but still let new requests
                        # wake the wait immediately.
                        event = self._wake_event
                        if event is None:
                            await asyncio.sleep(step_interval)
                        else:
                            event.clear()
                            with suppress(TimeoutError):
                                await asyncio.wait_for(
                                    event.wait(), timeout=step_interval
                                )
                else:
                    pacer = self.config.inference_pacer
                    if pacer is not None and not pacer.full_speed_snapshot:
                        pacer.set_engine_busy(self._engine_id, False)
                    event = self._wake_event
                    if event is None:
                        await asyncio.sleep(step_interval)
                    else:
                        event.clear()
                        # Avoid losing a request that arrived between
                        # has_requests() and clear().
                        if self.scheduler.has_requests():
                            continue
                        with suppress(TimeoutError):
                            await asyncio.wait_for(event.wait(), timeout=step_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                exit_if_gpu_submissions_ignored(e)
                import traceback

                logger.error(f"Engine loop error: {e}\n{traceback.format_exc()}")
                # Fail all requests and remove from scheduler to prevent
                # infinite loop (has_requests() must go False; a pending
                # idle reclaim may hold it True for one extra step, which
                # drains and clears it).
                failed_ids = await self._await_model_worker(
                    loop, self._run_device_cleanup, self.scheduler.fail_all_requests
                )
                for rid in failed_ids:
                    collector = self._output_collectors.get(rid)
                    if collector is not None:
                        collector.put(
                            RequestOutput(
                                request_id=rid,
                                finished=True,
                                finish_reason="error",
                                error=str(e),
                            )
                        )
                    self._mark_request_finished(rid)
                await asyncio.sleep(0.1)

    async def add_request(
        self,
        prompt: Union[str, List[int]],
        sampling_params: Optional[SamplingParams] = None,
        request_id: Optional[str] = None,
        images: Optional[List[Any]] = None,
        videos: Optional[List[Any]] = None,
        vlm_inputs_embeds: Optional[Any] = None,
        vlm_extra_kwargs: Optional[Dict[str, Any]] = None,
        vlm_image_hash: Optional[str] = None,
        vlm_cache_key_start: int = 0,
        vlm_cache_key_ranges: Optional[List[Tuple[int, str]]] = None,
        specprefill: Optional[bool] = None,
        specprefill_keep_pct: Optional[float] = None,
        specprefill_threshold: Optional[int] = None,
        specprefill_system_end: Optional[int] = None,
        generation_prompt_text: Optional[str] = None,
        generation_prompt_persists: bool = False,
        skip_cache_store: bool = False,
        preserve_reasoning: bool = False,
        benchmark_trace: bool = False,
        benchmark_ane_sequence_length: int = 0,
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        """
        Add a request for processing.

        Args:
            prompt: Input prompt (string or token IDs)
            sampling_params: Generation parameters
            request_id: Optional custom request ID
            images: Optional images for multimodal
            videos: Optional videos for multimodal
            vlm_inputs_embeds: Pre-computed vision+text embeddings for VLM
            vlm_extra_kwargs: Model-specific VLM kwargs (e.g., position_ids)
            vlm_image_hash: SHA256 hash of images for prefix cache
            specprefill: Per-request SpecPrefill override (True/False/None)
            specprefill_keep_pct: Per-request keep rate override
            specprefill_threshold: Per-request threshold override (min tokens)

        Returns:
            The request ID
        """
        self._generation_gate.check_admission()
        if request_id is None:
            request_id = str(uuid.uuid4())

        if sampling_params is None:
            sampling_params = SamplingParams()

        request = Request(
            request_id=request_id,
            prompt=prompt,
            sampling_params=sampling_params,
            tools=tools,
            images=images,
            videos=videos,
            vlm_inputs_embeds=vlm_inputs_embeds,
            vlm_extra_kwargs=vlm_extra_kwargs,
            vlm_image_hash=vlm_image_hash,
            vlm_cache_key_start=vlm_cache_key_start,
            vlm_cache_key_ranges=vlm_cache_key_ranges,
            skip_cache_store=skip_cache_store,
            preserve_reasoning=preserve_reasoning,
            benchmark_trace=benchmark_trace,
            benchmark_ane_sequence_length=benchmark_ane_sequence_length,
        )

        # SpecPrefill: resolve per-request settings.
        # The scheduler checks _specprefill_enabled to decide whether to score.
        if specprefill is not None:
            request._specprefill_enabled = specprefill
        elif self.scheduler._specprefill_draft_model is not None:
            # Draft model is loaded → enable by default
            request._specprefill_enabled = True
        if specprefill_keep_pct is not None:
            request._specprefill_keep_pct = specprefill_keep_pct
        if specprefill_threshold is not None:
            request._specprefill_threshold = specprefill_threshold
        if specprefill_system_end is not None and specprefill_system_end > 0:
            request.specprefill_system_end = specprefill_system_end
        if generation_prompt_text:
            request.generation_prompt_text = generation_prompt_text
            request.generation_prompt_persists = bool(generation_prompt_persists)

        # Setup output collector with stream_interval from config
        self._output_collectors[request_id] = RequestOutputCollector(aggregate=True)
        self._stream_states[request_id] = RequestStreamState(
            stream_interval=self.config.stream_interval
        )
        self._finished_events[request_id] = asyncio.Event()

        # Add to scheduler — route through the MLX executor so that
        # prefix cache reconstruction (mx.load, mx.concatenate) never
        # races with scheduler.step() on the Metal stream.  See #95.
        #
        # The scheduler may raise (PrefillMemoryExceededError, or other
        # validation errors) before the request enters self.waiting. In
        # that case the consumer in stream_outputs / generate never sees
        # the request_id and its finally-block cleanup never fires —
        # without the explicit cleanup below the per-rejection leak
        # accumulates one collector + one stream_state + one
        # asyncio.Event per refused request. Re-raise after cleanup so
        # the typed exception still reaches the FastAPI 400 handler.
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                self._mlx_executor, self.scheduler.add_request, request
            )
        except BaseException:
            # If the caller is cancelled here (e.g. the client disconnected
            # before the SSE stream began) — or the insert fails — the request
            # never reaches stream_outputs()/generate()'s try/finally, so
            # nothing would mark it finished or clean it up. The collector
            # created above would then linger forever as a phantom the reaper
            # cannot see (it was never stamped _finished_at), and the dashboard
            # would show it as "Generating" indefinitely (#1154).
            # Drop the tracking and abort any partial scheduler insert (the
            # deferred abort is idempotent and harmless if it never landed).
            try:
                self.scheduler.abort_request(request_id)
            except Exception as abort_exc:  # noqa: BLE001
                logger.debug(
                    f"Abort of partial insert for {request_id} failed: {abort_exc}"
                )
            self._cleanup_request(request_id)
            raise
        self._wake_engine_loop()

        return request_id

    async def run_model_preparation(self, operation, *args):
        """Pace vision preparation on the same coordinator as decode work."""
        gate = self._generation_gate
        gate.check_admission()
        pacer = self.config.inference_pacer
        engine_id = f"{self._engine_id}:vision:{uuid.uuid4()}"
        deferred = object()
        cancelled = threading.Event()
        loop = asyncio.get_running_loop()
        if pacer is not None:
            pacer.start_engine(engine_id)
            pacer.set_engine_busy(engine_id, True)
        try:
            while True:
                stop = getattr(self, "_generation_stop_event", None)
                if getattr(self, "_closed", False) or (
                    stop is not None and stop.is_set()
                ):
                    raise RuntimeError(
                        "Generation engine stopped during vision preparation"
                    )
                while gate.paused_snapshot:
                    await gate.wait()
                    if getattr(self, "_closed", False) or (
                        stop is not None and stop.is_set()
                    ):
                        raise RuntimeError(
                            "Generation engine stopped during vision preparation"
                        )
                permit = None
                coordinated = self.config.inference_coordinator is not None
                if (
                    pacer is not None
                    and not pacer.full_speed_snapshot
                    and not coordinated
                ):
                    acquired = await pacer.acquire(engine_id)
                    if acquired is None:
                        continue
                    if acquired is not PACING_DISABLED:
                        permit = acquired

                def uncoordinated_work(work_permit=permit):
                    intervals = []
                    try:
                        if cancelled.is_set():
                            return deferred
                        if getattr(self, "_closed", False) or (
                            stop is not None and stop.is_set()
                        ):
                            raise RuntimeError(
                                "Generation engine stopped during vision preparation"
                            )
                        if gate.paused_snapshot:
                            return deferred
                        if work_permit is not None and not pacer.begin_step(
                            work_permit
                        ):
                            return deferred
                        started = time.monotonic()
                        try:
                            return operation(*args)
                        finally:
                            intervals.append((started, time.monotonic()))
                    finally:
                        if work_permit is not None:
                            pacer.complete(work_permit, intervals)

                def work():
                    self._execution_thread_id = threading.get_ident()
                    with self._execution_context(
                        "vision", cancelled=cancelled.is_set
                    ) as owner:
                        self._execution_controls_pacing = owner is not None and (
                            not owner._passthrough
                            or (
                                permit is None
                                and pacer is not None
                                and not pacer.full_speed_snapshot
                            )
                        )
                        try:
                            if self._execution_controls_pacing:
                                if permit is not None:
                                    pacer.complete(permit, [])
                                self._acquire_execution_pacing(owner, cancelled.is_set)
                            result = (
                                uncoordinated_work(None)
                                if self._execution_controls_pacing
                                else uncoordinated_work()
                            )
                            return result
                        finally:
                            # Preparation does useful model work outside scheduler.step.
                            if (
                                getattr(self, "_execution_pacing_permit", None)
                                is not None
                            ):
                                self.scheduler._step_inference_work_started = True
                            self._finish_execution_pacing()
                            self._execution_controls_pacing = False

                try:
                    submitted = await self._submit_model_worker(work)
                except BaseException:
                    if permit is not None:
                        pacer.complete(permit, [])
                    raise
                future = asyncio.wrap_future(submitted, loop=loop)
                future.add_done_callback(
                    lambda done: None if done.cancelled() else done.exception()
                )
                result = await asyncio.shield(future)
                if result is not deferred:
                    return result
        finally:
            cancelled.set()
            if pacer is not None:
                pacer.set_engine_busy(engine_id, False)
                pacer.retire_engine(engine_id)

    async def abort_request(self, request_id: str) -> bool:
        """Abort a request.

        Uses deferred abort pattern: scheduler.abort_request() just enqueues
        the request ID into a thread-safe set. The actual abort is processed
        at the start of the next scheduler.step() call, ensuring it runs in
        the same execution context as generation (no race conditions).

        Signals the consumer (stream_outputs/generate) with an abort error
        so it can exit gracefully. Cleanup is handled by the consumer's
        finally block, NOT here -- calling _cleanup_request() immediately
        after put() would clear the output before the consumer can drain it.
        """
        scheduler = getattr(self, "scheduler", None)
        if getattr(self, "_closed", False) or scheduler is None:
            logger.debug(
                "Skipping abort for request %s because engine is already closed",
                request_id,
            )
            return False

        # A client cancel keeps the computed prefix for a resend.
        result = scheduler.abort_request(request_id, keep_cache=True)

        # Signal consumer with abort error so any waiting
        # stream_outputs() / generate() can exit gracefully.
        # Matches abort_all_requests() pattern.
        collector = self._output_collectors.get(request_id)
        if collector is not None:
            collector.put(
                RequestOutput(
                    request_id=request_id,
                    finished=True,
                    finish_reason="abort",
                    error="Request aborted",
                )
            )
        self._mark_request_finished(request_id)
        self._wake_engine_loop()

        return result

    async def abort_all_requests(
        self,
        *,
        reason: str | None = None,
        error_code: str | None = None,
    ) -> int:
        """Abort all active requests without stopping the engine.

        Sends error output to all active collectors and marks requests
        for deferred abort in the scheduler. Cleanup is handled by
        the consumer (stream_outputs/generate). Without an explicit reason,
        preserve the memory-pressure error used by the process enforcer.
        """
        from .utils.proc_memory import get_phys_footprint

        # Only abort requests not already finished or pending abort. The
        # enforcer calls this once per poll (1s) while pressure persists; a
        # deferred abort can take many seconds to drain (it executes at the
        # next step()/chunk boundary), so without this filter the same
        # request is re-aborted every poll: duplicate [Error: ...] frames
        # reach streaming clients and the returned count reports stale
        # aborts as fresh progress, hiding a stuck scheduler from the
        # enforcer's escalation logic.
        pending_aborts: set[str] = set()
        sched_for_filter = self.scheduler
        if sched_for_filter is not None:
            pending_aborts = set(
                getattr(sched_for_filter, "_pending_abort_ids", None) or ()
            )
        request_ids = [
            rid
            for rid in self._output_collectors
            if rid not in self._finished_at and rid not in pending_aborts
        ]
        ceiling = 0
        watermark = 0
        sched = self.scheduler
        if reason is None and sched is not None:
            ceiling = int(getattr(sched, "_memory_hard_limit_bytes", 0) or 0)
            watermark = int(getattr(sched, "_memory_hard_watermark_bytes", 0) or 0)
        usage = get_phys_footprint() if reason is None else 0
        usage_gb = usage / (1024**3)
        ceiling_gb = ceiling / (1024**3) if ceiling > 0 else 0.0
        watermark_gb = watermark / (1024**3) if watermark > 0 else 0.0
        # Name the component ceiling that actually produced this abort. A
        # generic "loosen memory_guard_tier" leaves users who are already on
        # `aggressive` with nothing to try (#2362); the ladder points at the
        # constraint that is really binding, or falls back to the same
        # generic advice when the enforcer has not propagated a breakdown.
        binding = "effective"
        advice = ""
        if reason is None:
            binding, advice = describe_ceiling_binding(
                static=int(getattr(sched, "_memory_static_ceiling_bytes", 0) or 0),
                dynamic=int(getattr(sched, "_memory_dynamic_ceiling_bytes", 0) or 0),
                metal_cap=int(getattr(sched, "_memory_metal_cap_bytes", 0) or 0),
                tier=str(getattr(sched, "_memory_guard_tier", "") or ""),
                current=usage,
                fmt=format_bytes,
                tail="reduce context length",
            )
            advice = f"{advice}."
        ceiling_label = f"{binding} ceiling" if binding != "effective" else "ceiling"
        for rid in request_ids:
            self.scheduler.abort_request(rid)
            collector = self._output_collectors.get(rid)
            if collector is not None:
                # Name the watermark that actually tripped; printing only the
                # ceiling reads as "usage below limit yet aborted" (#2321).
                if reason is not None:
                    error_msg = reason
                elif watermark > 0 and ceiling > 0:
                    error_msg = (
                        f"Request aborted: process memory limit exceeded "
                        f"(usage {usage_gb:.1f} GB, abort threshold "
                        f"(hard watermark) {watermark_gb:.1f} GB, "
                        f"{ceiling_label} {ceiling_gb:.1f} GB). "
                        f"{advice}"
                    )
                elif ceiling > 0:
                    error_msg = (
                        f"Request aborted: process memory limit exceeded "
                        f"(usage {usage_gb:.1f} GB, "
                        f"{ceiling_label} {ceiling_gb:.1f} GB). "
                        f"{advice}"
                    )
                else:
                    error_msg = (
                        f"Request aborted: process memory limit exceeded "
                        f"(usage {usage_gb:.1f} GB). "
                        f"{advice}"
                    )
                collector.put(
                    RequestOutput(
                        request_id=rid,
                        finished=True,
                        finish_reason="error",
                        new_text=f"\n\n[Error: {error_msg}]",
                        error=error_msg,
                        # Without a code this surfaced as a bare RuntimeError:
                        # the JSON keepalive wrapper never saw a memory error,
                        # so the response generator died mid-body and the
                        # client got a truncated read plus a 500 traceback
                        # instead of the same actionable 400 the pre-flight
                        # guard returns.
                        error_code=error_code or "prefill_memory_aborted",
                        error_metadata={
                            "request_id": rid,
                            # Only the limit is reported: the exception's
                            # estimated_bytes means "predicted peak", and this
                            # abort fires on measured usage, which the message
                            # already states.
                            "limit_bytes": (
                                watermark or ceiling or None
                                if reason is None
                                else None
                            ),
                        },
                    )
                )
            self._mark_request_finished(rid)
        if request_ids:
            if reason is None:
                logger.warning(
                    f"Aborted {len(request_ids)} requests due to memory pressure"
                )
            else:
                logger.warning("Aborted %d requests: %s", len(request_ids), reason)
            self._wake_engine_loop()
        return len(request_ids)

    def _mark_request_finished(self, request_id: str) -> None:
        """Signal the consumer a request finished and stamp the finish time.

        The timestamp lets _reap_orphaned_collectors() drop collectors whose
        consumer never cleaned up (e.g. the client disconnected mid-stream and
        the SSE generator chain was abandoned rather than closed).
        """
        self._finished_at.setdefault(request_id, time.monotonic())
        event = self._finished_events.get(request_id)
        if event is not None:
            event.set()

    def _reap_orphaned_collectors(self, now: float, grace: float = 5.0) -> int:
        """Drop tracking for finished requests whose consumer never cleaned up.

        stream_outputs()/generate() normally remove their own collector via
        _cleanup_request() once the final output is drained. But when a client
        disconnects mid-stream the SSE generator chain is abandoned instead of
        closed, so that finally block only runs at non-deterministic GC time —
        the collector lingers in _output_collectors and the request shows as
        "Generating" forever on the dashboard (#1154).

        This sweep removes the dict tracking for any request finished more than
        ``grace`` seconds ago. It is intentionally pop-only and never calls
        ``collector.clear()``: stream_outputs()/generate() hold their own
        reference to the collector object, so dropping the dict entry cannot
        truncate a slow-but-live consumer's output — only an over-eager clear()
        could (which is what made the earlier _delayed_cleanup() approach race).
        The grace period guarantees a live consumer (which drains in the same
        event-loop turn the request finishes) has already self-cleaned.
        """
        if not self._finished_at:
            return 0
        stale = [rid for rid, ts in self._finished_at.items() if now - ts >= grace]
        for rid in stale:
            # pop-only — see docstring; never clear() the collector object.
            self._output_collectors.pop(rid, None)
            self._stream_states.pop(rid, None)
            self._finished_events.pop(rid, None)
            self._finished_at.pop(rid, None)
        if stale:
            logger.debug(
                "Reaped %d orphaned output collector(s) after disconnect: %s",
                len(stale),
                stale,
            )
        return len(stale)

    def _cleanup_request(self, request_id: str) -> None:
        """Clean up request tracking.

        Only cleans engine-core level state (collectors, events).
        Scheduler state is cleaned by _do_abort_request (deferred abort)
        or _cleanup_finished (normal completion).
        """
        collector = self._output_collectors.pop(request_id, None)
        if collector:
            collector.clear()
        self._stream_states.pop(request_id, None)
        self._finished_events.pop(request_id, None)
        self._finished_at.pop(request_id, None)

    async def _delayed_cleanup(self, request_id: str, delay: float = 5.0) -> None:
        """
        Cleanup request after delay if not already cleaned.

        This handles the case where a client disconnects before consuming
        the stream_outputs() generator, which would prevent the finally
        block from running.
        """
        await asyncio.sleep(delay)
        if request_id in self._output_collectors:
            logger.debug(f"Delayed cleanup for request {request_id}")
            self._cleanup_request(request_id)

    async def stream_outputs(
        self,
        request_id: str,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[RequestOutput]:
        """
        Stream outputs for a request with low-latency non-blocking pattern.

        Uses the vLLM pattern: get_nowait() or await get()
        This avoids unnecessary task switches when output is available.

        Args:
            request_id: The request ID
            timeout: Optional timeout in seconds

        Yields:
            RequestOutput objects as tokens are generated
        """
        collector = self._output_collectors.get(request_id)
        if collector is None:
            # Request might not be added yet or already cleaned up
            return

        try:
            while True:
                try:
                    # Non-blocking drain pattern from vLLM
                    # Try get_nowait first to avoid task switch if output ready
                    if timeout:
                        output = collector.get_nowait()
                        if output is None:
                            output = await asyncio.wait_for(
                                collector.get(), timeout=timeout
                            )
                    else:
                        output = collector.get_nowait() or await collector.get()

                    yield output

                    if output.error:
                        _raise_request_output_error(output)

                    if output.finished:
                        break

                except asyncio.TimeoutError:
                    logger.warning(f"Timeout waiting for request {request_id}")
                    break

        finally:
            self._cleanup_request(request_id)

    async def generate(
        self,
        prompt: Union[str, List[int]],
        sampling_params: Optional[SamplingParams] = None,
        request_id: Optional[str] = None,
        **kwargs,
    ) -> RequestOutput:
        """
        Generate a complete response (non-streaming).

        This method is optimized to avoid streaming overhead when
        you only need the final result.

        Args:
            prompt: Input prompt
            sampling_params: Generation parameters
            request_id: Optional request ID

        Returns:
            Final RequestOutput with complete text
        """
        request_id = await self.add_request(
            prompt=prompt,
            sampling_params=sampling_params,
            request_id=request_id,
            **kwargs,
        )

        # Wait for completion using event instead of streaming
        # This avoids the waiting_consumer tracking overhead
        event = self._finished_events.get(request_id)
        if event is None:
            raise RuntimeError(f"No event for request {request_id}")

        # Capture the collector reference BEFORE awaiting, mirroring
        # stream_outputs(): the orphan reaper is pop-only and may drop the dict
        # entry once the request is finished, but a held reference still drains.
        # Re-fetching after the await would race the reaper if this coroutine is
        # starved past the grace window under heavy load (#1154).
        collector = self._output_collectors.get(request_id)
        if collector is None:
            raise RuntimeError(f"No collector for request {request_id}")

        try:
            # Wait for the request to finish
            await event.wait()
        except asyncio.CancelledError:
            # Client disconnected or task was cancelled - abort the request
            # to free scheduler/GPU resources (prevents orphaned requests)
            logger.info(f"Request {request_id} cancelled, aborting")
            await self.abort_request(request_id)
            self._cleanup_request(request_id)
            raise

        # Drain all outputs and get the last one (using the captured reference)
        final_output = None
        first_token_at = None
        while True:
            output = collector.get_nowait()
            if output is None:
                break
            if first_token_at is None and output.generated_at is not None:
                first_token_at = output.generated_at
            final_output = output

        # Cleanup
        self._cleanup_request(request_id)

        if final_output is None:
            raise RuntimeError(f"No output for request {request_id}")

        if final_output.error:
            _raise_request_output_error(final_output)

        final_output.first_token_at = first_token_at
        return final_output

    def generate_batch_sync(self, prompts, sampling_params=None):
        if self._running and self.scheduler.has_requests():
            raise RuntimeError(
                "Synchronous generation cannot race an active async batch"
            )

        def generate():
            self._execution_thread_id = threading.get_ident()
            with self._execution_context("sync") as work:
                self._execution_work = work
                self._execution_controls_pacing = (
                    work is not None and not work._passthrough
                )
                try:
                    return self._generate_batch_sync_worker(prompts, sampling_params)
                finally:
                    self._finish_execution_pacing()
                    self._execution_controls_pacing = False
                    self._execution_work = None

        if threading.get_ident() == self._execution_thread_id:
            return generate()
        return self._submit_model_worker_sync(generate).result()

    def _generate_batch_sync_worker(
        self,
        prompts: List[Union[str, List[int]]],
        sampling_params: Optional[SamplingParams] = None,
    ) -> List[RequestOutput]:
        """
        Generate responses synchronously for maximum throughput.

        This bypasses the async engine loop entirely, running the scheduler
        directly for optimal batching performance. Use this when you don't
        need streaming and want maximum throughput.

        Args:
            prompts: List of input prompts
            sampling_params: Generation parameters (same for all)

        Returns:
            List of RequestOutput in same order as prompts
        """
        import uuid as uuid_module

        from .request import Request

        self._generation_gate.check_admission()

        if sampling_params is None:
            sampling_params = SamplingParams()

        # Add all requests to scheduler
        request_ids = []
        for prompt in prompts:
            request_id = str(uuid_module.uuid4())
            request = Request(
                request_id=request_id,
                prompt=prompt,
                sampling_params=sampling_params,
            )
            self.scheduler.add_request(request)
            request_ids.append(request_id)

        # Process until all done - direct scheduler access, no async overhead
        results: Dict[str, RequestOutput] = {}
        while self.scheduler.has_requests():
            self._execution_checkpoint()
            if (
                self._generation_gate.paused_snapshot
                and getattr(self, "_execution_work", None) is not None
            ):
                self._finish_execution_pacing()
                while (
                    self._generation_gate.paused_snapshot
                    and not self._generation_stop_event.is_set()
                ):
                    self._execution_checkpoint()
                    self._generation_stop_event.wait(0.01)
            if not self._generation_gate.wait_sync(self._generation_stop_event.is_set):
                raise RuntimeError("Generation stopped while battery-suspended")
            started = time.monotonic()
            self._execution_checkpoint()
            try:
                output = self.scheduler.step()
            finally:
                self._finish_execution_pacing()
            pacer = self.config.inference_pacer
            if (
                pacer is not None
                and not pacer.full_speed_snapshot
                and not getattr(self, "_execution_controls_pacing", False)
            ):
                rest = min(1.0, (time.monotonic() - started) * (1 / pacer.share - 1))
                self._generation_stop_event.wait(rest)
            for req_output in output.outputs:
                if req_output.finished:
                    results[req_output.request_id] = req_output

        # Cleanup
        for rid in request_ids:
            self.scheduler.remove_finished_request(rid)

        # Return in original order
        return [results[rid] for rid in request_ids]

    def get_stats(self) -> Dict[str, Any]:
        """Get engine statistics."""
        scheduler_stats = self.scheduler.get_stats()
        uptime = time.time() - self._start_time if self._start_time else 0

        return {
            "running": self._running,
            "uptime_seconds": uptime,
            "steps_executed": self._steps_executed,
            "active_requests": len(self._output_collectors),
            "stream_interval": self.config.stream_interval,
            **scheduler_stats,
        }

    def get_cache_stats(self) -> Optional[Dict[str, Any]]:
        """Get prefix cache statistics."""
        return self.scheduler.get_cache_stats()

    def _release_model(self) -> None:
        """Release model ownership."""
        if self._owns_model and not self._closed:
            registry = get_registry()
            registry.release(self.model, self._engine_id)
            self._owns_model = False
            logger.debug(f"Engine {self._engine_id} released model ownership")

    def close(self) -> None:
        """Close once, preserving SSD stores within a progress-gated budget."""
        self._generation_stop_event.set()
        with self._close_lock:
            if self._closed:
                return
            self._closing = True
            try:
                with _EngineTeardown(
                    self._engine_id, FATAL_TEARDOWN_TIMEOUT_S
                ) as teardown:
                    self._close(teardown)
            finally:
                self._closing = False

    def _close(self, teardown) -> None:
        """
        Explicitly close the engine and release resources.

        This should be called when done using the engine, especially
        if you plan to create another engine with the same model.
        """
        if self._closed:
            return

        self.scheduler._engine_teardown = teardown

        def drain_gpu_errors() -> None:
            _drop_pending_gpu_errors(self._mlx_stream)

        # Both shutdown() and deep_reset() touch the engine stream (directly
        # or via _drain_pending_async_removes / _do_abort_request). The
        # stream is bound to the engine's executor thread, so dispatch both
        # through the executor. Rejection cannot safely move MLX cleanup
        # onto the close caller's thread. Errors a GPU reset left on the
        # stream are drained first so the two steps do not stop on them.
        for fn in (
            drain_gpu_errors,
            self.scheduler.shutdown,
            self.scheduler.deep_reset,
        ):
            fn_name = getattr(fn, "__name__", repr(fn))
            try:
                teardown.set_phase(fn_name)
                self._submit_model_worker_sync(self._run_device_cleanup, fn).result(
                    timeout=teardown.remaining(mlx=fn_name != "shutdown")
                )
            except concurrent.futures.TimeoutError:
                fatal_exit(
                    f"Engine teardown timed out after "
                    f"{time.monotonic() - teardown.started:.1f}s while running "
                    f"{fn_name} for engine {self._engine_id}"
                )
            except RuntimeError:
                fatal_exit(f"Engine {self._engine_id}: MLX teardown failed ({fn_name})")
            except Exception:
                # A failing shutdown/deep_reset must not abort close(), or the
                # SSD cache manager below stays open and its writer thread keeps
                # the manager (and its hot cache) alive until restart.
                logger.warning(
                    "Engine %s: %s raised during close()",
                    self._engine_id,
                    getattr(fn, "__name__", fn),
                    exc_info=True,
                )

        # Drop the last bound-method reference from the teardown loop before
        # the final GC/reclaim pass below.
        fn = None

        # Guarantee the SSD cache manager is released even if shutdown() did not
        # reach its own close() above. The manager's writer thread holds a strong
        # reference to it, so an unclosed manager leaks until restart.
        manager = getattr(self.scheduler, "paged_ssd_cache_manager", None)
        if manager is not None:
            try:
                teardown.set_phase("fallback_ssd", manager.persistence_progress)
                manager.close(teardown=teardown)
                teardown.set_phase("engine_cleanup")
            except Exception:
                logger.warning(
                    "Engine %s: SSD cache manager close() failed during teardown",
                    self._engine_id,
                    exc_info=True,
                )
            writer = getattr(manager, "_writer_thread", None)
            if writer is not None and writer.is_alive() is True:
                fatal_exit("SSD cache writer survived engine teardown")
            self.scheduler.paged_ssd_cache_manager = None
        manager = None

        teardown.set_phase("engine_cleanup")
        # Clear output collectors before dropping model/scheduler references so
        # any request-side caches they retain are eligible for the final reclaim.
        for collector in self._output_collectors.values():
            collector.clear()
        self._output_collectors.clear()
        self._stream_states.clear()
        self._finished_events.clear()
        self._finished_at.clear()

        release_model_resources = getattr(self.model, "release_resources", None)
        if callable(release_model_resources):
            try:
                release_model_resources()
            except Exception:
                logger.warning(
                    "Engine %s: model resource release failed during close()",
                    self._engine_id,
                    exc_info=True,
                )
        release_model_resources = None

        # Release model, tokenizer, and scheduler references before the final
        # MLX reclaim. The reclaim must run on this engine's worker thread and
        # stream; clearing on the global executor cannot reliably return this
        # thread/stream-local Metal memory to MLX.
        if self._owns_model:
            get_registry().release(self.model, self._engine_id)
            self._owns_model = False
        registration = getattr(self, "_execution_registration", None)
        if registration is not None:
            self.config.inference_coordinator.unregister_model(
                self.config.inference_model_id or self._engine_id,
                generation=registration.generation,
            )
            self._execution_registration = None
        self.model = None
        self.tokenizer = None
        self.scheduler = None

        if self._mlx_executor is not None:
            try:
                reclaim_future = self._mlx_executor.submit(
                    _final_engine_thread_reclaim, self._mlx_stream
                )
            except RuntimeError:
                reclaim_future = None

            if reclaim_future is not None:
                try:
                    teardown.set_phase("mlx_reclaim")
                    reclaim_future.result(timeout=teardown.remaining(mlx=True))
                except concurrent.futures.TimeoutError:
                    fatal_exit(
                        f"Engine teardown timed out after "
                        f"{FATAL_TEARDOWN_TIMEOUT_S:.0f}s while reclaiming "
                        f"MLX memory for engine {self._engine_id}"
                    )
                except Exception as exc:
                    fatal_exit(
                        f"Engine {self._engine_id}: final MLX reclaim failed: "
                        f"{exc!r}"
                    )

            # MLX 0.32.2 makes compiled AttachedData destruction GIL-safe, and
            # _final_engine_thread_reclaim clears the worker's per-thread stream
            # registry before ThreadPoolExecutor lets that worker exit.
            self._mlx_executor.shutdown(wait=False)
            self._mlx_executor = None

        self._closed = True
        logger.debug(f"Engine {self._engine_id} closed")

    def __del__(self):
        """Cleanup on destruction."""
        try:
            self._release_model()
        except Exception:
            # Ignore errors during garbage collection
            pass

    @property
    def engine_id(self) -> str:
        """Get the engine ID."""
        return self._engine_id


class AsyncEngineCore:
    """
    Async context manager wrapper for EngineCore.

    Usage:
        async with AsyncEngineCore(model, tokenizer) as engine:
            request_id = await engine.add_request("Hello")
            async for output in engine.stream_outputs(request_id):
                print(output.new_text)
    """

    def __init__(
        self,
        model: Any,
        tokenizer: Any,
        config: Optional[EngineConfig] = None,
    ):
        self.engine = EngineCore(model, tokenizer, config)
        # Drop wrapper-local aliases after EngineCore takes ownership.
        model = None
        tokenizer = None

    @property
    def _mlx_executor(self):
        """Expose the MLX executor for VLM vision encoding."""
        return self.engine._mlx_executor

    async def __aenter__(self) -> "AsyncEngineCore":
        await self.engine.start()
        return self

    async def __aexit__(self, *args) -> None:
        await self.stop()

    def start(self) -> None:
        """Start engine (creates task in current loop)."""
        asyncio.create_task(self.engine.start())

    async def stop(self) -> None:
        """Stop the engine."""
        engine = getattr(self, "engine", None)
        if engine is None:
            return
        await engine.stop()

    async def add_request(
        self,
        prompt: Union[str, List[int]],
        sampling_params: Optional[SamplingParams] = None,
        request_id: Optional[str] = None,
        **kwargs,
    ) -> str:
        """Add a request."""
        return await self.engine.add_request(
            prompt=prompt,
            sampling_params=sampling_params,
            request_id=request_id,
            **kwargs,
        )

    async def abort_request(self, request_id: str) -> bool:
        """Abort a request."""
        engine = getattr(self, "engine", None)
        if engine is None:
            logger.debug(
                "Skipping abort for request %s because async engine is closed",
                request_id,
            )
            return False
        return await engine.abort_request(request_id)

    async def abort_all_requests(
        self,
        *,
        reason: str | None = None,
        error_code: str | None = None,
    ) -> int:
        """Abort all active requests without stopping the engine."""
        engine = getattr(self, "engine", None)
        if engine is None:
            return 0
        return await engine.abort_all_requests(reason=reason, error_code=error_code)

    async def stream_outputs(
        self,
        request_id: str,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[RequestOutput]:
        """Stream outputs."""
        async for output in self.engine.stream_outputs(request_id, timeout):
            yield output

    async def generate(
        self,
        prompt: Union[str, List[int]],
        sampling_params: Optional[SamplingParams] = None,
        **kwargs,
    ) -> RequestOutput:
        """Generate complete response."""
        return await self.engine.generate(
            prompt=prompt,
            sampling_params=sampling_params,
            **kwargs,
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get engine stats."""
        return self.engine.get_stats()

    def get_cache_stats(self) -> Optional[Dict[str, Any]]:
        """Get prefix cache statistics."""
        return self.engine.get_cache_stats()
