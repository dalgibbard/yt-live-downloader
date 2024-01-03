# yt-live-downloader
Crappy Flask App to download Youtube live streams, since most other apps don't seem to natively support the ```live-from-start``` or ```wait-for-video``` flags, and they often have lots of fluffy features I don't need (just want to save an ongoing stream for later viewing for now).
This should also work for simple video downloading anyway, but not tested yet.

## Notes to self
* yt-dlp doesn't support interrupts, and python doesn't allow for thread killing. So initially I had downloader.py opening it's own Flask server, but that results in "Bad File Descriptor" errors. So for now, information exchange between threads is via files on disk, and that seems to work OK for now. Ideally, some sort of queue (and improved metadata) implementation would be better, be it Redis or MongoDB etc.

## To Do
* Fix/Test Docker image
* Add example docker-compose
* Add optional BasicAuth support
* Download dir set by Envvars
* Improve Queue Handling
* Improve Metadata handling
* Maybe use websockets instead of Ajax for status updates, and switch to React etc.
* Support file playback in browser
* Better handling of errors
* On completion, rename the downloaded file to the Video Title name
* Other bugs I probably haven't noticed yet. It's late, i'm tired.
