#!/usr/bin/env python3
import os

from yt_live_downloader import create_app

app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
        threaded=True,
        use_reloader=False,
    )
