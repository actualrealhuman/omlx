# SPDX-License-Identifier: Apache-2.0
"""CPU-only behavioral checks; also runnable with stdlib unittest discovery."""

import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from omlx.activity import ActivityReader, ActivityRecorder, ActivitySettings
from omlx.activity.store import MAX_CHUNK_BYTES, ActivityStore


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "activity.sqlite3"
        self.settings = ActivitySettings(enabled=True)

    def recorder(self, settings=None):
        result = ActivityRecorder(self.path, settings or self.settings)
        self.addCleanup(result.close)
        self.assertTrue(result.wait_ready(), result.health())
        return result

    def begin(self, recorder, **overrides):
        values = dict(
            model_id="model-a",
            endpoint="chat",
            source_id="source-a",
            source_name="Example client",
        )
        values.update(overrides)
        result = recorder.begin(**values)
        self.assertIsNotNone(result)
        return result

    def test_disabled_has_no_files_worker_or_capture(self):
        recorder = ActivityRecorder(self.path)
        self.addCleanup(recorder.close)
        self.assertIsNone(recorder.begin(model_id="a", endpoint="chat"))
        self.assertFalse(self.path.exists())
        self.assertIsNone(recorder._thread)
        self.assertEqual(recorder.reader.list_requests(), [])
        self.assertIsNone(recorder.reader.get("missing"))
        self.assertFalse(self.path.exists())

    def test_default_limits_and_strict_validation(self):
        settings = ActivitySettings()
        self.assertEqual(settings.max_storage_bytes, 5 * 1024**3)
        self.assertEqual(settings.max_requests, 1000)
        self.assertEqual(settings.max_age_days, 7)
        self.assertEqual(ActivitySettings.from_dict(settings.to_dict()), settings)
        self.assertEqual(replace(settings, max_requests=100000).max_requests, 100000)
        for values in [
            dict(enabled=1),
            dict(max_requests=True),
            dict(max_age_days=0),
            dict(max_age_days=float("nan")),
            dict(max_storage_bytes=1024),
            dict(max_request_bytes=6 * 1024**3),
            dict(max_attachment_bytes=65 * 1024**2),
            dict(max_memory_bytes=1024),
        ]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                replace(settings, **values)

    def test_global_settings_roundtrip_preserves_activity(self):
        from omlx.settings import GlobalSettings

        settings = GlobalSettings(base_path=Path(self.temp.name))
        self.assertFalse(settings.activity.enabled)
        settings.activity = replace(self.settings, max_requests=100000)
        path = Path(self.temp.name) / "settings.json"
        path.write_text(json.dumps(settings.to_dict()))
        restored = GlobalSettings(base_path=Path(self.temp.name))
        restored._load_from_file(path)
        self.assertEqual(restored.activity, settings.activity)

    def test_invalid_metadata_has_no_partial_state_transition(self):
        recorder = self.recorder()
        self.assertIsNone(
            recorder.begin(model_id="a", endpoint="chat", received_at=float("nan"))
        )
        handle = self.begin(recorder)
        with self.assertRaises(ValueError):
            recorder.state(handle, "generating", model_id="x" * 257)
        self.assertTrue(recorder.flush())
        self.assertEqual(recorder.reader.get(handle.request_id)["state"], "received")

    def test_input_output_source_and_independent_read_snapshots(self):
        recorder = self.recorder()
        a = self.begin(recorder)
        b = self.begin(recorder, model_id="model-b", source_id="source-b")
        self.assertTrue(recorder.append(a, "submitted", b'{"messages":[]}'))
        self.assertTrue(recorder.append(a, "model_input", b"<user>Hello<assistant>"))
        self.assertTrue(recorder.state(a, "generating"))
        self.assertTrue(recorder.append(a, "raw_output", "thinking🙂".encode()))
        self.assertTrue(recorder.append(a, "output", b"answer"))
        self.assertTrue(recorder.append(b, "output", b"other"))
        self.assertTrue(recorder.finish(a, reason="stop"))
        self.assertTrue(recorder.flush())
        row = recorder.reader.get(a.request_id)
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["source_name"], "Example client")
        self.assertEqual(row["finish_reason"], "stop")
        self.assertEqual(row["capture_gaps"], 0)
        self.assertEqual(
            recorder.reader.content(a.request_id, "output")["data"], b"answer"
        )
        self.assertEqual(
            recorder.reader.content(b.request_id, "output")["data"], b"other"
        )
        self.assertEqual(len(recorder.reader.list_requests(active=True)), 1)
        self.assertEqual(len(recorder.reader.list_requests(active=False)), 1)

    def test_nonblocking_payload_saturation_keeps_terminal_status(self):
        recorder = self.recorder(replace(self.settings, max_pending_bytes=512))
        handle = self.begin(recorder)
        self.assertTrue(recorder.flush())
        entered, release = threading.Event(), threading.Event()
        original = ActivityStore.append

        def stalled(store, *args):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(store, *args)

        with patch.object(ActivityStore, "append", stalled):
            try:
                self.assertTrue(recorder.append(handle, "output", b"a" * 256))
                self.assertTrue(entered.wait(3))
                # The in-flight payload must still consume queue capacity.
                self.assertFalse(recorder.append(handle, "output", b"b"))
                self.assertTrue(recorder.finish(handle))
                self.assertEqual(recorder.health()["pending_bytes"], 512)
            finally:
                release.set()
            self.assertTrue(recorder.flush())
        row = recorder.reader.get(handle.request_id)
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["capture_gaps"], 1)
        self.assertEqual(row["capture_reason"], "capture_queue_full")
        self.assertEqual(
            recorder.reader.content(handle.request_id, "output")["data"], b"a" * 256
        )
        self.assertEqual(recorder.health()["pending_bytes"], 0)

    def test_request_cap_and_rejected_chunk_are_visible(self):
        settings = replace(self.settings, max_request_bytes=4, max_attachment_bytes=4)
        recorder = self.recorder(settings)
        handle = self.begin(recorder)
        self.assertTrue(recorder.append(handle, "submitted", b"1234"))
        self.assertFalse(recorder.append(handle, "output", b"5"))
        self.assertFalse(
            recorder.append(handle, "output", b"x" * (MAX_CHUNK_BYTES + 1))
        )
        recorder.finish(handle, "error", reason="synthetic failure")
        self.assertTrue(recorder.flush())
        row = recorder.reader.get(handle.request_id)
        self.assertEqual(row["capture_gaps"], 2)
        self.assertEqual(row["captured_bytes"], 4)
        self.assertEqual(row["state"], "error")

    def test_metadata_slots_are_bounded_and_released_on_finish(self):
        recorder = self.recorder(replace(self.settings, max_active_requests=1))
        handle = self.begin(recorder)
        self.assertIsNone(recorder.begin(model_id="b", endpoint="chat"))
        recorder.finish(handle)
        self.assertTrue(recorder.flush())
        self.assertIsNotNone(recorder.begin(model_id="b", endpoint="chat"))
        self.assertEqual(recorder.health()["skipped_requests"], 1)

    def test_clear_fences_inflight_writes_and_old_handles(self):
        self._deletion_race(clear=True)

    def test_delete_fences_inflight_writes_and_old_handles(self):
        self._deletion_race(clear=False)

    def _deletion_race(self, clear):
        recorder = self.recorder()
        handle = self.begin(recorder)
        self.assertTrue(recorder.flush())
        entered, release = threading.Event(), threading.Event()
        original = ActivityStore.append

        def stalled(store, *args):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(store, *args)

        with patch.object(ActivityStore, "append", stalled):
            try:
                recorder.append(handle, "output", b"in flight")
                self.assertTrue(entered.wait(3))
                future = (
                    recorder.clear() if clear else recorder.delete(handle.request_id)
                )
                self.assertFalse(recorder.append(handle, "output", b"late output"))
                self.assertFalse(recorder.finish(handle))
                fresh = self.begin(recorder)
                recorder.append(fresh, "output", b"new request")
                recorder.finish(fresh)
            finally:
                release.set()
            future.result(3)
            self.assertTrue(recorder.flush())
        self.assertIsNone(recorder.reader.get(handle.request_id))
        self.assertEqual(
            recorder.reader.content(fresh.request_id, "output")["data"], b"new request"
        )
        self.assertGreaterEqual(recorder.health()["pending_bytes"], 0)

    def test_shutdown_preserves_partial_output_and_completed_requests(self):
        recorder = self.recorder()
        partial = self.begin(recorder)
        complete = self.begin(recorder)
        recorder.append(partial, "output", b"partial")
        recorder.finish(complete)
        self.assertTrue(recorder.close())
        reader = ActivityReader(self.path)
        self.assertEqual(reader.get(partial.request_id)["state"], "interrupted")
        self.assertEqual(reader.get(complete.request_id)["state"], "completed")
        self.assertEqual(
            reader.content(partial.request_id, "output")["data"], b"partial"
        )

    def test_restart_marks_unfinished_records_interrupted(self):
        recorder = self.recorder()
        handle = self.begin(recorder)
        recorder.append(handle, "output", b"persisted before crash")
        self.assertTrue(recorder.flush())
        # Simulate a previously active row in a closed database, without
        # pretending a process-local monotonic clock survives restart.
        recorder.close()
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE requests SET state='generating', finished_at=NULL")
        restarted = self.recorder()
        row = restarted.reader.get(handle.request_id)
        self.assertEqual(row["state"], "interrupted")
        self.assertIn("restarted", row["capture_reason"])

    def test_retention_counts_every_terminal_outcome_not_active(self):
        recorder = self.recorder(replace(self.settings, max_requests=2))
        active = self.begin(recorder)
        for outcome in ("completed", "rejected", "cancelled", "error", "interrupted"):
            handle = self.begin(recorder)
            recorder.finish(handle, outcome)
            self.assertTrue(recorder.flush())
        self.assertEqual(len(recorder.reader.list_requests(active=False)), 2)
        self.assertIsNotNone(recorder.reader.get(active.request_id))

    def test_age_retention_uses_terminal_time(self):
        recorder = self.recorder()
        old = self.begin(recorder)
        recorder.finish(old)
        self.assertTrue(recorder.close())
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE requests SET finished_at=?", (time.time() - 8 * 86400,))
        restarted = self.recorder()
        self.assertIsNone(restarted.reader.get(old.request_id))

    def test_compressed_unicode_content_and_bounded_byte_ranges(self):
        recorder = self.recorder()
        handle = self.begin(recorder)
        payload = ("Hello世界🙂\n" * 1000).encode()
        recorder.append(handle, "output", payload[:7000])
        recorder.append(handle, "output", payload[7000:])
        recorder.finish(handle)
        self.assertTrue(recorder.flush())
        reader = recorder.reader
        result = reader.content(handle.request_id, "output", offset=6995, limit=20)
        self.assertEqual(result["data"], payload[6995:7015])
        self.assertEqual(result["total_bytes"], len(payload))
        self.assertEqual(result["next_offset"], 7015)
        self.assertEqual(
            reader.content(handle.request_id, "output")["data"].decode(),
            payload.decode(),
        )
        with self.assertRaises(ValueError):
            reader.content(handle.request_id, "output", limit=2 * 1024**2)

    def test_large_text_encoding_is_chunked_and_token_snapshot_is_immutable(self):
        recorder = self.recorder()
        handle = self.begin(recorder)
        text = "A世🙂" * 100000
        tokens = list(range(20000))
        self.assertTrue(recorder.append_text(handle, "model_input", text))
        self.assertTrue(recorder.append_tokens(handle, tokens))
        tokens[:] = [999]
        recorder.finish(handle)
        self.assertTrue(recorder.flush())
        self.assertEqual(
            recorder.reader.content(handle.request_id, "model_input", limit=1024**2)[
                "data"
            ].decode(),
            text,
        )
        self.assertEqual(
            json.loads(
                recorder.reader.content(
                    handle.request_id, "model_tokens", limit=1024**2
                )["data"]
            ),
            list(range(20000)),
        )
        with sqlite3.connect(self.path) as db:
            self.assertLessEqual(
                db.execute("SELECT MAX(logical_bytes) FROM content_chunks").fetchone()[
                    0
                ],
                MAX_CHUNK_BYTES,
            )

    def test_filters_and_stable_keyset_pagination(self):
        recorder = self.recorder()
        for index in range(6):
            handle = self.begin(
                recorder, model_id=f"model-{index % 2}", received_at=time.time() + index
            )
            recorder.finish(handle)
        self.assertTrue(recorder.flush())
        page = recorder.reader.list_requests(limit=3)
        tail = recorder.reader.list_requests(
            limit=3, before=(page[-1]["received_at"], page[-1]["id"])
        )
        self.assertEqual(len({row["id"] for row in page + tail}), 6)
        self.assertEqual(len(recorder.reader.list_requests(model_id="model-1")), 3)
        self.assertEqual(recorder.reader.list_requests(model_id="x' OR 1=1 --"), [])
        self.assertEqual(len(recorder.reader.list_requests(source_id="source-a")), 6)

    def test_newer_schema_is_preserved_and_capture_fails_open(self):
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA user_version=99")
        before = hashlib.sha256(self.path.read_bytes()).digest()
        recorder = ActivityRecorder(self.path, self.settings)
        self.addCleanup(recorder.close)
        self.assertFalse(recorder.wait_ready())
        self.assertIn("suspended", recorder.health()["error"])
        self.assertIsNone(recorder.begin(model_id="a", endpoint="chat"))
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), before)

    def test_corrupt_store_preserved_without_backup_loop(self):
        original = b"this is not a SQLite database"
        self.path.write_bytes(original)
        recorder = ActivityRecorder(self.path, self.settings)
        self.addCleanup(recorder.close)
        self.assertFalse(recorder.wait_ready())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.corrupt*")), [])

    def test_writer_error_suspends_capture_and_bounds_cleanup(self):
        recorder = self.recorder()
        with patch.object(
            ActivityStore,
            "append",
            side_effect=sqlite3.OperationalError("synthetic disk failure"),
        ):
            handle = self.begin(recorder)
            recorder.append(handle, "output", b"text")
            self.assertFalse(recorder.flush())
            self.assertTrue(recorder.close())
        self.assertFalse(recorder.health()["available"])
        self.assertEqual(recorder.health()["pending_bytes"], 0)
        self.assertNotIn("synthetic disk failure", recorder.health()["error"])

    def test_multiple_producers_never_mix_output(self):
        recorder = self.recorder()
        handles = [self.begin(recorder) for _ in range(8)]

        def produce(index):
            for _ in range(50):
                self.assertTrue(
                    recorder.append(handles[index], "output", bytes([65 + index]))
                )
            recorder.finish(handles[index])

        with ThreadPoolExecutor(max_workers=8) as pool:
            for future in [pool.submit(produce, index) for index in range(8)]:
                future.result(3)
        self.assertTrue(recorder.flush())
        for index, handle in enumerate(handles):
            self.assertEqual(
                recorder.reader.content(handle.request_id, "output")["data"],
                bytes([65 + index]) * 50,
            )


if __name__ == "__main__":
    unittest.main()
