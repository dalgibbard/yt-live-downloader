# YouTube Live Downloader

![app image](image.png)

A small self-hosted web application for recording YouTube livestreams from the
earliest point YouTube still makes available. Scheduled streams can be queued
before they begin, and regular YouTube videos are supported too.

The Web UI shows the stream title and thumbnail, one coherent progress view,
durable history, actionable failures, and the state of the finished file on
disk.

## Features

- Capture live and scheduled streams with yt-dlp's live-from-start support.
- Read the source title and thumbnail automatically.
- Track video, audio, merging, speed, size, and ETA without progress jumping
  between separate media parts.
- Limit concurrent captures while retaining a durable queue in SQLite.
- Resume active jobs automatically after crashes or graceful service restarts.
- Show download failures and allow failed, cancelled, or interrupted jobs to be
  retried.
- Rename a download; completed files are renamed safely on disk as well.
- Download or delete a media file independently of its history entry.
- Detect files removed outside the application and report them as missing.
- Optionally protect the UI and API with HTTP Basic authentication.

## Docker Compose quick start

The supplied Compose configuration builds the application locally, runs it as
UID/GID `10001`, uses a read-only container filesystem, and publishes it only on
the host loopback interface.

From a checkout of this repository:

```bash
mkdir -p downloads
sudo chown 10001:10001 downloads
sudo chmod 0750 downloads

docker compose up --build -d
docker compose ps
```

Open <http://127.0.0.1:5000>. Follow logs with:

```bash
docker compose logs -f yt-live-downloader
```

To use another host port, set `PORT` for Compose; the container continues to
listen on port 5000:

```bash
PORT=8080 docker compose up --build -d
```

The `downloads` directory holds both media and application state. Do not replace
the bind mount with a temporary container directory.

### Add authentication to Compose

For a shared machine, add a Compose override such as `compose.auth.yml`:

```yaml
services:
  yt-live-downloader:
    environment:
      USERNAME: recorder
      PASSWORD_FILE: /run/secrets/recorder-password
    volumes:
      - ./secrets/recorder-password:/run/secrets/recorder-password:ro
```

Create `secrets/recorder-password` outside source control, make it readable by
UID `10001`, and start both files:

```bash
sudo chown 10001:10001 secrets/recorder-password
sudo chmod 0400 secrets/recorder-password
docker compose -f docker-compose.yml -f compose.auth.yml up --build -d
```

`PASSWORD` can be used instead, but a mounted `PASSWORD_FILE` avoids putting the
password directly in the Compose environment.

## Direct Docker run

```bash
docker build --pull -t yt-live-downloader:local .

docker run -d \
  --name yt-live-downloader \
  --restart unless-stopped \
  --publish 127.0.0.1:5000:5000 \
  --env DOWNLOAD_DIR=/data \
  --env MAX_CONCURRENT_DOWNLOADS=6 \
  --mount type=bind,source="$PWD/downloads",target=/data \
  --read-only \
  --tmpfs /tmp:rw,size=128m,mode=1777 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 \
  yt-live-downloader:local
```

Create and `chown` the `downloads` directory as shown in the Compose quick start
before running this command. Add `USERNAME` and either `PASSWORD` or a mounted
`PASSWORD_FILE` when authentication is required.

## Local Python development

Python 3.13 is recommended. Install `ffmpeg` with the operating system package
manager first; the Python requirements provide yt-dlp and its Deno JavaScript
runtime support.

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
mkdir -p downloads
python app.py
```

The development server listens on <http://127.0.0.1:5000> by default. It is not a
production server; the container runs Gunicorn instead.

Run the checks with:

```bash
python -m pytest
ruff check .
ruff format --check .
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOWNLOAD_DIR` | `./downloads` locally; `/data` in Docker | Media files, SQLite history, and worker state. Must be writable. |
| `MAX_CONCURRENT_DOWNLOADS` | `6` | Maximum simultaneous capture workers. Values are constrained to `1`–`16`. |
| `USERNAME` | unset | Enables Basic Auth when supplied with one password option. |
| `PASSWORD` | unset | Basic Auth password. Mutually exclusive with `PASSWORD_FILE`. |
| `PASSWORD_FILE` | unset | Path to a file containing the Basic Auth password; preferred for containers. |
| `YTDLP_CONCURRENT_FRAGMENTS` | `4` | Parallel fragments per active yt-dlp worker, constrained to `1`–`16`. |
| `LOG_LEVEL` | `INFO` | Worker log level, such as `DEBUG`, `INFO`, or `WARNING`. |
| `HOST` | `127.0.0.1` | Bind address used by `python app.py` only. |
| `PORT` | `5000` | Port used by `python app.py`; also selects the host-side port in the supplied Compose file. |

`USERNAME` and exactly one of `PASSWORD` or `PASSWORD_FILE` must be set together.
Invalid or incomplete authentication configuration stops the application at
startup rather than silently running without protection.

## Architecture

```text
Browser
   │
   ▼
Flask/Gunicorn coordinator ── SQLite history and queue
   │
   └── bounded, supervised worker processes ── yt-dlp ── ffmpeg
