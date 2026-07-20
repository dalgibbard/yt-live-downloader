import json

from yt_live_downloader.storage import DownloadStore


def test_store_persists_jobs(tmp_path):
    root = tmp_path / "downloads"
    first_store = DownloadStore(root)
    job = first_store.create("https://youtu.be/abc123")
    first_store.update(job["id"], title="Persistent title", custom_title=True)
    first_store.close()

    second_store = DownloadStore(root)
    assert second_store.get(job["id"])["title"] == "Persistent title"
    second_store.close()


def test_store_round_trips_unicode_titles_exactly(tmp_path):
    root = tmp_path / "downloads"
    title = "日本語 Cafe\N{COMBINING ACUTE ACCENT} 👩🏽\N{ZERO WIDTH JOINER}💻"
    first_store = DownloadStore(root)
    job = first_store.create("https://youtu.be/abc123")
    first_store.update(job["id"], title=title, custom_title=True)
    first_store.close()

    second_store = DownloadStore(root)
    assert second_store.get(job["id"])["title"] == title
    second_store.close()


def test_stale_attempt_cannot_update_current_job(tmp_path):
    store = DownloadStore(tmp_path / "downloads")
    job = store.create("https://youtu.be/abc123")
    store.update(job["id"], attempt=2)

    assert (
        store.update_for_attempt(job["id"], 1, state="downloading", downloaded_bytes=10)
        is None
    )
    assert store.get(job["id"])["downloaded_bytes"] == 0
    store.close()


def test_source_metadata_does_not_overwrite_custom_title(tmp_path):
    store = DownloadStore(tmp_path / "downloads")
    job = store.create("https://youtu.be/abc123")
    store.update(job["id"], title="My title", custom_title=True)
    store.update_source_metadata(
        job["id"],
        job["attempt"],
        source_title="YouTube title",
        source_id="abc123",
        thumbnail_url="https://i.ytimg.com/example.jpg",
    )

    updated = store.get(job["id"])
    assert updated["title"] == "My title"
    assert updated["source_title"] == "YouTube title"
    store.close()


def test_restart_preserves_queued_attempt_and_invalidates_active_attempt(tmp_path):
    store = DownloadStore(tmp_path / "downloads")
    queued = store.create("https://youtu.be/queued")
    active = store.create("https://youtu.be/active")
    store.update(active["id"], state="downloading", phase="downloading")

    restart_queue = {job["id"]: job for job in store.prepare_incomplete_for_restart()}

    assert restart_queue[queued["id"]]["attempt"] == queued["attempt"]
    assert restart_queue[active["id"]]["attempt"] == active["attempt"] + 1
    assert restart_queue[active["id"]]["state"] == "queued"
    store.close()


def test_legacy_metadata_is_imported_once(tmp_path):
    root = tmp_path / "downloads"
    root.mkdir()
    media = root / "Old_Title.mp4"
    media.write_bytes(b"media")
    (root / "downloads_metadata.json").write_text(
        json.dumps(
            [
                {
                    "id": "Old_Title",
                    "title": "Old Title",
                    "url": "https://youtu.be/abc123",
                    "status": "Finished",
                    "downloaded_bytes": 5,
                    "download_path": "downloads/Old_Title.mp4",
                }
            ]
        ),
        encoding="utf-8",
    )

    store = DownloadStore(root)
    imported = store.list()
    assert len(imported) == 1
    assert imported[0]["state"] == "completed"
    assert imported[0]["file_path"] == "Old_Title.mp4"
    store.close()

    reopened = DownloadStore(root)
    assert len(reopened.list()) == 1
    reopened.close()
