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
            stream = self.service.snapshots(
                request, reader, handle.request_id, None, None, initial
            )
            self.assertIn("resync_required", await anext(stream))
            snapshot = await anext(stream)
            self.assertIn("event: snapshot", snapshot)
            self.assertIn(handle.request_id, snapshot)
            self.assertNotIn("payload", snapshot)
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
                stream = self.service.snapshots(
                    request, reader, None, "model-a", "source-a", initial, **filters
                )
                try:
                    payload = json.loads((await anext(stream)).split("data: ", 1)[1])
                    self.assertEqual(
                        [row["id"] for row in payload["recent"]], [first.request_id]
                    )
                    second = self.request(received_at=base + 1)
                    self.request(received_at=base + 8)
                    self.request(terminal=False, received_at=base + 2)
                    event = await asyncio.wait_for(anext(stream), 3)
                    payload = json.loads(event.split("data: ", 1)[1])
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
