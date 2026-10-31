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
    data: bytes | str | tuple[int, ...] | SemanticSnapshot

    @property
    def estimated_bytes(self):
        if isinstance(self.data, SemanticSnapshot):
            return self.data.estimated_bytes
        if isinstance(self.data, str):
            return len(self.data) * 4
        if isinstance(self.data, tuple):
            return len(self.data) * 12 + 2
        return len(self.data)

    @property
    def cost(self):
        if isinstance(self.data, SemanticSnapshot):
            return self.data.memory_bytes + _CHUNK_OVERHEAD
        if isinstance(self.data, tuple):
            return len(self.data) * 40 + _CHUNK_OVERHEAD
        if isinstance(self.data, str):
            return len(self.data) * 4 + _CHUNK_OVERHEAD
        return len(self.data) + _CHUNK_OVERHEAD

    def encoded_chunks(self):
        """Encode only on the worker, with a bounded temporary buffer."""
        if isinstance(self.data, SemanticSnapshot):
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
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._ready = threading.Event()
        self._epoch = uuid.uuid4().hex
        self._pending: dict[str, _Pending] = {}
        self._chunks: deque[_Chunk] = deque()
        self._controls: deque[tuple[str, str | ActivitySettings | None, Future]] = (
            deque()
        )
        self._pending_bytes = 0
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
            reservation = self.settings.max_pending_bytes - self._pending_bytes
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
                request, memory_bytes=reservation - _CHUNK_OVERHEAD, max_bytes=logical
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

    def _offer(self, handle, representation, data, *, invocation_id):
        if handle is None or not self.settings.enabled or self._closed:
            return False
        if representation not in REPRESENTATIONS:
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
            if type(data) in (list, tuple):
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
            ):
                reason = "capture_queue_full"
            if reason:
                self._gap(pending, reason)
                self._wake.set()
                return False
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
                self._wake.wait(1)
                self._wake.clear()
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
                    chunk = chunks.popleft()
                    with self._lock:
                        current = self._lookup(chunk.handle)
                        valid = current is not None
                    if valid:
                        saved, encoded_bytes = True, 0
                        for data in chunk.encoded_chunks():
                            encoded_bytes += len(data)
                            if not store.append(
                                chunk.handle.request_id,
                                chunk.invocation_id,
                                chunk.representation,
                                data,
                            ):
                                saved = False
                                break
                        # The loop variable otherwise retains the final buffer.
                        data = None
                    else:
                        saved = True  # Explicit deletion/clear, not a capture gap.
                        encoded_bytes = chunk.estimated_bytes
                    handle, cost, estimate = (
                        chunk.handle,
                        chunk.cost,
                        chunk.estimated_bytes,
                    )
                    del chunk  # Release payload before advertising free capacity.
                    with self._lock:
                        current = self._lookup(handle)
                        self._pending_bytes -= cost
                        self._pending_count -= 1
                        if current is not None:
                            current.pending_chunks -= 1
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
                        if value.pending_chunks == 0
                        and value.saved_version != value.version
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