```

The Python coordinator owns the API, queue, and durable SQLite state. Each active
capture runs separately under supervision, with transactional progress updates
written to SQLite. yt-dlp handles YouTube extraction and downloads; ffmpeg handles
media merging and finalisation. The production image intentionally runs one
Gunicorn process with multiple threads so that there is only one queue owner.

On graceful service shutdown, the coordinator invalidates each active worker,
keeps its staging files, and persists the job as queued before terminating the
process group. Startup claims those jobs automatically. Explicitly cancelled jobs
remain cancelled and are never resumed automatically.

A Go rewrite would add risk without removing the fast-changing yt-dlp integration
or the need for ffmpeg. The expensive and failure-prone media work already runs
outside the coordinator, so Python's GIL is not the current throughput limit.
Revisiting Go would make sense if measurements show the coordinator itself is a
bottleneck or the application needs multi-host scheduling.

## Download and file states

Download state describes the job:

| State | Meaning |
| --- | --- |
| `queued` | Waiting for an available worker slot, including work paused during service shutdown. |
| `inspecting` | Reading YouTube metadata and selecting formats. |
| `waiting_live` | A scheduled stream has not started yet. |
| `downloading` | Capturing the available video/audio data. |
| `post_processing` | Merging, naming, and finalising the media file. |
| `completed` | Capture and finalisation succeeded. |
| `failed` | The attempt ended with an error; details are shown in the UI. |
| `cancelled` | A user stopped the capture. |
| `interrupted` | A worker was interrupted outside normal service shutdown, or a legacy job ended unexpectedly. |

File state is tracked separately:

| State | Meaning |
| --- | --- |
| `pending` | The job is active and has no final file yet. |
| `available` | The recorded path exists and can be downloaded or deleted. |
| `missing` | A recorded path no longer exists, usually because the file was removed outside the application. |
| `deleted` | The file was deliberately deleted through the Web UI. |
| `unavailable` | The job has no usable final file, commonly after a failure or cancellation. |

## Actions and data ownership

- **Rename** changes the displayed title immediately. An available completed file
  is also renamed safely; existing files are never overwritten.
- **Cancel** stops an active capture but keeps its history entry. Partial worker
  artifacts are retained so a retry can resume where yt-dlp supports it; removing
  the history entry cleans them up.
- **Retry** starts a new attempt for a failed, cancelled, or interrupted job.
- **Download** transfers an available media file through the browser.
- **Delete file** permanently removes only the media file. The history entry stays
  and is marked `deleted`.
- **Remove history** removes the SQLite/UI entry and internal status artifacts. It
  never deletes the media file; an available file becomes unmanaged on disk.

Active jobs must be cancelled before their file or history can be removed. A file
deleted manually from `DOWNLOAD_DIR` is reported as `missing`; its history is not
silently discarded.

Titles are stored as Unicode in SQLite. Final filenames are NFKC-normalised,
UTF-8 byte-limited, and retain Japanese text, combining characters, emoji
variation selectors, emoji ZWJ sequences, and linguistic join controls. Unsafe
filesystem punctuation, control characters, bidi formatting controls, and other
invisible format characters are removed from the filename.

## Upgrading from the legacy JSON application

On the first start with a new database, the application looks for
`downloads_metadata.json` in `DOWNLOAD_DIR` and imports it once into
`downloads.sqlite3`:

- legacy `finished` or `completed` entries become `completed`;
- legacy `failed` or `error` entries become `failed`;
- all other entries become `interrupted` and can be retried.

Only media paths safely contained within `DOWNLOAD_DIR` are retained. The legacy
JSON file is left untouched. Back up the whole directory and validate the JSON
before upgrading: the migration records a failed attempt instead of repeatedly
retrying malformed input.

## Backups and updates

Stop the application before a filesystem-level backup so SQLite, its WAL files,
history, and media are captured consistently:

```bash
docker compose stop
sudo tar -C . -czf yt-live-downloader-backup-YYYYMMDD.tar.gz downloads
docker compose start
```

Captures that were active when Compose stopped are reclaimed automatically and
reuse their partial staging data after startup.

YouTube changes frequently, so yt-dlp freshness matters. Pull repository updates
and rebuild on a regular cadence (weekly is practical) and promptly when
extractor failures begin:

```bash
git pull
docker compose build --pull
docker compose up -d
```

Dependencies are deliberately pinned in `requirements.txt`. Maintainers should
update the yt-dlp pin, run the test/lint commands, and rebuild the image. Do not
run `yt-dlp -U` inside an immutable running container.

## Troubleshooting

- **`Permission denied` under `/data`:** verify the bind-mounted directory is
  owned by UID/GID `10001` and is writable. Rootless Docker/Podman installations
  may require the equivalent user-namespace ownership mapping.
- **JavaScript-runtime or ffmpeg errors:** the Docker image includes Deno support
  and ffmpeg. For local development, reinstall the pinned Python requirements and
  confirm `ffmpeg -version` works. Rebuild from current dependencies after YouTube
  player changes.
- **A capture failed or appears stuck:** expand the error in the UI, then inspect
  `docker compose logs yt-live-downloader`. Check network access, free disk space,
  system time, the source URL, and the yt-dlp version before using **Retry**.
- **The UI cannot reach the service:** run `docker compose ps` and check
  `curl http://127.0.0.1:5000/healthz`. The health endpoint intentionally returns
  only service status and is not protected by Basic Auth.
- **A completed file says `missing`:** restore it at its recorded path or remove
  the stale history entry. Removing history does not remove any other file.

## Network security

HTTP Basic Auth encodes credentials; it does not encrypt them. Keep the default
loopback binding and, if the service must be reachable from another machine, put
an HTTPS reverse proxy such as Caddy, nginx, or Traefik in front of it. Preserve
the original `Host` header, enable `USERNAME` plus `PASSWORD_FILE`, and do not
publish the container's plain HTTP port directly to an untrusted network.

## Legal use

Download only streams and videos that you own or are authorised to retain. You
are responsible for complying with copyright law, YouTube's terms, and any other
rules that apply in your jurisdiction. This project is not affiliated with or
endorsed by YouTube or Google.
