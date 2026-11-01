# SPDX-License-Identifier: Apache-2.0
"""Worker-owned SQLite persistence and bounded, independent read snapshots."""

import hashlib
import json
import sqlite3
import time
import zlib
from pathlib import Path

from .config import ActivitySettings
from .coverage import evidence_coverage
from .invocations import (
    INVOCATION_SUMMARY_FIELDS,
    MAX_INVOCATION_BYTES,
    MAX_INVOCATIONS,
)

SCHEMA_VERSION = 3
MAX_CHUNK_BYTES = 256 * 1024
MAX_READ_BYTES = 1024 * 1024
# Leave bounded room for lifecycle/gap metadata writes after content admission.
# This is a per-content-operation reserve, not an aggregate metadata bank or a
# guarantee against unrelated SQLite/disk failures.
_CONTENT_METADATA_HEADROOM_BYTES = 128 * 1024
TERMINAL_STATES = frozenset(
    {"completed", "rejected", "cancelled", "error", "interrupted", "capture_stopped"}
)
STATES = TERMINAL_STATES | {
    "received",
    "loading",
    "preparing",
    "queued",
    "prefilling",
    "generating",
}
REPRESENTATIONS = frozenset(
    {
        "submitted",
        "model_input",
        "model_tokens",
        "raw_output",
        "output",
        "api_response",
        "events",
    }
)


