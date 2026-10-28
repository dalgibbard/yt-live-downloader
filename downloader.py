#!/usr/bin/env python3
"""Compatibility entry point for one persisted download worker."""

from yt_live_downloader.worker import main

if __name__ == "__main__":
    raise SystemExit(main())
