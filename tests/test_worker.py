from __future__ import annotations

import time

import yt_dlp

from yt_live_downloader.manager import DownloadManager
from yt_live_downloader.storage import DownloadStore
from yt_live_downloader.worker import ProgressReporter, _finalize, run_worker


VIDEO_FORMAT = {
    "format_id": "137",
    "vcodec": "avc1",
    "acodec": "none",
    "filesize": 100,
}
AUDIO_FORMAT = {
    "format_id": "140",
    "vcodec": "none",
    "acodec": "mp4a",
    "filesize": 50,
}


def test_progress_aggregates_video_and_audio_without_reset(tmp_path):
    store = DownloadStore(tmp_path / "downloads")
    job = store.create("https://youtu.be/abc123")
    reporter = ProgressReporter(store, job["id"], job["attempt"], flush_interval=0)
    reporter.seed({"requested_formats": [VIDEO_FORMAT, AUDIO_FORMAT]})

    reporter.progress_hook(
        {
            "status": "downloading",
            "info_dict": VIDEO_FORMAT,
            "downloaded_bytes": 60,
            "total_bytes": 100,
            "speed": 20,
            "eta": 2,
        }
    )
    reporter.progress_hook(
        {
            "status": "finished",
            "info_dict": VIDEO_FORMAT,
            "downloaded_bytes": 100,
            "total_bytes": 100,
        }
    )
    reporter.progress_hook(
        {
            "status": "downloading",
            "info_dict": AUDIO_FORMAT,
            "downloaded_bytes": 20,
            "total_bytes": 50,
            "speed": 10,
            "eta": 3,
        }
    )

    updated = store.get(job["id"])
    assert updated["state"] == "downloading"
    assert updated["downloaded_bytes"] == 120
    assert updated["total_bytes"] == 150
    assert updated["progress_percent"] == 80
    assert [part["state"] for part in updated["parts"]] == ["finished", "downloading"]

    reporter.postprocessor_hook({"status": "started", "postprocessor": "Merger"})
    assert store.get(job["id"])["state"] == "post_processing"
    store.close()


def test_resumed_progress_is_restored_before_new_download_hooks(tmp_path):
    store = DownloadStore(tmp_path / "downloads")
    job = store.create("https://youtu.be/abc123")
    previous_parts = [
        {
            "key": "video:137",
            "kind": "video",
            "format_id": "137",
            "state": "finished",
            "downloaded_bytes": 100,
            "total_bytes": 100,
        },
        {
            "key": "audio:140",
            "kind": "audio",
            "format_id": "140",
            "state": "downloading",
            "downloaded_bytes": 20,
            "total_bytes": 50,
        },
    ]
    store.update(
        job["id"],
        downloaded_bytes=120,
        total_bytes=150,
        progress_percent=80,
        parts=previous_parts,
    )
    restored_job = store.get(job["id"])
    reporter = ProgressReporter(store, job["id"], job["attempt"], flush_interval=0)

    reporter.restore(restored_job)
    reporter.seed({"requested_formats": [VIDEO_FORMAT, AUDIO_FORMAT]})
    reporter.mark_downloading()

    resumed = store.get(job["id"])
    assert resumed["downloaded_bytes"] == 120
    assert resumed["total_bytes"] == 150
    assert resumed["progress_percent"] == 80
    assert [part["state"] for part in resumed["parts"]] == ["finished", "pending"]
    store.close()


def test_finalization_uses_latest_title_and_marks_complete(tmp_path):
    root = tmp_path / "downloads"
    store = DownloadStore(root)
    job = store.create("https://youtu.be/abc123")
    store.update(
        job["id"],
        title="My / renamed stream",
        custom_title=True,
        source_id="abc123",
        state="post_processing",
    )
    staging = root / ".staging" / job["id"]
    staging.mkdir(parents=True)
    source = staging / "abc123.mp4"
    source.write_bytes(b"finished media")
    snapshot = {
        "downloaded_bytes": 14,
        "total_bytes": 14,
        "parts": [],
    }

    _finalize(store, job["id"], job["attempt"], source, snapshot)

    completed = store.get(job["id"])
    assert completed["state"] == "completed"
    assert completed["progress_percent"] == 100
    assert completed["file_path"] == "My – renamed stream [abc123].mp4"
    assert (root / completed["file_path"]).read_bytes() == b"finished media"
    store.close()


