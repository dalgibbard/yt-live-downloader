from __future__ import annotations

import atexit
import logging
import os
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from flask import Flask, Response, jsonify, render_template, request, send_file, url_for

from .filesystem import (
    artifact_filename,
    move_artifact,
    resolve_stored_path,
    unique_destination,
)
from .manager import DownloadManager
from .storage import ACTIVE_STATES, DownloadStore, JobNotFound, utc_now

LOGGER = logging.getLogger(__name__)
MAX_URL_LENGTH = 2_048
MAX_TITLE_LENGTH = 180
PLAYBACK_MIMETYPES = {
    ".m4v": "video/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".ogg": "video/ogg",
    ".ogv": "video/ogg",
    ".webm": "video/webm",
}
YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtube-nocookie.com",
        "youtube-nocookie.com",
    }
)


def create_app(
    config: dict[str, Any] | None = None,
    *,
    store: DownloadStore | None = None,
    manager: DownloadManager | None = None,
) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(Path(__file__).parent.parent / "templates"),
        static_folder=str(Path(__file__).parent.parent / "static"),
    )
    default_root = Path(__file__).parent.parent / "downloads"
    app.config.from_mapping(
        DOWNLOAD_DIR=os.environ.get("DOWNLOAD_DIR", str(default_root)),
        MAX_CONCURRENT_DOWNLOADS=_positive_int(
            os.environ.get("MAX_CONCURRENT_DOWNLOADS"), default=6, maximum=16
        ),
        MAX_CONTENT_LENGTH=16 * 1024,
        START_DOWNLOAD_MANAGER=True,
        TESTING=False,
    )
    if config:
        app.config.update(config)

    download_root = Path(app.config["DOWNLOAD_DIR"]).resolve()
    store = store or DownloadStore(download_root)
    manager = manager or DownloadManager(
        store,
        max_workers=int(app.config["MAX_CONCURRENT_DOWNLOADS"]),
    )
    app.extensions["download_store"] = store
    app.extensions["download_manager"] = manager

    credentials = _load_credentials()

    @app.before_request
    def protect_application() -> Response | None:
        if request.path == "/healthz":
            return None
        if credentials and not _authorized(*credentials):
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="YouTube Live Downloader"'},
            )
        if request.path.startswith("/api/") and request.method in {
            "POST",
            "PATCH",
            "PUT",
            "DELETE",
        }:
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                return _api_error(
                    "forbidden", "Cross-site requests are not allowed", 403
                )
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc.lower() != request.host.lower():
                return _api_error(
                    "forbidden", "Request origin does not match this server", 403
                )
        return None

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' https: data:; "
            "style-src 'self'; style-src-elem 'self'; style-src-attr 'unsafe-inline'; "
            "script-src 'self'; connect-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        if request.path.startswith("/api/") or request.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/")
    def index() -> str:
        return render_template("index.html")

    @app.get("/healthz")
    def health() -> tuple[Response, int] | Response:
        try:
            store.ping()
        except Exception:
            LOGGER.exception("Health check could not read the download store")
            return jsonify({"status": "unhealthy"}), 503
        return jsonify({"status": "ok"})

    @app.get("/api/downloads")
    def list_downloads() -> Response:
        _tick_safely(manager)
        return jsonify(
            {"downloads": [_serialize_job(job, download_root) for job in store.list()]}
        )

    @app.post("/api/downloads")
    def create_download() -> tuple[Response, int]:
        payload = _json_object()
        if payload is None:
            return _api_error("invalid_request", "Expected a JSON request body", 400)
        try:
            youtube_url = validate_youtube_url(payload.get("url"))
            requested_title = (
                validate_title(payload["title"]) if "title" in payload else None
            )
        except ValueError as error:
            return _api_error("invalid_request", str(error), 400)

        job = manager.submit(youtube_url)
        if requested_title:
            job = store.update(job["id"], title=requested_title, custom_title=True)
        response = jsonify({"download": _serialize_job(job, download_root)})
        response.headers["Location"] = url_for("get_download", job_id=job["id"])
        return response, 202

    @app.get("/api/downloads/<uuid:job_id>")
    def get_download(job_id: Any) -> Response | tuple[Response, int]:
        _tick_safely(manager)
        try:
            job = store.get(str(job_id))
        except JobNotFound:
            return _api_error("not_found", "Download not found", 404)
        return jsonify({"download": _serialize_job(job, download_root)})

    @app.patch("/api/downloads/<uuid:job_id>")
    def rename_download(job_id: Any) -> Response | tuple[Response, int]:
        payload = _json_object()
        if payload is None or "title" not in payload:
            return _api_error("invalid_request", "A title is required", 400)
        try:
            title = validate_title(payload["title"])
            job = _rename_download(store, download_root, str(job_id), title)
        except JobNotFound:
            return _api_error("not_found", "Download not found", 404)
        except (OSError, ValueError) as error:
            LOGGER.warning("Could not rename download %s: %s", job_id, error)
            return _api_error("rename_failed", str(error), 409)
        return jsonify({"download": _serialize_job(job, download_root)})

    @app.post("/api/downloads/<uuid:job_id>/cancel")
    def cancel_download(job_id: Any) -> Response | tuple[Response, int]:
        try:
            job = manager.cancel(str(job_id))
        except JobNotFound:
            return _api_error("not_found", "Download not found", 404)
        except ValueError as error:
            return _api_error("invalid_state", str(error), 409)
        return jsonify({"download": _serialize_job(job, download_root)})

    @app.post("/api/downloads/<uuid:job_id>/retry")
    def retry_download(job_id: Any) -> Response | tuple[Response, int]:
        try:
            job = manager.retry(str(job_id))
        except JobNotFound:
            return _api_error("not_found", "Download not found", 404)
        except ValueError as error:
            return _api_error("invalid_state", str(error), 409)
        return jsonify({"download": _serialize_job(job, download_root)})

    @app.delete("/api/downloads/<uuid:job_id>/file")
    def delete_download_file(job_id: Any) -> Response | tuple[Response, int]:
        try:
            job = _delete_file(store, download_root, str(job_id))
        except JobNotFound:
            return _api_error("not_found", "Download not found", 404)
        except ValueError as error:
            return _api_error("invalid_state", str(error), 409)
        except OSError:
            LOGGER.exception("Could not delete the file for download %s", job_id)
            return _api_error(
                "delete_failed", "The media file could not be deleted from disk", 500
            )
        return jsonify({"download": _serialize_job(job, download_root)})

    @app.delete("/api/downloads/<uuid:job_id>")
    def delete_download_history(job_id: Any) -> Response | tuple[Response, int]:
        try:
            job = store.get(str(job_id))
            if job["state"] in ACTIVE_STATES:
                raise ValueError(
                    "Cancel the active download before removing its history"
                )
            try:
                manager.cleanup_history_artifacts(job)
            except OSError:
                LOGGER.exception("Could not clean staging data for download %s", job_id)
            store.delete(str(job_id))
        except JobNotFound:
            return _api_error("not_found", "Download not found", 404)
        except ValueError as error:
            return _api_error("invalid_state", str(error), 409)
        return jsonify({"deleted": True, "id": str(job_id)})

    @app.get("/api/downloads/<uuid:job_id>/file")
    def download_file(job_id: Any) -> Response | tuple[Response, int]:
        try:
            job = store.get(str(job_id))
            path = _available_file(download_root, job)
        except JobNotFound:
            return _api_error("not_found", "Download not found", 404)
        except ValueError:
            return _api_error("unsafe_path", "The stored media path is invalid", 409)
        if path is None:
            return _api_error("file_missing", "The media file is not available", 404)
        return send_file(
            path, as_attachment=True, download_name=path.name, conditional=True
        )

    @app.get("/api/downloads/<uuid:job_id>/play")
    def play_download(job_id: Any) -> Response | tuple[Response, int]:
        try:
            job = store.get(str(job_id))
            path = _available_file(download_root, job)
        except JobNotFound:
            return _api_error("not_found", "Download not found", 404)
        except ValueError:
            return _api_error("unsafe_path", "The stored media path is invalid", 409)
        if path is None:
            return _api_error("file_missing", "The media file is not available", 404)
        mimetype = _playback_mimetype(path)
        if mimetype is None:
            return _api_error(
                "unsupported_media_type",
                "This file type cannot be played in the browser",
                415,
            )
        return send_file(
            path,
            as_attachment=False,
            download_name=path.name,
            mimetype=mimetype,
            conditional=True,
        )

    if app.config["START_DOWNLOAD_MANAGER"]:
        manager.start()
        atexit.register(manager.stop)

    return app