class ActivityStore:
    """Synchronous writer. Only the recorder worker may use this connection."""

    def __init__(self, path: Path, settings: ActivitySettings):
        self.path = Path(path)
        self.settings = settings
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=0.25)
        self.db = db
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, SCHEMA_VERSION):
                raise ValueError(
                    "Unsupported Activity schema; existing store preserved"
                )
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError(
                    "Activity integrity check failed; existing store preserved"
                )
            if version == 0:
                # This must precede creation of the first table.
                db.execute("PRAGMA auto_vacuum=INCREMENTAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA wal_autocheckpoint=64")
            self._limit_pages()
            db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    received_at REAL NOT NULL,
                    finished_at REAL,
                    state TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    capture_gaps INTEGER NOT NULL DEFAULT 0,
                    capture_reason TEXT NOT NULL DEFAULT '',
                    captured_bytes INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS activity_recent
                    ON requests(received_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS activity_model ON requests(model_id);
                CREATE INDEX IF NOT EXISTS activity_source ON requests(source_id);
                CREATE TABLE IF NOT EXISTS content_chunks (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                    invocation_id TEXT NOT NULL,
                    representation TEXT NOT NULL,
                    data BLOB NOT NULL,
                    compressed INTEGER NOT NULL,
                    logical_bytes INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS activity_content
                    ON content_chunks(request_id, invocation_id, representation, sequence);
                CREATE TABLE IF NOT EXISTS attachments (
                    request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                    attachment_id TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    data BLOB,
                    PRIMARY KEY(request_id, attachment_id)
                );
                CREATE TABLE IF NOT EXISTS invocations (
                    request_id TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                    invocation_id TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    PRIMARY KEY(request_id, invocation_id)
                );
                PRAGMA user_version=3;
                COMMIT;
                """
            )
            with db:
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                db.execute(
                    "UPDATE requests SET state='interrupted', finished_at=?, "
                    "capture_reason='server restarted before terminal observation' "
                    "WHERE finished_at IS NULL",
                    (time.time(),),
                )
            self.prune()
        except BaseException:
            db.close()
            raise

    def save_metadata(self, record: dict):
        metadata = json.dumps(record, ensure_ascii=True)
        terminal_update = (
            record.get("finished_at") is not None
            and self.db.execute(
                "SELECT 1 FROM requests WHERE id=?", (record["id"],)
            ).fetchone()
            is not None
        )
        # A budget reduction can leave only active records above the new limit.
        # Permit their bounded terminal metadata update before pruning: otherwise
        # they could never become eligible for reclamation. No content is added.
        if not terminal_update and not self._make_room(len(metadata) * 2 + 32768):
            raise RuntimeError("Activity metadata storage budget exhausted")
        values = (
            record["id"],
            record["received_at"],
            record.get("finished_at"),
            record["state"],
            record["model_id"],
            record["source_id"],
            record["source_name"],
            record["endpoint"],
            record["capture_gaps"],
            record["capture_reason"],
            metadata,
        )
        with self.db:
            self.db.execute(
                "INSERT INTO requests (id,received_at,finished_at,state,model_id,"
                "source_id,source_name,endpoint,capture_gaps,capture_reason,metadata) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "finished_at=excluded.finished_at,state=excluded.state,"
                "model_id=excluded.model_id,capture_gaps=excluded.capture_gaps,"
                "capture_reason=excluded.capture_reason,metadata=excluded.metadata",
                values,
            )

    def append(
        self, request_id: str, invocation_id: str, representation: str, data: bytes
    ):
        if not data or len(data) > MAX_CHUNK_BYTES:
            raise ValueError("Activity content chunk outside bounds")
        if representation not in REPRESENTATIONS:
            raise ValueError("Unknown Activity representation")
        packed = zlib.compress(data) if len(data) >= 256 else data
        compressed = len(packed) < len(data)
        if not compressed:
            packed = data
        if not self._make_content_room(len(packed) * 2 + 16384):
            return False
        with self.db:
            # Unknown/deleted requests must never be recreated by late output.
            exists = self.db.execute(
                "SELECT captured_bytes FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if exists is None:
                return False
            if exists[0] + len(data) > self.settings.max_request_bytes:
                return False
            self.db.execute(
                "INSERT INTO content_chunks "
                "(request_id,invocation_id,representation,data,compressed,logical_bytes) "
                "VALUES (?,?,?,?,?,?)",
                (
                    request_id,
                    invocation_id,
                    representation,
                    packed,
                    int(compressed),
                    len(data),
                ),
            )
            self.db.execute(
                "UPDATE requests SET captured_bytes=captured_bytes+? WHERE id=?",
                (len(data), request_id),
            )
        return True

    def save_invocation(self, request_id, invocation_id, data):
        """Merge a bounded immutable update, never recreating a deleted request."""
        if (
            type(invocation_id) is not str
            or not invocation_id.isascii()
            or not invocation_id.isdecimal()
            or len(invocation_id) > 3
            or not 1 <= int(invocation_id) <= MAX_INVOCATIONS
            or str(int(invocation_id)) != invocation_id
        ):
            return False
        if not data or len(data) > MAX_INVOCATION_BYTES:
            raise ValueError("Activity invocation update outside bounds")
        if not self._make_content_room(MAX_INVOCATION_BYTES * 2 + 16384):
            return False
        with self.db:
            request = self.db.execute(
                "SELECT captured_bytes FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if request is None:
                return False
            previous = self.db.execute(
                "SELECT metadata FROM invocations WHERE request_id=? AND invocation_id=?",
                (request_id, invocation_id),
            ).fetchone()
            record = json.loads(previous[0]) if previous else {}
            record.update(json.loads(data))
            encoded = json.dumps(record, ensure_ascii=True)
            if len(encoded) > MAX_INVOCATION_BYTES:
                return False
            delta = len(encoded) - (len(previous[0]) if previous else 0)
            if request[0] + delta > self.settings.max_request_bytes:
                return False
            self.db.execute(
                "INSERT INTO invocations VALUES (?,?,?) ON CONFLICT(request_id,invocation_id) "
                "DO UPDATE SET metadata=excluded.metadata",
                (request_id, invocation_id, encoded),
            )
            self.db.execute(
                "UPDATE requests SET captured_bytes=captured_bytes+? WHERE id=?",
                (delta, request_id),
            )
        return True

    def save_attachment(self, request_id, metadata, data):
        """One transactional reference/original; cascades with its request."""
        if data is not None and (
            type(data) is not bytes or len(data) > self.settings.max_attachment_bytes
        ):
            raise ValueError("Activity attachment outside bounds")
        size = len(data) if data is not None else 0
        encoded = json.dumps(metadata, ensure_ascii=True)
        if not self._make_content_room(size * 2 + len(encoded) * 2 + 16384):
            return False
        with self.db:
            row = self.db.execute(
                "SELECT captured_bytes FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None or row[0] + size > self.settings.max_request_bytes:
                return False
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO attachments(request_id,attachment_id,metadata,data) VALUES (?,?,?,?)",
                (request_id, metadata["id"], encoded, data),
            )
            if cursor.rowcount != 1:
                return False
            self.db.execute(
                "UPDATE requests SET captured_bytes=captured_bytes+? WHERE id=?",
                (size, request_id),
            )
        return True

    def physical_bytes(self):
        return sum(
            p.stat().st_size if p.exists() else 0
            for p in (
                self.path,
                Path(str(self.path) + "-wal"),
                Path(str(self.path) + "-shm"),
            )
        )

    def _reclaim(self):
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        # SQLite yields a row per reclaimed page. Drain the bounded operation;
        # executing without consuming the cursor can stop after the first page.
        self.db.execute("PRAGMA incremental_vacuum(128)").fetchall()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _limit_pages(self):
        page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
        # Leave space for a WAL of similar size plus sidecars/maintenance.
        page_limit = (self.settings.max_storage_bytes - 256 * 1024) // (2 * page_size)
        self.db.execute(f"PRAGMA max_page_count={max(32, page_limit)}")

    def _make_room(self, extra: int):
        def fits():
            if self.physical_bytes() + extra > self.settings.max_storage_bytes:
                return False
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            pages = self.db.execute("PRAGMA page_count").fetchone()[0]
            free = self.db.execute("PRAGMA freelist_count").fetchone()[0]
            page_limit = max(
                32, (self.settings.max_storage_bytes - 256 * 1024) // (2 * page_size)
            )
            # Callers reserve roughly twice the new pages for WAL/maintenance.
            # A checkpoint can make physical bytes fit while used DB pages
            # still exhaust max_page_count; reclaim/evict before that write.
            return (pages - free) * page_size + (
                extra + 1
            ) // 2 <= page_limit * page_size

        if fits():
            return True
        while not fits():
            before = self.physical_bytes()
            self._reclaim()
            if fits():
                return True
            row = self.db.execute(
                "SELECT id FROM requests WHERE finished_at IS NOT NULL "
                "ORDER BY finished_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                if self.physical_bytes() < before:
                    continue  # More free pages can be reclaimed in bounded steps.
                return False
            self.delete(row[0])
        return True

    def _make_content_room(self, extra: int):
        """Reserve space for content plus a later bounded lifecycle update."""
        return self._make_room(extra + _CONTENT_METADATA_HEADROOM_BYTES)

    def prune(self, now=None):
        cutoff = (
            time.time() if now is None else now
        ) - self.settings.max_age_days * 86400
        with self.db:
            self.db.execute(
                "DELETE FROM requests WHERE finished_at IS NOT NULL AND finished_at<?",
                (cutoff,),
            )
            self.db.execute(
                "DELETE FROM requests WHERE id IN (SELECT id FROM requests "
                "WHERE finished_at IS NOT NULL ORDER BY finished_at DESC,id DESC "
                "LIMIT -1 OFFSET ?)",
                (self.settings.max_requests,),
            )
        self._make_room(0)
        self._limit_pages()

    def delete(self, request_id: str):
        with self.db:
            self.db.execute("DELETE FROM requests WHERE id=?", (request_id,))

    def clear(self):
        with self.db:
            self.db.execute("DELETE FROM requests")
        self._reclaim()

    def close(self):
        self.db.close()


class ActivityReader:
    """Short independent read transactions; never create or initialize a store."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _connect(self):
        db = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.25
        )
        db.row_factory = sqlite3.Row
        try:
            if db.execute("PRAGMA user_version").fetchone()[0] not in (
                1,
                2,
                SCHEMA_VERSION,
            ):
                raise ValueError("Unsupported Activity history schema")
        except BaseException:
            db.close()
            raise
        return db

    @staticmethod
    def _record(row):
        result = json.loads(row["metadata"])
        if not isinstance(result, dict):
            raise ValueError("Invalid Activity record metadata")
        for key in row.keys():  # noqa: SIM118 -- sqlite3.Row iterates values, not names.
            if key != "metadata":
                result[key] = row[key]
        return result

    def list_requests(
        self,
        *,
        limit=50,
        before=None,
        model_id=None,
        source_id=None,
        active=None,
        state=None,
        since=None,
        until=None,
    ):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Activity page limit must be between 1 and 200")
        if not self.path.exists():
            return []
        db = self._connect()
        try:
            return self._rows(
                db,
                limit=limit,
                before=before,
                model_id=model_id,
                source_id=source_id,
                active=active,
                state=state,
                since=since,
                until=until,
            )
        finally:
            db.close()

    def _rows(
        self,
        db,
        *,
        limit=50,
        before=None,
        model_id=None,
        source_id=None,
        active=None,
        state=None,
        since=None,
        until=None,
    ):
        where, args = [], []
        for field, value in (
            ("model_id", model_id),
            ("source_id", source_id),
            ("state", state),
        ):
            if value is not None:
                where.append(f"{field}=?")
                args.append(value)
        if before is not None:
            where.append("(received_at,id)<(?,?)")
            args.extend(before)
        if active is not None:
            where.append("finished_at IS " + ("NULL" if active else "NOT NULL"))
        for operator, value in ((">=", since), ("<=", until)):
            if value is not None:
                where.append(f"received_at{operator}?")
                args.append(value)
        query = "SELECT * FROM requests"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY received_at DESC,id DESC LIMIT ?"
        args.append(limit)
        return [self._record(row) for row in db.execute(query, args)]

    def _detail(self, db, request_id, *, invocation_summary=False):
        row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            return None
        return self._detail_for_row(db, row, invocation_summary=invocation_summary)

    def _detail_for_row(self, db, row, *, invocation_summary=False):
        """Build detail from an already selected row in the caller's snapshot."""
        request_id = row["id"]
        result = self._record(row)
        result["representations"] = [
            {
                "invocation_id": item["invocation_id"],
                "representation": item["representation"],
                "bytes": item["bytes"],
                "chunks": item["chunks"],
                "last_sequence": item["last_sequence"],
                "encoding": "utf-8",
            }
            for item in db.execute(
                "SELECT invocation_id,representation,SUM(logical_bytes) AS bytes,"
                "COUNT(*) AS chunks,MAX(sequence) AS last_sequence FROM content_chunks "
                "WHERE request_id=? GROUP BY invocation_id,representation "
                "ORDER BY invocation_id,representation",
                (request_id,),
            )
        ]
        result["attachments"] = self._attachments(db, request_id)
        result["invocations"] = self._invocations(
            db, request_id, result, summary=invocation_summary
        )
        result["invocation_detail_scope"] = "budgeted_snapshots_may_have_gaps"
        result["invocation_options_included"] = not invocation_summary
        result["producer_output_layer"] = (
            "engine output before API formatting; native engine parsers may already have transformed text"
        )
        result["capture_status"] = (
            "gaps_recorded" if result["capture_gaps"] else "no_gaps_recorded"
        )
        retained = {item["representation"] for item in result["representations"]}
        result["observation"] = {
            "invocations_observed": result.get("observed_invocations", 0),
            "counts_scope": "observed_scheduler_invocations_only",
            "input_count_source": "admitted_token_ids"
            if "observed_input_tokens" in result
            else "unavailable",
            "generated_count_source": "engine_cumulative_counter"
            if "observed_generated_tokens" in result
            else "unavailable",
            "prefill_callback_observed": bool(result.get("prefilling_invocations")),
            "prepared_text_retained": "model_input" in retained,
            "prepared_token_ids_retained": "model_tokens" in retained,
            "producer_output_retained": "raw_output" in retained,
            "api_wire_retained": "api_response" in retained,
            "preparser_output_guaranteed": False,
        }
        result["coverage"] = evidence_coverage(
            result, result["representations"], result["attachments"]
        )
        result["version"] = hashlib.sha256(
            json.dumps(result, sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest()
        return result

    @staticmethod
    def _json_functions_available(db):
        """Probe JSON support on this connection; only absence selects fallback."""
        try:
            db.execute(
                "SELECT json_type('{}', '$'), json_extract('{}', '$')"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such function: json_" in str(exc).lower():
                return False
            raise
        return True

    @staticmethod
    def _invocation_summary(row, request):
        if row["root_type"] != "object":
            raise TypeError("Activity invocation metadata must be an object")
        record = {"id": row["invocation_id"]}
        for key in INVOCATION_SUMMARY_FIELDS:
            value_type = row[f"{key}_type"]
            if value_type is None:
                continue
            value = row[key]
            if value_type == "true":
                value = True
            elif value_type == "false":
                value = False
            elif value_type in ("array", "object"):
                # Only an allowed summary field is decoded, never option payloads.
                value = json.loads(value)
            record[key] = value
        record["scope"] = "observed_scheduler_invocation_snapshot"
        return ActivityReader._mark_invocation_observation_ended(record, request)

    @staticmethod
    def _mark_invocation_observation_ended(record, request):
        if (
            request["finished_at"] is not None
            and record.get("state") not in TERMINAL_STATES
        ):
            record["last_observed_state"] = record.get("state")
            record["state"] = "observation_ended"
            if "currently_paused" in record:
                record["last_observed_paused"] = record["currently_paused"]
                record["currently_paused"] = None
        return record

    @staticmethod
    def _invocations(db, request_id, request, *, summary=False, invocation_id=None):
        if db.execute("PRAGMA user_version").fetchone()[0] < 3:
            return []
        result = []
        projected = summary and ActivityReader._json_functions_available(db)
        if projected:
            columns = ["invocation_id", "json_type(metadata, '$') AS root_type"]
            for key in INVOCATION_SUMMARY_FIELDS:
                path = f"$.{key}"
                columns.extend(
                    (
                        f"json_type(metadata, '{path}') AS {key}_type",
                        f"json_extract(metadata, '{path}') AS {key}",
                    )
                )
            query = (
                "SELECT " + ",".join(columns) + " FROM invocations "
                "WHERE request_id=? AND length(metadata)<=? "
            )
        else:
            query = (
                "SELECT invocation_id,metadata FROM invocations WHERE request_id=? "
                "AND length(metadata)<=? "
            )
        args = [request_id, MAX_INVOCATION_BYTES]
        if invocation_id is not None:
            query += "AND invocation_id=? "
            args.append(invocation_id)
        query += "ORDER BY length(invocation_id),invocation_id LIMIT ?"
        args.append(1 if invocation_id is not None else MAX_INVOCATIONS)
        for row in db.execute(query, args):
            if projected:
                result.append(ActivityReader._invocation_summary(row, request))
                continue
            record = json.loads(row["metadata"])
            record["id"] = row["invocation_id"]
            record["scope"] = "observed_scheduler_invocation_snapshot"
            if summary:
                record = {
                    key: value
                    for key, value in record.items()
                    if key in {"id", "scope", *INVOCATION_SUMMARY_FIELDS}
                }
            record = ActivityReader._mark_invocation_observation_ended(record, request)
            result.append(record)
        return result

    @staticmethod
    def _attachments(db, request_id):
        if db.execute("PRAGMA user_version").fetchone()[0] < 2:
            return []
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT metadata FROM attachments WHERE request_id=? ORDER BY length(attachment_id),attachment_id LIMIT 128",
                (request_id,),
            )
        ]

    def attachment(self, request_id, attachment_id, *, offset=0, limit=65536):
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= MAX_READ_BYTES
        ):
            raise ValueError("Activity attachment range outside bounds")
        if not self.path.exists():
            return None
        db = self._connect()
        try:
            db.execute("BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] < 2:
                return None
            # SQLite substr bounds the Python buffer even for a large original.
            row = db.execute(
                "SELECT metadata,length(data) AS size,substr(data,?,?) AS data FROM attachments "
                "WHERE request_id=? AND attachment_id=? AND data IS NOT NULL",
                (offset + 1, limit, request_id, attachment_id),
            ).fetchone()
            if row is None:
                return None
            return {
                "metadata": json.loads(row["metadata"]),
                "size": row["size"],
                "data": row["data"],
            }
        finally:
            db.close()

    def invocation(self, request_id, invocation_id):
        if (
            type(invocation_id) is not str
            or not invocation_id.isascii()
            or not invocation_id.isdecimal()
            or len(invocation_id) > 3
            or not 1 <= int(invocation_id) <= MAX_INVOCATIONS
            or str(int(invocation_id)) != invocation_id
            or not self.path.exists()
        ):
            return None
        db = self._connect()
        try:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT * FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                return None
            records = self._invocations(
                db, request_id, self._record(row), invocation_id=invocation_id
            )
            return records[0] if records else None
        finally:
            db.close()

    def detail(self, request_id):
        if not self.path.exists():
            return None
        db = self._connect()
        try:
            db.execute("BEGIN")
            return self._detail(db, request_id)
        finally:
            db.close()

    def snapshot(
        self,
        *,
        request_id=None,
        model_id=None,
        source_id=None,
        scope="all",
        state=None,
        since=None,
        until=None,
    ):
        """One committed metadata/index snapshot; no content BLOBs are read."""
        if not self.path.exists():
            return {
                "active": [],
                "recent": [],
                "active_more": False,
                "recent_more": False,
                "detail": None,
                "selected_outside_filters": None,
            }
        db = self._connect()
        try:
            db.execute("BEGIN")
            shared = dict(
                model_id=model_id,
                source_id=source_id,
                state=state,
                since=since,
                until=until,
            )
            active = (
                self._rows(db, active=True, limit=51, **shared)
                if scope in ("all", "active")
                else []
            )
            recent = (
                self._rows(db, active=False, limit=51, **shared)
                if scope in ("all", "recent")
                else []
            )
            selected_row = (
                db.execute(
                    "SELECT * FROM requests WHERE id=?", (request_id,)
                ).fetchone()
                if request_id
                else None
            )
            selected_detail = (
                self._detail_for_row(db, selected_row, invocation_summary=True)
                if selected_row is not None
                else None
            )
            if selected_row is None:
                outside = None
            else:
                selected_active = selected_row["finished_at"] is None
                outside = not (
                    (
                        scope == "all"
                        or (scope == "active" and selected_active)
                        or (scope == "recent" and not selected_active)
                    )
                    and (model_id is None or selected_row["model_id"] == model_id)
                    and (source_id is None or selected_row["source_id"] == source_id)
                    and (state is None or selected_row["state"] == state)
                    and (since is None or selected_row["received_at"] >= since)
                    and (until is None or selected_row["received_at"] <= until)
                )
            return {
                "active": active[:50],
                "recent": recent[:50],
                "active_more": len(active) > 50,
                "recent_more": len(recent) > 50,
                "detail": selected_detail,
                "selected_outside_filters": outside,
            }
        finally:
            db.close()

    def facets(self):
        if not self.path.exists():
            return {"models": [], "sources": [], "truncated": False}
        db = self._connect()
        try:
            db.execute("BEGIN")
            models = [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT model_id FROM requests ORDER BY model_id LIMIT 201"
                )
            ]
            sources = [
                dict(row)
                for row in db.execute(
                    "SELECT source_id,source_name FROM (SELECT source_id,source_name,"
                    "ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY received_at DESC,id DESC) AS n "
                    "FROM requests) WHERE n=1 ORDER BY source_name,source_id LIMIT 201"
                )
            ]
            return {
                "models": models[:200],
                "sources": sources[:200],
                "truncated": len(models) > 200 or len(sources) > 200,
            }
        finally:
            db.close()

    def get(self, request_id: str):
        if not self.path.exists():
            return None
        db = self._connect()
        try:
            row = db.execute(
                "SELECT * FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            return self._record(row) if row is not None else None
        finally:
            db.close()

    def content(
        self,
        request_id,
        representation,
        *,
        invocation_id="0",
        offset=0,
        limit=65536,
        require_exists=False,
    ):
        if representation not in REPRESENTATIONS:
            raise ValueError("Unknown Activity representation")
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= MAX_READ_BYTES
        ):
            raise ValueError("Activity content range outside bounds")
        if require_exists and not self.path.exists():
            return None
        db = self._connect()
        try:
            # Total length and chunks must describe one committed snapshot.
            db.execute("BEGIN")
            if (
                require_exists
                and db.execute(
                    "SELECT 1 FROM requests WHERE id=?", (request_id,)
                ).fetchone()
                is None
            ):
                return None
            params = (request_id, invocation_id, representation)
            total = db.execute(
                "SELECT COALESCE(SUM(logical_bytes),0) FROM content_chunks "
                "WHERE request_id=? AND invocation_id=? AND representation=?",
                params,
            ).fetchone()[0]
            result = bytearray()
            position = 0
            for row in db.execute(
                "SELECT data,compressed,logical_bytes FROM content_chunks "
                "WHERE request_id=? AND invocation_id=? AND representation=? ORDER BY sequence",
                params,
            ):
                length = row["logical_bytes"]
                if not 0 < length <= MAX_CHUNK_BYTES:
                    raise ValueError("Invalid Activity content length")
                if position + length > offset:
                    data = row["data"]
                    if row["compressed"]:
                        decoder = zlib.decompressobj()
                        data = decoder.decompress(data, length + 1)
                        if (
                            not decoder.eof
                            or decoder.unused_data
                            or len(data) != length
                        ):
                            raise ValueError("Invalid compressed Activity content")
                    elif len(data) != length:
                        raise ValueError("Invalid Activity content length")
                    begin = max(0, offset - position)
                    result.extend(data[begin : begin + limit - len(result)])
                position += length
                if len(result) >= limit:
                    break
            return {
                "offset": offset,
                "data": bytes(result),
                "total_bytes": total,
                "next_offset": offset + len(result),
            }
        finally:
            db.close()
