# yt-live-downloader

![Screenshot_20240103_234758](https://github.com/dalgibbard/yt-live-downloader/assets/1159620/82b26879-f7eb-42f1-95b4-98445d049a08)

Crappy Flask App to download Youtube live streams, since most other apps don't seem to natively support the ```live-from-start``` or ```wait-for-video``` flags, and they often have lots of fluffy features I don't need (just want to save an ongoing stream for later viewing for now).
This should also work for simple video downloading anyway, but not tested yet.

## Example Docker Run
Docker:
```
mkdir downloads
docker run -it -p 5000:5000 -v downloads:/app/downloads dalgibbard/yt-live-downloader:latest
```

Docker Compose:
```
wget https://raw.githubusercontent.com/dalgibbard/yt-live-downloader/main/docker-compose.yml -O docker-compose.yml
docker compose up -d
```

## Docker Environment Variables
* ```USERNAME``` and ```PASSWORD``` - set these to enable Basic Auth authentication

## Docker Volumes
* ```/app/downloads``` should be mounted in order to preserve the downloads and metadata on disk.

## Notes to self
* yt-dlp doesn't support interrupts, and python doesn't allow for thread killing. So initially I had downloader.py opening it's own Flask server, but that results in "Bad File Descriptor" errors. So for now, information exchange between threads is via files on disk, and that seems to work OK for now. Ideally, some sort of queue (and improved metadata) implementation would be better, be it Redis or MongoDB etc.

## To Do
* Add video title back into download table
* Improve Queue Handling
* Improve Metadata handling
* Maybe use websockets instead of Ajax for status updates, and switch to React etc.
* Support file playback in browser
* Better handling of errors
* On completion, rename the downloaded file to the Video Title name
* Other bugs I probably haven't noticed yet. It's late, i'm tired.
