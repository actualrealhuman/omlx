# SPDX-License-Identifier: Apache-2.0
"""Test adapter for exercising the production Activity replay generator."""

from omlx.activity.routes import _activity_channel_digest


async def replay_stream(
    service,
    request,
    reader,
    selection,
    model_id,
    source_id,
    initial,
    *,
    scope="all",
    state=None,
    since=None,
    until=None,
    publication_stamp=None,
):
    stamp = publication_stamp or service.publication_stamp()
    digest = _activity_channel_digest(
        selection, model_id, source_id, scope, state, since, until
    )
    lease, lock = service.acquire_replay(digest, stamp)
    try:
        cursor = request.headers.get("last-event-id")
        async with lock:
            payload = service.replay_payload(initial)
            observed = service._replay.observe(lease, payload)
            missing = selection is not None and initial.get("detail") is None
            head_frame = observed.frame
            del observed, payload
            if cursor is None:
                status, frame, reason = "head", head_frame, "cursor_unavailable"
            elif missing:
                status, frame, reason = "resync", None, "selected_detail_missing"
            else:
                result = service._replay.next_after(lease, cursor)
                if result.status == "event":
                    head_frame = None
                    status, frame, reason = "event", result.frame, "cursor_unavailable"
                elif result.status == "caught_up":
                    status, frame, reason = "head", head_frame, "cursor_unavailable"
                else:
                    status, frame, reason = "resync", None, "cursor_unavailable"
                del result
        async for event in service.replay_snapshots(
            request,
            reader,
            selection,
            model_id,
            source_id,
            scope,
            state,
            since,
            until,
            stamp,
            lease,
            lock,
            status,
            frame,
            cursor,
            reason,
            missing,
        ):
            yield event
    finally:
        service.release_replay(digest, lease)
