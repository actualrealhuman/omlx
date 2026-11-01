# SPDX-License-Identifier: Apache-2.0
"""A child-process OS file-size limit must fail open and permit restart recovery."""

import json
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _child(parent_directory):
    """Run the destructive-to-writes phase under a disposable child limit."""
    try:
        import resource
    except ImportError:
        print(json.dumps({"unsupported": "resource.RLIMIT_FSIZE unavailable"}))
        return 77

    from omlx.activity import ActivityRecorder, ActivitySettings
    from omlx.activity.store import ActivityStore

    if not hasattr(resource, "RLIMIT_FSIZE") or not hasattr(signal, "SIGXFSZ"):
        print(json.dumps({"unsupported": "RLIMIT_FSIZE/SIGXFSZ unavailable"}))
        return 77

    errors = []
    original_append = ActivityStore.append

    def observe_real_sqlite_error(store, *args, **kwargs):
        try:
            return original_append(store, *args, **kwargs)
        except sqlite3.Error as exc:
            errors.append(
                {
                    "type": type(exc).__name__,
                    "code": getattr(exc, "sqlite_errorcode", None),
                    "name": getattr(exc, "sqlite_errorname", None),
                }
            )
            raise

    settings = ActivitySettings(
        enabled=True,
        max_storage_bytes=16 * 1024**2,
        max_request_bytes=8 * 1024**2,
        max_attachment_bytes=4 * 1024**2,
        max_memory_bytes=64 * 1024**2,
        max_pending_bytes=4 * 1024**2,
        max_active_requests=16,
    )
    report = {}
    recorder = recovered = None
    limits_before = resource.getrlimit(resource.RLIMIT_FSIZE)
    soft_before, hard_before = limits_before
    with tempfile.TemporaryDirectory(
        prefix="activity-file-limit-", dir=parent_directory
    ) as directory:
        db_path = Path(directory) / "activity.sqlite3"
        try:
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            ActivityStore.append = observe_real_sqlite_error
            recorder = ActivityRecorder(db_path, settings)
            if not recorder.wait_ready(timeout=5):
                raise AssertionError(f"initial recorder unavailable: {recorder.health()}")

            handle = recorder.begin(model_id="file-limit", endpoint="chat")
            if handle is None or not recorder.flush(timeout=5):
                raise AssertionError("could not persist pre-limit request")

            largest_file = max(
                (
                    candidate.stat().st_size
                    for candidate in (
                        db_path,
                        Path(str(db_path) + "-wal"),
                        Path(str(db_path) + "-shm"),
                    )
                    if candidate.exists()
                ),
                default=0,
            )
            if soft_before != resource.RLIM_INFINITY:
                limit = min(int(soft_before), largest_file + 64 * 1024)
            else:
                limit = largest_file + 64 * 1024
            if hard_before != resource.RLIM_INFINITY:
                limit = min(int(limit), int(hard_before))
            if limit <= largest_file:
                print(
                    json.dumps(
                        {"unsupported": "no RLIMIT_FSIZE headroom above existing store"}
                    )
                )
                return 77

            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, hard_before))
            generator = __import__("random").Random(0xA6717)
            attempts = 0
            flush_result = True
            while attempts < 20 and not errors:
                payload = generator.randbytes(128 * 1024)
                if not recorder.append(handle, "raw_output", payload):
                    break
                attempts += 1
                flush_result = recorder.flush(timeout=3)
                if not flush_result:
                    break

            health = recorder.health()
            if not errors:
                raise AssertionError(
                    "OS file-size limit did not produce a SQLite write error "
                    f"after {attempts} writes (limit={limit}, health={health})"
                )
            code = errors[0]["code"]
            primary_code = code & 0xFF if isinstance(code, int) else None
            if primary_code not in (sqlite3.SQLITE_IOERR, sqlite3.SQLITE_FULL):
                raise AssertionError(f"unexpected SQLite error code: {errors[0]}")
            if health["available"] or not health["error"]:
                raise AssertionError(f"writer did not suspend cleanly: {health}")
            if "Activity persistence suspended" not in health["error"]:
                raise AssertionError(f"writer error was not sanitized: {health}")
            if health["pending_bytes"] != 0 or health["writer_scratch_bytes"] != 0:
                raise AssertionError(f"writer reservations were not drained: {health}")
            # Calls on the failed observer stay ordinary capture refusals.
            if recorder.begin(model_id="later", endpoint="chat") is not None:
                raise AssertionError("suspended writer admitted a later request")
            if recorder.append(handle, "raw_output", b"after failure"):
                raise AssertionError("suspended writer accepted later content")
            if not recorder.close(timeout=5):
                raise AssertionError("failed recorder worker did not stop")
            recorder = None

            resource.setrlimit(resource.RLIMIT_FSIZE, limits_before)
            recovered = ActivityRecorder(db_path, settings)
            if not recovered.wait_ready(timeout=5):
                raise AssertionError(f"reopened recorder unavailable: {recovered.health()}")
            fresh = recovered.begin(model_id="recovered", endpoint="chat")
            content = b"recovered after child file-size limit was removed"
            if fresh is None or not recovered.append(fresh, "output", content):
                raise AssertionError("fresh capture was not admitted after restart")
            if not recovered.finish(fresh, reason="recovered"):
                raise AssertionError("fresh request did not finish after restart")
            if not recovered.flush(timeout=5):
                raise AssertionError(f"fresh request did not persist: {recovered.health()}")
            if (
                recovered.reader.content(fresh.request_id, "output")["data"]
                != content
            ):
                raise AssertionError("fresh request content did not round-trip")
            with closing(
                sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=0.25)
            ) as independent:
                quick_check = independent.execute("PRAGMA quick_check").fetchall()
            if quick_check != [("ok",)]:
                raise AssertionError(f"reopened database failed quick_check: {quick_check}")
            report = {
                "sqlite_error": errors[0],
                "attempts": attempts,
                "limit_bytes": limit,
                "prelimit_largest_file_bytes": largest_file,
                "failed_writer_available": False,
                "failed_writer_health_error": health["error"],
                "restart_recovery": True,
                "db_quick_check": quick_check[0][0],
            }
            if recovered.reader.get(fresh.request_id)["state"] != "completed":
                raise AssertionError("recovered request is not terminal")
            if not recovered.health()["available"] or recovered.health()["error"]:
                raise AssertionError(f"recovered writer unhealthy: {recovered.health()}")
        finally:
            resource.setrlimit(resource.RLIMIT_FSIZE, limits_before)
            ActivityStore.append = original_append
            if recorder is not None:
                recorder.close(timeout=5)
            if recovered is not None:
                recovered.close(timeout=5)

    print(json.dumps(report))
    return 0


