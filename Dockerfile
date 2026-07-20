# syntax=docker/dockerfile:1

FROM python:3.13.14-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DOWNLOAD_DIR=/data \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    DENO_DIR=/tmp/deno

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --no-log-init \
        --home-dir /tmp --shell /usr/sbin/nologin app \
    && mkdir -p /app /data \
    && chown app:app /app /data

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install -r requirements.txt

COPY --chown=app:app *.py ./
COPY --chown=app:app yt_live_downloader/ ./yt_live_downloader/
COPY --chown=app:app templates/ ./templates/
COPY --chown=app:app static/ ./static/

USER 10001:10001

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=2).read()"]

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