def test_worker_exception_persists_terminal_failure(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    store = DownloadStore(root)
    job = store.create("https://youtu.be/abc123")
    store.close()

    class FailingYDL:
        def __init__(self, _options):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, *_args, **_kwargs):
            raise yt_dlp.utils.DownloadError("simulated extractor failure")

    monkeypatch.setattr(yt_dlp, "YoutubeDL", FailingYDL)
    assert run_worker(root, job["id"], job["attempt"]) == 1

    reopened = DownloadStore(root)
    failed = reopened.get(job["id"])
    assert failed["state"] == "failed"
    assert failed["error_summary"] == "The stream could not be downloaded"
    assert "simulated extractor failure" in failed["error_detail"]
    reopened.close()


def test_successful_worker_persists_metadata_progress_and_artifact(
    tmp_path, monkeypatch
):
    root = tmp_path / "downloads"
    store = DownloadStore(root)
    job = store.create("https://youtu.be/abc123")
    store.close()

    info = {
        "id": "abc123",
        "title": "A source title",
        "thumbnail": "https://i.ytimg.com/vi/abc123/maxresdefault.jpg",
        "live_status": "not_live",
        "requested_formats": [VIDEO_FORMAT, AUDIO_FORMAT],
    }

    class SuccessfulYDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, *, download):
            assert download is False
            return info

        def process_ie_result(self, extracted, *, download):
            assert extracted is info
            assert download is True
            staging = root / ".staging" / job["id"]
            output = staging / "abc123.mp4"
            output.write_bytes(b"media")
            for media_format, size in ((VIDEO_FORMAT, 100), (AUDIO_FORMAT, 50)):
                event = {
                    "info_dict": media_format,
                    "downloaded_bytes": size,
                    "total_bytes": size,
                }
                self.options["progress_hooks"][0]({"status": "downloading", **event})
                self.options["progress_hooks"][0]({"status": "finished", **event})
            self.options["postprocessor_hooks"][0](
                {"status": "started", "postprocessor": "Merger"}
            )
            self.options["post_hooks"][0](str(output))
            return {**info, "filepath": str(output)}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", SuccessfulYDL)
    assert run_worker(root, job["id"], job["attempt"]) == 0

    reopened = DownloadStore(root)
    completed = reopened.get(job["id"])
    assert completed["state"] == "completed"
    assert completed["source_title"] == "A source title"
    assert completed["thumbnail_url"].endswith("/maxresdefault.jpg")
    assert completed["downloaded_bytes"] == 150
    assert completed["file_path"] == "A source title [abc123].mp4"
    assert (root / completed["file_path"]).read_bytes() == b"media"
    reopened.close()


def test_upcoming_stream_uses_bounded_wait_configuration(tmp_path, monkeypatch):
    root = tmp_path / "downloads"
    store = DownloadStore(root)
    job = store.create("https://youtube.com/live/abc123")
    store.close()
    seen_options = []

    upcoming = {
        "id": "abc123",
        "title": "Scheduled stream",
        "live_status": "is_upcoming",
        "requested_formats": [VIDEO_FORMAT],
    }

    class ScheduledYDL:
        def __init__(self, options):
            self.options = options
            seen_options.append(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, *, download):
            if not download:
                return upcoming
            staging = root / ".staging" / job["id"]
            output = staging / "abc123.mp4"
            output.write_bytes(b"live media")
            event = {
                "info_dict": VIDEO_FORMAT,
                "downloaded_bytes": 100,
                "total_bytes": 100,
            }
            self.options["progress_hooks"][0]({"status": "finished", **event})
            self.options["post_hooks"][0](str(output))
            return {**upcoming, "live_status": "was_live", "filepath": str(output)}

    monkeypatch.setattr(yt_dlp, "YoutubeDL", ScheduledYDL)
    assert run_worker(root, job["id"], job["attempt"]) == 0
    assert len(seen_options) == 2
    assert "wait_for_video" not in seen_options[0]
    assert seen_options[1]["wait_for_video"] == (5, 60)

    reopened = DownloadStore(root)
    assert reopened.get(job["id"])["state"] == "completed"
    reopened.close()


