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

from .runtime import get_recorder
from .store import MAX_READ_BYTES, REPRESENTATIONS, STATES, ActivityReader

_NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_MAX_READERS = 4
_MAX_STREAMS = 8
_MAX_EVENT_BYTES = 1024 * 1024


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


class _StreamResponse(StreamingResponse):
    """Release admission even if the body iterator never starts."""

    def __init__(self, body, release):
        super().__init__(
            body,
            media_type="text/event-stream",
            headers={**_NO_STORE, "X-Accel-Buffering": "no"},
        )
        self._release = weakref.finalize(self, release)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._release()


class ActivityAdmin:
    def __init__(self, get_settings, require_admin, *, poll_seconds=0.5):
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

    async def read(self, operation):
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
        try:
            # Keep admission until the actual worker completes, even if the
            # HTTP caller disconnects. Cancellation cannot grow the work queue.
            return await asyncio.shield(asyncio.wrap_future(future))
        except (sqlite3.Error, OSError, ValueError, zlib.error) as exc:
            raise HTTPException(503, "Activity history unavailable") from exc

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
            "replay": "snapshot_only",
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

    async def snapshots(self, request, reader, selection, model_id, source_id, initial):
        epoch, sequence, previous = uuid.uuid4().hex, 0, None
        generation = self._generation
        started, last_sent = time.monotonic(), time.monotonic()
        if request.headers.get("last-event-id"):
            yield 'event: resync_required\ndata: {"reason":"snapshot_only_reconnect"}\n\n'
        snapshot = initial
        while True:
            if self._closed or generation != self._generation:
                yield "event: reconnect\ndata: {}\n\n"
                return
            try:
                # Revalidate signed sessions/main keys throughout a long stream.
                authorization = self.require_admin(request)
                if inspect.isawaitable(authorization):
                    await authorization
            except Exception:
                yield "event: authorization_lost\ndata: {}\n\n"
                return
            payload = json.dumps(
                {**snapshot, "health": self.health()},
                ensure_ascii=True,
                separators=(",", ":"),
            )
            if len(payload) > _MAX_EVENT_BYTES:
                yield 'event: unavailable\ndata: {"reason":"snapshot_size_limit"}\n\n'
                return
            signature = hashlib.sha256(payload.encode()).digest()
            if signature != previous:
                sequence += 1
                yield f"id: {epoch}:{sequence}\nevent: snapshot\ndata: {payload}\n\n"
                previous, last_sent = signature, time.monotonic()
            elif time.monotonic() - last_sent >= 15:
                yield ": keepalive\n\n"
                last_sent = time.monotonic()
            if time.monotonic() - started >= 1800:
                yield "event: reconnect\ndata: {}\n\n"
                return
            await asyncio.sleep(self.poll_seconds)
            try:
                snapshot = await self.read(
                    lambda: reader.snapshot(
                        request_id=selection, model_id=model_id, source_id=source_id
                    )
                )
            except HTTPException:
                yield 'event: unavailable\ndata: {"reason":"history_unavailable"}\n\n'
                return


def create_activity_router(*, get_settings, require_admin, poll_seconds=0.5):
    service = ActivityAdmin(get_settings, require_admin, poll_seconds=poll_seconds)

    @asynccontextmanager
    async def lifespan(_app):
        service.start()
        try:
            yield
        finally:
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
        return {"health": service.health(), "settings": settings.activity.to_dict()}

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
        if state is not None and state not in STATES:
            raise HTTPException(400, "Unknown Activity state")
        if any(
            value is not None and not math.isfinite(value) for value in (since, until)
        ):
            raise HTTPException(400, "Invalid Activity time range")
        if since is not None and until is not None and since > until:
            raise HTTPException(400, "Invalid Activity time range")
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
        model_id: str | None = Query(None, max_length=256),
        source_id: str | None = Query(None, max_length=256),
    ):
        release = service.reserve_stream()
        try:
            reader = service.reader()
            initial = await service.read(
                lambda: reader.snapshot(
                    request_id=request_id, model_id=model_id, source_id=source_id
                )
            )
            return _StreamResponse(
                service.snapshots(
                    request, reader, request_id, model_id, source_id, initial
                ),
                release,
            )
        except BaseException:
            release()
            raise

    return router
