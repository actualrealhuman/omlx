# SPDX-License-Identifier: Apache-2.0
"""Bounded storage regression with a real SQLite WAL reader pinned."""

import random
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from omlx.activity import ActivityRecorder, ActivitySettings


class PinnedReaderStorageTests(unittest.TestCase):
    def test_storage_pressure_with_pinned_wal_reader_keeps_metadata_and_recovers(self):
        settings = replace(
            ActivitySettings(enabled=True),
            max_storage_bytes=1024**2,
            max_request_bytes=768 * 1024,
            max_attachment_bytes=128 * 1024,
            max_memory_bytes=2 * 1024**2,
            max_pending_bytes=512 * 1024,
            max_active_requests=64,
        )
        self.assertLess(
            settings.max_pending_bytes + settings.max_active_requests * 16 * 1024,
            settings.max_memory_bytes,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "activity.sqlite3"
            recorder = ActivityRecorder(path, settings)
            pinned_reader = None
            try:
                self.assertTrue(recorder.wait_ready(), recorder.health())

                def wide_text():
                    return "".join(chr(0x100000 + index) for index in range(256))

                wide = wide_text()
                handle = recorder.begin(
                    model_id=wide,
                    endpoint=wide,
                    source_id=wide,
                    source_name=wide,
                )
                self.assertIsNotNone(handle)
                # Drive the largest public identity, scalar, timing, and finish
                # metadata values while content competes for the same store.
                self.assertTrue(recorder.state(handle, "preparing", model_id=wide))
                self.assertTrue(
                    recorder.metrics(
                        handle,
                        counts={
                            name: 9223372036854775806
                            for name in (
                                "observed_invocations",
                                "prepared_invocations",
                                "prepared_text_invocations",
                                "prefilling_invocations",
                                "observed_input_tokens",
                                "observed_generated_tokens",
                            )
                        },
                        times={
                            "first_observed_token_ms": 1000000000000.0,
                            "first_observed_text_ms": 1000000000000.0,
                        },
                    )
                )
                self.assertTrue(recorder.flush())

                pinned_reader = sqlite3.connect(path, timeout=0.25)
                pinned_reader.execute("BEGIN")
                pinned_reader.execute("SELECT COUNT(*) FROM requests").fetchone()

                generator = random.Random(0xA6717)
                accepted_bytes = 0
                peak_physical_bytes = 0
                saw_storage_gap = False
                for _ in range(12):
                    payload = generator.randbytes(64 * 1024)
                    # append is the producer-side API: refusal is a capture gap,
                    # not an exception that may escape into request handling.
                    accepted = recorder.append(handle, "raw_output", payload)
                    if accepted:
                        accepted_bytes += len(payload)
                    self.assertTrue(recorder.flush(), recorder.health())

                    peak_physical_bytes = max(
                        peak_physical_bytes,
                        sum(
                            sidecar.stat().st_size if sidecar.exists() else 0
                            for sidecar in (
                                path,
                                Path(str(path) + "-wal"),
                                Path(str(path) + "-shm"),
                            )
                        ),
                    )
                    row = recorder.reader.get(handle.request_id)
                    self.assertIsNotNone(row)
                    if row["capture_gaps"]:
                        saw_storage_gap = True
                        self.assertEqual(
                            row["capture_reason"], "storage_budget_or_missing_record"
                        )
                        break

                self.assertTrue(
                    saw_storage_gap,
                    f"storage refusal was not visible; health={recorder.health()}",
                )
                self.assertLess(accepted_bytes, settings.max_request_bytes)
                self.assertTrue(recorder.health()["available"], recorder.health())
                self.assertIsNone(recorder.health()["error"], recorder.health())

                self.assertTrue(recorder.finish(handle, reason=wide))
                self.assertTrue(recorder.flush(), recorder.health())
                row = recorder.reader.get(handle.request_id)
                self.assertEqual(row["state"], "completed")
                self.assertEqual(row["model_id"], wide)
                self.assertEqual(row["finish_reason"], wide)
                self.assertGreaterEqual(row["capture_gaps"], 1)
                self.assertEqual(
                    row["capture_reason"], "storage_budget_or_missing_record"
                )
                self.assertLess(row["captured_bytes"], settings.max_request_bytes)

                pinned_reader.rollback()
                pinned_reader.close()
                pinned_reader = None

                recorder.update_settings(settings).result(timeout=5)
                recovered = recorder.begin(
                    model_id="synthetic-model",
                    endpoint="chat",
                    source_id="storage-regression",
                    source_name="Recovery check",
                )
                self.assertIsNotNone(recovered)
                content = b"writer recovered after pinned reader released"
                self.assertTrue(recorder.append(recovered, "output", content))
                self.assertTrue(recorder.finish(recovered, reason="recovered"))
                self.assertTrue(recorder.flush(), recorder.health())
                recovered_row = recorder.reader.get(recovered.request_id)
                self.assertEqual(recovered_row["state"], "completed")
                self.assertEqual(
                    recorder.reader.content(recovered.request_id, "output")["data"],
                    content,
                )
                self.assertTrue(recorder.health()["available"], recorder.health())
                self.assertIsNone(recorder.health()["error"], recorder.health())

                # Diagnostic only: a pinned SQLite snapshot can prevent WAL
                # truncation, so this deliberately has no budget assertion.
                self.assertGreater(peak_physical_bytes, 0)
            finally:
                if pinned_reader is not None:
                    pinned_reader.rollback()
                    pinned_reader.close()
                recorder.close()


if __name__ == "__main__":
    unittest.main()