def validate_youtube_url(raw_url: Any) -> str:
    if not isinstance(raw_url, str):
        raise ValueError("Enter a YouTube URL")
    url = raw_url.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise ValueError("Enter a valid YouTube URL")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("Enter a valid YouTube URL") from error
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or hostname not in YOUTUBE_HOSTS:
        raise ValueError("Only HTTPS YouTube video and live-stream URLs are supported")
    if parsed.username or parsed.password or (port is not None and port != 443):
        raise ValueError(
            "Enter a standard YouTube URL without credentials or a custom port"
        )
    if hostname == "youtu.be":
        if not parsed.path.strip("/"):
            raise ValueError("The YouTube share URL does not contain a video ID")
    elif not _supported_youtube_path(parsed.path, parse_qs(parsed.query)):
        raise ValueError("Enter a URL for one YouTube video or live stream")
    return url


def validate_title(raw_title: Any) -> str:
    if not isinstance(raw_title, str):
        raise ValueError("Title must be text")
    title = raw_title.strip()
    if not title:
        raise ValueError("Title cannot be empty")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValueError(f"Title cannot be longer than {MAX_TITLE_LENGTH} characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in title):
        raise ValueError("Title cannot contain control characters")
    return title


def _supported_youtube_path(path: str, query: dict[str, list[str]]) -> bool:
    segments = [segment for segment in path.split("/") if segment]
    if path.rstrip("/") == "/watch":
        return bool(query.get("v", [""])[0])
    if len(segments) >= 2 and segments[0] in {"live", "shorts", "embed"}:
        return True
    if len(segments) >= 2 and segments[-1] == "live":
        return segments[0].startswith("@") or segments[0] in {"channel", "c", "user"}
    return False


def _rename_download(
    store: DownloadStore, download_root: Path, job_id: str, title: str
) -> dict[str, Any]:
    job = store.get(job_id)
    current = _available_file(download_root, job)
    if job["state"] != "completed" or current is None:
        return store.update(job_id, title=title, custom_title=True)

    filename = artifact_filename(
        title,
        current.suffix,
        source_id=job.get("source_id"),
        job_id=job_id,
    )
    destination = unique_destination(download_root, filename, current=current)
    moved = destination != current
    if moved:
        move_artifact(current, destination)
    try:
        return store.update(
            job_id,
            title=title,
            custom_title=True,
            file_path=destination.relative_to(download_root).as_posix(),
            file_size=destination.stat().st_size,
        )
    except Exception:
        if moved and destination.exists() and not current.exists():
            move_artifact(destination, current)
        raise


def _delete_file(
    store: DownloadStore, download_root: Path, job_id: str
) -> dict[str, Any]:
    job = store.get(job_id)
    if job["state"] in ACTIVE_STATES:
        raise ValueError("Cancel the active download before deleting its file")
    if job.get("file_deleted_at"):
        return job
    path = _available_file(download_root, job)
    if path is None:
        return job
    raw_path = download_root / str(job["file_path"])
    if raw_path.is_symlink():
        raise ValueError("Refusing to delete a symbolic link")
    path.unlink()
    return store.update(job_id, file_deleted_at=utc_now())


def _available_file(download_root: Path, job: dict[str, Any]) -> Path | None:
    if job.get("file_deleted_at"):
        return None
    path = resolve_stored_path(download_root, job.get("file_path"))
    return path if path is not None and path.is_file() else None


def _playback_mimetype(path: Path) -> str | None:
    return PLAYBACK_MIMETYPES.get(path.suffix.lower())


def _serialize_job(job: dict[str, Any], download_root: Path) -> dict[str, Any]:
    relative_path = job.get("file_path")
    file_name = Path(relative_path).name if relative_path else None
    actual_size = job.get("file_size")
    if job.get("file_deleted_at"):
        file_state = "deleted"
    elif relative_path:
        try:
            path = resolve_stored_path(download_root, relative_path)
            if path and path.is_file():
                file_state = "available"
                actual_size = path.stat().st_size
            else:
                file_state = "missing"
        except (OSError, ValueError):
            file_state = "missing"
    elif job["state"] in ACTIVE_STATES:
        file_state = "pending"
    else:
        file_state = "unavailable"

    total_bytes = job.get("total_bytes")
    if total_bytes:
        progress_mode = "bytes"
    elif job["state"] == "downloading":
        progress_mode = "live"
    else:
        progress_mode = "indeterminate"

    error = None
    if job.get("error_summary"):
        error = {
            "summary": job["error_summary"],
            "detail": job.get("error_detail"),
            "retryable": job["state"] in {"failed", "interrupted"},
        }

    return {
        "id": job["id"],
        "url": job["url"],
        "title": job["title"],
        "source_title": job.get("source_title"),
        "thumbnail_url": job.get("thumbnail_url"),
        "state": job["state"],
        "phase": job.get("phase"),
        "detail": job.get("detail"),
        "progress": {
            "mode": progress_mode,
            "downloaded_bytes": job.get("downloaded_bytes") or 0,
            "total_bytes": total_bytes,
            "percent": job.get("progress_percent"),
            "speed_bps": job.get("speed_bps"),
            "eta_seconds": job.get("eta_seconds"),
            "parts": job.get("parts") or [],
        },
        "file": {
            "state": file_state,
            "name": file_name,
            "size_bytes": actual_size,
            "download_url": (
                url_for("download_file", job_id=job["id"])
                if file_state == "available"
                else None
            ),
            "playback_url": (
                url_for("play_download", job_id=job["id"])
                if file_state == "available"
                and file_name
                and _playback_mimetype(Path(file_name))
                else None
            ),
        },
        "error": error,
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "completed_at": job.get("completed_at"),
        "attempt": job["attempt"],
    }


def _json_object() -> dict[str, Any] | None:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else None


def _api_error(code: str, message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": {"code": code, "message": message}}), status


def _tick_safely(manager: DownloadManager) -> None:
    try:
        manager.tick()
    except Exception:
        LOGGER.exception("Download manager reconciliation failed")


def _positive_int(raw_value: str | None, *, default: int, maximum: int) -> int:
    try:
        value = int(raw_value) if raw_value is not None else default
    except ValueError:
        return default
    return min(max(value, 1), maximum)


def _load_credentials() -> tuple[str, str] | None:
    username = os.environ.get("USERNAME")
    password = os.environ.get("PASSWORD")
    password_file = os.environ.get("PASSWORD_FILE")
    if password and password_file:
        raise RuntimeError("Set PASSWORD or PASSWORD_FILE, not both")
    if password_file:
        try:
            password = Path(password_file).read_text(encoding="utf-8").rstrip("\r\n")
        except OSError as error:
            raise RuntimeError(f"Could not read PASSWORD_FILE: {error}") from error
    if bool(username) != bool(password):
        raise RuntimeError(
            "USERNAME and PASSWORD (or PASSWORD_FILE) must be set together"
        )
    return (username, password) if username and password else None


def _authorized(username: str, password: str) -> bool:
    provided = request.authorization
    if not provided or str(provided.type or "").lower() != "basic":
        return False
    provided_username = (provided.username or "").encode("utf-8")
    provided_password = (provided.password or "").encode("utf-8")
    return secrets.compare_digest(
        provided_username, username.encode("utf-8")
    ) and secrets.compare_digest(provided_password, password.encode("utf-8"))
