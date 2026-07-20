from __future__ import annotations

import base64

import pytest

from tests.conftest import FakeManager
from yt_live_downloader.storage import DownloadStore, utc_now
from yt_live_downloader.web import create_app, validate_youtube_url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://youtu.be/abc123",
        "https://youtube.com/live/abc123",
        "https://youtube.com/@example/live",
    ],
)
def test_validate_youtube_url_accepts_single_video_forms(url):
    assert validate_youtube_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://youtube.com/watch?v=abc123",
        "file:///etc/passwd",
        "https://youtube.com.example.test/watch?v=abc123",
        "https://youtube.com/playlist?list=abc123",
        "https://example.test/watch?v=abc123",
    ],
)
def test_validate_youtube_url_rejects_non_video_or_unsafe_targets(url):
    with pytest.raises(ValueError):
        validate_youtube_url(url)


def test_create_and_list_download(app_bundle):
    _app, client, _store, _manager, _root = app_bundle
    response = client.post("/api/downloads", json={"url": "https://youtu.be/abc123"})
    assert response.status_code == 202
    created = response.get_json()["download"]
    assert created["state"] == "queued"
    assert created["file"]["state"] == "pending"

    listed = client.get("/api/downloads").get_json()["downloads"]
    assert [item["id"] for item in listed] == [created["id"]]
    assert "file_path" not in listed[0]


def test_failure_is_visible_in_api(app_bundle):
    _app, client, store, manager, _root = app_bundle
    job = manager.submit("https://youtu.be/abc123")
    store.update(
        job["id"],
        state="failed",
        phase="failed",
        error_summary="YouTube rejected the request",
        error_detail="HTTP Error 403",
    )

    payload = client.get(f"/api/downloads/{job['id']}").get_json()["download"]
    assert payload["state"] == "failed"
    assert payload["error"]["summary"] == "YouTube rejected the request"
    assert payload["error"]["retryable"] is True


def test_rename_completed_download_moves_file(app_bundle):
    _app, client, store, manager, root = app_bundle
    job = manager.submit("https://youtu.be/abc123")
    original = root / "Original [abc123].mp4"
    original.write_bytes(b"video")
    store.update(
        job["id"],
        title="Original",
        source_title="Original",
        source_id="abc123",
        state="completed",
        phase="completed",
        progress_percent=100,
        file_path=original.name,
        file_size=original.stat().st_size,
        completed_at=utc_now(),
    )

    response = client.patch(
        f"/api/downloads/{job['id']}", json={"title": "A better / title"}
    )
    assert response.status_code == 200
    renamed = response.get_json()["download"]
    assert renamed["title"] == "A better / title"
    assert renamed["file"]["name"] == "A better – title [abc123].mp4"
    assert not original.exists()
    assert (root / renamed["file"]["name"]).read_bytes() == b"video"


def test_delete_file_keeps_history(app_bundle):
    _app, client, store, manager, root = app_bundle
    job = manager.submit("https://youtu.be/abc123")
    media = root / "Stream [abc123].mp4"
    media.write_bytes(b"video")
    store.update(
        job["id"],
        title="Stream",
        state="completed",
        phase="completed",
        file_path=media.name,
        file_size=media.stat().st_size,
    )

    response = client.delete(f"/api/downloads/{job['id']}/file")
    assert response.status_code == 200
    assert response.get_json()["download"]["file"]["state"] == "deleted"
    assert not media.exists()
    assert store.get(job["id"])["state"] == "completed"


def test_external_file_removal_is_reported_as_missing(app_bundle):
    _app, client, store, manager, _root = app_bundle
    job = manager.submit("https://youtu.be/abc123")
    store.update(
        job["id"],
        state="completed",
        phase="completed",
        file_path="removed-elsewhere.mp4",
        file_size=100,
    )

    payload = client.get(f"/api/downloads/{job['id']}").get_json()["download"]
    assert payload["state"] == "completed"
    assert payload["file"]["state"] == "missing"


def test_remove_history_never_deletes_media(app_bundle):
    _app, client, store, manager, root = app_bundle
    job = manager.submit("https://youtu.be/abc123")
    media = root / "Keep me.mp4"
    media.write_bytes(b"video")
    store.update(
        job["id"],
        state="completed",
        phase="completed",
        file_path=media.name,
        file_size=media.stat().st_size,
    )

    response = client.delete(f"/api/downloads/{job['id']}")
    assert response.status_code == 200
    assert media.exists()
    assert client.get(f"/api/downloads/{job['id']}").status_code == 404


def test_active_file_and_history_deletion_are_rejected(app_bundle):
    _app, client, _store, manager, _root = app_bundle
    job = manager.submit("https://youtu.be/abc123")
    assert client.delete(f"/api/downloads/{job['id']}/file").status_code == 409
    assert client.delete(f"/api/downloads/{job['id']}").status_code == 409


def test_cross_site_mutation_is_rejected(app_bundle):
    _app, client, _store, _manager, _root = app_bundle
    response = client.post(
        "/api/downloads",
        json={"url": "https://youtu.be/abc123"},
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403


def test_basic_auth_protects_ui_and_api(tmp_path, monkeypatch):
    monkeypatch.setenv("USERNAME", "viewer")
    monkeypatch.setenv("PASSWORD", "secret")
    root = tmp_path / "downloads"
    store = DownloadStore(root)
    manager = FakeManager(store)
    app = create_app(
        {
            "TESTING": True,
            "DOWNLOAD_DIR": str(root),
            "START_DOWNLOAD_MANAGER": False,
        },
        store=store,
        manager=manager,
    )
    client = app.test_client()
    assert client.get("/").status_code == 401
    assert client.get("/api/downloads").status_code == 401
    token = base64.b64encode(b"viewer:secret").decode()
    assert (
        client.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200
    )
    assert client.get("/healthz").status_code == 200
    store.close()
