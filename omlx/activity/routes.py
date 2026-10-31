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
from .runtime import configure_recorder, get_recorder
from .store import MAX_READ_BYTES, REPRESENTATIONS, STATES, ActivityReader

_NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
_MAX_READERS = 4
_MAX_STREAMS = 8
_MAX_EVENT_BYTES = 1024 * 1024


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

    async def control(self, operation):
        # A task owns the shared settings lock through the actual worker result,
        # even when its HTTP caller disconnects. Admission bounds queued tasks.
        if self._controls >= 8:
            raise HTTPException(429, "Activity control capacity reached")
        self._controls += 1

        async def run():
            try:
                async with self._settings_lock:
                    return await self.read(operation)
            finally:
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
                recorder.update_settings(candidate).result(5)
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
        result = await service.control(lambda: service.apply_settings(values, version))
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
        result = await service.control(lambda: service.remove(request_id))
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
        result = await service.control(service.remove)
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
