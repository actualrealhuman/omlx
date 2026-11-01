# SPDX-License-Identifier: Apache-2.0
"""Admin access, read snapshots, limits and live delivery without inference."""

import asyncio
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from activity_replay_test_helpers import replay_stream
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from omlx.activity import ActivityRecorder, ActivitySettings
from omlx.activity.routes import create_activity_router
from omlx.activity.runtime import RequestCapture, configure_recorder
from omlx.admin import auth
from omlx.settings import GlobalSettings, SubKeyEntry


class ActivityRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = GlobalSettings(base_path=self.root)
        self.settings.auth.api_key = "synthetic-owner-only"
        self.settings.auth.sub_keys = [
            SubKeyEntry(key="synthetic-inference-only", name="Client")
        ]
        for attribute, value in (
            ("_get_global_settings", lambda: self.settings),
            ("_key_failures", {}),
        ):
            change = patch.object(auth, attribute, value)
            change.start()
            self.addCleanup(change.stop)
        self.recorder = ActivityRecorder(
            self.root / "activity.sqlite3", ActivitySettings(enabled=True)
        )
        self.assertTrue(self.recorder.wait_ready())
        previous = configure_recorder(self.recorder)
        self.addCleanup(lambda: configure_recorder(previous))
        self.addCleanup(self.recorder.close)
        self.router = create_activity_router(
            get_settings=lambda: self.settings,
            require_admin=auth.require_admin,
            poll_seconds=0.001,
        )
        self.service = self.router.activity_service
        self.addCleanup(self.service.close)
        self.app = FastAPI()
        self.app.include_router(self.router, prefix="/admin")
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"authorization": "Bearer synthetic-owner-only"}

    def request(
        self,
        *,
        model="model-a",
        source="source-a",
        terminal=True,
        text="payload",
        **kwargs,
    ):
        handle = self.recorder.begin(
            model_id=model,
            endpoint="/v1/messages",
            source_id=source,
            source_name="Example client",
            **kwargs,
        )
        self.assertIsNotNone(handle)
        self.recorder.append_text(
            handle,
            "submitted",
            json.dumps({"messages": [{"role": "user", "content": text}]}),
        )
        self.recorder.append_text(handle, "raw_output", "answer", invocation_id="1")
        if terminal:
            self.recorder.finish(handle)
        self.assertTrue(self.recorder.flush())
        return handle

    def get(self, path, **kwargs):
        return self.client.get(
            "/admin/api/activity" + path, headers=self.headers, **kwargs
        )

    def test_named_inference_key_and_anonymous_cannot_read_any_history(self):
        handle = self.request()
        for suffix in (
            "/status",
            "/requests",
            "/facets",
            f"/requests/{handle.request_id}",
            f"/requests/{handle.request_id}/content?representation=submitted",
            f"/requests/{handle.request_id}/invocations/1",
            "/events",
        ):
            for headers in ({}, {"authorization": "Bearer synthetic-inference-only"}):
                result = self.client.get(
                    "/admin/api/activity" + suffix, headers=headers
                )
                self.assertEqual(result.status_code, 401)
            # A successful main-key read clears the shared failed-key budget.
            self.assertEqual(self.get("/status").status_code, 200)

    def test_status_and_history_are_available_when_capture_is_disabled(self):
        handle = self.request()
        self.recorder.close()
        configure_recorder(None)
        status = self.get("/status")
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["health"]["enabled"])
        self.assertEqual(status.json()["settings"]["max_storage_bytes"], 5 * 1024**3)
        self.assertEqual(self.get(f"/requests/{handle.request_id}").status_code, 200)
        self.assertEqual(status.headers["cache-control"], "no-store")

    def test_list_does_not_read_context_blobs_and_supports_stable_pagination(self):
        ids = [self.request(received_at=time.time() + i).request_id for i in range(4)]
        with patch.object(
            self.recorder.reader.__class__,
            "content",
            side_effect=AssertionError("eager context read"),
        ):
            page = self.get("/requests?limit=2").json()
            next_page = self.get(
                "/requests", params={"limit": 2, "cursor": page["next_cursor"]}
            ).json()
        self.assertEqual(
            {row["id"] for row in page["requests"] + next_page["requests"]}, set(ids)
        )
        self.assertNotIn("payload", json.dumps(page))
        for cursor in ("bad!", "bnVsbA", "WzBd"):
            self.assertEqual(
                self.get("/requests", params={"cursor": cursor}).status_code, 400
            )

    def test_filters_and_facets(self):
        self.request(model="model-a", source="source-a")
        active = self.request(model="model-b", source="source-b", terminal=False)
        self.assertEqual(len(self.get("/requests?scope=recent").json()["requests"]), 1)
        self.assertEqual(
            self.get("/requests?scope=active").json()["requests"][0]["id"],
            active.request_id,
        )
        self.assertEqual(
            self.get(
                "/requests", params={"model_id": "model-a", "source_id": "source-a"}
            ).json()["requests"][0]["source_id"],
            "source-a",
        )
        self.assertEqual(
            self.get("/requests", params={"model_id": "x' OR 1=1 --"}).json()[
                "requests"
            ],
            [],
        )
        self.assertEqual(self.get("/requests?state=not-real").status_code, 400)
        self.assertEqual(self.get("/requests?since=nan").status_code, 400)
        self.assertEqual(self.get("/requests?since=10&until=1").status_code, 400)
        self.assertEqual(
            set(self.get("/facets").json()["models"]), {"model-a", "model-b"}
        )

    def test_snapshot_applies_full_filter_set_and_checks_selected_row_independently(
        self,
    ):
        base = time.time() - 100
        selected = None
        for index in range(55):
            handle = self.request(
                model="model-a",
                source="source-a",
                received_at=base + index,
            )
            if index == 0:
                selected = handle
        reader = self.recorder.reader
        matching = reader.snapshot(
            request_id=selected.request_id,
            scope="recent",
            model_id="model-a",
            source_id="source-a",
            state="completed",
            since=base,
            until=base + 54,
        )
        self.assertEqual(len(matching["recent"]), 50)
        self.assertTrue(matching["recent_more"])
        self.assertFalse(matching["active"])
        self.assertFalse(matching["selected_outside_filters"])
        self.assertEqual(matching["detail"]["id"], selected.request_id)
        for filters in (
            {"scope": "active"},
            {"model_id": "other"},
            {"source_id": "other"},
            {"state": "error"},
            {"since": base + 1},
            {"until": base - 1},
        ):
            excluded = reader.snapshot(request_id=selected.request_id, **filters)
            self.assertTrue(excluded["selected_outside_filters"], filters)
            self.assertEqual(excluded["detail"]["id"], selected.request_id)
        self.assertIsNone(
            reader.snapshot(request_id="missing")["selected_outside_filters"]
        )
        defaults = reader.snapshot()
        self.assertIn("active", defaults)
        self.assertIn("recent", defaults)

    def test_detail_indexes_are_metadata_only_and_versioned(self):
        handle = self.request(terminal=False)
        first = self.get(f"/requests/{handle.request_id}").json()
        self.assertEqual(
            {item["representation"] for item in first["representations"]},
            {"submitted", "raw_output"},
        )
        self.assertNotIn("payload", json.dumps(first))
        self.assertEqual(first["capture_status"], "no_gaps_recorded")
        self.recorder.append_text(handle, "raw_output", " more", invocation_id="1")
        self.assertTrue(self.recorder.flush())
        second = self.get(f"/requests/{handle.request_id}").json()
        self.assertNotEqual(first["version"], second["version"])

    def test_content_ranges_preserve_raw_utf8_bytes_and_are_bounded(self):
        handle = self.request(text="世界🙂")
        expected = self.recorder.reader.content(handle.request_id, "submitted")["data"]
        path = f"/requests/{handle.request_id}/content"
        result = self.get(
            path, params={"representation": "submitted", "offset": 3, "limit": 9}
        )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content, expected[3:12])
        self.assertEqual(result.headers["x-activity-next-offset"], "12")
        self.assertEqual(result.headers["x-activity-total-bytes"], str(len(expected)))
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(result.headers["x-content-type-options"], "nosniff")
        self.assertEqual(
            self.get(
                path, params={"representation": "submitted", "limit": 2 * 1024**2}
            ).status_code,
            422,
        )
        self.assertEqual(
            self.get(path, params={"representation": "not-real"}).status_code, 400
        )
        self.assertEqual(
            self.get("/requests/missing/content?representation=submitted").status_code,
            404,
        )

    def test_empty_disabled_history_does_not_create_a_database(self):
        self.recorder.close()
        configure_recorder(None)
        for file in self.root.glob("activity.sqlite3*"):
            file.unlink()
        self.assertEqual(self.get("/requests").json()["requests"], [])
        self.assertEqual(self.get("/facets").json()["models"], [])
        self.assertEqual(self.get("/requests/missing").status_code, 404)
        self.assertEqual(
            self.get("/requests/missing/content?representation=submitted").status_code,
            404,
        )
        self.assertEqual(list(self.root.glob("activity.sqlite3*")), [])

    def test_newer_history_schema_is_rejected_and_preserved(self):
        self.recorder.close()
        configure_recorder(None)
        with sqlite3.connect(self.recorder.path) as db:
            db.execute("PRAGMA user_version=99")
        result = self.get("/requests")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.json()["detail"], "Activity history unavailable")
        with sqlite3.connect(self.recorder.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 99)

    def test_sse_snapshot_resync_and_session_revalidation(self):
        handle = self.request(terminal=False)
        calls = []

        async def authorization(request):
            calls.append(True)
            if len(calls) > 1:
                raise HTTPException(401)

        self.service.require_admin = authorization
        request = Request({"type": "http", "headers": [(b"last-event-id", b"old:9")]})

        async def check():
            reader = self.service.reader()
            initial = reader.snapshot(request_id=handle.request_id)
            stream = replay_stream(
                self.service, request, reader, handle.request_id, None, None, initial
            )
            self.assertIn("resync_required", await anext(stream))
            self.assertIn("authorization_lost", await anext(stream))
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)

        asyncio.run(check())

    def test_sse_and_page_validate_and_apply_matching_filters(self):
        base = time.time() - 10
        completed = self.request(received_at=base)
        self.request(terminal=False, received_at=base + 1)
        params = {
            "scope": "recent",
            "state": "completed",
            "since": str(base - 1),
            "until": str(base + 1),
        }
        page = self.get("/requests", params=params)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(
            [row["id"] for row in page.json()["requests"]], [completed.request_id]
        )
        self.assertEqual(
            self.get("/events", params={**params, "state": "bad"}).status_code,
            400,
        )
        self.assertEqual(
            self.get("/events", params={**params, "scope": "bad"}).status_code,
            422,
        )
        self.assertEqual(
            self.get("/events", params={**params, "since": "nan"}).status_code,
            400,
        )
        self.assertEqual(
            self.get("/events", params={**params, "until": str(base - 2)}).status_code,
            400,
        )
        with self.client.stream(
            "GET", "/admin/api/activity/events", params=params, headers=self.headers
        ) as response:
            self.assertEqual(response.status_code, 200)
            lines = response.iter_lines()
            event = next(line for line in lines if line == "event: snapshot")
            self.assertEqual(event, "event: snapshot")
            payload = json.loads(
                next(line[6:] for line in lines if line.startswith("data: "))
            )
            self.assertEqual(
                [row["id"] for row in payload["recent"]], [completed.request_id]
            )
            self.assertEqual(payload["active"], [])

    def test_events_replay_one_new_snapshot_and_header_cursor_wins(self):
        selected = self.request(terminal=False)
        params = {"request_id": selected.request_id}

        def first_event(lines):
            frame = []
            for line in lines:
                frame.append(line)
                if line == "":
                    return frame
            self.fail("stream ended before its first frame")

        with TestClient(self.app) as client:
            with client.stream(
                "GET",
                "/admin/api/activity/events",
                params=params,
                headers=self.headers,
            ) as response:
                lines = response.iter_lines()
                initial_frame = first_event(lines)
                old_cursor = next(
                    line[4:] for line in initial_frame if line.startswith("id: ")
                )
                self.recorder.finish(selected)
                self.assertTrue(self.recorder.flush())
                updated_frame = first_event(lines)
                self.assertNotEqual(
                    next(line[4:] for line in updated_frame if line.startswith("id: ")),
                    old_cursor,
                )
            with client.stream(
                "GET",
                "/admin/api/activity/events",
                params={**params, "cursor": "invalid-query-cursor"},
                headers={**self.headers, "last-event-id": old_cursor},
            ) as response:
                replay_frame = first_event(response.iter_lines())
        self.assertIn("event: snapshot", replay_frame, repr(replay_frame))
        replay_cursor = next(
            line[4:] for line in replay_frame if line.startswith("id: ")
        )
        self.assertNotEqual(replay_cursor, old_cursor)
        self.assertEqual(replay_cursor.split(":")[:2], old_cursor.split(":")[:2])
        payload = json.loads(
            next(line[6:] for line in replay_frame if line.startswith("data: "))
        )
        self.assertIn(selected.request_id, json.dumps(payload))

    def test_missing_selected_detail_never_replays_contiguous_old_snapshot(self):
        selected = self.request(terminal=False)
        fresh_missing = {
            "active": [],
            "recent": [],
            "recent_more": False,
            "selected_outside_filters": None,
            "detail": None,
        }
        with TestClient(self.app) as client:
            with client.stream(
                "GET",
                "/admin/api/activity/events",
                params={"request_id": selected.request_id},
                headers=self.headers,
            ) as response:
                first_frame = []
                for line in response.iter_lines():
                    first_frame.append(line)
                    if line == "":
                        break
            old_cursor = next(
                line[4:] for line in first_frame if line.startswith("id: ")
            )
            with (
                patch.object(
                    self.service.reader().__class__,
                    "snapshot",
                    return_value=fresh_missing,
                ),
                client.stream(
                    "GET",
                    "/admin/api/activity/events",
                    params={
                        "request_id": selected.request_id,
                        "cursor": old_cursor,
                    },
                    headers=self.headers,
                ) as response,
            ):
                lines = response.iter_lines()
                first = next(lines)
                self.assertEqual(first, "event: resync_required")
                for line in lines:
                    if line == "event: snapshot":
                        break
                data = next(line for line in lines if line.startswith("data: "))
                payload = json.loads(data[6:])
                self.assertIsNone(payload["detail"])
                self.assertEqual(payload["active"], [])
                self.assertEqual(payload["recent"], [])

    def test_second_viewer_receives_head_published_by_first_viewer(self):
        selected = self.request(terminal=False)
        with (
            TestClient(self.app) as client,
            client.stream(
                "GET",
                "/admin/api/activity/events",
                params={"request_id": selected.request_id},
                headers=self.headers,
            ) as second_viewer,
        ):
            second_lines = second_viewer.iter_lines()
            old_frame = []
            for line in second_lines:
                old_frame.append(line)
                if line == "":
                    break
            old_cursor = next(line[4:] for line in old_frame if line.startswith("id: "))

            self.recorder.finish(selected)
            self.assertTrue(self.recorder.flush())
            with client.stream(
                "GET",
                "/admin/api/activity/events",
                params={"request_id": selected.request_id},
                headers=self.headers,
            ) as first_viewer:
                first_frame = []
                for line in first_viewer.iter_lines():
                    first_frame.append(line)
                    if line == "":
                        break
            published_cursor = next(
                line[4:] for line in first_frame if line.startswith("id: ")
            )
            delivered = []
            for line in second_lines:
                delivered.append(line)
                if line == "":
                    break
            delivered_cursor = next(
                line[4:] for line in delivered if line.startswith("id: ")
            )
            self.assertNotEqual(old_cursor, published_cursor)
            self.assertEqual(delivered_cursor, published_cursor)

    def test_filtered_sse_refresh_uses_same_predicates_and_reader_admission(self):
        base = time.time() - 10
        first = self.request(received_at=base)
        self.request(model="other", received_at=base)
        self.service.require_admin = lambda request: None
        request = Request({"type": "http", "headers": []})
        filters = dict(scope="recent", state="completed", since=base, until=base + 5)

        async def check():
            reader = self.service.reader()
            initial = reader.snapshot(
                model_id="model-a", source_id="source-a", **filters
            )
            reads = []
            admitted_read = self.service.read

            async def counted_read(function):
                reads.append(True)
                return await admitted_read(function)

            with patch.object(self.service, "read", counted_read):
                stream = replay_stream(
                    self.service,
                    request,
                    reader,
                    None,
                    "model-a",
                    "source-a",
                    initial,
                    **filters,
                )
                try:
                    payload = json.loads(
                        (await anext(stream)).decode().split("data: ", 1)[1]
                    )
                    self.assertEqual(
                        [row["id"] for row in payload["recent"]], [first.request_id]
                    )
                    second = self.request(received_at=base + 1)
                    self.request(received_at=base + 8)
                    self.request(terminal=False, received_at=base + 2)
                    event = await asyncio.wait_for(anext(stream), 3)
                    payload = json.loads(event.decode().split("data: ", 1)[1])
                    self.assertEqual(
                        {row["id"] for row in payload["recent"]},
                        {first.request_id, second.request_id},
                    )
                    self.assertEqual(payload["active"], [])
                    self.assertTrue(reads)
                finally:
                    await stream.aclose()

        asyncio.run(check())

    def test_stream_admission_is_bounded_and_release_is_idempotent(self):
        release = [self.service.reserve_stream() for _ in range(8)]
        with self.assertRaises(HTTPException) as error:
            self.service.reserve_stream()
        self.assertEqual(error.exception.status_code, 429)
        for close in release:
            close()
            close()
        self.assertEqual(self.service._streams, set())

    def test_replay_coordinator_rejects_active_loop_rebind_and_allows_idle_rebind(self):
        digest = bytes.fromhex("01" * 32)

        async def acquire_first():
            return self.service.acquire_replay(digest, self.service.publication_stamp())

        lease, first_lock = asyncio.run(acquire_first())

        async def acquire_again():
            return self.service.acquire_replay(digest, self.service.publication_stamp())

        with self.assertRaises(HTTPException) as error:
            asyncio.run(acquire_again())
        self.assertEqual(error.exception.status_code, 503)
        self.service.release_replay(digest, lease)

        second_lease, second_lock = asyncio.run(acquire_again())
        self.assertIsNot(first_lock, second_lock)
        self.service.release_replay(digest, second_lease)
        self.assertEqual(self.service._coordinators, {})

    def test_replay_coordinator_serializes_same_channel_and_separates_channels(self):
        async def check():
            stamp = self.service.publication_stamp()
            shared = bytes.fromhex("02" * 32)
            separate = bytes.fromhex("03" * 32)
            first, first_lock = self.service.acquire_replay(shared, stamp)
            second, second_lock = self.service.acquire_replay(shared, stamp)
            third, third_lock = self.service.acquire_replay(separate, stamp)
            self.assertIs(first_lock, second_lock)
            self.assertIsNot(first_lock, third_lock)

            entered = asyncio.Event()

            async def wait_for_shared():
                entered.set()
                async with second_lock:
                    return True

            async with first_lock:
                waiter = asyncio.create_task(wait_for_shared())
                await entered.wait()
                await asyncio.sleep(0)
                self.assertFalse(waiter.done())
                async with third_lock:
                    self.assertFalse(waiter.done())
            self.assertTrue(await waiter)
            self.service.release_replay(shared, first)
            self.service.release_replay(shared, second)
            self.service.release_replay(separate, third)

        asyncio.run(check())

    def test_events_rejects_initial_read_when_source_changes_mid_read(self):
        original_read = self.service.read

        async def switched_source(operation):
            snapshot = await original_read(operation)
            self.settings.base_path = self.root / "replacement-source"
            return snapshot

        with patch.object(self.service, "read", switched_source):
            response = self.get("/events")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Activity history changed", response.json()["detail"])

    def test_actual_route_serializes_initial_read_after_existing_poll(self):
        handle = self.request(terminal=False)
        endpoint = next(
            route.endpoint
            for route in self.router.routes
            if getattr(route, "path", None) == "/api/activity/events"
        )
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/admin/api/activity/events",
            "headers": [(b"authorization", b"Bearer synthetic-owner-only")],
        }
        request = Request(scope)

        async def invoke_endpoint():
            return await endpoint(
                request,
                request_id=handle.request_id,
                cursor=None,
                scope="all",
                model_id=None,
                source_id=None,
                state=None,
                since=None,
                until=None,
            )

        async def check():
            first_response = await invoke_endpoint()
            first_frame = await anext(first_response.body_iterator)
            first_cursor = first_frame.split(b"\n", 1)[0][4:].decode("ascii")
            self.recorder.finish(handle)
            self.assertTrue(self.recorder.flush())

            original_read = self.service.read
            reader_type = type(self.service.reader())
            original_snapshot = reader_type.snapshot
            poll_entered = asyncio.Event()
            release_poll = asyncio.Event()
            second_route_started = asyncio.Event()
            read_calls = []
            snapshots = []

            async def gate_read(operation, *, on_submitted=None):
                read_calls.append(operation)
                if len(read_calls) == 1:
                    poll_entered.set()
                    await release_poll.wait()
                return await original_read(operation, on_submitted=on_submitted)

            def snapshot_then_publish(reader, **kwargs):
                result = original_snapshot(reader, **kwargs)
                snapshots.append(True)
                if len(snapshots) == 1:
                    self.request(text="arrived after the poll read")
                return result

            with (
                patch.object(self.service, "read", gate_read),
                patch.object(reader_type, "snapshot", snapshot_then_publish),
            ):
                poll_task = asyncio.create_task(anext(first_response.body_iterator))
                await asyncio.wait_for(poll_entered.wait(), 2)

                async def second_route():
                    second_route_started.set()
                    return await invoke_endpoint()

                route_task = asyncio.create_task(second_route())
                await asyncio.wait_for(second_route_started.wait(), 2)
                await asyncio.sleep(0)
                self.assertEqual(len(read_calls), 1)
                self.assertFalse(route_task.done())

                release_poll.set()
                poll_frame = await asyncio.wait_for(poll_task, 2)
                second_response = await asyncio.wait_for(route_task, 2)
                second_frame = await anext(second_response.body_iterator)
                poll_cursor = poll_frame.split(b"\n", 1)[0][4:].decode("ascii")
                second_cursor = second_frame.split(b"\n", 1)[0][4:].decode("ascii")
                self.assertNotEqual(first_cursor, poll_cursor)
                self.assertEqual(
                    poll_cursor.split(":")[:2], second_cursor.split(":")[:2]
                )
                self.assertLess(
                    int(poll_cursor.rsplit(":", 1)[1]),
                    int(second_cursor.rsplit(":", 1)[1]),
                )
                second_response._release()

            await first_response.body_iterator.aclose()
            first_response._release()

        asyncio.run(check())

    def test_actual_route_reads_independent_channels_concurrently(self):
        handle = self.request(terminal=False)
        endpoint = next(
            route.endpoint
            for route in self.router.routes
            if getattr(route, "path", None) == "/api/activity/events"
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/admin/api/activity/events",
                "headers": [(b"authorization", b"Bearer synthetic-owner-only")],
            }
        )

        async def invoke(*, model_id=None):
            return await endpoint(
                request,
                request_id=handle.request_id,
                cursor=None,
                scope="all",
                model_id=model_id,
                source_id=None,
                state=None,
                since=None,
                until=None,
            )

        async def check():
            response = await invoke()
            await anext(response.body_iterator)
            poll_entered = asyncio.Event()
            separate_entered = asyncio.Event()
            release_poll = asyncio.Event()
            original_read = self.service.read
            reads = 0

            async def gate_read(operation, *, on_submitted=None):
                nonlocal reads
                reads += 1
                if reads == 1:
                    poll_entered.set()
                    await release_poll.wait()
                elif reads == 2:
                    separate_entered.set()
                return await original_read(operation, on_submitted=on_submitted)

            with patch.object(self.service, "read", gate_read):
                poll = asyncio.create_task(anext(response.body_iterator))
                try:
                    await asyncio.wait_for(poll_entered.wait(), 2)
                    other = await asyncio.wait_for(invoke(model_id="other-model"), 2)
                    await asyncio.wait_for(separate_entered.wait(), 2)
                    self.assertEqual(reads, 2)
                    await anext(other.body_iterator)
                    other._release()
                finally:
                    release_poll.set()
                    poll.cancel()
                    await asyncio.gather(poll, return_exceptions=True)
                    await response.body_iterator.aclose()
                    response._release()

        asyncio.run(check())

    def test_unstarted_replay_response_rejects_close_start_generation(self):
        endpoint = next(
            route.endpoint
            for route in self.router.routes
            if getattr(route, "path", None) == "/api/activity/events"
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/admin/api/activity/events",
                "headers": [(b"authorization", b"Bearer synthetic-owner-only")],
            }
        )

        async def check():
            response = await endpoint(
                request,
                request_id=None,
                cursor=None,
                scope="all",
                model_id=None,
                source_id=None,
                state=None,
                since=None,
                until=None,
            )
            old_generation = response.body_iterator
            self.service.close()
            self.service.start()
            first = await anext(old_generation)
            self.assertIn("event: reconnect", first)
            self.assertNotIn("event: snapshot", first)
            await old_generation.aclose()
            response._release()
            self.assertEqual(self.service._coordinators, {})

        asyncio.run(check())

    def test_known_missing_selection_polls_once_without_reopening_history(self):
        endpoint = next(
            route.endpoint
            for route in self.router.routes
            if getattr(route, "path", None) == "/api/activity/events"
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/admin/api/activity/events",
                "headers": [(b"authorization", b"Bearer synthetic-owner-only")],
            }
        )

        async def check():
            response = await endpoint(
                request,
                request_id="missing-selected-request",
                cursor="malformed-cursor",
                scope="all",
                model_id=None,
                source_id=None,
                state=None,
                since=None,
                until=None,
            )
            body = response.body_iterator
            self.assertIn("resync_required", await anext(body))
            self.assertIn(b"event: snapshot", await anext(body))

            original_read = self.service.read
            original_next_after = self.service._replay.next_after
            read_count = 0
            poll_read_complete = asyncio.Event()
            next_after_calls = []

            async def counted_read(operation, *, on_submitted=None):
                nonlocal read_count
                read_count += 1
                result = await original_read(operation, on_submitted=on_submitted)
                if read_count == 1:
                    poll_read_complete.set()
                return result

            def counted_next_after(*args):
                next_after_calls.append(True)
                return original_next_after(*args)

            with (
                patch.object(self.service, "read", counted_read),
                patch.object(self.service._replay, "next_after", counted_next_after),
            ):
                poll = asyncio.create_task(anext(body))
                await asyncio.wait_for(poll_read_complete.wait(), 2)
                await asyncio.sleep(0)
                self.assertEqual(read_count, 1)
                self.assertEqual(next_after_calls, [])
                self.assertFalse(poll.done())
                poll.cancel()
                await asyncio.gather(poll, return_exceptions=True)

            await body.aclose()
            response._release()
            self.assertEqual(self.service._coordinators, {})

        asyncio.run(check())

    def test_rechecks_authorization_after_slow_head_reads_before_snapshot(self):
        handle = self.request(terminal=False)
        endpoint = next(
            route.endpoint
            for route in self.router.routes
            if getattr(route, "path", None) == "/api/activity/events"
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/admin/api/activity/events",
                "headers": [(b"authorization", b"Bearer synthetic-owner-only")],
            }
        )

        async def check():
            for phase in ("resync_bootstrap", "poll"):
                with self.subTest(phase=phase):
                    authorized = [True]
                    lock_ref = [None]
                    revoked_auth_lock_states = []

                    def require_admin(
                        _request,
                        authorized=authorized,
                        revoked_auth_lock_states=revoked_auth_lock_states,
                        lock_ref=lock_ref,
                    ):
                        if not authorized[0]:
                            revoked_auth_lock_states.append(lock_ref[0].locked())
                            raise HTTPException(401, "revoked")

                    self.service.require_admin = require_admin
                    response = await endpoint(
                        request,
                        request_id=None,
                        cursor="invalid-cursor"
                        if phase == "resync_bootstrap"
                        else None,
                        scope="all",
                        model_id=None,
                        source_id=None,
                        state=None,
                        since=None,
                        until=None,
                    )
                    body = response.body_iterator
                    if phase == "resync_bootstrap":
                        self.assertIn("resync_required", await anext(body))
                    else:
                        self.assertIn(b"event: snapshot", await anext(body))
                        self.recorder.append_text(
                            handle, "raw_output", "late metadata", invocation_id="1"
                        )
                        self.assertTrue(self.recorder.flush())

                    lock_ref[0] = next(
                        item["lock"] for item in self.service._coordinators.values()
                    )
                    original_read = self.service.read
                    read_finished = asyncio.Event()
                    return_read = asyncio.Event()

                    async def blocked_read(
                        operation,
                        *,
                        on_submitted=None,
                        original_read=original_read,
                        read_finished=read_finished,
                        return_read=return_read,
                    ):
                        result = await original_read(
                            operation, on_submitted=on_submitted
                        )
                        read_finished.set()
                        await return_read.wait()
                        return result

                    with patch.object(self.service, "read", blocked_read):
                        next_event = asyncio.create_task(anext(body))
                        try:
                            await asyncio.wait_for(read_finished.wait(), 2)
                            authorized[0] = False
                            return_read.set()
                            event = await asyncio.wait_for(next_event, 2)
                            self.assertIn("authorization_lost", event)
                            self.assertNotIn("event: snapshot", str(event))
                            self.assertEqual(revoked_auth_lock_states, [False])
                        finally:
                            return_read.set()
                            if not next_event.done():
                                next_event.cancel()
                            await asyncio.gather(next_event, return_exceptions=True)
                            await body.aclose()
                            response._release()

        asyncio.run(check())

    def test_snapshot_pins_lists_and_content_indexes_to_one_commit(self):
        handle = self.request(terminal=False)
        reader = self.service.reader()
        original = reader._rows
        called = []

        def rows(*args, **kwargs):
            result = original(*args, **kwargs)
            if not called:
                called.append(True)
                self.recorder.append_text(
                    handle, "raw_output", "later", invocation_id="1"
                )
                self.assertTrue(self.recorder.flush())
            return result

        with patch.object(reader, "_rows", rows):
            snapshot = reader.snapshot(request_id=handle.request_id)
        before = snapshot["detail"]["captured_bytes"]
        self.assertEqual(snapshot["active"][0]["captured_bytes"], before)
        self.assertEqual(
            sum(item["bytes"] for item in snapshot["detail"]["representations"]), before
        )
        self.assertGreater(reader.detail(handle.request_id)["captured_bytes"], before)

    def test_stream_disconnect_before_body_releases_admission(self):
        scope = {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "path": "/admin/api/activity/events",
            "raw_path": b"/admin/api/activity/events",
            "query_string": b"",
            "http_version": "1.1",
            "headers": [(b"authorization", b"Bearer synthetic-owner-only")],
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "asgi": {"version": "3.0", "spec_version": "2.4"},
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            raise OSError("synthetic client disconnect")

        async def check():
            with self.assertRaises(ClientDisconnect):
                await self.app(scope, receive, send)
            self.assertEqual(self.service._streams, set())
            self.assertEqual(self.service._coordinators, {})

        asyncio.run(check())

    def test_reader_pool_supports_reentering_application_lifespan(self):
        self.request()
        for _ in range(2):
            with TestClient(self.app) as client:
                response = client.get(
                    "/admin/api/activity/requests", headers=self.headers
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()["requests"]), 1)

    def test_cancelled_readers_keep_admission_until_worker_finishes(self):
        entered, release, finished = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        lock = threading.Lock()
        counts = {"started": 0, "finished": 0}

        def blocked():
            with lock:
                counts["started"] += 1
                if counts["started"] >= 2:
                    entered.set()
            if not release.wait(3):
                raise RuntimeError("test worker not released")
            with lock:
                counts["finished"] += 1
                if counts["finished"] == 4:
                    finished.set()

        async def check():
            work = [asyncio.create_task(self.service.read(blocked)) for _ in range(4)]
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                for task in work:
                    task.cancel()
                await asyncio.gather(*work, return_exceptions=True)
                with self.assertRaises(HTTPException) as error:
                    await self.service.read(lambda: None)
                self.assertEqual(error.exception.status_code, 429)
            finally:
                release.set()
            self.assertTrue(await asyncio.to_thread(finished.wait, 2))
            for _ in range(50):
                if self.service._reads == 0:
                    break
                await asyncio.sleep(0.001)
            self.assertEqual(self.service._reads, 0)

        asyncio.run(check())

    def test_single_invocation_read_is_bounded_metadata_only_and_no_store(self):
        handle = self.request(terminal=False)
        capture = RequestCapture(self.recorder, handle)
        one = capture.invocation("engine-one", options=(("seed", 123),))
        two = capture.invocation("engine-two", options=(("seed", 456),))
        one.end("completed", finish_reason="stop")
        two.end("completed", finish_reason="length")
        capture.api_done("completed")
        self.assertTrue(self.recorder.flush())
        reader = self.recorder.reader
        connect = type(reader)._connect

        def deny_content(instance):
            db = connect(instance)
            db.set_authorizer(
                lambda action, first, *_: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ
                    and first in {"content_chunks", "attachments"}
                    else sqlite3.SQLITE_OK
                )
            )
            return db

        with patch.object(type(reader), "_connect", deny_content):
            response = self.get(f"/requests/{handle.request_id}/invocations/1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        row = response.json()
        self.assertEqual(row["id"], "1")
        self.assertEqual(row["admission_options"], {"seed": 123})
        self.assertEqual(row["finish_reason"], "stop")
        self.assertNotIn("engine-two", json.dumps(row))
        self.assertLess(len(response.content), 16384)

    def test_single_invocation_read_handles_disabled_history_invalid_ids_and_deletion(
        self,
    ):
        from dataclasses import replace

        handle = self.request(terminal=False)
        capture = RequestCapture(self.recorder, handle)
        observation = capture.invocation("engine")
        observation.end("completed")
        capture.api_done("completed")
        self.assertTrue(self.recorder.flush())
        prefix = f"/requests/{handle.request_id}/invocations/"
        self.recorder.update_settings(
            replace(self.recorder.settings, enabled=False)
        ).result(3)
        self.assertEqual(self.get(prefix + "1").status_code, 200)
        for value in ("0", "001", "257", "-1", "unknown", "2", "1' OR 1=1"):
            self.assertEqual(self.get(prefix + value).status_code, 404)
        self.recorder.delete(handle.request_id).result(3)
        self.assertEqual(self.get(prefix + "1").status_code, 404)


if __name__ == "__main__":
    unittest.main()
