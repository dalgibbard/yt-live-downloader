#!/usr/bin/env python3
import os
import sys
import yt_dlp
import json


def download_video(url, download_path, status_file):
    def progress_hook(d):
        if d['status'] == 'downloading':
            status = {
                "status": "Downloading",
                "downloaded_bytes": d.get('downloaded_bytes', 0)
            }
            with open(status_file, 'w') as f:
                json.dump(status, f)
        elif d['status'] == 'finished':
            status = {
                "status": "Finished",
                "downloaded_bytes": d.get('downloaded_bytes', 0)
            }
            with open(status_file, 'w') as f:
                json.dump(status, f)
                
    ydl_opts = {
        'progress_hooks': [progress_hook],
        'outtmpl': download_path,
        'live_from_start': True,
        'wait_for_video': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # Get the available formats
        info = ydl.extract_info(url, download=False)
        formats = info.get('formats', [])
        print("Available formats:")
        for fmt in formats:
            print(fmt['format_id'], fmt['ext'])

        # Choose the best available format
        best_format = formats[0]['format_id']
        print("Selected format:", best_format)

        # Download the video
        ydl_opts['format'] = best_format
        ydl.download([url])

    # Update status to Finished after download is complete
    with open(status_file, 'w') as f:
        json.dump({"status": "Finished", "downloaded_bytes": 0}, f)

if __name__ == "__main__":
    video_url = sys.argv[1]
    output_path = sys.argv[2]
    status_file = sys.argv[3]
    download_video(video_url, output_path, status_file)
