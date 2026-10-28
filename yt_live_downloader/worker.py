from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import yt_dlp

from .filesystem import (
    artifact_filename,
    move_artifact,
    resolve_stored_path,
    unique_destination,
)
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

    def begin_extraction(self, total_bytes: int) -> None:
        """Track a local MP3 extraction as one synthetic part."""
        with self._lock:
            self.parts.clear()
            self.parts["extract"] = {
                "key": "extract",
                "kind": "audio",
                "format_id": "mp3",
                "state": "downloading",
                "downloaded_bytes": 0,
                "total_bytes": max(int(total_bytes), 1),
                "speed_bps": None,
                "eta_seconds": None,
            }
        self._write(
            state="post_processing",
            phase="extracting_audio",
            detail="Extracting MP3 audio from the captured video",
            force=True,
        )

    def report_extraction(
        self,
        *,
        downloaded_bytes: int,
        speed_bps: float | None,
        eta_seconds: int | None,
    ) -> None:
        """Publish extraction progress through the same status writer as downloads."""
        with self._lock:
            part = self.parts.get("extract")
            if part is None:
                return
            part["downloaded_bytes"] = max(
                int(part.get("downloaded_bytes") or 0), int(downloaded_bytes)
            )
            part["speed_bps"] = speed_bps
            part["eta_seconds"] = eta_seconds
            self._flush_locked(
                state="post_processing",
                phase="extracting_audio",
                detail="Extracting MP3 audio from the captured video",
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


def _ydl_options(
    staging: Path,
    reporter: ProgressReporter,
    *,
    audio_only: bool = False,
) -> dict[str, Any]:
    fragments = _integer(os.environ.get("YTDLP_CONCURRENT_FRAGMENTS")) or 4
    options: dict[str, Any] = {
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
    if audio_only:
        # Best audio only, then transcode to MP3. `preferredquality` 0 is LAME's
        # highest variable bitrate, which keeps livestream audio transparent.
        options["format"] = "ba/b"
        options.pop("merge_output_format")
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "0",
            }
        ]
    return options


MP3_SUFFIX = ".mp3"
_EXTRACTION_REPORT_INTERVAL = 0.5
_KEY_VALUE_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _tool_executable(name: str) -> str | None:
    """Resolve an FFmpeg tool the way yt-dlp does, falling back to PATH."""
    lookup = getattr(yt_dlp.utils, f"which_{name}", None)
    if callable(lookup):
        resolved = lookup()
        if resolved:
            return str(resolved)
    resolved = shutil.which(name)
    return str(resolved) if resolved else None


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _clock_seconds(value: str) -> float | None:
    text = str(value).strip()
    if not text:
        return None
    if ":" in text:
        total = 0.0
        for component in text.split(":"):
            part = _number(component)
            if part is None:
                return None
            total = total * 60 + part
        return total
    return _number(text)


def _progress_seconds(line: str) -> float | None:
    """Read FFmpeg's `-progress` timestamps, which arrive as microseconds."""
    key, _, value = line.strip().partition("=")
    if key == "out_time":
        return _clock_seconds(value)
    if key in {"out_time_us", "out_time_ms"}:
        microseconds = _number(value)
        return None if microseconds is None else microseconds / 1_000_000.0
    return None


def _probe_media(path: Path) -> tuple[float | None, bool]:
    """Return (duration_seconds, has_audio) for a stored media file.

    Probing is best effort: a missing or failing ffprobe assumes audio is present
    so a capture is never blocked by the probe itself.
    """
    ffprobe = _tool_executable("ffprobe")
    if ffprobe is None:
        return None, True
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=codec_type:format=duration",
        "-of",
        "default=noprint_wrappers=1",
        str(path),
    ]
    try:
        completed = subprocess.run(  # noqa: S603
            command, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        LOGGER.warning("Could not probe media file %s", path.name)
        return None, True
    duration: float | None = None
    has_audio = False
    for line in completed.stdout.splitlines():
        key, _, value = line.partition("=")
        key = key.strip()
        if key == "codec_type" and value.strip() == "audio":
            has_audio = True
        elif key == "duration":
            parsed = _number(value)
            if parsed is not None and parsed > 0:
                duration = parsed
    if completed.returncode != 0:
        LOGGER.warning(
            "Media probe for %s exited with code %s: %s",
            path.name,
            completed.returncode,
            _clean_message(completed.stderr, 400),
        )
        return duration, True
    return duration, has_audio


def _ffmpeg_extract_command(ffmpeg: str, source: Path, destination: Path) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-nostats",
        "-progress",
        "pipe:2",
        "-i",
        str(source),
        "-vn",
        "-map",
        "0:a:0",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "0",
        "-y",
        str(destination),
    ]


