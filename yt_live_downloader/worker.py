from __future__ import annotations

import argparse
import json
import logging
import os
import re
import signal
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import yt_dlp

from .filesystem import artifact_filename, move_artifact, unique_destination
from .storage import ACTIVE_STATES, DownloadStore, JobNotFound, utc_now

LOGGER = logging.getLogger(__name__)
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SIDECAR_SUFFIXES = {".part", ".ytdl", ".json", ".jpg", ".jpeg", ".png", ".webp"}


class AttemptSuperseded(RuntimeError):
    """Raised when a worker no longer owns the current job attempt."""


class StopRequested(RuntimeError):
    """Raised when the worker receives an interrupt signal."""


class WorkerFailure(RuntimeError):
    """A user-actionable worker failure."""


def _clean_message(value: Any, limit: int = 2_000) -> str:
    message = _ANSI_ESCAPE.sub("", str(value or "")).replace("\x00", " ").strip()
    return message[:limit]


def _integer(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(number, 0)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(number, 0.0)


def _format_kind(info: dict[str, Any]) -> str:
    video = info.get("vcodec") not in {None, "none"}
    audio = info.get("acodec") not in {None, "none"}
    if video and not audio:
        return "video"
    if audio and not video:
        return "audio"
    return "media" if video or audio else "other"


def _part_key(info: dict[str, Any], fallback: Any = None) -> str:
    kind = _format_kind(info)
    identifier = _clean_message(info.get("format_id") or fallback or "unknown", 120)
    return f"{kind}:{identifier}"


class ProgressReporter:
    """Aggregate yt-dlp's per-format hooks into monotonic job progress."""

    def __init__(
        self,
        store: DownloadStore,
        job_id: str,
        attempt: int,
        *,
        flush_interval: float = 0.5,
    ) -> None:
        self.store = store
        self.job_id = job_id
        self.attempt = attempt
        self.flush_interval = flush_interval
        self.parts: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.final_paths: list[Path] = []
        self.last_error: str | None = None
        self._downloaded_floor = 0
        self._total_floor: int | None = None
        self._last_flush = 0.0
        self._lock = threading.RLock()

    def restore(self, job: dict[str, Any]) -> None:
        """Restore persisted progress before a resumed worker emits new hooks."""
        with self._lock:
            self._downloaded_floor = _integer(job.get("downloaded_bytes")) or 0
            self._total_floor = _integer(job.get("total_bytes"))
            for item in job.get("parts") or []:
                if not isinstance(item, dict):
                    continue
                key = _clean_message(item.get("key"), 160)
                if not key:
                    continue
                kind = _clean_message(item.get("kind"), 20)
                if kind not in {"video", "audio", "media", "other"}:
                    kind = "other"
                state = str(item.get("state") or "").lower()
                self.parts[key] = {
                    "key": key,
                    "kind": kind,
                    "format_id": _clean_message(item.get("format_id"), 80) or None,
                    "state": "finished"
                    if state in {"finished", "completed"}
                    else "pending",
                    "downloaded_bytes": _integer(item.get("downloaded_bytes")) or 0,
                    "total_bytes": _integer(item.get("total_bytes")),
                    "speed_bps": None,
                    "eta_seconds": None,
                }

    def seed(self, info: dict[str, Any]) -> None:
        formats = info.get("requested_formats") or info.get("requested_downloads") or []
        if not isinstance(formats, (list, tuple)):
            return
        with self._lock:
            for item in formats:
                if not isinstance(item, dict):
                    continue
                key = _part_key(item)
                total = _integer(item.get("filesize") or item.get("filesize_approx"))
                self.parts.setdefault(
                    key,
                    {
                        "key": key,
                        "kind": _format_kind(item),
                        "format_id": _clean_message(item.get("format_id"), 80) or None,
                        "state": "pending",
                        "downloaded_bytes": 0,
                        "total_bytes": total,
                        "speed_bps": None,
                        "eta_seconds": None,
                    },
                )

    def progress_hook(self, data: dict[str, Any]) -> None:
        status = str(data.get("status") or "")
        info = data.get("info_dict") if isinstance(data.get("info_dict"), dict) else {}
        key = _part_key(info, Path(str(data.get("filename") or "unknown")).name)
        with self._lock:
            part = self.parts.setdefault(
                key,
                {
                    "key": key,
                    "kind": _format_kind(info),
                    "format_id": _clean_message(info.get("format_id"), 80) or None,
                    "state": "pending",
                    "downloaded_bytes": 0,
                    "total_bytes": None,
                    "speed_bps": None,
                    "eta_seconds": None,
                },
            )
            if status in {"downloading", "finished"}:
                part["downloaded_bytes"] = max(
                    int(part.get("downloaded_bytes") or 0),
                    _integer(data.get("downloaded_bytes")) or 0,
                )
                total = _integer(data.get("total_bytes"))
                if total is None:
                    total = _integer(data.get("total_bytes_estimate"))
                if total is not None:
                    part["total_bytes"] = max(total, part["downloaded_bytes"])
                part["speed_bps"] = _number(data.get("speed"))
                part["eta_seconds"] = _integer(data.get("eta"))
                part["state"] = "finished" if status == "finished" else "downloading"
                self._flush_locked(force=status == "finished")
            elif status == "error":
                part["state"] = "failed"
                self.last_error = _clean_message(data.get("error") or "Download failed")
                self._flush_locked(force=True)

    def postprocessor_hook(self, data: dict[str, Any]) -> None:
        status = str(data.get("status") or "")
        processor = _clean_message(data.get("postprocessor") or "media", 100)
        if status in {"started", "processing", "finished"}:
            detail = (
                f"Processing media with {processor}"
                if processor
                else "Processing media"
            )
            self._write(
                state="post_processing",
                phase="post_processing",
                detail=detail,
                force=True,
            )

    def post_hook(self, filename: str) -> None:
        with self._lock:
            self.final_paths.append(Path(filename))
        self._write(
            state="post_processing",
            phase="finalizing",
            detail="Finalizing the media file",
            force=True,
        )

    def mark_waiting(self, detail: str | None = None) -> None:
        self._write(
            state="waiting_live",
            phase="waiting_live",
            detail=detail or "Waiting for the scheduled stream to begin",
            force=True,
        )

    def mark_downloading(self) -> None:
        self._write(
            state="downloading",
            phase="downloading",
            detail="Downloading stream media",
            force=True,
        )

    def mark_finalizing(self) -> None:
        with self._lock:
            for part in self.parts.values():
                if part["state"] in {"pending", "downloading"}:
                    part["state"] = "finished"
                    part["speed_bps"] = None
                    part["eta_seconds"] = None
        self._write(
            state="post_processing",
            phase="finalizing",
            detail="Finalizing the media file",
            force=True,
        )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _write(
        self,
        *,
        state: str,
        phase: str,
        detail: str,
        force: bool,
    ) -> None:
        with self._lock:
            self._flush_locked(
                force=force,
                state=state,
                phase=phase,
                detail=detail,
            )

    def _flush_locked(
        self,
        *,
        force: bool = False,
        state: str = "downloading",
        phase: str | None = None,
        detail: str | None = None,
    ) -> None:
        now = time.monotonic()
        if not force and now - self._last_flush < self.flush_interval:
            return
        snapshot = self._snapshot_locked()
        kind = next(
            (
                part["kind"]
                for part in self.parts.values()
                if part["state"] == "downloading"
            ),
            None,
        )
        result = self.store.update_for_attempt(
            self.job_id,
            self.attempt,
            state=state,
            phase=phase or (f"downloading_{kind}" if kind else state),
            detail=detail
            or (f"Downloading {kind}" if kind else "Downloading stream media"),
            **snapshot,
        )
        if result is None:
            raise AttemptSuperseded("Download attempt is no longer current")
        self._last_flush = now

    def _snapshot_locked(self) -> dict[str, Any]:
        public_parts = []
        downloaded = 0
        totals: list[int] = []
        all_totals_known = bool(self.parts)
        speeds: list[float] = []
        etas: list[int] = []
        for part in self.parts.values():
            downloaded += int(part.get("downloaded_bytes") or 0)
            total = _integer(part.get("total_bytes"))
            if total is None:
                all_totals_known = False
            else:
                totals.append(total)
            if part["state"] == "downloading":
                if part.get("speed_bps") is not None:
                    speeds.append(float(part["speed_bps"]))
                if part.get("eta_seconds") is not None:
                    etas.append(int(part["eta_seconds"]))
            public_parts.append(
                {
                    "key": part["key"],
                    "kind": part["kind"],
                    "format_id": part.get("format_id"),
                    "state": part["state"],
                    "downloaded_bytes": int(part.get("downloaded_bytes") or 0),
                    "total_bytes": total,
                }
            )
        downloaded = max(downloaded, self._downloaded_floor)
        total_bytes = (
            max(sum(totals), downloaded, self._total_floor or 0)
            if all_totals_known
            else None
        )
        percent = None
        if total_bytes:
            percent = min(downloaded / total_bytes * 100.0, 99.9)
        return {
            "downloaded_bytes": downloaded,
            "total_bytes": total_bytes,
            "progress_percent": percent,
            "speed_bps": sum(speeds) if speeds else None,
            "eta_seconds": max(etas) if etas else None,
            "parts": public_parts,
        }


class YtDlpLogger:
    def __init__(self, reporter: ProgressReporter) -> None:
        self.reporter = reporter

    def debug(self, message: str) -> None:
        clean = _clean_message(message)
        if "[wait]" in clean.lower() or "will begin" in clean.lower():
            self.reporter.mark_waiting(clean)
        LOGGER.debug("yt-dlp: %s", clean)

    def info(self, message: str) -> None:
        self.debug(message)

    def warning(self, message: str) -> None:
        clean = _clean_message(message)
        LOGGER.warning("yt-dlp: %s", clean)

    def error(self, message: str) -> None:
        clean = _clean_message(message)
        self.reporter.last_error = clean
        LOGGER.error("yt-dlp: %s", clean)


def _ydl_options(staging: Path, reporter: ProgressReporter) -> dict[str, Any]:
    fragments = _integer(os.environ.get("YTDLP_CONCURRENT_FRAGMENTS")) or 4
    return {
        "format": "bv*+ba/b",
        "outtmpl": str(staging / "%(id)s.%(ext)s"),
        "paths": {"home": str(staging), "temp": str(staging)},
        "noplaylist": True,
        "live_from_start": True,
        "merge_output_format": "mp4/mkv",
        "continuedl": True,
        "concurrent_fragment_downloads": min(max(fragments, 1), 16),
        "retries": 10,
        "fragment_retries": 10,
        "socket_timeout": 30,
        "progress_hooks": [reporter.progress_hook],
        "postprocessor_hooks": [reporter.postprocessor_hook],
        "post_hooks": [reporter.post_hook],
        "logger": YtDlpLogger(reporter),
        "quiet": True,
        "no_warnings": False,
    }


def _single_video(info: Any) -> dict[str, Any]:
    if not isinstance(info, dict):
        raise WorkerFailure("YouTube did not return stream metadata")
    if info.get("_type") not in {"playlist", "multi_video"}:
        return info
    entries = iter(info.get("entries") or ())
    first = next(entries, None)
    second = next(entries, None)
    if not isinstance(first, dict) or second is not None:
        raise WorkerFailure("The URL must resolve to exactly one YouTube video")
    return _single_video(first)


def _thumbnail(info: dict[str, Any]) -> str | None:
    direct = info.get("thumbnail")
    if isinstance(direct, str) and direct.startswith("https://"):
        return direct[:4_096]
    thumbnails = [
        item for item in info.get("thumbnails") or [] if isinstance(item, dict)
    ]
    thumbnails.sort(
        key=lambda item: (
            (_integer(item.get("width")) or 0) * (_integer(item.get("height")) or 0)
        )
    )
    for item in reversed(thumbnails):
        url = item.get("url")
        if isinstance(url, str) and url.startswith("https://"):
            return url[:4_096]
    return None


def _persist_metadata(
    store: DownloadStore,
    job_id: str,
    attempt: int,
    info: dict[str, Any],
) -> None:
    source_id = _clean_message(info.get("id"), 128) or None
    source_title = (
        _clean_message(info.get("title"), 500) or source_id or "Untitled stream"
    )
    result = store.update_source_metadata(
        job_id,
        attempt,
        source_title=source_title,
        source_id=source_id,
        thumbnail_url=_thumbnail(info),
        detail="Stream details loaded",
    )
    if result is None:
        raise AttemptSuperseded("Download attempt is no longer current")


def _candidate_artifact(staging: Path, reporter: ProgressReporter, info: Any) -> Path:
    candidates: list[Path] = list(reversed(reporter.final_paths))
    if isinstance(info, dict):
        for value in (info.get("filepath"), info.get("_filename")):
            if value:
                candidates.append(Path(str(value)))
        for item in info.get("requested_downloads") or []:
            if isinstance(item, dict) and item.get("filepath"):
                candidates.append(Path(str(item["filepath"])))
    with suppress(OSError):
        candidates.extend(
            sorted(
                (path for path in staging.rglob("*") if path.is_file()),
                key=lambda path: path.stat().st_size,
                reverse=True,
            )
        )

    root = staging.resolve()
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            if candidate.is_symlink():
                continue
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved in seen or resolved.is_symlink() or not resolved.is_file():
            continue
        seen.add(resolved)
        if resolved.suffix.lower() in _SIDECAR_SUFFIXES or ".f" in resolved.stem:
            continue
        return resolved
    raise WorkerFailure("yt-dlp finished without producing a media file")


@contextmanager
def _block_termination_signals() -> Iterator[None]:
    if not hasattr(signal, "pthread_sigmask"):
        yield
        return
    signals = {signal.SIGTERM, signal.SIGINT}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _finalize(
    store: DownloadStore,
    job_id: str,
    attempt: int,
    source: Path,
    snapshot: dict[str, Any],
) -> None:
    connection = sqlite3.connect(store.db_path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    destination: Path | None = None
    moved = False
    committed = False
    try:
        with _block_termination_signals():
            connection.execute("BEGIN IMMEDIATE")
            active = tuple(sorted(ACTIVE_STATES))
            placeholders = ", ".join("?" for _ in active)
            row = connection.execute(
                f"SELECT * FROM downloads WHERE id = ? AND attempt = ? AND state IN ({placeholders})",
                (job_id, attempt, *active),
            ).fetchone()
            if row is None:
                raise AttemptSuperseded("Download attempt is no longer current")
            filename = artifact_filename(
                str(row["title"]),
                source.suffix,
                source_id=row["source_id"],
                job_id=job_id,
            )
            destination = unique_destination(store.download_root, filename)
            move_artifact(source, destination)
            moved = True
            file_size = destination.stat().st_size
            downloaded = max(int(snapshot["downloaded_bytes"] or 0), file_size)
            parts = json.dumps(snapshot["parts"], separators=(",", ":"))
            now = utc_now()
            cursor = connection.execute(
                f"""
                UPDATE downloads
                SET state = 'completed', phase = 'completed', detail = 'Download completed',
                    downloaded_bytes = ?, total_bytes = ?, progress_percent = 100.0,
                    speed_bps = NULL, eta_seconds = NULL, parts = ?,
                    error_summary = NULL, error_detail = NULL,
                    file_path = ?, file_size = ?, file_deleted_at = NULL,
                    completed_at = ?, updated_at = ?
                WHERE id = ? AND attempt = ? AND state IN ({placeholders})
                """,
                (
                    downloaded,
                    snapshot["total_bytes"],
                    parts,
                    destination.relative_to(store.download_root).as_posix(),
                    file_size,
                    now,
                    now,
                    job_id,
                    attempt,
                    *active,
                ),
            )
            if cursor.rowcount != 1:
                raise AttemptSuperseded("Download attempt changed while finalizing")
            connection.commit()
            committed = True
    except Exception:
        if not committed:
            connection.rollback()
        if (
            not committed
            and moved
            and destination is not None
            and destination.exists()
            and not source.exists()
        ):
            try:
                move_artifact(destination, source)
            except OSError:
                LOGGER.exception("Could not roll back media move for job %s", job_id)
        raise
    finally:
        connection.close()


def _failure_details(error: Exception, reporter: ProgressReporter) -> tuple[str, str]:
    detail = reporter.last_error or _clean_message(error)
    if isinstance(error, WorkerFailure):
        return str(error), detail
    if isinstance(error, yt_dlp.utils.DownloadError):
        return "The stream could not be downloaded", detail
    if isinstance(error, FileNotFoundError):
        return "A required download tool is missing", detail
    if isinstance(error, OSError) and getattr(error, "errno", None) == 28:
        return "The download storage is full", detail
    return "The download failed unexpectedly", detail or error.__class__.__name__


def run_worker(download_root: Path | str, job_id: str, attempt: int) -> int:
    store = DownloadStore(download_root)
    reporter = ProgressReporter(store, job_id, attempt)
    try:
        try:
            job = store.get(job_id)
        except JobNotFound as error:
            raise WorkerFailure("The download job no longer exists") from error
        if job["attempt"] != attempt or job["state"] not in ACTIVE_STATES:
            raise AttemptSuperseded("Download attempt is no longer current")
        reporter.restore(job)
        if (
            store.update_for_attempt(
                job_id,
                attempt,
                state="inspecting",
                phase="inspecting",
                detail="Reading stream details",
                error_summary=None,
                error_detail=None,
            )
            is None
        ):
            raise AttemptSuperseded("Download attempt is no longer current")

        staging = store.download_root / ".staging" / job_id
        staging.mkdir(parents=True, exist_ok=True)
        options = _ydl_options(staging, reporter)
        inspect_options = dict(options, ignore_no_formats_error=True)
        with yt_dlp.YoutubeDL(inspect_options) as ydl:
            info = _single_video(ydl.extract_info(job["url"], download=False))
            _persist_metadata(store, job_id, attempt, info)
            reporter.seed(info)
            if info.get("live_status") == "is_upcoming":
                result = None
            else:
                reporter.mark_downloading()
                result = ydl.process_ie_result(info, download=True)

        if info.get("live_status") == "is_upcoming":
            reporter.mark_waiting()
            wait_options = dict(options, wait_for_video=(5, 60))
            with yt_dlp.YoutubeDL(wait_options) as ydl:
                result = ydl.extract_info(job["url"], download=True)
            latest = _single_video(result)
            _persist_metadata(store, job_id, attempt, latest)

        reporter.mark_finalizing()
        source = _candidate_artifact(staging, reporter, result)
        _finalize(store, job_id, attempt, source, reporter.snapshot())
        return 0
    except AttemptSuperseded:
        LOGGER.info(
            "Worker output ignored for stale job %s attempt %s", job_id, attempt
        )
        return 3
    except (StopRequested, KeyboardInterrupt) as error:
        store.update_for_attempt(
            job_id,
            attempt,
            state="interrupted",
            phase="interrupted",
            detail="Download worker was interrupted",
            speed_bps=None,
            eta_seconds=None,
            error_summary="The download was interrupted",
            error_detail=_clean_message(error) or None,
        )
        return 130
    except Exception as error:
        summary, detail = _failure_details(error, reporter)
        LOGGER.exception(
            "Download worker failed for job %s attempt %s", job_id, attempt
        )
        try:
            store.update_for_attempt(
                job_id,
                attempt,
                state="failed",
                phase="failed",
                detail=summary,
                speed_bps=None,
                eta_seconds=None,
                error_summary=summary,
                error_detail=_clean_message(detail),
            )
        except Exception:
            LOGGER.exception("Could not persist failure for job %s", job_id)
        return 1
    finally:
        store.close()


def _signal_handler(signum: int, _frame: Any) -> None:
    raise StopRequested(f"Received signal {signum}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one persisted download attempt")
    parser.add_argument("--download-root", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    return run_worker(Path(args.download_root), args.job_id, args.attempt)


if __name__ == "__main__":
    raise SystemExit(main())
