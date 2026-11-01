# SPDX-License-Identifier: Apache-2.0
"""Admin-only request inspection, bounded reads and snapshot-based live delivery."""

import asyncio
import base64
import hashlib
import inspect
import json
import math
import sqlite3
import threading
import time
import uuid
import weakref
import zlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .config import ActivitySettings
from .images import IMAGE_FORMATS, MAX_PREVIEW_BYTES
from .recorder import ActivityRecorder
from .replay import ActivityReplayBuffer, ReplayBufferFullError, StaleLeaseError
from .runtime import configure_recorder, get_recorder
from .store import MAX_READ_BYTES, REPRESENTATIONS, STATES, ActivityReader

_NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_MAX_READERS = 4
_MAX_STREAMS = 8
_MAX_EVENT_BYTES = 1024 * 1024
_MAX_MUTATION_FENCES = 16
_MAX_REPLAY_CURSOR = 128


def _frame_cursor(frame):
    """Read the bounded SSE id line without copying the remaining payload."""
    line_end = frame.find(b"\n", 0, _MAX_REPLAY_CURSOR + 6)
    if line_end < 0 or not frame.startswith(b"id: "):
        raise ValueError("invalid Activity replay frame")
    return frame[4:line_end].decode("ascii")


def _activity_channel_digest(
    selection, model_id, source_id, scope, state, since, until
):
    normalized = json.dumps(
        [scope, model_id, source_id, state, since, until, selection],
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(normalized).digest()


class _MutationFence:
    """Bounded publication gate for controls and their recorder writes."""

    def __init__(self):
        self._lock = threading.RLock()
        self._generation = uuid.uuid4().hex
        self._tokens = {}

    def begin(self):
        with self._lock:
            if len(self._tokens) >= _MAX_MUTATION_FENCES:
                raise HTTPException(
                    429,
                    "Activity mutation capacity reached",
                    headers={"Retry-After": "1"},
                )
            token = uuid.uuid4().hex
            self._tokens[token] = [False, "none"]
            self._generation = uuid.uuid4().hex
            return token

    def track(self, token, future):
        if token is None:
            return
        with self._lock:
            state = self._tokens.get(token)
            if state is None or state[1] != "none":
                # Preserve the gate if future ownership became ambiguous.
                if state is not None:
                    state[1] = "ambiguous"
                return
            state[1] = "pending"
        try:
            future.add_done_callback(
                lambda completed, fence=self, token_id=token: fence._future_done(
                    token_id, completed
                )
            )
        except Exception:
            # The write may already be queued. Keep its bounded token fenced.
            with self._lock:
                state = self._tokens.get(token)
                if state is not None:
                    state[1] = "ambiguous"

    def worker_done(self, token):
        if token is None:
            return
        with self._lock:
            state = self._tokens.get(token)
            if state is None:
                return
            state[0] = True
            self._finish_if_safe(token, state)

    def _future_done(self, token, future):
        with self._lock:
            state = self._tokens.get(token)
            if state is None or state[1] != "pending":
                return
            # Cancellation leaves the write outcome uncertain. Fail closed
            # until this service is replaced.
            if future.cancelled():
                state[1] = "cancelled"
            else:
                state[1] = "done"
            self._finish_if_safe(token, state)

    def _finish_if_safe(self, token, state):
        if state[0] and state[1] in {"none", "done"}:
            self._tokens.pop(token, None)
            self._generation = uuid.uuid4().hex

    def stamp(self):
        with self._lock:
            if self._tokens:
                return None
            return self._generation

    def valid(self, generation):
        if generation is None:
            return False
        with self._lock:
            return not self._tokens and self._generation == generation


class ActivityUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool | None = None
    retain_images: StrictBool | None = None
    max_requests: StrictInt | None = Field(None, gt=0, le=2**63 - 1)
    max_age_days: StrictInt | None = Field(None, gt=0, le=2**31 - 1)
    max_storage_bytes: StrictInt | None = Field(None, gt=0, le=2**63 - 1)


class ActivityConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal["delete", "clear"]


def _settings_version(settings):
    return hashlib.sha256(
        json.dumps(settings.to_dict(), sort_keys=True).encode()
    ).hexdigest()


def _encode_cursor(record):
    data = json.dumps(
        [record["received_at"], record["id"]], separators=(",", ":")
    ).encode()
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decode_cursor(value):
    if value is None:
        return None
    try:
        data = json.loads(
            base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
        )
        if (
            not isinstance(data, list)
            or len(data) != 2
            or type(data[0]) not in (int, float)
            or not math.isfinite(data[0])
            or not isinstance(data[1], str)
            or len(data[1]) > 128
        ):
            raise ValueError
        return data
    except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
        raise HTTPException(400, "Invalid Activity page cursor") from exc


def _validate_activity_filters(state, since, until):
    if state is not None and state not in STATES:
        raise HTTPException(400, "Unknown Activity state")
    if any(value is not None and not math.isfinite(value) for value in (since, until)):
        raise HTTPException(400, "Invalid Activity time range")
    if since is not None and until is not None and since > until:
        raise HTTPException(400, "Invalid Activity time range")


class _StreamResponse(StreamingResponse):
    """Release admission even if the body iterator never starts."""

    def __init__(self, body, release, *, media_type="text/event-stream", headers=None):
        super().__init__(
            body,
            media_type=media_type,
            headers={**_NO_STORE, "X-Accel-Buffering": "no", **(headers or {})},
        )
        self._release = weakref.finalize(self, release)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()


class ActivityAdmin:
    def __init__(
        self, get_settings, require_admin, *, poll_seconds=0.5, settings_lock=None
    ):
        self.get_settings, self.require_admin = get_settings, require_admin
        self.poll_seconds = poll_seconds
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="activity-read"
        )
        self._reads = 0
        self._read_lock = threading.Lock()
        self._closed = False
        self._generation = 0
        self._streams = set()
        self._stream_lock = threading.Lock()
        self._settings_lock = settings_lock or asyncio.Lock()
        self._controls = 0
        self._control_tasks = set()
        self._mutation_fences = _MutationFence()
        self._mutation_context = threading.local()
        self._replay = ActivityReplayBuffer(max_leases=_MAX_STREAMS)
        self._coordinator_lock = threading.RLock()
        self._coordinators = {}
        self._replay_loop = None
        self._replay_stamp = None

    def start(self):
        with self._read_lock:
            if self._closed:
                self._executor = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="activity-read"
                )
                self._closed = False

    def close(self):
        with self._read_lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
        self._executor.shutdown(wait=False, cancel_futures=True)

    def reader(self):
        settings = self.get_settings()
        if settings is None:
            raise HTTPException(503, "Server not initialized")
        return ActivityReader(Path(settings.base_path) / "activity.sqlite3")

    def _source_identity(self):
        settings = self.get_settings()
        if settings is None:
            raise HTTPException(503, "Server not initialized")
        path = str(Path(settings.base_path) / "activity.sqlite3")
        recorder = get_recorder()
        epoch = getattr(recorder, "_epoch", None) if recorder is not None else None
        if type(epoch) is not str or len(epoch) > 64:
            epoch = id(epoch) if epoch is not None else None
        return path, id(recorder) if recorder is not None else None, epoch

    def publication_stamp(self):
        generation = self._mutation_fences.stamp()
        if generation is None:
            return None
        stamp = (self._generation, generation, *self._source_identity())
        return stamp if self.publication_valid(stamp) else None

    def publication_valid(self, stamp):
        if (
            stamp is None
            or len(stamp) != 5
            or stamp[0] != self._generation
            or not self._mutation_fences.valid(stamp[1])
        ):
            return False
        try:
            identity = self._source_identity()
        except HTTPException:
            return False
        return (
            stamp[2:] == identity
            and stamp[0] == self._generation
            and self._mutation_fences.valid(stamp[1])
        )

    async def read(self, operation, *, on_submitted=None):
        # Only admin readers can wait for this isolated executor. Decode and
        # capture producers never use it, and admission itself is bounded.
        with self._read_lock:
            if self._closed:
                raise HTTPException(503, "Activity reader is stopping")
            if self._reads >= _MAX_READERS:
                raise HTTPException(
                    429,
                    "Activity reader capacity reached",
                    headers={"Retry-After": "1"},
                )
            self._reads += 1

        def release(_future=None):
            with self._read_lock:
                self._reads -= 1

        try:
            future = self._executor.submit(operation)
        except RuntimeError as exc:
            release()
            raise HTTPException(503, "Activity reader is stopping") from exc
        future.add_done_callback(release)
        if on_submitted is not None:
            on_submitted(future)
        try:
            # Keep admission until the actual worker completes, even if the
            # HTTP caller disconnects. Cancellation cannot grow the work queue.
            return await asyncio.shield(asyncio.wrap_future(future))
        except (sqlite3.Error, OSError, ValueError, zlib.error) as exc:
            raise HTTPException(503, "Activity history unavailable") from exc

    async def control(self, operation, mutation=False):
        # A task owns the shared settings lock through the actual worker result,
        # even when its HTTP caller disconnects. Admission bounds queued tasks.
        if self._controls >= 8:
            raise HTTPException(429, "Activity control capacity reached")
        self._controls += 1

        async def run():
            token = None
            submitted = False
            try:
                async with self._settings_lock:
                    if mutation:
                        token = self._mutation_fences.begin()

                    def invoke():
                        previous = getattr(self._mutation_context, "token", None)
                        self._mutation_context.token = token
                        try:
                            return operation()
                        finally:
                            self._mutation_context.token = previous

                    def on_submitted(future):
                        nonlocal submitted
                        submitted = True
                        if token is not None:
                            future.add_done_callback(
                                lambda _completed, fence=self._mutation_fences, token_id=token: (
                                    fence.worker_done(token_id)
                                )
                            )

                    return await self.read(invoke, on_submitted=on_submitted)
            finally:
                if token is not None and not submitted:
                    self._mutation_fences.worker_done(token)
                self._controls -= 1

        task = asyncio.create_task(run())
        self._control_tasks.add(task)

        def finished(task):
            self._control_tasks.discard(task)
            if not task.cancelled():
                task.exception()  # Observe errors if the HTTP caller disconnected.

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    def _writer(self):
        recorder = get_recorder()
        if recorder is not None:
            return recorder
        settings = self.get_settings()
        if settings is None:
            raise HTTPException(503, "Server not initialized")
        path = Path(settings.base_path) / "activity.sqlite3"
        if not path.exists():
            return None
        recorder = ActivityRecorder(path, settings.activity, maintenance=True)
        if not recorder.wait_ready():
            if not recorder.close():
                configure_recorder(recorder)
            raise HTTPException(
                503, "Activity history unavailable; existing store preserved"
            )
        configure_recorder(recorder)
        return recorder

    def apply_settings(self, update, version):
        settings = self.get_settings()
        if settings is None:
            raise HTTPException(503, "Server not initialized")
        if version and version != _settings_version(settings.activity):
            raise HTTPException(409, "Activity settings changed; reload before saving")
        try:
            candidate = ActivitySettings.from_dict(
                {**settings.activity.to_dict(), **update}
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        recorder, staged = get_recorder(), None
        if recorder is not None and (
            recorder.health().get("stopping")
            or (recorder.health()["error"] and candidate.enabled)
        ):
            if not recorder.close():
                raise HTTPException(503, "Previous Activity writer is still stopping")
            recorder = None
        if candidate.enabled:
            settings.ensure_activity_source_ids()
        if recorder is None and (
            candidate.enabled
            or (Path(settings.base_path) / "activity.sqlite3").exists()
        ):
            path = Path(settings.base_path) / "activity.sqlite3"
            # Readiness must not prune history under an uncommitted proposal.
            # Use current policy until the section save succeeds. A new empty
            # store needs an enabled bootstrap writer, never published to hooks.
            bootstrap = (
                settings.activity
                if path.exists()
                else ActivitySettings.from_dict(
                    {**settings.activity.to_dict(), "enabled": True}
                )
            )
            staged = ActivityRecorder(
                path,
                bootstrap,
                maintenance=True,
            )
            if not staged.wait_ready():
                if not staged.close():
                    configure_recorder(staged)
                raise HTTPException(
                    503, "Activity capture unavailable; existing store preserved"
                )
        try:
            settings.save_activity_settings(candidate)
        except (OSError, ValueError):
            if staged is not None and not staged.close():
                configure_recorder(staged)
            raise HTTPException(503, "Activity settings could not be saved") from None
        if staged is not None:
            recorder = staged
        if recorder is not None:
            try:
                future = recorder.update_settings(candidate)
                self._mutation_fences.track(
                    getattr(self._mutation_context, "token", None), future
                )
                future.result(5)
            except (RuntimeError, TimeoutError):
                raise HTTPException(
                    503, "Activity settings saved; live change did not complete"
                ) from None
            finally:
                if staged is not None:
                    configure_recorder(staged)
        return {
            "settings": candidate.to_dict(),
            "config_version": _settings_version(candidate),
            "health": self.health(),
        }

    def remove(self, request_id=None):
        recorder = self._writer()
        if recorder is None:
            if request_id:
                raise HTTPException(404, "Activity request not retained")
            return {"cleared": True}
        if request_id and recorder.reader.get(request_id) is None:
            raise HTTPException(404, "Activity request not retained")
        try:
            future = recorder.delete(request_id) if request_id else recorder.clear()
            self._mutation_fences.track(
                getattr(self._mutation_context, "token", None), future
            )
            future.result(5)
        except (RuntimeError, TimeoutError):
            raise HTTPException(
                503, "Activity history change did not complete"
            ) from None
        return {"deleted": request_id} if request_id else {"cleared": True}

    def health(self):
        recorder = get_recorder()
        health = (
            recorder.health()
            if recorder is not None
            else {
                "enabled": False,
                "available": False,
                "error": None,
                "active_captures": 0,
                "pending_bytes": 0,
                "dropped_events": 0,
                "skipped_requests": 0,
            }
        )
        return {
            **health,
            "live_delivery": "committed_metadata_snapshots",
            "replay": "bounded_metadata_replay",
            "poll_seconds": self.poll_seconds,
        }

    def reserve_stream(self):
        identity = uuid.uuid4().hex
        with self._stream_lock:
            if len(self._streams) >= _MAX_STREAMS:
                raise HTTPException(
                    429,
                    "Activity viewer capacity reached",
                    headers={"Retry-After": "5"},
                )
            self._streams.add(identity)

        def release():
            with self._stream_lock:
                self._streams.discard(identity)

        return release

    def acquire_replay(self, channel_digest, publication_stamp):
        """Lease a bounded replay channel and its ASGI-loop-owned lock."""
        loop = asyncio.get_running_loop()
        with self._coordinator_lock:
            if self._replay_loop is not loop:
                if self._coordinators:
                    raise HTTPException(
                        503, "Activity stream belongs to another event loop"
                    )
                self._replay.reset()
                self._replay_loop = loop
            if self._replay_stamp != publication_stamp:
                self._replay.reset()
                self._replay_stamp = publication_stamp
            try:
                lease = self._replay.acquire(channel_digest)
            except ReplayBufferFullError as exc:
                raise HTTPException(
                    429,
                    "Activity replay capacity reached",
                    headers={"Retry-After": "1"},
                ) from exc
            coordinator = self._coordinators.get(channel_digest)
            if coordinator is None:
                coordinator = {"lock": asyncio.Lock(), "refs": 0}
                self._coordinators[channel_digest] = coordinator
            coordinator["refs"] += 1
            return lease, coordinator["lock"]

    def release_replay(self, channel_digest, lease):
        """Release from a response finalizer, including non-ASGI threads."""
        with self._coordinator_lock:
            self._replay.release(lease)
            coordinator = self._coordinators.get(channel_digest)
            if coordinator is not None:
                coordinator["refs"] -= 1
                if coordinator["refs"] == 0:
                    self._coordinators.pop(channel_digest, None)

    def replay_payload(self, snapshot):
        payload = json.dumps(
            {**snapshot, "health": self.health()},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        if len(payload) > _MAX_EVENT_BYTES:
            raise HTTPException(503, "Activity snapshot exceeds the event size limit")
        return payload

    async def replay_snapshots(
        self,
        request,
        reader,
        selection,
        model_id,
        source_id,
        scope,
        state,
        since,
        until,
        publication_stamp,
        lease,
        channel_lock,
        first_status,
        first_frame,
        cursor,
        resync_reason="cursor_unavailable",
        selected_missing=False,
    ):
        """Deliver retained frames from one cursor, then sample at the poll rate."""
        started, last_sent = time.monotonic(), time.monotonic()
        generation = publication_stamp[0]
        selected_bootstrap_only = selected_missing

        async def authorize():
            try:
                result = self.require_admin(request)
                if inspect.isawaitable(result):
                    await result
                return True
            except Exception:
                return False

        async def publication_error():
            """Recheck identity and source after awaits, immediately before output."""
            if not await authorize():
                return "event: authorization_lost\ndata: {}\n\n"
            if not self.publication_valid(publication_stamp):
                return "event: reconnect\ndata: {}\n\n"
            return None

        if not await authorize():
            yield "event: authorization_lost\ndata: {}\n\n"
            return
        if (
            self._closed
            or generation != self._generation
            or not self.publication_valid(publication_stamp)
        ):
            yield "event: reconnect\ndata: {}\n\n"
            return

        async def read_fresh_head():
            async with channel_lock:
                if not self.publication_valid(publication_stamp):
                    raise StaleLeaseError("publication changed")
                snapshot = await self.read(
                    lambda: reader.snapshot(
                        request_id=selection,
                        model_id=model_id,
                        source_id=source_id,
                        scope=scope,
                        state=state,
                        since=since,
                        until=until,
                    )
                )
                if not self.publication_valid(publication_stamp):
                    del snapshot
                    raise StaleLeaseError("publication changed")
                missing = selection is not None and snapshot.get("detail") is None
                payload = self.replay_payload(snapshot)
                observed = self._replay.observe(lease, payload)
                frame, head_cursor = observed.frame, observed.cursor
                del observed, payload, snapshot
                return frame, head_cursor, missing

        def read_failure():
            if not self.publication_valid(publication_stamp):
                return "event: reconnect\ndata: {}\n\n"
            return 'event: unavailable\ndata: {"reason":"history_unavailable"}\n\n'

        frame = first_frame
        first_frame = None
        replaying = first_status == "event"
        if first_status == "resync":
            # Drop the probe head before the marker and read a new framed head
            # only after the client has reset its cursor/deduplication state.
            frame = None
            yield (
                "event: resync_required\ndata: "
                + json.dumps({"reason": resync_reason}, separators=(",", ":"))
                + "\n\n"
            )
            if not await authorize():
                yield "event: authorization_lost\ndata: {}\n\n"
                return
            if not self.publication_valid(publication_stamp):
                yield "event: reconnect\ndata: {}\n\n"
                return
            try:
                frame, cursor, _missing = await read_fresh_head()
            except (HTTPException, StaleLeaseError):
                yield read_failure()
                return
            replaying = False

        if frame is not None:
            try:
                cursor = _frame_cursor(frame)
                error = await publication_error()
                if error is not None:
                    frame = None
                    yield error
                    return
                yield frame
                last_sent = time.monotonic()
            finally:
                frame = None

        while True:
            if self._closed or generation != self._generation:
                yield "event: reconnect\ndata: {}\n\n"
                return
            if time.monotonic() - started >= 1800:
                yield "event: reconnect\ndata: {}\n\n"
                return

            if not replaying:
                await asyncio.sleep(self.poll_seconds)
            if not await authorize():
                yield "event: authorization_lost\ndata: {}\n\n"
                return
            if not self.publication_valid(publication_stamp):
                yield "event: reconnect\ndata: {}\n\n"
                return

            try:
                if replaying:
                    async with channel_lock:
                        if not self.publication_valid(publication_stamp):
                            raise StaleLeaseError("publication changed")
                        result = self._replay.next_after(lease, cursor)
                    if result.status == "event":
                        frame, cursor = result.frame, result.cursor
                        del result
                        error = await publication_error()
                        if error is not None:
                            frame = None
                            yield error
                            return
                        yield frame
                        frame = None
                        last_sent = time.monotonic()
                        continue
                    if result.status == "caught_up":
                        del result
                        replaying = False
                        continue
                    del result
                    yield 'event: resync_required\ndata: {"reason":"cursor_gap"}\n\n'
                    if not await authorize():
                        yield "event: authorization_lost\ndata: {}\n\n"
                        return
                    if not self.publication_valid(publication_stamp):
                        yield "event: reconnect\ndata: {}\n\n"
                        return
                    try:
                        frame, cursor, _missing = await read_fresh_head()
                    except (HTTPException, StaleLeaseError):
                        yield read_failure()
                        return
                    if not self.publication_valid(publication_stamp):
                        frame = None
                        yield "event: reconnect\ndata: {}\n\n"
                        return
                    if frame is not None:
                        cursor = _frame_cursor(frame)
                        error = await publication_error()
                        if error is not None:
                            frame = None
                            yield error
                            return
                        yield frame
                        frame = None
                        last_sent = time.monotonic()
                    replaying = False
                    continue

                frame, head_cursor, missing_selection = await read_fresh_head()
            except (HTTPException, StaleLeaseError):
                yield read_failure()
                return

            if missing_selection and not selected_bootstrap_only:
                selected_bootstrap_only = True
                frame = None
                yield 'event: resync_required\ndata: {"reason":"selected_detail_missing"}\n\n'
                if not await authorize():
                    yield "event: authorization_lost\ndata: {}\n\n"
                    return
                if not self.publication_valid(publication_stamp):
                    yield "event: reconnect\ndata: {}\n\n"
                    return
                try:
                    frame, cursor, _missing = await read_fresh_head()
                except (HTTPException, StaleLeaseError):
                    yield read_failure()
                    return
                replaying = False
            elif selected_bootstrap_only:
                # Once selected detail is known missing, never walk retained
                # history for that selection. Publish only a changed fresh head.
                if head_cursor != cursor:
                    cursor = head_cursor
                else:
                    frame = None
                replaying = False
            else:
                frame = None
                try:
                    async with channel_lock:
                        if not self.publication_valid(publication_stamp):
                            raise StaleLeaseError("publication changed")
                        result = self._replay.next_after(lease, cursor)
                except StaleLeaseError:
                    yield "event: reconnect\ndata: {}\n\n"
                    return
                if result.status == "event":
                    frame, cursor = result.frame, result.cursor
                    replaying = True
                    del result
                elif result.status == "resync":
                    del result
                    yield 'event: resync_required\ndata: {"reason":"cursor_gap"}\n\n'
                    if not await authorize():
                        yield "event: authorization_lost\ndata: {}\n\n"
                        return
                    if not self.publication_valid(publication_stamp):
                        yield "event: reconnect\ndata: {}\n\n"
                        return
                    try:
                        frame, cursor, _missing = await read_fresh_head()
                    except (HTTPException, StaleLeaseError):
                        yield read_failure()
                        return
                    replaying = False
                else:
                    del result
                    cursor = head_cursor

            if frame is not None:
                try:
                    cursor = _frame_cursor(frame)
                    error = await publication_error()
                    if error is not None:
                        frame = None
                        yield error
                        return
                    yield frame
                    last_sent = time.monotonic()
                finally:
                    frame = None
            elif time.monotonic() - last_sent >= 15:
                error = await publication_error()
                if error is not None:
                    yield error
                    return
                yield ": keepalive\n\n"
                last_sent = time.monotonic()


def create_activity_router(
    *, get_settings, require_admin, poll_seconds=0.5, settings_lock=None
):
    service = ActivityAdmin(
        get_settings,
        require_admin,
        poll_seconds=poll_seconds,
        settings_lock=settings_lock,
    )

    @asynccontextmanager
    async def lifespan(_app):
        service.start()
        try:
            yield
        finally:
            if service._control_tasks:
                await asyncio.gather(*service._control_tasks, return_exceptions=True)
            service.close()

    router = APIRouter(
        prefix="/api/activity", dependencies=[Depends(require_admin)], lifespan=lifespan
    )
    # Explicit close access is useful to hosts that do not run ASGI lifespan.
    router.activity_service = service

    @router.get("/status")
    async def status(response: Response):
        response.headers.update(_NO_STORE)
        settings = get_settings()
        if settings is None:
            raise HTTPException(503, "Server not initialized")
        return {
            "health": service.health(),
            "settings": settings.activity.to_dict(),
            "config_version": _settings_version(settings.activity),
        }

    @router.put("/settings")
    async def update_settings(
        update: ActivityUpdate, request: Request, response: Response
    ):
        if request.headers.get("x-activity-action") != "settings":
            raise HTTPException(400, "Explicit Activity action header required")
        values = update.model_dump(exclude_unset=True)
        if any(value is None for value in values.values()):
            raise HTTPException(400, "Activity settings cannot be null")
        version = request.headers.get("if-match")
        result = await service.control(
            lambda: service.apply_settings(values, version), mutation=True
        )
        response.headers.update(_NO_STORE)
        return result

    @router.delete("/requests/{request_id}")
    async def delete_request(
        request_id: str,
        confirmation: ActivityConfirmation,
        request: Request,
        response: Response,
    ):
        if (
            confirmation.confirm != "delete"
            or request.headers.get("x-activity-action") != "delete"
        ):
            raise HTTPException(400, "Explicit delete confirmation required")
        if len(request_id) > 128:
            raise HTTPException(404, "Activity request not retained")
        result = await service.control(
            lambda: service.remove(request_id), mutation=True
        )
        response.headers.update(_NO_STORE)
        return result

    @router.post("/clear")
    async def clear(
        confirmation: ActivityConfirmation, request: Request, response: Response
    ):
        if (
            confirmation.confirm != "clear"
            or request.headers.get("x-activity-action") != "clear"
        ):
            raise HTTPException(400, "Explicit clear confirmation required")
        result = await service.control(service.remove, mutation=True)
        response.headers.update(_NO_STORE)
        return result

    @router.get("/requests")
    async def requests_page(
        response: Response,
        limit: int = Query(50, ge=1, le=200),
        cursor: str | None = Query(None, max_length=512),
        scope: Literal["all", "active", "recent"] = "all",
        model_id: str | None = Query(None, max_length=256),
        source_id: str | None = Query(None, max_length=256),
        state: str | None = Query(None, max_length=32),
        since: float | None = None,
        until: float | None = None,
    ):
        _validate_activity_filters(state, since, until)
        before, reader = _decode_cursor(cursor), service.reader()
        rows = await service.read(
            lambda: reader.list_requests(
                limit=limit,
                before=before,
                model_id=model_id,
                source_id=source_id,
                active=True
                if scope == "active"
                else False
                if scope == "recent"
                else None,
                state=state,
                since=since,
                until=until,
            )
        )
        response.headers.update(_NO_STORE)
        return {
            "requests": rows,
            "next_cursor": _encode_cursor(rows[-1]) if len(rows) == limit else None,
        }

    @router.get("/facets")
    async def facets(response: Response):
        reader = service.reader()
        response.headers.update(_NO_STORE)
        return await service.read(reader.facets)

    @router.get("/requests/{request_id}")
    async def detail(request_id: str, response: Response):
        if len(request_id) > 128:
            raise HTTPException(404, "Activity request not retained")
        reader = service.reader()
        result = await service.read(lambda: reader.detail(request_id))
        if result is None:
            raise HTTPException(404, "Activity request not retained")
        response.headers.update(_NO_STORE)
        return result

    @router.get("/requests/{request_id}/invocations/{invocation_id}")
    async def invocation(request_id: str, invocation_id: str, response: Response):
        if len(request_id) > 128 or len(invocation_id) > 3:
            raise HTTPException(404, "Activity invocation not retained")
        reader = service.reader()
        result = await service.read(
            lambda: reader.invocation(request_id, invocation_id)
        )
        if result is None:
            raise HTTPException(404, "Activity invocation not retained")
        response.headers.update(_NO_STORE)
        return result

    @router.get("/requests/{request_id}/content")
    async def content(
        request_id: str,
        representation: str = Query(..., max_length=32),
        invocation_id: str = Query("0", max_length=128),
        offset: int = Query(0, ge=0),
        limit: int = Query(65536, ge=1, le=MAX_READ_BYTES),
    ):
        if representation not in REPRESENTATIONS:
            raise HTTPException(400, "Unknown Activity representation")
        if len(request_id) > 128:
            raise HTTPException(404, "Activity request not retained")
        reader = service.reader()

        def read_content():
            return reader.content(
                request_id,
                representation,
                invocation_id=invocation_id,
                offset=offset,
                limit=limit,
                require_exists=True,
            )

        page = await service.read(read_content)
        if page is None:
            raise HTTPException(404, "Activity request not retained")
        return Response(
            page["data"],
            media_type="application/octet-stream",
            headers={
                **_NO_STORE,
                "X-Activity-Offset": str(page["offset"]),
                "X-Activity-Next-Offset": str(page["next_offset"]),
                "X-Activity-Total-Bytes": str(page["total_bytes"]),
                "X-Activity-Encoding": "utf-8",
            },
        )

    @router.get("/events")
    async def events(
        request: Request,
        request_id: str | None = Query(None, max_length=128),
        cursor: str | None = Query(None, max_length=_MAX_REPLAY_CURSOR),
        scope: Literal["all", "active", "recent"] = "all",
        model_id: str | None = Query(None, max_length=256),
        source_id: str | None = Query(None, max_length=256),
        state: str | None = Query(None, max_length=32),
        since: float | None = None,
        until: float | None = None,
    ):
        _validate_activity_filters(state, since, until)
        release = service.reserve_stream()
        channel_digest = _activity_channel_digest(
            request_id, model_id, source_id, scope, state, since, until
        )
        lease = None
        lock = None
        try:
            publication_stamp = service.publication_stamp()
            if publication_stamp is None:
                raise HTTPException(
                    503,
                    "Activity history is changing; retry the request",
                    headers={"Retry-After": "1"},
                )
            lease, lock = service.acquire_replay(channel_digest, publication_stamp)
            header_cursor = request.headers.get("last-event-id")
            replay_cursor = header_cursor if header_cursor is not None else cursor
            if replay_cursor is not None and (
                not replay_cursor.isascii() or len(replay_cursor) > _MAX_REPLAY_CURSOR
            ):
                # Keep malformed opaque IDs untrusted; the bounded buffer maps
                # them to a fresh-head resync without parsing or allocating by ID.
                replay_cursor = "!invalid-cursor!"
            reader = service.reader()
            async with lock:
                if not service.publication_valid(publication_stamp):
                    raise HTTPException(
                        503,
                        "Activity history changed; retry the request",
                        headers={"Retry-After": "1"},
                    )
                # The committed initial read and sequence publication share the
                # same per-channel lock as every poll.
                initial = await service.read(
                    lambda: reader.snapshot(
                        request_id=request_id,
                        model_id=model_id,
                        source_id=source_id,
                        scope=scope,
                        state=state,
                        since=since,
                        until=until,
                    )
                )
                if not service.publication_valid(publication_stamp):
                    raise HTTPException(
                        503,
                        "Activity history changed; retry the request",
                        headers={"Retry-After": "1"},
                    )
                payload = service.replay_payload(initial)
                observed = service._replay.observe(lease, payload)
                missing_selected = (
                    request_id is not None and initial.get("detail") is None
                )
                head_frame = observed.frame
                del observed, payload, initial
                if replay_cursor is None:
                    first_status, first_frame = "head", head_frame
                elif missing_selected:
                    first_status, first_frame = "resync", head_frame
                else:
                    replay = service._replay.next_after(lease, replay_cursor)
                    if replay.status == "event":
                        head_frame = None
                        first_status, first_frame = "event", replay.frame
                    elif replay.status == "caught_up":
                        first_status, first_frame = "head", head_frame
                    else:
                        first_status, first_frame = "resync", head_frame
                    del replay

            finalized = False
            finalizer_lock = threading.Lock()

            def release_all():
                nonlocal finalized
                with finalizer_lock:
                    if finalized:
                        return
                    finalized = True
                if lease is not None:
                    service.release_replay(channel_digest, lease)
                release()

            return _StreamResponse(
                service.replay_snapshots(
                    request,
                    reader,
                    request_id,
                    model_id,
                    source_id,
                    scope,
                    state,
                    since,
                    until,
                    publication_stamp,
                    lease,
                    lock,
                    first_status,
                    first_frame,
                    replay_cursor,
                    "selected_detail_missing"
                    if missing_selected and replay_cursor is not None
                    else "cursor_unavailable",
                    missing_selected,
                ),
                release_all,
            )
        except BaseException:
            if lease is not None:
                service.release_replay(channel_digest, lease)
            release()
            raise

    @router.get("/requests/{request_id}/attachments/{attachment_id}")
    async def attachment(request: Request, request_id: str, attachment_id: str):
        if len(request_id) > 128 or len(attachment_id) > 64:
            raise HTTPException(404, "Activity image not retained")
        release = service.reserve_stream()
        try:
            reader = service.reader()
            first = await service.read(
                lambda: reader.attachment(request_id, attachment_id)
            )
            if first is None:
                raise HTTPException(404, "Activity image not retained")
            info = first["metadata"]
            size, mime = first["size"], info.get("mime_type")
            if (
                mime not in IMAGE_FORMATS
                or info.get("status") != "retained"
                or info.get("validation") != "observer_container_checked"
                or type(size) is not int
                or not 0 < size <= MAX_PREVIEW_BYTES
                or size != info.get("bytes")
            ):
                raise HTTPException(503, "Activity image unavailable")

            async def body():
                page, offset = first, 0
                started = time.monotonic()
                while page and offset < size and time.monotonic() - started < 60:
                    try:
                        authorization = service.require_admin(request)
                        if inspect.isawaitable(authorization):
                            await authorization
                    except Exception:
                        return
                    if (
                        page["size"] != size
                        or page["metadata"] != info
                        or not page["data"]
                    ):
                        return
                    yield page["data"]
                    offset += len(page["data"])
                    page = None
                    if offset < size:
                        page = await service.read(
                            lambda offset=offset: reader.attachment(
                                request_id, attachment_id, offset=offset
                            )
                        )

            extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}[
                mime
            ]
            return _StreamResponse(
                body(),
                release,
                media_type=mime,
                headers={
                    "Content-Length": str(size),
                    "Content-Disposition": f'inline; filename="activity-image.{extension}"',
                    "Content-Security-Policy": "default-src 'none'; sandbox",
                },
            )
        except BaseException:
            release()
            raise

    return router
