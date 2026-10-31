# SPDX-License-Identifier: Apache-2.0
"""Worker-owned SQLite persistence and bounded, independent read snapshots."""

import json
import sqlite3
import time
import zlib
from pathlib import Path

from .config import ActivitySettings

SCHEMA_VERSION = 1
MAX_CHUNK_BYTES = 256 * 1024
MAX_READ_BYTES = 1024 * 1024
TERMINAL_STATES = frozenset(
    {"completed", "rejected", "cancelled", "error", "interrupted"}
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
            if version not in (0, SCHEMA_VERSION):
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
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            # Leave space for a WAL of similar size plus sidecars/maintenance.
            page_limit = (settings.max_storage_bytes - 256 * 1024) // (2 * page_size)
            db.execute(f"PRAGMA max_page_count={max(32, page_limit)}")
            db.executescript(
                """
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
        if not self._make_room(len(metadata) * 2 + 32768):
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
        if not self._make_room(len(packed) * 2 + 16384):
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
        self.db.execute("PRAGMA incremental_vacuum(128)")
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _make_room(self, extra: int):
        if self.physical_bytes() + extra <= self.settings.max_storage_bytes:
            return True
        self._reclaim()
        while self.physical_bytes() + extra > self.settings.max_storage_bytes:
            row = self.db.execute(
                "SELECT id FROM requests WHERE finished_at IS NOT NULL "
                "ORDER BY finished_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                return False
            self.delete(row[0])
            self._reclaim()
        return True

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
        return db

    @staticmethod
    def _record(row):
        result = json.loads(row["metadata"])
        for key in row.keys():  # noqa: SIM118 -- sqlite3.Row iterates values, not names.
            if key != "metadata":
                result[key] = row[key]
        return result

    def list_requests(
        self, *, limit=50, before=None, model_id=None, source_id=None, active=None
    ):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Activity page limit must be between 1 and 200")
        if not self.path.exists():
            return []
        where, args = [], []
        for field, value in (("model_id", model_id), ("source_id", source_id)):
            if value is not None:
                where.append(f"{field}=?")
                args.append(value)
        if before is not None:
            where.append("(received_at,id)<(?,?)")
            args.extend(before)
        if active is not None:
            where.append("finished_at IS " + ("NULL" if active else "NOT NULL"))
        query = "SELECT * FROM requests"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY received_at DESC,id DESC LIMIT ?"
        args.append(limit)
        db = self._connect()
        try:
            return [self._record(row) for row in db.execute(query, args)]
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
        self, request_id, representation, *, invocation_id="0", offset=0, limit=65536
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
        db = self._connect()
        try:
            # Total length and chunks must describe one committed snapshot.
            db.execute("BEGIN")
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
