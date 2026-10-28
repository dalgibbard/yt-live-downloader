from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .storage import ACTIVE_STATES, DownloadStore, JobNotFound

LOGGER = logging.getLogger(__name__)


@dataclass
class _RunningWorker:
    job_id: str
    attempt: int
    process: subprocess.Popen[Any]
    terminate_deadline: float | None = None


class DownloadManager:
    """Bounded supervisor for persisted yt-dlp worker subprocesses."""

    def __init__(
        self,
        store: DownloadStore,
        *,
        max_workers: int = 6,
        poll_interval: float = 0.5,
        terminate_timeout: float = 5.0,
        worker_script: Path | str | None = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least one")
        self.store = store
        self.max_workers = max_workers
        self.poll_interval = max(float(poll_interval), 0.05)
        self.terminate_timeout = max(float(terminate_timeout), 0.1)
        self.worker_script = Path(
            worker_script or Path(__file__).resolve().parents[1] / "downloader.py"
        )
        self._workers: dict[str, _RunningWorker] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._prepared = False
        self._stopping = False

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping = False
            self._stop_event.clear()
            if not self._prepared:
                self.store.prepare_incomplete_for_restart()
                self._prepared = True
            self.tick()
            self._thread = threading.Thread(
                target=self._run,
                name="download-manager",
                daemon=True,
            )
            self._thread.start()

    def submit(
        self,
        url: str,
        *,
        audio_only: bool = False,
        derived_from: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._stopping:
                raise RuntimeError("Download manager is stopping")
            job = self.store.create(
                url, audio_only=audio_only, derived_from=derived_from
            )
            self.tick()
            return self.store.get(job["id"])

    def tick(self) -> None:
        with self._lock:
            self._reap_workers()
            self._escalate_terminations()
            if self._stopping:
                return
            available = self.max_workers - len(self._workers)
            if available <= 0:
                return
            queued = list(reversed(self.store.list({"queued"})))
            for job in queued:
                if available <= 0:
                    break
                if job["id"] in self._workers:
                    continue
                if self._start_worker(job):
                    available -= 1

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.store.get(job_id)
            if job["state"] == "cancelled":
                return job
            if job["state"] not in ACTIVE_STATES:
                raise ValueError("Only an active download can be cancelled")
            cancelled = self.store.update_for_attempt(
                job_id,
                job["attempt"],
                state="cancelled",
                phase="cancelled",
                detail="Download cancelled",
                speed_bps=None,
                eta_seconds=None,
                error_summary=None,
                error_detail=None,
            )
            if cancelled is None:
                current = self.store.get(job_id)
                if current["state"] == "cancelled":
                    return current
                raise ValueError("The download is no longer active")
            worker = self._workers.get(job_id)
            if worker is not None and worker.attempt == job["attempt"]:
                self._terminate(worker)
            return cancelled

    def retry(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.store.get(job_id)
            if job["state"] not in {"failed", "cancelled", "interrupted"}:
                raise ValueError(
                    "Only a failed, cancelled, or interrupted download can be retried"
                )
            retried = self.store.update(
                job_id,
                state="queued",
                phase="queued",
                detail="Waiting for a download slot",
                downloaded_bytes=0,
                total_bytes=None,
                progress_percent=None,
                speed_bps=None,
                eta_seconds=None,
                parts=[],
                error_summary=None,
                error_detail=None,
                completed_at=None,
                attempt=job["attempt"] + 1,
                worker_revision=0,
            )
            self.tick()
            return retried

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            if self._stopping and not self._workers:
                return
            self._stopping = True
            self._stop_event.set()
            for worker in list(self._workers.values()):
                try:
                    self.store.update_for_attempt(
                        worker.job_id,
                        worker.attempt,
                        state="queued",
                        phase="queued",
                        detail="Paused while the recorder stopped; resuming on startup",
                        attempt=worker.attempt + 1,
                        worker_revision=0,
                        speed_bps=None,
                        eta_seconds=None,
                        error_summary=None,
                        error_detail=None,
                    )
                except Exception:
                    # Still terminate the worker. If persistence was unavailable,
                    # its active state will be recovered by startup reconciliation.
                    LOGGER.exception(
                        "Could not queue download %s during shutdown", worker.job_id
                    )
                self._terminate(worker)
            thread = self._thread

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=min(max(timeout, 0.0), self.poll_interval + 1.0))

        deadline = time.monotonic() + max(timeout, 0.0)
        while time.monotonic() < deadline:
            with self._lock:
                self._reap_workers()
                if not self._workers:
                    break
            time.sleep(0.05)

        with self._lock:
            for worker in list(self._workers.values()):
                self._signal(worker, signal.SIGKILL)
            for worker in list(self._workers.values()):
                try:
                    worker.process.wait(timeout=1)
                except (subprocess.TimeoutExpired, OSError):
                    LOGGER.warning(
                        "Could not reap worker process %s", worker.process.pid
                    )
            self._workers.clear()
            self._thread = None

    def cleanup_history_artifacts(self, job: dict[str, Any] | str) -> None:
        job_id = job if isinstance(job, str) else str(job["id"])
        try:
            canonical_id = str(uuid.UUID(job_id))
        except (ValueError, AttributeError) as error:
            raise ValueError("Invalid download identifier") from error
        staging_root = (self.store.download_root / ".staging").resolve()
        target = staging_root / canonical_id
        if target.is_symlink():
            target.unlink()
        elif target.exists():
            shutil.rmtree(target)

    def _run(self) -> None:
        while not self._stop_event.wait(self.poll_interval):
            try:
                self.tick()
            except Exception:
                LOGGER.exception("Download manager tick failed")

    def _start_worker(self, job: dict[str, Any]) -> bool:
        attempt = int(job["attempt"])
        claimed = self.store.update_for_attempt(
            job["id"],
            attempt,
            state="inspecting",
            phase="inspecting",
            detail="Starting download worker",
            error_summary=None,
            error_detail=None,
        )
        if claimed is None:
            return False
        command = [
            sys.executable,
            str(self.worker_script),
            "--download-root",
            str(self.store.download_root),
            "--job-id",
            job["id"],
            "--attempt",
            str(attempt),
        ]
        environment = os.environ.copy()
        environment.setdefault("NO_COLOR", "1")
        try:
            # argv list is internal only: sys.executable, the pinned worker script,
            # the configured download root, a UUID job id and an int attempt - no
            # shell, no user-supplied command.
            process = subprocess.Popen(  # noqa: S603
                command,
                cwd=str(self.worker_script.parent),
                env=environment,
                shell=False,
                start_new_session=True,
            )
        except OSError as error:
            LOGGER.exception("Could not start worker for download %s", job["id"])
            self.store.update_for_attempt(
                job["id"],
                attempt,
                state="failed",
                phase="failed",
                detail="The download worker could not be started",
                error_summary="The download worker could not be started",
                error_detail=str(error)[:2_000],
            )
            return False
        self._workers[job["id"]] = _RunningWorker(job["id"], attempt, process)
        return True

    def _reap_workers(self) -> None:
        for job_id, worker in list(self._workers.items()):
            return_code = worker.process.poll()
            if return_code is None:
                continue
            del self._workers[job_id]
            try:
                job = self.store.get(job_id)
            except JobNotFound:
                continue
            if job["attempt"] != worker.attempt or job["state"] not in ACTIVE_STATES:
                continue
            if return_code == 0:
                summary = "The worker exited before the download was finalized"
            else:
                summary = "The download worker stopped unexpectedly"
            self.store.update_for_attempt(
                job_id,
                worker.attempt,
                state="failed",
                phase="failed",
                detail=summary,
                speed_bps=None,
                eta_seconds=None,
                error_summary=summary,
                error_detail=f"Worker exit code: {return_code}",
            )

    def _terminate(self, worker: _RunningWorker) -> None:
        if worker.process.poll() is not None:
            return
        self._signal(worker, signal.SIGTERM)
        worker.terminate_deadline = time.monotonic() + self.terminate_timeout

    def _escalate_terminations(self) -> None:
        now = time.monotonic()
        for worker in self._workers.values():
            if (
                worker.terminate_deadline is not None
                and worker.process.poll() is None
                and now >= worker.terminate_deadline
            ):
                self._signal(worker, signal.SIGKILL)
                worker.terminate_deadline = None

    @staticmethod
    def _signal(worker: _RunningWorker, signum: signal.Signals) -> None:
        if worker.process.poll() is not None:
            return
        try:
            if (
                os.name == "posix"
                and os.getpgid(worker.process.pid) == worker.process.pid
            ):
                os.killpg(worker.process.pid, signum)
            else:
                worker.process.send_signal(signum)
        except ProcessLookupError:
            return
        except OSError:
            LOGGER.exception("Could not signal worker process %s", worker.process.pid)