class ActivityFileLimitTests(unittest.TestCase):
    def test_child_rlimit_file_write_failure_fails_open_and_recovers(self):
        # Parent ownership also cleans a child's partial database after a timeout
        # kills it before its own finally/TemporaryDirectory can execute.
        with tempfile.TemporaryDirectory(prefix="activity-file-limit-parent-") as parent:
            result = subprocess.run(
                [sys.executable, "-B", str(Path(__file__).resolve()), "--child", parent],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        if result.returncode == 77:
            try:
                reason = json.loads(result.stdout).get("unsupported", result.stdout)
            except json.JSONDecodeError:
                reason = result.stdout or result.stderr
            self.skipTest(reason)
        self.assertEqual(
            result.returncode,
            0,
            f"child failed (stdout={result.stdout!r}, stderr={result.stderr!r})",
        )
        report = json.loads(result.stdout)
        error_name = report["sqlite_error"]["name"] or ""
        self.assertTrue(
            error_name.startswith("SQLITE_IOERR") or error_name == "SQLITE_FULL",
            report["sqlite_error"],
        )
        self.assertFalse(report["failed_writer_available"])
        self.assertTrue(report["restart_recovery"])
        self.assertEqual(report["db_quick_check"], "ok")
        print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        raise SystemExit(_child(sys.argv[2]))
    unittest.main()
