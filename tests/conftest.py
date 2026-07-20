from __future__ import annotations

from typing import Any

import pytest

from yt_live_downloader.storage import ACTIVE_STATES, DownloadStore
from yt_live_downloader.web import create_app


class FakeManager:
    def __init__(self, store: DownloadStore):
        self.store = store
        self.started = False
        self.cleaned: list[str] = []

    def start(self) -> None:
        self.started = True

    def stop(self, *_args: Any, **_kwargs: Any) -> None:
        self.started = False

    def tick(self) -> None:
        return None

    def submit(self, url: str) -> dict[str, Any]:
        return self.store.create(url)

    def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job["state"] not in ACTIVE_STATES:
            raise ValueError("Only an active download can be cancelled")
        return self.store.update(
            job_id,
            state="cancelled",
            phase="cancelled",
            detail="Cancelled by user",
            speed_bps=None,
            eta_seconds=None,
        )

    def retry(self, job_id: str) -> dict[str, Any]:
        job = self.store.get(job_id)
        if job["state"] not in {"failed", "cancelled", "interrupted"}:
            raise ValueError("Only a failed or cancelled download can be retried")
        return self.store.update(
            job_id,
            state="queued",
            phase="queued",
            detail="Waiting for a download slot",
            attempt=job["attempt"] + 1,
            downloaded_bytes=0,
            total_bytes=None,
            progress_percent=None,
            parts=[],
            error_summary=None,
            error_detail=None,
        )

    def cleanup_history_artifacts(self, job: dict[str, Any] | str) -> None:
        job_id = job if isinstance(job, str) else job["id"]
        self.cleaned.append(job_id)


@pytest.fixture
def app_bundle(tmp_path, monkeypatch):
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("PASSWORD", raising=False)
    monkeypatch.delenv("PASSWORD_FILE", raising=False)
    download_root = tmp_path / "downloads"
    store = DownloadStore(download_root)
    manager = FakeManager(store)
    app = create_app(
        {
            "TESTING": True,
            "DOWNLOAD_DIR": str(download_root),
            "START_DOWNLOAD_MANAGER": False,
        },
        store=store,
        manager=manager,
    )
    yield app, app.test_client(), store, manager, download_root
    store.close()
