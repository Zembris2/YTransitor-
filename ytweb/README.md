# ytweb — YouTube to MP4 / MP3

A small local web app that wraps the yt-dlp binary already installed on this
machine. Paste a link in the browser, pick MP4 or MP3, watch live progress.

No pip packages: the server is Python standard library only.

## Run

```
start.bat
```

or:

```
python server.py --port 8765 --no-browser
```

Then open http://127.0.0.1:8765 — it only listens on localhost.

## Requirements

- **yt-dlp** — auto-detected on PATH and in the usual install folders
  (currently found at `C:\Users\STS\Downloads\yt-dlp.exe`, version 2026.08.19).
  Any other location can be typed into the settings panel.
- **ffmpeg** — required in practice. YouTube now serves video and audio as
  separate tracks for essentially every video, so they have to be merged, and
  MP3 extraction needs it too. Already present on this machine via the winget
  package `yt-dlp.FFmpeg`; the server globs the WinGet package folder to find
  it, because that install adds no PATH entry a running process can see.

Without ffmpeg the app falls back to pre-muxed streams. Those barely exist on
YouTube any more, so most links will fail with a message saying exactly this.

## Features

- MP4 with a quality cap (360p … 4K, or best available), H.264 preferred over
  AV1/VP9 so the file plays anywhere
- MP3 at 192 kbps
- A real queue: only N jobs download at once (default 2, configurable 1–8), the
  rest wait and show their position
- Abort sweeps up the partial files that the killed download left behind
- Optional browser session for age-gated and members-only videos
- Startup capability probe: checks the yt-dlp binary still has every flag this
  server depends on, and says so in the UI if not
- Fallback progress reader, used if the custom progress template ever stops
  producing output
- Stall detection: a live but silent job shows a sweeping bar instead of a
  frozen 0%
- "Fetch info" shows title, channel, duration, thumbnail, and the highest
  resolution the video actually offers
- Live progress bar with speed and ETA, streamed over server-sent events
- Playlist mode (off by default, so a link with `&list=` grabs one video)
- A finished job offers "play" (hands the file to the default player) and
  "folder" (opens Explorer with the file selected). The server and the browser
  are on the same machine, so re-downloading the file through the browser would
  only make a duplicate; the `/file` endpoint still exists for the case where
  the server is reached from another machine.
- Settings for binary paths, output folder, parallel slots and cookie source,
  saved to `config.json` next to the server

## Files

| File | Purpose |
| --- | --- |
| `server.py` | HTTP server, job queue, yt-dlp process handling |
| `index.html` | The whole front end (no build step, no dependencies) |
| `start.bat` / `start.sh` | Launchers |
| `config.json` | Written on first save of the settings panel |

## Notes

- Downloads land in `~/Downloads/ytweb` unless changed in settings.
- The file endpoint refuses to serve anything outside the output folder.
- Jobs live in memory, so the list resets when the server restarts; the files
  themselves stay on disk.

## Test status

Verified end to end against real videos: metadata probe, MP4 download with
H.264 + AAC merge, MP3 at 192 kbps, file endpoint headers, and the path-escape
guard (returns 403).

Written but **not yet executed**: the queue, abort cleanup, cookie support,
capability probe, fallback parser, stall detection, and the UI fixes that
followed the H.264 change.
