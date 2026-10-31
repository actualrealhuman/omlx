# SPDX-License-Identifier: Apache-2.0
"""Bounded CPU-side capture. Producer methods never perform SQLite or waits."""

import json
import math
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path

from .config import ActivitySettings
from .coverage import BACKENDS
from .images import prepare_image
from .invocations import (
    INVOCATION_MEMORY_BYTES,
    MAX_INVOCATION_BYTES,
    MAX_INVOCATIONS,
    InvocationSnapshot,
)
from .snapshot import SemanticSnapshot, snapshot_request
from .store import (
    MAX_CHUNK_BYTES,
    REPRESENTATIONS,
    STATES,
    TERMINAL_STATES,
    ActivityReader,
    ActivityStore,
)

_MAX_PENDING_CHUNKS = 4096
_MAX_CONTROLS = 16
_CHUNK_OVERHEAD = 256
_COLLECT_SECONDS = 0.05
_COALESCE_ITEMS = 64
_COALESCE_BYTES = 64 * 1024
_MERGE_SCRATCH_BYTES = 5 * _COALESCE_BYTES
_GROUP_METADATA_BYTES = 2048


@dataclass(frozen=True)
class CaptureHandle:
    request_id: str
    epoch: str


@dataclass
class _Pending:
    metadata: dict
    version: int = 1
    saved_version: int = 0
    accepted_bytes: int = 0
    pending_chunks: int = 0
    persisted: bool = False


