# syntax=docker/dockerfile:1

# Alpine (musl) base instead of Debian slim: `apt-get install ffmpeg tini` on the
# Debian base pulled in ~200 extra OS packages and ~890 known CVEs (Trivy scan).
# Alpine's ffmpeg is musl-based and much smaller — same codecs and containers,
# ~8 known CVEs in the final image and roughly half the image size.
FROM python:3.15.0-alpine3.24

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DOWNLOAD_DIR=/data \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    DENO_DIR=/tmp/deno

# Deno comes from the Alpine package repository, not from PyPI: the `deno` PyPI
# wheel is glibc-only and fails to build on musl. yt-dlp's default extras
# (yt-dlp-ejs) auto-detect a `deno` binary on PATH, so requirements-docker.txt
# installs `yt-dlp[default]` here.
RUN apk add --no-cache \
        ffmpeg \
        tini \
        deno \
    && addgroup -g 10001 -S app \
    && adduser -u 10001 -S app -G app -H -h /tmp -s /sbin/nologin \
    && mkdir -p /app /data \
    && chown app:app /app /data

WORKDIR /app

COPY requirements-docker.txt ./
RUN python -m pip install --no-cache-dir -r requirements-docker.txt

COPY --chown=app:app *.py ./
COPY --chown=app:app yt_live_downloader/ ./yt_live_downloader/
COPY --chown=app:app templates/ ./templates/
COPY --chown=app:app static/ ./static/

USER 10001:10001

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=2).read()"]

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
