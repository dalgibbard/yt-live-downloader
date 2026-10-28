from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

ACTIVE_STATES = frozenset(
    {"queued", "inspecting", "waiting_live", "downloading", "post_processing"}
)
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
ALL_STATES = ACTIVE_STATES | TERMINAL_STATES


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class JobNotFound(KeyError):
    """Raised when a download job does not exist."""


class DownloadStore:
    """Thread-safe SQLite-backed download history.

    A single connection is intentionally shared behind a lock. The application runs
    one coordinator process, while download workers communicate through atomic JSON
    status files. SQLite's WAL protects history from partial writes and makes reads
    durable across restarts.
    """

    _JSON_FIELDS = frozenset({"parts"})
    # Columns added since the first released schema; applied to existing databases.
    # `derived_from` records the capture whose media file produced this capture.
    _ADDED_COLUMNS = (
        ("audio_only", "INTEGER NOT NULL DEFAULT 0"),
        ("derived_from", "TEXT"),
    )
    _BOOL_FIELDS = frozenset({"custom_title", "audio_only"})
    _UPDATABLE_FIELDS = frozenset(
        {
            "url",
            "title",
            "source_title",
            "custom_title",
            "audio_only",
            "derived_from",
            "source_id",
            "thumbnail_url",
            "state",
            "phase",
            "detail",
            "downloaded_bytes",
            "total_bytes",
            "progress_percent",
            "speed_bps",
            "eta_seconds",
            "parts",
            "error_summary",
            "error_detail",
            "file_path",
            "file_size",
            "file_deleted_at",
            "completed_at",
            "attempt",
            "worker_revision",
            "updated_at",
        }
    )

    def __init__(self, download_root: Path | str):
        self.download_root = Path(download_root).resolve()
        self.download_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.download_root / "downloads.sqlite3"
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.db_path,
            timeout=10,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock, self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=10000")
            self._create_schema()
            self._add_missing_columns()
            self._migrate_legacy_json()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT NOT NULL,
                source_title TEXT,
                custom_title INTEGER NOT NULL DEFAULT 0,
                audio_only INTEGER NOT NULL DEFAULT 0,
                source_id TEXT,
                thumbnail_url TEXT,
                state TEXT NOT NULL,
                phase TEXT,
                detail TEXT,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER,
                progress_percent REAL,
                speed_bps REAL,
                eta_seconds INTEGER,
                parts TEXT NOT NULL DEFAULT '[]',
                error_summary TEXT,
                error_detail TEXT,
                file_path TEXT,
                file_size INTEGER,
                file_deleted_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                attempt INTEGER NOT NULL DEFAULT 1,
                worker_revision INTEGER NOT NULL DEFAULT 0,
                CHECK (state IN (
                    'queued', 'inspecting', 'waiting_live', 'downloading',
                    'post_processing', 'completed', 'failed', 'cancelled',
                    'interrupted'
                ))
            );

            CREATE INDEX IF NOT EXISTS downloads_created_at_idx
                ON downloads(created_at DESC);
            CREATE INDEX IF NOT EXISTS downloads_state_idx ON downloads(state);

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

    def _add_missing_columns(self) -> None:
        """Add columns introduced after the first released schema."""
        existing = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(downloads)")
        }
        for name, definition in self._ADDED_COLUMNS:
            if name not in existing:
                # Column names and definitions are module constants, never user input.
                self._connection.execute(
                    f"ALTER TABLE downloads ADD COLUMN {name} {definition}"
                )

    def create(
        self,
        url: str,
        *,
        audio_only: bool = False,
        derived_from: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        job = {
            "id": str(uuid.uuid4()),
            "url": url,
            "title": "Inspecting stream…",
            "source_title": None,
            "custom_title": False,
            "audio_only": bool(audio_only),
            "derived_from": derived_from,
            "source_id": None,
            "thumbnail_url": None,
            "state": "queued",
            "phase": "queued",
            "detail": "Waiting for a download slot",
            "downloaded_bytes": 0,
            "total_bytes": None,
            "progress_percent": None,
            "speed_bps": None,
            "eta_seconds": None,
            "parts": [],
            "error_summary": None,
            "error_detail": None,
            "file_path": None,
            "file_size": None,
            "file_deleted_at": None,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "attempt": 1,
            "worker_revision": 0,
        }
        encoded = self._encode(job)
        columns = ", ".join(encoded)
        placeholders = ", ".join("?" for _ in encoded)
        with self._lock, self._connection:
            self._connection.execute(
                f"INSERT INTO downloads ({columns}) VALUES ({placeholders})",
                tuple(encoded.values()),
            )
        return job

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM downloads WHERE id = ?", (job_id,)
            ).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        return self._decode(row)

    def list(self, states: Iterable[str] | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM downloads"
        values: tuple[Any, ...] = ()
        if states is not None:
            selected = tuple(states)
            if not selected:
                return []
            query += f" WHERE state IN ({', '.join('?' for _ in selected)})"
            values = selected
        query += " ORDER BY created_at DESC, id DESC"
        with self._lock:
            rows = self._connection.execute(query, values).fetchall()
        return [self._decode(row) for row in rows]

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        self._validate_changes(changes)
        if not changes:
            return self.get(job_id)
        changes.setdefault("updated_at", utc_now())
        encoded = self._encode(changes)
        assignments = ", ".join(f"{field} = ?" for field in encoded)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"UPDATE downloads SET {assignments} WHERE id = ?",
                (*encoded.values(), job_id),
            )
        if cursor.rowcount == 0:
            raise JobNotFound(job_id)
        return self.get(job_id)

    def update_for_attempt(
        self, job_id: str, expected_attempt: int, **changes: Any
    ) -> dict[str, Any] | None:
        """Update an active attempt, ignoring output from stale worker processes."""
        self._validate_changes(changes)
        if not changes:
            job = self.get(job_id)
            return job if job["attempt"] == expected_attempt else None
        changes.setdefault("updated_at", utc_now())
        encoded = self._encode(changes)
        assignments = ", ".join(f"{field} = ?" for field in encoded)
        active = tuple(sorted(ACTIVE_STATES))
        placeholders = ", ".join("?" for _ in active)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"""
                UPDATE downloads SET {assignments}
                WHERE id = ? AND attempt = ? AND state IN ({placeholders})
                """,
                (*encoded.values(), job_id, expected_attempt, *active),
            )
        if cursor.rowcount == 0:
            return None
        return self.get(job_id)

    def update_source_metadata(
        self,
        job_id: str,
        attempt: int,
        *,
        source_title: str,
        source_id: str | None,
        thumbnail_url: str | None,
        detail: str | None = None,
    ) -> dict[str, Any] | None:
        """Persist extractor metadata without racing a user-supplied title."""
        now = utc_now()
        active = tuple(sorted(ACTIVE_STATES))
        placeholders = ", ".join("?" for _ in active)
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"""
                UPDATE downloads
                SET source_title = ?,
                    title = CASE WHEN custom_title = 0 THEN ? ELSE title END,
                    source_id = ?, thumbnail_url = ?, detail = ?, updated_at = ?
                WHERE id = ? AND attempt = ? AND state IN ({placeholders})
                """,
                (
                    source_title,
                    source_title,
                    source_id,
                    thumbnail_url,
                    detail,
                    now,
                    job_id,
                    attempt,
                    *active,
                ),
            )
        if cursor.rowcount == 0:
            return None
        return self.get(job_id)

    def delete(self, job_id: str) -> None:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM downloads WHERE id = ?", (job_id,)
            )
        if cursor.rowcount == 0:
            raise JobNotFound(job_id)

    def prepare_incomplete_for_restart(self) -> list[dict[str, Any]]:
        """Queue jobs left active by an application restart for safe resumption."""
        jobs = self.list(ACTIVE_STATES)
        for job in jobs:
            # Queued jobs have no current worker. Other active states may belong
            # to an orphaned worker, so invalidate that attempt before resuming.
            attempt = job["attempt"] if job["state"] == "queued" else job["attempt"] + 1
            self.update(
                job["id"],
                state="queued",
                phase="queued",
                detail="Resuming after an application restart",
                attempt=attempt,
                worker_revision=0,
                speed_bps=None,
                eta_seconds=None,
            )
        return self.list({"queued"})

    def _validate_changes(self, changes: dict[str, Any]) -> None:
        unknown = set(changes) - self._UPDATABLE_FIELDS
        if unknown:
            raise ValueError(
                f"Unsupported download fields: {', '.join(sorted(unknown))}"
            )
        if "state" in changes and changes["state"] not in ALL_STATES:
            raise ValueError(f"Unsupported download state: {changes['state']}")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def ping(self) -> None:
        with self._lock:
            self._connection.execute("SELECT 1").fetchone()

    def _encode(self, values: dict[str, Any]) -> dict[str, Any]:
        encoded: dict[str, Any] = {}
        for key, value in values.items():
            if key in self._JSON_FIELDS:
                encoded[key] = json.dumps(value, separators=(",", ":"))
            elif key in self._BOOL_FIELDS:
                encoded[key] = int(bool(value))
            else:
                encoded[key] = value
        return encoded

    def _decode(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for field in self._JSON_FIELDS:
            try:
                result[field] = json.loads(result[field] or "[]")
            except (TypeError, json.JSONDecodeError):
                result[field] = []
        for field in self._BOOL_FIELDS:
            result[field] = bool(result[field])
        return result

    def _setting_exists(self, key: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        return row is not None

    def _set_setting(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO app_settings(key, value) VALUES (?, ?)",
            (key, value),
        )

    def _migrate_legacy_json(self) -> None:
        migration_key = "legacy_json_migrated"
        if self._setting_exists(migration_key):
            return

        metadata_path = self.download_root / "downloads_metadata.json"
        if not metadata_path.exists():
            self._set_setting(migration_key, utc_now())
            return

        try:
            records = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                raise TypeError("legacy metadata root is not a list")
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            LOGGER.warning("Could not migrate %s: %s", metadata_path, error)
            self._set_setting(migration_key, f"failed: {utc_now()}")
            return

        for record in records:
            if not isinstance(record, dict) or not record.get("url"):
                continue
            now = utc_now()
            title = str(record.get("title") or "Untitled download")
            file_path = self._legacy_relative_path(record.get("download_path"))
            file_exists = bool(file_path and (self.download_root / file_path).is_file())
            legacy_status = str(record.get("status") or "").lower()
            if legacy_status in {"finished", "completed"}:
                state = "completed"
                error_summary = None
            elif legacy_status in {"failed", "error"}:
                state = "failed"
                error_summary = "The legacy download failed"
            else:
                state = "interrupted"
                error_summary = (
                    "Download was interrupted before the application was upgraded"
                )
            values = {
                "id": str(uuid.uuid4()),
                "url": str(record["url"]),
                "title": title,
                "source_title": title,
                "custom_title": 0,
                "source_id": None,
                "thumbnail_url": None,
                "state": state,
                "phase": state,
                "detail": "Imported from downloads_metadata.json",
                "downloaded_bytes": int(record.get("downloaded_bytes") or 0),
                "total_bytes": None,
                "progress_percent": 100.0 if state == "completed" else None,
                "speed_bps": None,
                "eta_seconds": None,
                "parts": "[]",
                "error_summary": error_summary,
                "error_detail": None,
                "file_path": file_path,
                "file_size": (
                    (self.download_root / file_path).stat().st_size
                    if file_exists
                    else None
                ),
                "file_deleted_at": None,
                "created_at": now,
                "updated_at": now,
                "completed_at": now if state == "completed" else None,
                "attempt": 1,
                "worker_revision": 0,
            }
            columns = ", ".join(values)
            placeholders = ", ".join("?" for _ in values)
            self._connection.execute(
                f"INSERT INTO downloads ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )

        self._set_setting(migration_key, utc_now())
        LOGGER.info("Imported %d legacy download record(s)", len(records))

    def _legacy_relative_path(self, raw_path: Any) -> str | None:
        if not raw_path:
            return None
        path = Path(str(raw_path))
        if path.is_absolute():
            try:
                return path.resolve().relative_to(self.download_root).as_posix()
            except ValueError:
                return None

        parts = path.parts
        if parts and parts[0] == self.download_root.name:
            path = Path(*parts[1:])
        candidate = (self.download_root / path).resolve()
        try:
            return candidate.relative_to(self.download_root).as_posix()
        except ValueError:
            return None
