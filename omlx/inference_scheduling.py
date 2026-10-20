# SPDX-License-Identifier: Apache-2.0
"""Cooperative, model-level ownership for local inference workers.

The coordinator arbitrates only when enabled. Each model registers a drain
callback that completes work already submitted to its device stream. Workers
call :meth:`ModelWork.checkpoint` at safe boundaries; the worker must release
shared locks and resources before checkpointing.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field


class ModelExecutionError(RuntimeError):
    """Base exception for coordinator failures."""


class ModelExecutionCancelledError(ModelExecutionError):
    """Raised when a worker's stop event is set while it waits or checkpoints."""


class StaleModelRegistrationError(ModelExecutionError):
    """Raised when work tries to continue under an obsolete model generation."""


class CoordinatorClosedError(ModelExecutionError):
    """Raised when a prior drain failure has closed the coordinator."""


@dataclass
class _Model:
    model_id: str
    generation: int
    priority: int
    exclusive: bool
    drain: Callable[[], object] | None
    admission_enabled: bool = True
    runnable: bool = False
    registered: bool = True
    active: dict[int, ModelWork] = field(default_factory=dict)
    waiting: set[int] = field(default_factory=set)
    executor_thread: int | None = None
    executor_depth: int = 0

    @property
    def rank(self) -> tuple[bool, int]:
        return (self.exclusive, self.priority)


class ModelRegistration:
    """Lifecycle token returned by :meth:`register_model`."""

    def __init__(
        self, coordinator: ModelExecutionCoordinator, model_id: str, generation: int
    ):
        self._coordinator = coordinator
        self.model_id = model_id
        self.generation = generation

    def close(self) -> None:
        """Stop admitting work for this generation; active work must still exit."""
        self._coordinator.unregister_model(self.model_id, generation=self.generation)

    def configure(
        self, *, priority: int | None = None, exclusive: bool | None = None
    ) -> None:
        self._coordinator.configure_model(
            self.model_id,
            generation=self.generation,
            priority=priority,
            exclusive=exclusive,
        )

    def work(
        self,
        *,
        stop_event: threading.Event | None = None,
        cancelled: Callable[[], bool] | None = None,
        kind: str = "inference",
    ) -> ModelWork:
        return self._coordinator.work(
            self.model_id,
            generation=self.generation,
            stop_event=stop_event,
            cancelled=cancelled,
            kind=kind,
        )

    def reserve_submission(self) -> ModelSubmission | None:
        """Reserve a worker-queue slot before submitting its callable.

        The reservation prevents an idle-owner drain from being queued behind
        a worker that has been submitted but has not entered ``work``. A None
        result means this model is being drained; retry from the submitter.
        """
        return self._coordinator.reserve_submission(
            self.model_id, generation=self.generation
        )


class ModelSubmission:
    """Queue reservation that must be consumed by a worker or released."""

    def __init__(self, coordinator: ModelExecutionCoordinator, work: ModelWork):
        self._coordinator = coordinator
        self._work = work
        work._submission = self
        self._released = False
        self._consumed = False

    def work(
        self,
        *,
        stop_event: threading.Event | None = None,
        cancelled: Callable[[], bool] | None = None,
        kind: str = "inference",
    ) -> ModelWork:
        """Bind the reserved slot to a worker context (call once)."""
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")
        if cancelled is not None and not callable(cancelled):
            raise TypeError("cancelled must be callable or None")
        with self._coordinator._condition:
            if self._released or self._consumed:
                raise RuntimeError("submission reservation is no longer available")
            self._consumed = True
            self._work.stop_event = stop_event
            self._work.cancelled = cancelled
            self._work.kind = kind
            return self._work

    def release(self) -> None:
        """Release a reservation when submission failed or was cancelled."""
        self._coordinator._release_submission(self)

    cancel = release