def _extract_audio_variant(
    store: DownloadStore,
    job: dict[str, Any],
    attempt: int,
    reporter: ProgressReporter,
    staging: Path,
) -> Path:
    """Turn a completed video capture into a standalone MP3 capture with FFmpeg."""
    parent_id = str(job.get("derived_from") or "")
    try:
        parent = store.get(parent_id)
    except JobNotFound as error:
        raise WorkerFailure(
            "The video capture this MP3 was requested from no longer exists"
        ) from error
    source = resolve_stored_path(store.download_root, parent.get("file_path"))
    if parent.get("file_deleted_at") or source is None or not source.is_file():
        raise WorkerFailure("The video file is not available to extract an MP3 from")
    ffmpeg = _tool_executable("ffmpeg")
    if ffmpeg is None:
        raise WorkerFailure("FFmpeg is required to extract MP3 audio")

    duration, has_audio = _probe_media(source)
    if not has_audio:
        raise WorkerFailure("The video file does not contain an audio track")

    source_title = (
        _clean_message(parent.get("source_title") or parent.get("title"), 500)
        or "Untitled stream"
    )
    if (
        store.update_source_metadata(
            job["id"],
            attempt,
            source_title=source_title,
            source_id=parent.get("source_id"),
            thumbnail_url=parent.get("thumbnail_url"),
            detail="Extracting MP3 audio from the video capture",
        )
        is None
    ):
        raise AttemptSuperseded("Download attempt is no longer current")

    destination = staging / f"audio{MP3_SUFFIX}"
    total_bytes = max(_file_size(source), 1)
    reporter.begin_extraction(total_bytes)

    process = subprocess.Popen(  # noqa: S603
        _ffmpeg_extract_command(ffmpeg, source, destination),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    problems: list[str] = []
    last_report = time.monotonic()
    last_bytes = 0
    try:
        progress_lines = process.stderr
        if progress_lines is None:
            raise WorkerFailure("FFmpeg did not report extraction progress")
        for line in progress_lines:
            elapsed = _progress_seconds(line)
            if elapsed is not None:
                if duration:
                    fraction = min(max(elapsed / duration, 0.0), 1.0)
                    done = int(total_bytes * fraction)
                else:
                    done = min(_file_size(destination), total_bytes)
                now = time.monotonic()
                window = now - last_report
                if window >= _EXTRACTION_REPORT_INTERVAL:
                    speed = (done - last_bytes) / window
                    eta = int((total_bytes - done) / speed) if speed > 0 else None
                    reporter.report_extraction(
                        downloaded_bytes=done,
                        speed_bps=speed,
                        eta_seconds=eta,
                    )
                    last_report = now
                    last_bytes = done
                continue
            if not _KEY_VALUE_LINE.match(line.strip()):
                message = _clean_message(line, 300)
                if message:
                    problems.append(message)
        returncode = process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.terminate()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
    if returncode != 0:
        reason = " ".join(problems[-3:]).strip()
        summary = _classify_failure(reason) or "FFmpeg could not extract the MP3 audio"
        raise WorkerFailure(f"{summary}: {reason}" if reason else summary)
    if _file_size(destination) <= 0:
        raise WorkerFailure("FFmpeg produced an empty MP3 file")
    reporter.report_extraction(
        downloaded_bytes=total_bytes, speed_bps=None, eta_seconds=None
    )
    return destination


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


# Raw extractor/ffmpeg messages mapped onto summaries a person can act on. The
# original message stays in `error_detail` so nothing is lost.
_FAILURE_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "no video formats found",
            "no formats found",
            "requested format is not available",
        ),
        "YouTube did not offer a format that can be downloaded",
    ),
    (
        (
            "this video is unavailable",
            "video is unavailable",
            "no longer available",
            "has been removed",
            "removed from youtube",
            "account holding this video",
        ),
        "This video is no longer available on YouTube",
    ),
    (("this video is private", "private video"), "This video is private"),
    (
        ("members-only", "members only", "channel membership"),
        "This video is members-only",
    ),
    (("age-restricted", "confirm your age"), "This video is age-restricted"),
    (
        ("not a bot", "sign in to confirm"),
        "YouTube asked for a sign-in before it would serve this video",
    ),
    (("premiere",), "This video has not premiered yet"),
    (
        (
            "blocked because of licensing",
            "not available in your country",
            "not available in your region",
        ),
        "This video is not available in your region",
    ),
    (("http error 404",), "YouTube could not find this video"),
    (("http error 410",), "YouTube says this video has been removed"),
    (("http error 403",), "YouTube rejected the request"),
    (("http error 429",), "YouTube is rate limiting these requests"),
    (
        (
            "getaddrinfo failed",
            "name or service not known",
            "temporary failure in name resolution",
            "unable to connect",
            "connection reset",
            "connection refused",
            "timed out",
            "network is unreachable",
        ),
        "YouTube could not be reached",
    ),
    (
        ("does not contain any stream", "no audio track", "invalid audio stream"),
        "The media file does not contain an audio track",
    ),
    (
        ("invalid data found when processing input", "moov atom not found"),
        "The stored media file looks damaged",
    ),
)


def _classify_failure(message: Any) -> str | None:
    """Turn a raw extractor or FFmpeg message into a human-readable summary."""
    lowered = _clean_message(message).lower()
    if not lowered:
        return None
    for needles, summary in _FAILURE_HINTS:
        if any(needle in lowered for needle in needles):
            return summary
    return None


def _failure_details(error: Exception, reporter: ProgressReporter) -> tuple[str, str]:
    detail = reporter.last_error or _clean_message(error)
    classified = _classify_failure(detail) or _classify_failure(error)
    if isinstance(error, WorkerFailure):
        return str(error), detail
    if isinstance(error, (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError)):
        return classified or "The stream could not be downloaded", detail
    if isinstance(error, FileNotFoundError):
        return "A required download tool is missing", detail
    if isinstance(error, OSError) and getattr(error, "errno", None) == 28:
        return "The download storage is full", detail
    return (
        classified or "The download failed unexpectedly",
        detail or error.__class__.__name__,
    )


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

        if job.get("derived_from"):
            # An MP3 capture extracted from a completed video capture: nothing is
            # fetched from YouTube, FFmpeg re-reads the stored media file instead.
            source = _extract_audio_variant(store, job, attempt, reporter, staging)
            reporter.mark_finalizing()
            _finalize(store, job_id, attempt, source, reporter.snapshot())
            return 0

        options = _ydl_options(
            staging, reporter, audio_only=bool(job.get("audio_only"))
        )
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