def test_nonzero_child_exit_becomes_failed_instead_of_stuck(tmp_path):
    root = tmp_path / "downloads"
    store = DownloadStore(root)
    worker_script = tmp_path / "failing_worker.py"
    worker_script.write_text("raise SystemExit(7)\n", encoding="utf-8")
    manager = DownloadManager(
        store,
        worker_script=worker_script,
        max_workers=1,
        poll_interval=0.01,
    )

    job = manager.submit("https://youtu.be/abc123")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        manager.tick()
        current = store.get(job["id"])
        if current["state"] == "failed":
            break
        time.sleep(0.01)

    current = store.get(job["id"])
    assert current["state"] == "failed"
    assert current["error_summary"] == "The download worker stopped unexpectedly"
    assert current["error_detail"] == "Worker exit code: 7"
    manager.stop()
    store.close()


def test_cancel_terminates_and_reaps_worker_process(tmp_path):
    root = tmp_path / "downloads"
    store = DownloadStore(root)
    worker_script = tmp_path / "sleeping_worker.py"
    worker_script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    manager = DownloadManager(
        store,
        worker_script=worker_script,
        max_workers=1,
        poll_interval=0.01,
        terminate_timeout=0.1,
    )

    job = manager.submit("https://youtu.be/abc123")
    assert manager.cancel(job["id"])["state"] == "cancelled"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and manager._workers:
        manager.tick()
        time.sleep(0.01)

    assert not manager._workers
    assert store.get(job["id"])["state"] == "cancelled"
    manager.stop()
    assert job["id"] not in {
        queued["id"] for queued in store.prepare_incomplete_for_restart()
    }
    store.close()


def test_graceful_stop_queues_worker_and_keeps_partial_data(tmp_path):
    root = tmp_path / "downloads"
    store = DownloadStore(root)
    worker_script = tmp_path / "sleeping_worker.py"
    worker_script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    manager = DownloadManager(
        store,
        worker_script=worker_script,
        max_workers=1,
        poll_interval=0.01,
        terminate_timeout=0.1,
    )
    job = manager.submit("https://youtu.be/abc123")
    staging = root / ".staging" / job["id"]
    staging.mkdir(parents=True)
    partial = staging / "abc123.mp4.part"
    partial.write_bytes(b"partial media")
    store.update_for_attempt(
        job["id"],
        job["attempt"],
        state="downloading",
        phase="downloading",
        downloaded_bytes=13,
    )

    manager.stop(timeout=2)

    paused = store.get(job["id"])
    assert paused["state"] == "queued"
    assert paused["attempt"] == job["attempt"] + 1
    assert paused["downloaded_bytes"] == 13
    assert paused["error_summary"] is None
    assert partial.read_bytes() == b"partial media"

    queued = {item["id"]: item for item in store.prepare_incomplete_for_restart()}
    assert job["id"] in queued
    assert queued[job["id"]]["attempt"] == paused["attempt"]

    resumed_manager = DownloadManager(
        store,
        worker_script=worker_script,
        max_workers=1,
        poll_interval=0.01,
        terminate_timeout=0.1,
    )
    resumed_manager.start()
    assert job["id"] in resumed_manager._workers
    resumed = store.get(job["id"])
    assert resumed["state"] == "inspecting"
    assert resumed["attempt"] == paused["attempt"]
    resumed_manager.stop(timeout=2)
    store.close()