class ModelWork:
    """One synchronous worker operation, including its arbitration wait time."""

    def __init__(
        self,
        coordinator: ModelExecutionCoordinator,
        model_id: str,
        generation: int | None,
        stop_event: threading.Event | None,
        cancelled: Callable[[], bool] | None,
        kind: str,
        submission: ModelSubmission | None = None,
    ) -> None:
        self._coordinator = coordinator
        self.model_id = model_id
        self.generation = generation
        self.stop_event = stop_event
        self.cancelled = cancelled
        self._checkpoint_cancelled: Callable[[], bool] | None = None
        self.kind = kind
        self._entered = False
        self._passthrough = False
        self._parked = False
        self._cancelled = False
        self._thread_id: int | None = None
        self._wait_intervals: list[tuple[float, float]] = []
        self._wait_started: float | None = None
        self._submission = submission
        self._reserved = False

    @property
    def wait_intervals(self) -> tuple[tuple[float, float], ...]:
        """Monotonic ``(start, end)`` pairs spent acquiring or resuming work."""
        return tuple(self._wait_intervals)

    @property
    def waited_seconds(self) -> float:
        return sum(end - start for start, end in self._wait_intervals)

    def __enter__(self) -> ModelWork:
        self._coordinator._enter_work(self)
        self._entered = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._entered:
            self._coordinator._exit_work(self)
            self._entered = False
        return False

    def checkpoint(self, *, cancelled: Callable[[], bool] | None = None) -> bool:
        """Yield at a safe boundary if policy requires it; return whether it waited."""
        if cancelled is not None and not callable(cancelled):
            raise TypeError("cancelled must be callable or None")
        self._checkpoint_cancelled = cancelled
        try:
            if not self._entered or self._passthrough:
                self._check_cancelled()
                return False
            return self._coordinator._checkpoint(self)
        finally:
            self._checkpoint_cancelled = None

    def raise_if_cancelled(self) -> None:
        self._check_cancelled()

    def _check_cancelled(self) -> None:
        if self._cancelled:
            raise ModelExecutionCancelledError(
                f"work for model {self.model_id!r} was cancelled"
            )
        cancelled = self.stop_event is not None and self.stop_event.is_set()
        predicates = (self.cancelled, self._checkpoint_cancelled)
        cancelled = cancelled or any(
            predicate is not None and predicate() for predicate in predicates
        )
        if cancelled:
            self._cancelled = True
            if self._wait_started is not None:
                self._wait_intervals.append(
                    (self._wait_started, self._coordinator._clock())
                )
                self._wait_started = None
            raise ModelExecutionCancelledError(
                f"work for model {self.model_id!r} was cancelled"
            )