@dataclass(frozen=True)
class _Chunk:
    handle: CaptureHandle
    representation: str
    invocation_id: str
    data: bytes | str | tuple[int, ...] | SemanticSnapshot | InvocationSnapshot

    @property
    def item_count(self):
        return 1

    @property
    def estimated_bytes(self):
        if isinstance(self.data, InvocationSnapshot):
            return MAX_INVOCATION_BYTES
        if isinstance(self.data, SemanticSnapshot):
            return self.data.estimated_bytes
        if isinstance(self.data, str):
            return len(self.data) * 4
        if isinstance(self.data, tuple):
            return len(self.data) * 12 + 2
        return len(self.data)

    @property
    def cost(self):
        if isinstance(self.data, InvocationSnapshot):
            return INVOCATION_MEMORY_BYTES + _CHUNK_OVERHEAD
        if isinstance(self.data, SemanticSnapshot):
            return self.data.memory_bytes + _CHUNK_OVERHEAD
        if isinstance(self.data, tuple):
            return len(self.data) * 40 + _CHUNK_OVERHEAD
        if isinstance(self.data, str):
            return len(self.data) * 4 + _CHUNK_OVERHEAD
        return len(self.data) + _CHUNK_OVERHEAD

    def encoded_chunks(self):
        """Encode only on the worker, with a bounded temporary buffer."""
        if isinstance(self.data, InvocationSnapshot):
            yield self.data.encode()
        elif isinstance(self.data, SemanticSnapshot):
            yield from self.data.encoded_chunks(MAX_CHUNK_BYTES)
        elif isinstance(self.data, bytes):
            yield self.data
        elif isinstance(self.data, str):
            for start in range(0, len(self.data), MAX_CHUNK_BYTES // 4):
                yield self.data[start : start + MAX_CHUNK_BYTES // 4].encode("utf-8")
        else:
            if any(
                type(token) is not int or not 0 <= token <= 0xFFFFFFFF
                for token in self.data
            ):
                raise ValueError("Invalid captured token IDs")
            parts, size = [], 0
            for part in json.JSONEncoder().iterencode(self.data):
                parts.append(part)
                size += len(part)
                if size >= MAX_CHUNK_BYTES // 2:
                    yield "".join(parts).encode("ascii")
                    parts, size = [], 0
            if parts:
                yield "".join(parts).encode("ascii")


class _WeakBatch:
    __slots__ = ("__weakref__",)


@dataclass(frozen=True, slots=True)
class _ChunkBatch(_WeakBatch):
    chunks: tuple[_Chunk, ...]
    merge: bool = True
    data = None  # No semantic snapshot/image/invocation payload.

    @property
    def handle(self):
        return self.chunks[0].handle

    @property
    def invocation_id(self):
        return self.chunks[0].invocation_id

    @property
    def representation(self):
        return self.chunks[0].representation

    @property
    def item_count(self):
        return len(self.chunks)

    @property
    def cost(self):
        return sum(chunk.cost for chunk in self.chunks)

    @property
    def estimated_bytes(self):
        return sum(chunk.estimated_bytes for chunk in self.chunks)

    def encoded_chunks(self):
        if not self.merge:
            for chunk in self.chunks:
                yield from chunk.encoded_chunks()
            return
        yield b"".join(
            chunk.data.encode("utf-8") if type(chunk.data) is str else chunk.data
            for chunk in self.chunks
        )


class ActivityRecorder:
    """One background writer, bounded content and separately reserved lifecycle.

    All producer lock ownership is limited to bounded in-memory operations. The
    worker releases the lock before serialization, compression or SQLite. Use
    flush/wait_ready/close only at admin/test/shutdown boundaries, never decode.
    """

    def __init__(
        self, path: Path, settings: ActivitySettings | None = None, *, maintenance=False
    ):
        self.path = Path(path)
        self.settings = settings or ActivitySettings()
        self.reader = ActivityReader(self.path)
        # Object finalizers can run during allocations while this thread owns
        # the ledger lock; releasing an observer reservation must be reentrant.
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._urgent = threading.Event()
        self._ready = threading.Event()
        self._epoch = uuid.uuid4().hex
        self._pending: dict[str, _Pending] = {}
        self._chunks: deque[_Chunk] = deque()
        self._controls: deque[tuple[str, str | ActivitySettings | None, Future]] = (
            deque()
        )
        self._pending_bytes = 0
        self._invocation_live_bytes = 0
        self._writer_scratch_bytes = 0
        self._prepared_group_bytes = 0
        self._pending_count = 0
        self._freezing_bytes = 0
        self._freezing_count = 0
        self._closed = False
        self._stopping = False
        self._available = False
        self._error: str | None = None
        self._dropped_events = 0
        self._skipped_requests = 0
        self._thread = None
        if self.settings.enabled or (maintenance and self.path.exists()):
            self._thread = threading.Thread(
                target=self._run, name="omlx-activity", daemon=True
            )
            self._thread.start()
        else:
            self._ready.set()

    def health(self):
        with self._lock:
            return {
                "enabled": self.settings.enabled and not self._closed,
                "available": self._available,
                "stopping": self._closed,
                "error": self._error,
                "active_captures": len(self._pending),
                "pending_bytes": self._pending_bytes,
                "invocation_live_bytes": self._invocation_live_bytes,
                "writer_scratch_bytes": self._writer_scratch_bytes,
                "prepared_group_bytes": self._prepared_group_bytes,
                "dropped_events": self._dropped_events,
                "skipped_requests": self._skipped_requests,
            }

    def begin(
        self,
        *,
        model_id: str,
        endpoint: str,
        source_id="unknown",
        source_name="Unknown",
        received_at=None,
    ):
        if not self.settings.enabled or self._closed:
            return None
        # Six bounded strings fit the separately reserved per-request metadata.
        values = (model_id, endpoint, source_id, source_name)
        if any(not isinstance(value, str) or len(value) > 256 for value in values):
            return None
        if received_at is not None and (
            type(received_at) not in (int, float) or not math.isfinite(received_at)
        ):
            return None
        with self._lock:
            if (
                self._closed
                or not self.settings.enabled
                or self._error
                or len(self._pending) >= self.settings.max_active_requests
                or self._memory_used(capture_slots=1) > self.settings.max_memory_bytes
            ):
                self._skipped_requests += 1
                return None
            handle = CaptureHandle(uuid.uuid4().hex, self._epoch)
            self._pending[handle.request_id] = _Pending(
                {
                    "id": handle.request_id,
                    "received_at": time.time() if received_at is None else received_at,
                    "finished_at": None,
                    "state": "received",
                    "model_id": model_id,
                    "source_id": source_id,
                    "source_name": source_name,
                    "endpoint": endpoint,
                    "capture_gaps": 0,
                    "capture_reason": "",
                    "finish_reason": None,
                }
            )
        self._wake.set()
        return handle

    def _lookup(self, handle):
        if not isinstance(handle, CaptureHandle) or handle.epoch != self._epoch:
            return None
        return self._pending.get(handle.request_id)

    def _gap(self, pending, reason):
        pending.metadata["capture_gaps"] += 1
        pending.metadata["capture_reason"] = reason
        pending.version += 1
        self._dropped_events += 1

    def append(self, handle, representation: str, data: bytes, *, invocation_id="0"):
        if type(data) is not bytes:
            return False
        return self._offer(handle, representation, data, invocation_id=invocation_id)

    def append_text(self, handle, representation: str, text: str, *, invocation_id="0"):
        """Transfer an immutable string; no encoding in the producer thread."""
        if type(text) is not str:
            return False
        return self._offer(handle, representation, text, invocation_id=invocation_id)

    def append_tokens(self, handle, tokens: list[int], *, invocation_id="0"):
        """Copy CPU token references after checking bounds, never a device array."""
        if type(tokens) not in (list, tuple):
            return False
        return self._offer(handle, "model_tokens", tokens, invocation_id=invocation_id)

    def capture_submitted(self, handle, request):
        """Reserve first, freeze bounded semantics outside the producer lock.

        The mutable request remains exclusively in the API admission call; only
        the completed immutable snapshot is handed to the persistence worker.
        """
        with self._lock:
            pending = self._lookup(handle)
            if (
                pending is None
                or self._closed
                or not self.settings.enabled
                or pending.metadata["finished_at"] is not None
            ):
                return False
            reservation = min(
                self.settings.max_pending_bytes - self._pending_bytes,
                self.settings.max_memory_bytes - self._memory_used(),
            )
            logical = self.settings.max_request_bytes - pending.accepted_bytes
            if (
                reservation < 8448
                or logical < 8192
                or self._pending_count >= _MAX_PENDING_CHUNKS
            ):
                self._gap(pending, "submitted_snapshot_capacity")
                self._wake.set()
                return False
            self._pending_bytes += reservation
            self._pending_count += 1
            self._freezing_bytes += reservation
            self._freezing_count += 1
        try:
            snapshot = snapshot_request(
                request,
                memory_bytes=reservation - _CHUNK_OVERHEAD,
                max_bytes=logical,
                retain_images=self.settings.retain_images,
                max_attachment_bytes=self.settings.max_attachment_bytes,
            )
            chunk = (
                _Chunk(handle, "submitted", "0", snapshot)
                if snapshot is not None
                else None
            )
        except Exception:
            chunk = None
        with self._lock:
            self._pending_bytes -= reservation
            self._pending_count -= 1
            self._freezing_bytes -= reservation
            self._freezing_count -= 1
            self._wake.set()  # A stopping worker may be waiting for this reservation.
            pending = self._lookup(handle)
            if (
                pending is None
                or self._closed
                or not self.settings.enabled
                or pending.metadata["finished_at"] is not None
            ):
                return False
            if (
                chunk is None
                or chunk.cost > reservation
                or pending.accepted_bytes + chunk.estimated_bytes
                > self.settings.max_request_bytes
            ):
                self._gap(pending, "submitted_snapshot_unavailable")
                self._wake.set()
                return False
            pending.metadata["submitted_normalized"] = bool(snapshot.media_count)
            pending.metadata["submitted_media_references"] = snapshot.media_count
            for reason in snapshot.reasons:
                self._gap(pending, reason)
            pending.accepted_bytes += chunk.estimated_bytes
            pending.pending_chunks += 1
            pending.version += 1
            self._chunks.append(chunk)
            self._pending_bytes += chunk.cost
            self._pending_count += 1
        self._wake.set()
        return True

    def _memory_used(self, *, capture_slots=0, pending_bytes=0, invocation_bytes=0):
        # Caller owns the ledger lock. Includes in-flight worker allocations.
        return (
            self._pending_bytes
            + self._invocation_live_bytes
            + max(self._writer_scratch_bytes, self._prepared_group_bytes)
            + pending_bytes
            + invocation_bytes
            + (len(self._pending) + capture_slots) * 8192
        )

    def reserve_invocation(self, handle):
        with self._lock:
            pending = self._lookup(handle)
            if (
                pending is None
                or not self.settings.enabled
                or pending.metadata["finished_at"] is not None
            ):
                return False
            if (
                self._memory_used(invocation_bytes=2048)
                > self.settings.max_memory_bytes
            ):
                self._gap(pending, "invocation_memory_capacity")
                self._wake.set()
                return False
            self._invocation_live_bytes += 2048
        return True

    def release_invocation(self):
        # A live engine may still own this small observer after clear/off/error.
        with self._lock:
            self._invocation_live_bytes -= 2048

    def invocation_snapshot(self, handle, invocation_id, **values):
        if (
            type(invocation_id) is not str
            or not invocation_id.isascii()
            or not invocation_id.isdecimal()
            or len(invocation_id) > 3
            or not 1 <= int(invocation_id) <= MAX_INVOCATIONS
            or str(int(invocation_id)) != invocation_id
        ):
            return False
        return self._offer(handle, "invocation", values, invocation_id=invocation_id)

    def _offer(self, handle, representation, data, *, invocation_id):
        if handle is None or not self.settings.enabled or self._closed:
            return False
        if representation not in REPRESENTATIONS and representation != "invocation":
            return False
        if not isinstance(invocation_id, str) or len(invocation_id) > 128:
            return False
        with self._lock:
            pending = self._lookup(handle)
            if (
                pending is None
                or not self.settings.enabled
                or pending.metadata["finished_at"] is not None
            ):
                return False
            if representation == "invocation":
                logical_size, cost = (
                    MAX_INVOCATION_BYTES,
                    INVOCATION_MEMORY_BYTES + _CHUNK_OVERHEAD,
                )
            elif type(data) in (list, tuple):
                logical_size, cost = (
                    len(data) * 12 + 2,
                    len(data) * 40 + _CHUNK_OVERHEAD,
                )
            elif isinstance(data, str):
                logical_size, cost = len(data) * 4, len(data) * 4 + _CHUNK_OVERHEAD
            else:
                logical_size, cost = len(data), len(data) + _CHUNK_OVERHEAD
            reason = None
            if not data or (isinstance(data, bytes) and len(data) > MAX_CHUNK_BYTES):
                reason = "chunk_size_limit"
            elif (
                pending.accepted_bytes + logical_size > self.settings.max_request_bytes
            ):
                reason = "request_size_limit"
            elif (
                self._pending_count >= _MAX_PENDING_CHUNKS
                or self._pending_bytes + cost > self.settings.max_pending_bytes
                or self._memory_used(pending_bytes=cost)
                > self.settings.max_memory_bytes
            ):
                reason = "capture_queue_full"
            if reason:
                self._gap(pending, reason)
                self._wake.set()
                return False
            if representation == "invocation":
                data = InvocationSnapshot.freeze(data, pending.metadata["model_id"])
            chunk = _Chunk(
                handle,
                representation,
                invocation_id,
                tuple(data) if type(data) is list else data,
            )
            self._chunks.append(chunk)
            self._pending_bytes += chunk.cost
            self._pending_count += 1
            pending.pending_chunks += 1
            pending.accepted_bytes += logical_size
        self._wake.set()
        return True

    def state(self, handle, state: str, *, model_id=None):
        if state not in STATES or state in TERMINAL_STATES:
            raise ValueError("Use finish for terminal Activity states")
        if model_id is not None and (
            not isinstance(model_id, str) or len(model_id) > 256
        ):
            raise ValueError("Activity model ID outside bounds")
        with self._lock:
            pending = self._lookup(handle)
            if pending is None or pending.metadata["finished_at"] is not None:
                return False
            if pending.metadata["state"] == state and (
                model_id is None or pending.metadata["model_id"] == model_id
            ):
                return True
            pending.metadata["state"] = state
            if model_id is not None:
                pending.metadata["model_id"] = model_id
            pending.version += 1
        self._wake.set()
        return True

    def finish(self, handle, state="completed", *, reason=None):
        if state not in TERMINAL_STATES:
            raise ValueError("Invalid terminal Activity state")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 256):
            raise ValueError("Activity finish reason outside bounds")
        with self._lock:
            pending = self._lookup(handle)
            if pending is None or pending.metadata["finished_at"] is not None:
                return False
            pending.metadata.update(
                state=state, finish_reason=reason, finished_at=time.time()
            )
            pending.version += 1
        self._wake.set()
        return True

    def metrics(self, handle, *, counts=None, times=None):
        """Bounded CPU scalar observations; never derive tokens from text length."""
        counts, times = counts or {}, times or {}
        if set(counts) - {
            "observed_invocations",
            "prepared_invocations",
            "prepared_text_invocations",
            "prefilling_invocations",
            "observed_input_tokens",
            "observed_generated_tokens",
        }:
            return False
        if set(times) - {"first_observed_token_ms", "first_observed_text_ms"}:
            return False
        if any(
            type(value) is not int or not 0 <= value < 2**63
            for value in counts.values()
        ):
            return False
        if any(
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not 0 <= value <= 1e12
            for value in times.values()
        ):
            return False
        with self._lock:
            pending = self._lookup(handle)
            if (
                pending is None
                or not self.settings.enabled
                or pending.metadata["finished_at"] is not None
            ):
                return False
            changed = False
            for name, delta in counts.items():
                value = pending.metadata.get(name, 0) + delta
                if value >= 2**63:
                    self._gap(pending, "observed_counter_overflow")
                    continue
                if pending.metadata.get(name) != value:
                    pending.metadata[name] = value
                    changed = True
            for name, value in times.items():
                if name not in pending.metadata or value < pending.metadata[name]:
                    pending.metadata[name] = value
                    changed = True
            if changed:
                pending.version += 1
        if changed:
            self._wake.set()
        return True

    def gap(self, handle, reason):
        if not isinstance(reason, str) or len(reason) > 256:
            return False
        with self._lock:
            pending = self._lookup(handle)
            if pending is None:
                return False
            self._gap(pending, reason)
        self._wake.set()
        return True

    def api_failure(self, handle, outcome):
        """Protected scalar metadata, independent of the content queue."""
        if outcome not in {"error", "rejected", "cancelled"}:
            return False
        with self._lock:
            pending = self._lookup(handle)
            if (
                pending is None
                or not self.settings.enabled
                or pending.metadata["finished_at"] is not None
            ):
                return False
            if pending.metadata.get("api_failure_outcome") == outcome:
                return True
            pending.metadata["api_failure_outcome"] = outcome
            pending.metadata["api_failure_source"] = "api_formatter"
            pending.version += 1
        self._wake.set()
        return True

    def backend(self, handle, kind):
        """Bounded immutable dispatch observations; never retain the engine."""
        if type(kind) is not str or kind not in BACKENDS:
            return False
        with self._lock:
            pending = self._lookup(handle)
            if (
                pending is None
                or not self.settings.enabled
                or pending.metadata["finished_at"] is not None
            ):
                return False
            kinds = pending.metadata.get("resolved_backends", ())
            if kind in kinds:
                return True
            pending.metadata["resolved_backends"] = (*kinds, kind)
            pending.version += 1
        self._wake.set()
        return True

    def _control(self, kind, request_id=None):
        future = Future()
        with self._lock:
            if self._closed or self._thread is None or self._error:
                future.set_exception(RuntimeError("Activity writer unavailable"))
                return future
            if len(self._controls) >= _MAX_CONTROLS:
                future.set_exception(
                    RuntimeError("Activity admin operation queue full")
                )
                return future
            if kind == "settings":
                self.settings = request_id
                if not request_id.enabled:
                    self._stop_pending("capture_disabled")
            elif kind == "clear":
                self._epoch = uuid.uuid4().hex
                self._pending.clear()
            elif kind == "delete":
                self._pending.pop(request_id, None)
            if kind in {"clear", "delete"}:
                retained = deque()
                for chunk in self._chunks:
                    if kind == "delete" and chunk.handle.request_id != request_id:
                        retained.append(chunk)
                    else:
                        self._pending_bytes -= chunk.cost
                        self._pending_count -= 1
                self._chunks = retained
            self._controls.append((kind, request_id, future))
        self._urgent.set()
        self._wake.set()
        return future

    def _stop_pending(self, reason):
        """Caller holds the metadata lock. Does not interact with inference."""
        now = time.time()
        for pending in self._pending.values():
            if pending.metadata["finished_at"] is None:
                pending.metadata.update(state="capture_stopped", finished_at=now)
                self._gap(pending, reason)

    def update_settings(self, settings):
        if not isinstance(settings, ActivitySettings):
            raise ValueError("Invalid Activity settings")
        with self._lock:
            if self._error and not settings.enabled:
                self.settings = settings
                future = Future()
                future.set_result(None)
                return future
        return self._control("settings", settings)

    def clear(self):
        """Return an admin future; its completion fences prior in-flight writes."""
        return self._control("clear")

    def delete(self, request_id):
        return self._control("delete", request_id)

    def flush(self, timeout=5):
        """Admin/test barrier. Does not end active requests."""
        if self._thread is None:
            return True
        try:
            self._control("flush").result(timeout)
            return True
        except (RuntimeError, TimeoutError):
            return False

    def wait_ready(self, timeout=5):
        return self._ready.wait(timeout) and self._available

    def close(self, timeout=2):
        with self._lock:
            if self._closed:
                return self._thread is None or not self._thread.is_alive()
            self._closed = True
            self._stopping = True
            for pending in self._pending.values():
                if pending.metadata["finished_at"] is None:
                    pending.metadata.update(
                        state="interrupted",
                        finished_at=time.time(),
                        capture_reason="server shutdown before terminal observation",
                    )
                    pending.version += 1
        self._urgent.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def _snapshot(self):
        with self._lock:
            controls = list(self._controls)
            self._controls.clear()
            chunks = self._chunks
            self._chunks = deque()
            # Do not release byte reservations until the worker drops its refs.
            metadata = [
                (key, dict(value.metadata), value.version, value.persisted)
                for key, value in self._pending.items()
            ]
        return controls, chunks, metadata

    def _next_chunk(self, chunks):
        """Worker only: plan each bounded window once, including unique channels."""
        first = chunks.popleft()
        if isinstance(first, _ChunkBatch):
            if first.item_count == 1:
                return first  # A planned singleton must not rescan.
            with self._lock:
                if self._lookup(first.handle) is None:
                    return first  # Drop the whole invalid group without encoding.
                if (
                    self._memory_used()
                    - self._prepared_group_bytes
                    + _MERGE_SCRATCH_BYTES
                    <= self.settings.max_memory_bytes
                ):
                    self._writer_scratch_bytes = _MERGE_SCRATCH_BYTES
                    return first
            # Capacity may have changed since this window was planned. Keep
            # every original piece and avoid replanning the refused group.
            return _ChunkBatch(first.chunks, merge=False)

        def eligible(chunk):
            return (
                chunk.representation in {"raw_output", "api_response"}
                and type(chunk.data) in (str, bytes)
                and chunk.estimated_bytes <= _COALESCE_BYTES
            )

        if not chunks or not eligible(first):
            return first
        with self._lock:
            if (
                self._lookup(first.handle) is None
                or self._memory_used() + _MERGE_SCRATCH_BYTES
                > self.settings.max_memory_bytes
            ):
                return first
            # Covers temporary planning maps as well as encoding/merge buffers.
            self._writer_scratch_bytes = _MERGE_SCRATCH_BYTES
        key = (first.handle, first.representation, first.invocation_id)
        groups, sizes = {key: [first]}, {key: first.estimated_bytes}
        for _ in range(min(len(chunks), _COALESCE_ITEMS - 1)):
            candidate = chunks.popleft()
            if not eligible(candidate):
                chunks.appendleft(candidate)
                break
            key = (candidate.handle, candidate.representation, candidate.invocation_id)
            previous = groups.get(key)
            if previous and (
                type(candidate.data) is not type(previous[0].data)
                or sizes[key] + candidate.estimated_bytes > _COALESCE_BYTES
            ):
                chunks.appendleft(candidate)
                break
            groups.setdefault(key, []).append(candidate)
            sizes[key] = sizes.get(key, 0) + candidate.estimated_bytes
        planned = [_ChunkBatch(tuple(parts)) for parts in groups.values()]
        with self._lock:
            # At most64 slotted descriptors/tuples remain after planning.
            # They share the scratch credit while buffers are active, then
            # retain their own smaller reservation until each write ends.
            self._prepared_group_bytes += len(planned) * _GROUP_METADATA_BYTES
        chunks.extendleft(reversed(planned[1:]))
        return planned[0]

    def _run(self):
        store = None
        outstanding = []
        try:
            store = ActivityStore(self.path, self.settings)
            with self._lock:
                self._available = True
            self._ready.set()
            last_prune = time.monotonic()
            while True:
                woke = self._wake.wait(1)
                self._wake.clear()
                if woke:
                    self._urgent.wait(_COLLECT_SECONDS)
                self._urgent.clear()
                controls, chunks, metadata = self._snapshot()
                outstanding = controls
                # Controls fence all earlier writes. New request snapshots are
                # persisted after clear/delete, never before them.
                for kind, request_id, _future in controls:
                    if kind == "clear":
                        store.clear()
                    elif kind == "delete":
                        store.delete(request_id)
                    elif kind == "settings":
                        store.settings = request_id
                        if not store._make_room(0):
                            with self._lock:
                                self._stop_pending("storage_budget_reduced")
                for key, record, _version, persisted in metadata:
                    if persisted:
                        continue
                    # Lifecycle finalization waits until its queued chunks drain.
                    initial = dict(record)
                    initial["finished_at"] = None
                    if record["finished_at"] is not None:
                        initial["state"] = "received"
                    store.save_metadata(initial)
                    with self._lock:
                        pending = self._pending.get(key)
                        if pending is not None:
                            pending.persisted = True
                while chunks:
                    chunk = self._next_chunk(chunks)
                    with self._lock:
                        current = self._lookup(chunk.handle)
                        valid = current is not None
                    if valid:
                        saved, encoded_bytes = True, 0
                        for data in chunk.encoded_chunks():
                            encoded_bytes += len(data)
                            accepted = (
                                store.save_invocation(
                                    chunk.handle.request_id, chunk.invocation_id, data
                                )
                                if isinstance(chunk.data, InvocationSnapshot)
                                else store.append(
                                    chunk.handle.request_id,
                                    chunk.invocation_id,
                                    chunk.representation,
                                    data,
                                )
                            )
                            if not accepted:
                                saved = False
                                break
                        # The loop variable otherwise retains the final buffer.
                        data = None
                        if saved and isinstance(chunk.data, SemanticSnapshot):
                            for image in chunk.data.images:
                                info, payload = prepare_image(image)
                                if not store.save_attachment(
                                    chunk.handle.request_id, info, payload
                                ):
                                    saved = False
                                    break
                                if payload is not None:
                                    encoded_bytes += len(payload)
                                if info["status"] != "retained":
                                    with self._lock:
                                        current = self._lookup(chunk.handle)
                                        if current is not None:
                                            self._gap(current, info["reason"])
                                payload = None
                            # Loop variables must not keep an original alive
                            # after the queue advertises its reservation as free.
                            image = payload = None
                    else:
                        saved = True  # Explicit deletion/clear, not a capture gap.
                        encoded_bytes = chunk.estimated_bytes
                    handle, cost, estimate, item_count, group_credit = (
                        chunk.handle,
                        chunk.cost,
                        chunk.estimated_bytes,
                        chunk.item_count,
                        _GROUP_METADATA_BYTES if isinstance(chunk, _ChunkBatch) else 0,
                    )
                    del chunk  # Release payload before advertising free capacity.
                    with self._lock:
                        current = self._lookup(handle)
                        self._pending_bytes -= cost
                        self._pending_count -= item_count
                        self._writer_scratch_bytes = 0
                        self._prepared_group_bytes -= group_credit
                        if current is not None:
                            current.pending_chunks -= item_count
                            if saved:
                                current.accepted_bytes -= max(
                                    0, estimate - encoded_bytes
                                )
                            if not saved:
                                self._gap(current, "storage_budget_or_missing_record")
                # Drop worker payload references before reporting a free queue.
                chunks.clear()
                with self._lock:
                    final = [
                        (key, dict(value.metadata), value.version)
                        for key, value in self._pending.items()
                        if value.saved_version != value.version
                        and (
                            value.pending_chunks == 0
                            or value.metadata["finished_at"] is None
                        )
                    ]
                for key, record, version in final:
                    store.save_metadata(record)
                    with self._lock:
                        pending = self._pending.get(key)
                        if pending is not None:
                            pending.saved_version = version
                            if (
                                pending.metadata["finished_at"] is not None
                                and pending.version == version
                                and pending.pending_chunks == 0
                            ):
                                self._pending.pop(key)
                if final or controls or time.monotonic() - last_prune >= 60:
                    store.prune()
                    last_prune = time.monotonic()
                for _, _, future in controls:
                    if not future.done():
                        future.set_result(None)
                outstanding = []
                with self._lock:
                    done = (
                        self._stopping
                        and not self._pending
                        and not self._chunks
                        and not self._controls
                        and not self._freezing_count
                    )
                if done:
                    break
        except Exception as exc:
            # No request content, credentials or SQLite parameters in this status.
            with self._lock:
                self._error = f"Activity persistence suspended ({type(exc).__name__})"
                self._available = False
                waiting = list(self._controls) + outstanding
                self._controls.clear()
                self._chunks.clear()
                self._pending.clear()
                self._pending_bytes = self._freezing_bytes
                self._writer_scratch_bytes = 0
                self._prepared_group_bytes = 0
                self._pending_count = self._freezing_count
            for _, _, future in waiting:
                if not future.done():
                    future.set_exception(RuntimeError(self._error))
        finally:
            if store is not None:
                store.close()
            self._ready.set()
            with self._lock:
                self._available = False