class ModelExecutionCoordinator:
    """Thread-safe cooperative coordinator with fail-closed handover drains.

    A model is exclusive only while ``enabled`` is true. Higher ranks win
    immediately at a worker checkpoint; equal ranks rotate after the quantum
    only while another model is eligible. Runnability lets a scheduler retain
    a model's turn across short gaps between worker calls.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        quantum_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        quantum_seconds = float(quantum_seconds)
        if not math.isfinite(quantum_seconds) or quantum_seconds < 0:
            raise ValueError("quantum_seconds must be finite and non-negative")
        self._condition = threading.Condition(threading.RLock())
        self._clock = clock
        self._enabled = bool(enabled)
        self._quantum_seconds = quantum_seconds
        self._suspended = False
        self._models: dict[str, _Model] = {}
        self._generations: dict[str, int] = {}
        self._context_ids = 0
        self._owner: str | None = None
        self._owner_generation: int | None = None
        self._turn_started: float | None = None
        self._last_granted: dict[tuple[bool, int], str] = {}
        self._draining = False
        self._drain_serial = 0
        self._drain_owner: tuple[str, int] | None = None
        self._drain_failed: str | None = None
        self._passthrough_active = 0

    @staticmethod
    def _priority_value(priority: int) -> int:
        if (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or not 0 <= priority <= 100
        ):
            raise ValueError("priority must be an integer from 0 through 100")
        return priority

    def register_model(
        self,
        model_id: str,
        *,
        priority: int = 0,
        exclusive: bool = False,
        drain: Callable[[], object] | None = None,
    ) -> ModelRegistration:
        """Register a model generation and its required handover drain."""
        if not model_id:
            raise ValueError("model_id must be non-empty")
        priority = self._priority_value(priority)
        if not isinstance(exclusive, bool):
            raise TypeError("exclusive must be a bool")
        if drain is not None and not callable(drain):
            raise TypeError("drain must be callable or None")
        with self._condition:
            old = self._models.get(model_id)
            if old is not None and old.registered:
                raise ValueError(f"model {model_id!r} is already registered")
            if old is not None and (
                old.active
                or self._owner == model_id
                or (self._drain_owner is not None and self._drain_owner[0] == model_id)
            ):
                raise RuntimeError(
                    f"model {model_id!r} still has work in its previous generation"
                )
            generation = self._generations.get(model_id, 0) + 1
            self._generations[model_id] = generation
            self._models[model_id] = _Model(
                model_id=model_id,
                generation=generation,
                priority=priority,
                exclusive=bool(exclusive),
                drain=drain,
            )
            self._condition.notify_all()
            return ModelRegistration(self, model_id, generation)

    def unregister_model(self, model_id: str, *, generation: int | None = None) -> None:
        """Mark a generation unavailable; it cannot release ownership mid-call."""
        drain_job = None
        with self._condition:
            model = self._models.get(model_id)
            if model is None or (
                generation is not None and model.generation != generation
            ):
                return
            model.registered = False
            model.runnable = False
            if self._owner == model_id and not model.active and self._enabled:
                drain_job = self._claim_drain_locked(model)
            self._condition.notify_all()
        if drain_job is not None:
            self._run_drain(drain_job)

    def configure(
        self,
        *,
        enabled: bool | None = None,
        quantum_seconds: float | None = None,
    ) -> None:
        """Apply live global settings and wake all waiting workers."""
        if enabled is not None and not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        if quantum_seconds is not None:
            quantum_seconds = float(quantum_seconds)
            if not math.isfinite(quantum_seconds) or quantum_seconds < 0:
                raise ValueError("quantum_seconds must be finite and non-negative")
        drain_job = None
        with self._condition:
            if enabled is not None:
                self._enabled = bool(enabled)
            if quantum_seconds is not None:
                self._quantum_seconds = quantum_seconds
            owner = self._models.get(self._owner) if self._owner else None
            if not self._enabled:
                self._owner = None
                self._owner_generation = None
                self._turn_started = None
                for model in self._models.values():
                    for work in model.active.values():
                        if not work._passthrough and not work._reserved:
                            work._passthrough = True
                            self._passthrough_active += 1
            elif (
                owner is not None
                and not owner.active
                and self._should_handover_locked(owner)
            ):
                drain_job = self._claim_drain_locked(owner)
            elif self._owner is None and self._enabled and not self._suspended:
                next_model = self._choose_next_locked()
                if next_model is not None:
                    self._owner = next_model.model_id
                    self._owner_generation = next_model.generation
                    self._turn_started = self._clock()
                    self._last_granted[next_model.rank] = next_model.model_id
            self._condition.notify_all()
        if drain_job is not None:
            self._run_drain(drain_job)

    def configure_model(
        self,
        model_id: str,
        *,
        generation: int | None = None,
        priority: int | None = None,
        exclusive: bool | None = None,
    ) -> None:
        if priority is not None:
            priority = self._priority_value(priority)
        if exclusive is not None and not isinstance(exclusive, bool):
            raise TypeError("exclusive must be a bool")
        drain_job = None
        with self._condition:
            model = self._require_model_locked(model_id, generation)
            if priority is not None:
                model.priority = priority
            if exclusive is not None:
                model.exclusive = bool(exclusive)
            owner = self._models.get(self._owner) if self._owner else None
            if (
                owner is not None
                and not owner.active
                and self._should_handover_locked(owner)
            ):
                drain_job = self._claim_drain_locked(owner)
            elif owner is None and self._enabled and not self._suspended:
                next_model = self._choose_next_locked()
                if next_model is not None:
                    self._owner = next_model.model_id
                    self._owner_generation = next_model.generation
                    self._turn_started = self._clock()
                    self._last_granted[next_model.rank] = next_model.model_id
            self._condition.notify_all()
        if drain_job is not None:
            self._run_drain(drain_job)

    def set_runnable(
        self,
        model_id: str,
        runnable: bool,
        *,
        priority: int | None = None,
        generation: int | None = None,
    ) -> None:
        """Publish whether queued or imminent work makes a model eligible."""
        if not isinstance(runnable, bool):
            raise TypeError("runnable must be a bool")
        if priority is not None:
            priority = self._priority_value(priority)
        drain_job = None
        with self._condition:
            model = self._require_model_locked(model_id, generation)
            model.runnable = bool(runnable) and model.admission_enabled
            if priority is not None:
                model.priority = priority
            owner = self._models.get(self._owner) if self._owner else None
            if (
                owner is not None
                and not owner.active
                and self._should_handover_locked(owner)
            ):
                drain_job = self._claim_drain_locked(owner)
            elif owner is None and self._enabled and not self._suspended:
                next_model = self._choose_next_locked()
                if next_model is not None:
                    self._owner = next_model.model_id
                    self._owner_generation = next_model.generation
                    self._turn_started = self._clock()
                    self._last_granted[next_model.rank] = next_model.model_id
            self._condition.notify_all()
        if drain_job is not None:
            self._run_drain(drain_job)

    def set_suspended(self, suspended: bool) -> None:
        """Park arbitration during a system suspension, draining at a safe boundary."""
        if not isinstance(suspended, bool):
            raise TypeError("suspended must be a bool")
        drain_job = None
        with self._condition:
            self._suspended = bool(suspended)
            owner = self._models.get(self._owner) if self._owner else None
            if owner is not None and suspended and not owner.active and self._enabled:
                drain_job = self._claim_drain_locked(owner)
            elif owner is None and not suspended and self._enabled:
                next_model = self._choose_next_locked()
                if next_model is not None:
                    self._owner = next_model.model_id
                    self._owner_generation = next_model.generation
                    self._turn_started = self._clock()
                    self._last_granted[next_model.rank] = next_model.model_id
            self._condition.notify_all()
        if drain_job is not None:
            self._run_drain(drain_job)

    def set_model_admission(self, model_id: str, enabled: bool) -> None:
        """Pause one model's inference admission while retaining maintenance."""
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        drain_job = None
        with self._condition:
            model = self._models.get(model_id)
            if model is None or not model.registered:
                return
            model.admission_enabled = enabled
            if not enabled:
                model.runnable = False
            owner = self._models.get(self._owner) if self._owner else None
            if (
                owner is not None
                and not owner.active
                and self._should_handover_locked(owner)
            ):
                drain_job = self._claim_drain_locked(owner)
            elif owner is None and self._enabled and not self._suspended:
                next_model = self._choose_next_locked()
                if next_model is not None:
                    self._owner = next_model.model_id
                    self._owner_generation = next_model.generation
                    self._turn_started = self._clock()
                    self._last_granted[next_model.rank] = next_model.model_id
            self._condition.notify_all()
        if drain_job is not None:
            self._run_drain(drain_job)

    def work(
        self,
        model_id: str,
        *,
        generation: int | None = None,
        stop_event: threading.Event | None = None,
        cancelled: Callable[[], bool] | None = None,
        kind: str = "inference",
    ) -> ModelWork:
        """Create a worker operation. Kinds may describe vision or maintenance work."""
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("model_id must be a non-empty string")
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")
        if cancelled is not None and not callable(cancelled):
            raise TypeError("cancelled must be callable or None")
        with self._condition:
            model = self._models.get(model_id)
            if generation is None and model is not None:
                generation = model.generation
        return ModelWork(self, model_id, generation, stop_event, cancelled, kind)

    def reserve_submission(
        self, model_id: str, *, generation: int | None = None
    ) -> ModelSubmission | None:
        """Reserve a queued worker operation without blocking its submitter.

        Reservations count as active, unparked work until their callable starts
        or the token is released. Thus a drain cannot be queued behind a worker
        that would block waiting for that same drain.
        """
        with self._condition:
            model = self._require_model_locked(model_id, generation)
            if self._draining and self._drain_owner == (model_id, model.generation):
                return None
            if self._drain_failed:
                raise CoordinatorClosedError(self._drain_failed)
            work = ModelWork(self, model_id, model.generation, None, None, "inference")
            self._context_ids += 1
            work._work_id = self._context_ids
            work._reserved = True
            model.active[work._work_id] = work
            model.waiting.add(work._work_id)
            self._condition.notify_all()
            return ModelSubmission(self, work)

    def _release_submission(self, submission: ModelSubmission) -> None:
        drain_job = None
        with self._condition:
            if submission._released or submission._consumed:
                return
            submission._released = True
            work = submission._work
            model = self._models.get(work.model_id)
            if model is not None:
                if work._passthrough:
                    self._passthrough_active = max(0, self._passthrough_active - 1)
                self._cleanup_work_locked(model, work)
                if (
                    self._owner == model.model_id
                    and self._enabled
                    and self._should_handover_locked(model)
                ):
                    drain_job = self._claim_drain_locked(model)
            self._condition.notify_all()
        if drain_job is not None:
            self._run_drain(drain_job)

    def status_snapshot(self) -> dict[str, object]:
        """Return a detached, read-only-shaped view of arbitration state."""
        with self._condition:
            now = self._clock()
            models: dict[str, dict[str, object]] = {}
            for model_id, model in self._models.items():
                models[model_id] = {
                    "generation": model.generation,
                    "registered": model.registered,
                    "runnable": model.runnable,
                    "priority": model.priority,
                    "exclusive": model.exclusive,
                    "admission_enabled": model.admission_enabled,
                    "active": len(model.active),
                    "waiting": len(model.waiting),
                    "owns_turn": self._owner == model_id,
                    "rank": model.rank,
                }
            return {
                "enabled": self._enabled,
                "suspended": self._suspended,
                "owner": self._owner,
                "owner_generation": self._owner_generation,
                "turn_elapsed_seconds": (
                    max(0.0, now - self._turn_started)
                    if self._turn_started is not None
                    else None
                ),
                "draining": self._draining,
                "drain_failed": self._drain_failed,
                "quantum_seconds": self._quantum_seconds,
                "models": models,
            }

    def _require_model_locked(self, model_id: str, generation: int | None) -> _Model:
        model = self._models.get(model_id)
        if model is None or not model.registered:
            raise StaleModelRegistrationError(f"model {model_id!r} is not registered")
        if generation is not None and model.generation != generation:
            raise StaleModelRegistrationError(
                f"model {model_id!r} registration is stale"
            )
        return model

    def _owns_locked(self, model: _Model) -> bool:
        return (
            self._owner == model.model_id and self._owner_generation == model.generation
        )

    def _enter_work(self, work: ModelWork) -> None:
        condition = self._condition
        started = None
        thread_id = threading.get_ident()
        with condition:
            submission = work._submission
            reserved = submission is not None
            if reserved:
                if submission._released or not submission._consumed:
                    raise RuntimeError("submission reservation was not consumed")
                model = self._models.get(work.model_id)
                if (
                    model is None
                    or not model.registered
                    or model.generation != work.generation
                ):
                    if model is not None:
                        self._cleanup_work_locked(model, work)
                        if work._passthrough:
                            self._passthrough_active = max(
                                0, self._passthrough_active - 1
                            )
                    submission._released = True
                    condition.notify_all()
                    raise StaleModelRegistrationError(
                        f"model {work.model_id!r} registration is stale"
                    )
                try:
                    work._check_cancelled()
                except BaseException:
                    self._cleanup_work_locked(model, work)
                    if work._passthrough:
                        self._passthrough_active = max(0, self._passthrough_active - 1)
                    submission._released = True
                    condition.notify_all()
                    raise
            model = self._models.get(work.model_id)
            while (
                model is not None
                and model.registered
                and not model.admission_enabled
                and work.kind != "maintenance"
            ):
                try:
                    work._check_cancelled()
                except BaseException:
                    if reserved:
                        self._cleanup_work_locked(model, work)
                        submission._released = True
                    condition.notify_all()
                    raise
                condition.wait(timeout=0.1)
                model = self._models.get(work.model_id)
            if not self._enabled:
                if not work._passthrough:
                    work._passthrough = True
                    self._passthrough_active += 1
                return
            model = self._require_model_locked(work.model_id, work.generation)
            if reserved and work._passthrough:
                # Keep the reserved slot as a hot-enable barrier until older
                # passthrough calls have exited.
                while self._passthrough_active > 1:
                    try:
                        work._check_cancelled()
                    except BaseException:
                        self._cleanup_work_locked(model, work)
                        self._passthrough_active = max(0, self._passthrough_active - 1)
                        submission._released = True
                        condition.notify_all()
                        raise
                    condition.wait(timeout=0.1)
                    model = self._require_model_locked(work.model_id, work.generation)
                self._passthrough_active = max(0, self._passthrough_active - 1)
                work._passthrough = False
            if self._drain_failed:
                if reserved:
                    self._cleanup_work_locked(model, work)
                    submission._released = True
                    condition.notify_all()
                raise CoordinatorClosedError(self._drain_failed)
            if not reserved:
                self._context_ids += 1
                work_id = self._context_ids
                work._work_id = work_id
            else:
                work_id = work._work_id
            while model.executor_thread not in (None, thread_id):
                if started is None:
                    started = self._clock()
                    work._wait_started = started
                try:
                    work._check_cancelled()
                except BaseException:
                    if reserved:
                        self._cleanup_work_locked(model, work)
                        submission._released = True
                        condition.notify_all()
                    raise
                condition.wait(timeout=0.1)
                try:
                    model = self._require_model_locked(work.model_id, work.generation)
                except BaseException:
                    if reserved:
                        self._cleanup_work_locked(model, work)
                        submission._released = True
                        condition.notify_all()
                    raise
            if model.executor_thread is None:
                model.executor_thread = thread_id
            model.executor_depth += 1
            work._thread_id = thread_id
            if not reserved:
                model.active[work_id] = work
            work._parked = False
            model.waiting.add(work_id)
            work._reserved = False
            if started is None and not self._owns_locked(model):
                started = self._clock()
                work._wait_started = started
            try:
                self._acquire_locked(work, model, started)
            except BaseException:
                self._cleanup_work_locked(model, work)
                if self._owner == model.model_id:
                    # The call never entered user code, so it submitted no
                    # device work that could require a drain.
                    self._owner = None
                    self._owner_generation = None
                    self._turn_started = None
                    next_model = self._choose_next_locked()
                    if next_model is not None:
                        self._owner = next_model.model_id
                        self._owner_generation = next_model.generation
                        self._turn_started = self._clock()
                        self._last_granted[next_model.rank] = next_model.model_id
                self._condition.notify_all()
                raise

    @staticmethod
    def _cleanup_work_locked(model: _Model, work: ModelWork) -> None:
        work_id = getattr(work, "_work_id", None)
        if work_id is None:
            return
        model.active.pop(work_id, None)
        model.waiting.discard(work_id)
        if work._thread_id is not None:
            model.executor_depth = max(0, model.executor_depth - 1)
            if model.executor_depth == 0:
                model.executor_thread = None
            work._thread_id = None

    def _acquire_locked(
        self, work: ModelWork, model: _Model, started: float | None
    ) -> None:
        condition = self._condition
        work_id = work._work_id
        while True:
            work._check_cancelled()
            model = self._require_model_locked(work.model_id, work.generation)
            if not model.admission_enabled and work.kind != "maintenance":
                if started is None:
                    started = self._clock()
                    work._wait_started = started
                condition.wait(timeout=0.1)
                continue
            if not self._enabled:
                self._cleanup_work_locked(model, work)
                if not work._passthrough:
                    work._passthrough = True
                    self._passthrough_active += 1
                if started is not None:
                    work._wait_intervals.append((started, self._clock()))
                    work._wait_started = None
                return
            if self._passthrough_active:
                if started is None:
                    started = self._clock()
                    work._wait_started = started
                condition.wait(timeout=0.1)
                continue
            if self._drain_failed:
                model.waiting.discard(work_id)
                raise CoordinatorClosedError(self._drain_failed)
            if (
                self._enabled
                and (not self._suspended or work.kind == "maintenance")
                and not self._draining
                and self._owner in (None, model.model_id)
            ):
                if self._owner is None:
                    self._owner = model.model_id
                    self._owner_generation = model.generation
                    self._turn_started = self._clock()
                    self._last_granted[model.rank] = model.model_id
                model.waiting.discard(work_id)
                if started is not None:
                    work._wait_intervals.append((started, self._clock()))
                    work._wait_started = None
                return
            if (
                self._enabled
                and (not self._suspended or work.kind == "maintenance")
                and self._owner == model.model_id
            ):
                model.waiting.discard(work_id)
                if started is not None:
                    work._wait_intervals.append((started, self._clock()))
                    work._wait_started = None
                return
            if started is None:
                started = self._clock()
                work._wait_started = started
            condition.wait(timeout=0.1)

    def _exit_work(self, work: ModelWork) -> None:
        drain_job = None
        with self._condition:
            if work._passthrough:
                model = self._models.get(work.model_id)
                if (
                    model is not None
                    and getattr(work, "_work_id", None) in model.active
                ):
                    self._cleanup_work_locked(model, work)
                self._passthrough_active = max(0, self._passthrough_active - 1)
                self._condition.notify_all()
                return
            model = self._models.get(work.model_id)
            if model is None:
                return
            work_id = getattr(work, "_work_id", None)
            if work_id is None:
                return
            model.active.pop(work_id, None)
            model.waiting.discard(work_id)
            model.executor_depth = max(0, model.executor_depth - 1)
            if model.executor_depth == 0:
                model.executor_thread = None
            if self._owner == model.model_id:
                if self._enabled and self._should_handover_locked(model):
                    drain_job = self._claim_drain_locked(model)
                elif not self._enabled:
                    self._owner = None
                    self._owner_generation = None
                    self._turn_started = None
            self._condition.notify_all()
        if drain_job is not None:
            self._run_drain(drain_job)

    def _checkpoint(self, work: ModelWork) -> bool:
        condition = self._condition
        drain_job = None
        waited = False
        with condition:
            work._check_cancelled()
            model = self._require_model_locked(work.model_id, work.generation)
            if self._drain_failed:
                raise CoordinatorClosedError(self._drain_failed)
            if not self._enabled:
                return False
            if self._owner != model.model_id:
                raise ModelExecutionError(
                    f"model {model.model_id!r} checkpointed without ownership"
                )
            same_thread_work = [
                active
                for active in model.active.values()
                if active._thread_id == work._thread_id
            ]
            for active in same_thread_work:
                active._parked = True
            if not self._should_handover_locked(model):
                for active in same_thread_work:
                    active._parked = False
                return False
            work._wait_started = self._clock()
            waited = True
            self._condition.notify_all()
            drain_job = self._claim_drain_locked(model)
            while self._owner == model.model_id or self._draining:
                try:
                    work._check_cancelled()
                except ModelExecutionCancelledError:
                    for active in same_thread_work:
                        active._parked = False
                    self._condition.notify_all()
                    raise
                if drain_job is not None:
                    break
                if not waited:
                    work._wait_started = self._clock()
                    waited = True
                condition.wait(timeout=0.1)
                model = self._require_model_locked(work.model_id, work.generation)
                if (
                    self._owner == model.model_id
                    and (not self._suspended or work.kind == "maintenance")
                    and not self._draining
                ):
                    for active in same_thread_work:
                        active._parked = False
                    if work._wait_started is not None:
                        work._wait_intervals.append((work._wait_started, self._clock()))
                        work._wait_started = None
                    return waited
                if drain_job is None:
                    drain_job = self._claim_drain_locked(model)
            # The final owner worker may have claimed the drain. Run it outside
            # the condition lock and then wait below for the next grant.
        if drain_job is not None:
            self._run_drain(drain_job)
        if waited:
            with condition:
                while (
                    self._owner != work.model_id
                    and self._enabled
                    and not self._drain_failed
                ):
                    work._check_cancelled()
                    condition.wait(timeout=0.1)
                if self._drain_failed:
                    raise CoordinatorClosedError(self._drain_failed)
                if not self._enabled:
                    work._parked = False
                    if work._wait_started is not None:
                        work._wait_intervals.append((work._wait_started, self._clock()))
                        work._wait_started = None
                    return waited
                model = self._require_model_locked(work.model_id, work.generation)
                for active in same_thread_work:
                    active._parked = False
                if work._wait_started is not None:
                    work._wait_intervals.append((work._wait_started, self._clock()))
                    work._wait_started = None
        return waited

    def _should_handover_locked(self, owner: _Model) -> bool:
        if not self._enabled:
            return True
        if self._suspended:
            owner_has_maintenance = any(
                work.kind == "maintenance" for work in owner.active.values()
            )
            if not owner_has_maintenance:
                return True
        peers = [m for m in self._eligible_locked() if m.model_id != owner.model_id]
        active_unparked = any(not work._parked for work in owner.active.values())
        if not owner.admission_enabled and not active_unparked:
            return True
        if not peers:
            return not owner.registered or (not owner.runnable and not owner.active)
        if not owner.active and not owner.runnable:
            return True
        best_rank = max(model.rank for model in peers)
        if best_rank > owner.rank:
            return True
        return (
            best_rank == owner.rank
            and self._turn_started is not None
            and (self._clock() - self._turn_started >= self._quantum_seconds)
            and not active_unparked
        )

    def _eligible_locked(self) -> list[_Model]:
        return [
            model
            for model in self._models.values()
            if model.registered
            and (
                any(
                    work.kind == "maintenance" and not work._passthrough
                    for work in model.active.values()
                )
                if self._suspended
                else (
                    any(
                        work.kind == "maintenance" and not work._passthrough
                        for work in model.active.values()
                    )
                    or (
                        model.admission_enabled
                        and (
                            model.runnable
                            or any(
                                work.kind != "maintenance" and not work._passthrough
                                for work in model.active.values()
                            )
                            or any(
                                work_id in model.waiting
                                and work.kind != "maintenance"
                                and not work._passthrough
                                for work_id, work in model.active.items()
                            )
                        )
                    )
                )
            )
        ]

    def _choose_next_locked(self) -> _Model | None:
        if not self._enabled or self._draining:
            return None
        eligible = self._eligible_locked()
        if not eligible:
            return None
        rank = max(model.rank for model in eligible)
        ordered_ids = list(self._models)
        candidates = sorted(
            (model for model in eligible if model.rank == rank),
            key=lambda model: ordered_ids.index(model.model_id),
        )
        last = self._last_granted.get(rank)
        if last is not None and last in ordered_ids:
            last_index = ordered_ids.index(last)
            after = [
                model
                for model in candidates
                if ordered_ids.index(model.model_id) > last_index
            ]
            if after:
                return after[0]
        return candidates[0]

    def _claim_drain_locked(self, owner: _Model):
        if self._draining or self._owner != owner.model_id:
            return None
        if any(not work._parked for work in owner.active.values()):
            return None
        self._draining = True
        self._drain_serial += 1
        serial = self._drain_serial
        model_generation = owner.generation
        drain = owner.drain
        self._drain_owner = (owner.model_id, model_generation)
        self._owner = None
        self._owner_generation = None
        self._turn_started = None
        return (serial, owner.model_id, model_generation, drain)

    def _run_drain(self, job) -> None:
        serial, model_id, generation, drain = job
        failure = None
        try:
            if drain is None:
                raise RuntimeError(f"model {model_id!r} has no device drain callback")
            result = drain()
            if result is False:
                raise RuntimeError("device drain callback returned False")
        except Exception as exc:
            failure = f"device drain failed for model {model_id!r} generation {generation}: {exc}"
        with self._condition:
            if serial != self._drain_serial:
                return
            self._draining = False
            self._drain_owner = None
            if failure is not None:
                self._drain_failed = failure
            else:
                current = self._models.get(model_id)
                if current is not None and current.generation != generation:
                    # A stale drain receipt cannot clear a newer generation's
                    # failure or authorize it to start work.
                    self._drain_failed = (
                        "stale device drain receipt; coordinator remains closed"
                    )
                elif self._owner is None:
                    next_model = self._choose_next_locked()
                    if next_model is not None:
                        self._owner = next_model.model_id
                        self._owner_generation = next_model.generation
                        self._turn_started = self._clock()
                        self._last_granted[next_model.rank] = next_model.model_id
            self._condition.notify_all()
