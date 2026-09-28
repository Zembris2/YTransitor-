#!/usr/bin/env python3
"""Local web app that converts YouTube links to MP4 or MP3.

The heavy lifting is done by a yt-dlp binary that is already installed on this
machine; this server only drives it and streams progress to the browser.

Run:
    python server.py [--port 8765] [--no-browser]

Then open http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import collections
import json
import mimetypes
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
INDEX_PATH = APP_DIR / "index.html"

PROGRESS_PREFIX = "__PROG__"
PROGRESS_TEMPLATE = (
    "download:" + PROGRESS_PREFIX + "%(progress.status)s\t"
    "%(progress.downloaded_bytes)s\t"
    "%(progress.total_bytes,progress.total_bytes_estimate)s\t"
    "%(progress.speed)s\t%(progress.eta)s"
)

# Windows-only flag; keeps a console window from flashing on every call.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------


def default_outdir() -> str:
    downloads = Path.home() / "Downloads"
    base = downloads if downloads.is_dir() else Path.home()
    return str(base / "ytweb")


def clamp_concurrency(value) -> int:
    """How many downloads may run at once. Keep it small: parallel jobs fight
    over the same bandwidth and YouTube throttles hard on burst traffic."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 2
    return max(1, min(8, number))


def candidate_ytdlp_paths() -> list[str]:
    """Common install locations, in rough order of likelihood."""
    home = Path.home()
    env = os.environ
    windows = [
        (env.get("LOCALAPPDATA"), r"Microsoft\WindowsApps\yt-dlp.exe"),
        (env.get("LOCALAPPDATA"), r"Programs\yt-dlp\yt-dlp.exe"),
        (env.get("USERPROFILE"), r"scoop\shims\yt-dlp.exe"),
        (env.get("ProgramData"), r"chocolatey\bin\yt-dlp.exe"),
        (env.get("ProgramFiles"), r"yt-dlp\yt-dlp.exe"),
    ]
    paths = [str(Path(base) / tail) for base, tail in windows if base]
    paths += [
        str(home / "Downloads" / "yt-dlp.exe"),
        str(home / "yt-dlp.exe"),
        str(home / ".local" / "bin" / "yt-dlp"),
        "/usr/local/bin/yt-dlp",
        "/usr/bin/yt-dlp",
    ]
    return paths


def candidate_ffmpeg_paths() -> list[str]:
    """Where the common Windows installers drop ffmpeg.exe.

    winget keeps the real binary under a versioned package folder and only adds
    a shim to PATH, which a running process will not see until it restarts, so
    the package folder is globbed directly.
    """
    home = Path.home()
    env = os.environ
    paths: list[str] = []

    local = env.get("LOCALAPPDATA")
    if local:
        winget = Path(local) / "Microsoft" / "WinGet"
        paths.append(str(winget / "Links" / "ffmpeg.exe"))
        packages = winget / "Packages"
        if packages.is_dir():
            paths += [str(p) for p in packages.glob("*FFmpeg*/**/bin/ffmpeg.exe")]

    fixed = [
        (env.get("USERPROFILE"), r"scoop\shims\ffmpeg.exe"),
        (env.get("ProgramData"), r"chocolatey\bin\ffmpeg.exe"),
        (env.get("ProgramFiles"), r"ffmpeg\bin\ffmpeg.exe"),
    ]
    paths += [str(Path(base) / tail) for base, tail in fixed if base]
    paths += [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        str(home / "Downloads" / "ffmpeg" / "bin" / "ffmpeg.exe"),
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ]
    paths += [str(p) for p in (home / "Downloads").glob("ffmpeg*/**/bin/ffmpeg.exe")]
    return paths


def find_binary(name: str, extra: list[str] | None = None) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for path in extra or []:
        if Path(path).is_file():
            return path
    return None


# Flags this server depends on. If a future yt-dlp drops one of them the app
# would fail in confusing ways, so the startup probe says so up front.
REQUIRED_FLAGS = (
    "--progress-template",
    "--print-to-file",
    "--remux-video",
    "--cookies-from-browser",
    "--ffmpeg-location",
)
CAPABILITY_CACHE: dict[str, dict] = {}


def check_capabilities(runner: list[str]) -> dict:
    """Ask yt-dlp for its help text and confirm the flags we rely on exist."""
    key = " ".join(runner)
    cached = CAPABILITY_CACHE.get(key)
    if cached:
        return cached

    try:
        out = subprocess.run(
            runner + ["--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Not cached: a transient failure should not poison later checks.
        return {"checked": False, "missing": [], "error": str(exc)}

    help_text = (out.stdout or "") + (out.stderr or "")
    result = {
        "checked": True,
        "missing": [flag for flag in REQUIRED_FLAGS if flag not in help_text],
        "error": "",
    }
    CAPABILITY_CACHE[key] = result
    return result


def probe_runner(runner: list[str]) -> str | None:
    """Return the version string if `runner --version` works, else None."""
    try:
        out = subprocess.run(
            runner + ["--version"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


COOKIE_BROWSERS = (
    "chrome",
    "edge",
    "firefox",
    "brave",
    "chromium",
    "opera",
    "vivaldi",
    "safari",
)


class Config:
    def __init__(self) -> None:
        self.ytdlp: str = ""
        self.ffmpeg: str = ""
        self.outdir: str = default_outdir()
        self.max_concurrent: int = 2
        self.cookies_browser: str = ""
        self.load()

    def load(self) -> None:
        if CONFIG_PATH.is_file():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            self.ytdlp = data.get("ytdlp", "") or ""
            self.ffmpeg = data.get("ffmpeg", "") or ""
            self.outdir = data.get("outdir") or default_outdir()
            self.max_concurrent = clamp_concurrency(data.get("maxConcurrent"))
            browser = (data.get("cookiesBrowser") or "").lower()
            self.cookies_browser = browser if browser in COOKIE_BROWSERS else ""
        # A saved path goes stale when the config is copied to another machine
        # or winget upgrades into a new versioned folder; re-detect in that case.
        if self.ytdlp and not Path(self.ytdlp).is_file():
            self.ytdlp = ""
        if self.ffmpeg and not Path(self.ffmpeg).is_file():
            self.ffmpeg = ""
        if not self.ytdlp:
            self.ytdlp = find_binary("yt-dlp", candidate_ytdlp_paths()) or ""
        if not self.ffmpeg:
            self.ffmpeg = find_binary("ffmpeg", candidate_ffmpeg_paths()) or ""

    def save(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps(
                {
                    "ytdlp": self.ytdlp,
                    "ffmpeg": self.ffmpeg,
                    "outdir": self.outdir,
                    "maxConcurrent": self.max_concurrent,
                    "cookiesBrowser": self.cookies_browser,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def runner(self) -> list[str]:
        """Command prefix that invokes yt-dlp."""
        if self.ytdlp:
            return [self.ytdlp]
        return [sys.executable, "-m", "yt_dlp"]

    def status(self) -> dict:
        version = probe_runner(self.runner())
        label = self.ytdlp or "python -m yt_dlp"
        if version is None and self.ytdlp:
            # The configured path is broken; see if the module is importable.
            version = probe_runner([sys.executable, "-m", "yt_dlp"])
            if version:
                self.ytdlp = ""
                label = "python -m yt_dlp"
        capabilities = check_capabilities(self.runner()) if version else {
            "checked": False,
            "missing": [],
            "error": "yt-dlp not runnable",
        }
        return {
            "ytdlp": label if version else self.ytdlp,
            "ytdlpVersion": version,
            "capabilities": capabilities,
            "ffmpeg": self.ffmpeg,
            "hasFfmpeg": bool(self.ffmpeg),
            "outdir": self.outdir,
            "maxConcurrent": self.max_concurrent,
            "cookiesBrowser": self.cookies_browser,
            "cookieBrowsers": list(COOKIE_BROWSERS),
        }


CONFIG = Config()


# --------------------------------------------------------------------------
# jobs
# --------------------------------------------------------------------------


class Job:
    def __init__(self, url: str, mode: str, quality: str, playlist: bool) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.url = url
        self.mode = mode
        self.quality = quality
        self.playlist = playlist
        self.title = url
        self.status = "queued"
        self.stage = "waiting in queue"
        self.queue_pos = 0
        self.destinations: list[str] = []
        self.progress_seen = False
        self.template_seen = False
        self.last_progress = time.time()
        self.stalled = False
        self.percent = 0.0
        self.speed = 0.0
        self.eta = 0
        self.error = ""
        self.files: list[str] = []
        self.created = time.time()
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.log: list[str] = []
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        return {
            "id": self.id,
            "url": self.url,
            "mode": self.mode,
            "quality": self.quality,
            "title": self.title,
            "status": self.status,
            "stage": self.stage,
            "queuePos": self.queue_pos,
            "stalled": self.stalled,
            "percent": round(self.percent, 1),
            "speed": self.speed,
            "eta": self.eta,
            "error": self.error,
            "files": [Path(f).name for f in self.files],
            "created": self.created,
        }

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._subscribers.append(q)
        q.put(self.snapshot())
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self) -> None:
        snap = self.snapshot()
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(snap)
            except queue.Full:
                pass


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()

# Scheduler: PENDING holds jobs waiting for a slot, RUNNING holds the ids of
# jobs with a live yt-dlp process. pump() is the only place a job is started.
PENDING: collections.deque = collections.deque()
RUNNING: set[str] = set()
SCHED_LOCK = threading.Lock()


def refresh_queue_positions() -> None:
    with SCHED_LOCK:
        waiting = list(PENDING)
    for index, job in enumerate(waiting, start=1):
        if job.queue_pos != index:
            job.queue_pos = index
            job.stage = f"queued · position {index}"
            job.publish()


def pump() -> None:
    """Start as many waiting jobs as the concurrency limit allows."""
    starting: list[Job] = []
    with SCHED_LOCK:
        while PENDING and len(RUNNING) < CONFIG.max_concurrent:
            job = PENDING.popleft()
            if job.cancelled:
                continue
            RUNNING.add(job.id)
            starting.append(job)
    for job in starting:
        job.queue_pos = 0
        threading.Thread(target=run_and_release, args=(job,), daemon=True).start()
    refresh_queue_positions()


def run_and_release(job: Job) -> None:
    try:
        run_job(job)
    finally:
        with SCHED_LOCK:
            RUNNING.discard(job.id)
        pump()


def enqueue(job: Job) -> None:
    with SCHED_LOCK:
        PENDING.append(job)
    pump()


def drop_from_queue(job: Job) -> bool:
    """Pull a job out of the waiting list. True if it had not started yet."""
    with SCHED_LOCK:
        if job in PENDING:
            PENDING.remove(job)
            return True
    return False


def format_selector(mode: str, quality: str, ffmpeg: bool) -> list[str]:
    if mode == "mp3":
        return ["-f", "bestaudio/best"]

    if not ffmpeg:
        # Nothing can be merged, so only pre-muxed streams are usable. Most
        # YouTube videos no longer have any, hence NO_MUXED_HELP below.
        if quality == "best":
            return ["-f", "best[ext=mp4]/best"]
        return [
            "-f",
            f"best[ext=mp4][height<={quality}]/best[height<={quality}]/"
            f"best[ext=mp4]/best",
        ]

    height = "" if quality == "best" else f"[height<={quality}]"
    # H.264 + AAC first: AV1 and VP9 are smaller but stutter on older players
    # and hardware decoders, so they are only a fallback.
    return [
        "-f",
        f"bestvideo{height}[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo{height}[ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo{height}+bestaudio/best{height}",
    ]


def build_command(job: Job, result_file: Path) -> list[str]:
    ffmpeg = bool(CONFIG.ffmpeg)
    outdir = Path(CONFIG.outdir).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)

    cmd = CONFIG.runner() + [
        "--newline",
        "--no-colors",
        "--ignore-config",
        "--retries",
        "5",
        "--fragment-retries",
        "5",
        "--progress-template",
        PROGRESS_TEMPLATE,
        "--print-to-file",
        "after_move:filepath",
        str(result_file),
        "-o",
        str(outdir / "%(title)s [%(id)s].%(ext)s"),
    ]
    cmd += format_selector(job.mode, job.quality, ffmpeg)
    cmd += ["--yes-playlist"] if job.playlist else ["--no-playlist"]

    if CONFIG.ffmpeg:
        cmd += ["--ffmpeg-location", CONFIG.ffmpeg]

    # Age-gated and members-only videos only resolve when yt-dlp can present a
    # signed-in session, which it borrows from a local browser profile.
    if CONFIG.cookies_browser:
        cmd += ["--cookies-from-browser", CONFIG.cookies_browser]

    if job.mode == "mp3":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "192K"]
    elif ffmpeg:
        cmd += ["--merge-output-format", "mp4", "--remux-video", "mp4"]

    cmd.append(job.url)
    return cmd


DESTINATION_RE = re.compile(r"\[download\] Destination: (.+)")
NO_MUXED_HELP = (
    "This video has no ready-made audio+video stream, so the two tracks have to "
    "be merged - which needs ffmpeg. Install it (winget install Gyan.FFmpeg), "
    "then set its path in settings."
)
STAGE_HINTS = (
    ("[Merger]", "merging"),
    ("[ExtractAudio]", "converting audio"),
    ("[VideoRemuxer]", "remuxing"),
    ("[VideoConvertor]", "converting"),
    ("[download] Destination", "downloading"),
    ("[info]", "reading info"),
)


def parse_progress(job: Job, line: str) -> None:
    parts = line[len(PROGRESS_PREFIX) :].split("\t")
    if len(parts) < 5:
        return
    status, downloaded, total, speed, eta = parts[:5]

    def num(value: str) -> float:
        try:
            return float(value)
        except ValueError:
            return 0.0

    total_bytes = num(total)
    if total_bytes > 0:
        job.percent = min(100.0, num(downloaded) / total_bytes * 100.0)
    job.speed = num(speed)
    job.eta = int(num(eta))
    job.stage = "downloading" if status == "downloading" else status
    if status == "finished":
        job.percent = 100.0
    job.template_seen = True
    mark_progress(job)


# Fallback: the human-readable progress line, used only when the custom
# template above stops producing anything (a future yt-dlp renaming its
# template fields, say). Two independent readers means one can rot silently.
FALLBACK_RE = re.compile(
    r"\[download\]\s+(?P<pct>\d{1,3}(?:\.\d+)?)%"
    r"(?:.*?\sat\s+(?P<speed>[\d.]+)(?P<sunit>[KMGT]?i?B)/s)?"
    r"(?:.*?\sETA\s+(?P<eta>[\d:]+))?"
)
BYTE_UNITS = {
    "B": 1,
    "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4,
    "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4,
}


def eta_to_seconds(text: str) -> int:
    """Turn 12, 01:23 or 1:02:03 into a plain number of seconds."""
    seconds = 0
    for chunk in text.split(":"):
        try:
            seconds = seconds * 60 + int(chunk)
        except ValueError:
            return 0
    return seconds


def parse_download_line(job: Job, line: str) -> bool:
    match = FALLBACK_RE.search(line)
    if not match:
        return False

    try:
        job.percent = min(100.0, float(match.group("pct")))
    except (TypeError, ValueError):
        return False

    speed, unit = match.group("speed"), match.group("sunit")
    if speed and unit:
        try:
            job.speed = float(speed) * BYTE_UNITS.get(unit, 1)
        except ValueError:
            job.speed = 0.0
    eta = match.group("eta")
    job.eta = eta_to_seconds(eta) if eta else 0
    job.stage = "downloading (fallback reader)"
    mark_progress(job)
    return True


def mark_progress(job: Job) -> None:
    job.progress_seen = True
    job.last_progress = time.time()
    if job.stalled:
        job.stalled = False


STALL_AFTER = 12.0


def watch_for_stall(job: Job) -> None:
    """Flag a job whose process is alive but has gone quiet.

    Without this a job that produces no parsable progress just sits at 0% and
    looks frozen, which is indistinguishable from a hang.
    """
    while True:
        time.sleep(4)
        proc = job.proc
        if proc is None or proc.poll() is not None or job.status != "running":
            break
        quiet = time.time() - job.last_progress
        if quiet > STALL_AFTER and not job.stalled:
            job.stalled = True
            job.publish()
    if job.stalled:
        job.stalled = False


def cleanup_partials(job: Job) -> list[str]:
    """Delete the half-written files a killed yt-dlp leaves behind.

    Every destination the run announced is removed along with its `.part`,
    `.ytdl` and fragment siblings. Only paths inside the output folder are
    touched, and a finished file that already made it into `job.files` is kept.
    """
    outdir = Path(CONFIG.outdir).expanduser().resolve()
    keep = {Path(f).resolve() for f in job.files}
    removed: list[str] = []

    for dest in job.destinations:
        base = Path(dest)
        targets = [base, Path(f"{base}.part"), Path(f"{base}.ytdl")]
        try:
            targets += sorted(base.parent.glob(f"{base.name}.part-Frag*"))
        except OSError:
            pass
        for target in targets:
            try:
                path = target.resolve()
            except OSError:
                continue
            if path in keep or outdir not in path.parents or not path.is_file():
                continue
            try:
                path.unlink()
            except OSError:
                continue
            removed.append(path.name)
    return removed


def open_path(path: Path) -> None:
    """Hand a path to whatever app the desktop uses for it."""
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def reveal_path(path: Path) -> None:
    """Open the containing folder with the file already selected."""
    if sys.platform == "win32":
        # explorer wants the flag and the path glued into one argument, and it
        # exits non-zero even when it did the right thing.
        subprocess.Popen(["explorer", f"/select,{path}"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


def run_job(job: Job) -> None:
    # Cancel can land between pump() picking the job and this thread waking up,
    # in which case no process should ever be spawned.
    if job.cancelled:
        job.status = "cancelled"
        job.stage = "cancelled before start"
        job.publish()
        return

    result_file = APP_DIR / f".result-{job.id}.txt"
    try:
        cmd = build_command(job, result_file)
    except OSError as exc:
        job.status = "error"
        job.error = f"Cannot use output folder: {exc}"
        job.publish()
        return

    job.status = "running"
    job.publish()

    try:
        job.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=NO_WINDOW,
        )
    except OSError as exc:
        job.status = "error"
        job.error = f"Could not start yt-dlp: {exc}"
        job.publish()
        return

    threading.Thread(target=watch_for_stall, args=(job,), daemon=True).start()

    for raw in job.proc.stdout:
        line = raw.rstrip("\n")
        if not line:
            continue
        if line.startswith(PROGRESS_PREFIX):
            parse_progress(job, line)
            job.publish()
            continue

        # Only consulted while the template reader has produced nothing at all.
        if not job.template_seen and line.startswith("[download]"):
            if parse_download_line(job, line):
                job.publish()
                continue

        job.log.append(line)
        del job.log[:-200]

        match = DESTINATION_RE.search(line)
        if match:
            destination = match.group(1).strip()
            job.title = Path(destination).stem
            if destination not in job.destinations:
                job.destinations.append(destination)
        for hint, stage in STAGE_HINTS:
            if line.startswith(hint):
                job.stage = stage
                break
        if line.lower().startswith("error"):
            job.error = line
        job.publish()

    code = job.proc.wait()

    if result_file.is_file():
        text = result_file.read_text(encoding="utf-8", errors="replace")
        job.files = [line.strip() for line in text.splitlines() if line.strip()]
        result_file.unlink(missing_ok=True)

    if job.cancelled:
        job.status = "cancelled"
        removed = cleanup_partials(job)
        job.stage = (
            f"cancelled · removed {len(removed)} partial file(s)"
            if removed
            else "cancelled · nothing to clean"
        )
    elif code == 0 and job.files:
        job.status = "done"
        job.stage = "done"
        job.percent = 100.0
        job.title = Path(job.files[0]).stem
    else:
        job.status = "error"
        job.stage = "failed"
        if not job.error:
            job.error = " | ".join(job.log[-3:]) or f"yt-dlp exited with code {code}"
        if "format is not available" in job.error and not CONFIG.ffmpeg:
            job.error = NO_MUXED_HELP
    job.publish()


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ytweb"

    def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
        pass

    # -- helpers ----------------------------------------------------------

    def send_json(self, payload, code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message: str, code: int = 400) -> None:
        self.send_json({"error": message}, code)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def send_file(self, path: Path) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            self.send_error_json("File is gone", 404)
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        quoted = urllib.parse.quote(path.name)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
        self.end_headers()
        with path.open("rb") as fh:
            shutil.copyfileobj(fh, self.wfile)

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path in ("/", "/index.html"):
            self.serve_index()
            return

        if path == "/api/config":
            self.send_json(CONFIG.status())
            return

        if path == "/api/jobs":
            with JOBS_LOCK:
                jobs = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)
            self.send_json([j.snapshot() for j in jobs])
            return

        match = re.fullmatch(r"/api/jobs/([0-9a-f]+)/events", path)
        if match:
            self.stream_events(match.group(1))
            return

        match = re.fullmatch(r"/api/jobs/([0-9a-f]+)/file", path)
        if match:
            self.serve_job_file(match.group(1), parsed.query)
            return

        self.send_error_json("Not found", 404)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/config":
            data = self.read_json()
            CONFIG.ytdlp = (data.get("ytdlp") or "").strip()
            CONFIG.ffmpeg = (data.get("ffmpeg") or "").strip()
            CONFIG.outdir = (data.get("outdir") or "").strip() or default_outdir()
            CONFIG.max_concurrent = clamp_concurrency(data.get("maxConcurrent"))
            browser = (data.get("cookiesBrowser") or "").strip().lower()
            CONFIG.cookies_browser = browser if browser in COOKIE_BROWSERS else ""
            CONFIG.save()
            # A different binary needs its flags re-checked.
            CAPABILITY_CACHE.clear()
            # A raised limit should release waiting jobs straight away.
            pump()
            self.send_json(CONFIG.status())
            return

        if path == "/api/probe":
            self.probe((self.read_json().get("url") or "").strip())
            return

        if path == "/api/jobs":
            self.create_job(self.read_json())
            return

        if path == "/api/open-folder":
            self.open_folder()
            return

        match = re.fullmatch(r"/api/jobs/([0-9a-f]+)/cancel", path)
        if match:
            self.cancel_job(match.group(1))
            return

        match = re.fullmatch(r"/api/jobs/([0-9a-f]+)/(open|reveal)", path)
        if match:
            self.launch_job_file(match.group(1), parsed.query, match.group(2))
            return

        self.send_error_json("Not found", 404)

    # -- route implementations -------------------------------------------

    def serve_index(self) -> None:
        if not INDEX_PATH.is_file():
            self.send_error_json("index.html is missing", 500)
            return
        body = INDEX_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def probe(self, url: str) -> None:
        if not url:
            self.send_error_json("Paste a URL first")
            return
        cmd = CONFIG.runner() + [
            "-J",
            "--no-warnings",
            "--ignore-config",
            "--no-playlist",
            "--skip-download",
        ]
        if CONFIG.cookies_browser:
            cmd += ["--cookies-from-browser", CONFIG.cookies_browser]
        cmd.append(url)
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=90,
                creationflags=NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            self.send_error_json("yt-dlp took too long to read that link", 504)
            return
        except OSError as exc:
            self.send_error_json(f"Could not start yt-dlp: {exc}", 500)
            return

        if out.returncode != 0:
            tail = (out.stderr or out.stdout).strip().splitlines()
            self.send_error_json(tail[-1] if tail else "yt-dlp failed", 502)
            return

        try:
            info = json.loads(out.stdout)
        except ValueError:
            self.send_error_json("Could not read yt-dlp output", 502)
            return

        heights = sorted(
            {
                f.get("height")
                for f in info.get("formats") or []
                if isinstance(f.get("height"), int)
            },
            reverse=True,
        )
        self.send_json(
            {
                "title": info.get("title") or "",
                "uploader": info.get("uploader") or info.get("channel") or "",
                "duration": info.get("duration") or 0,
                "thumbnail": info.get("thumbnail") or "",
                "heights": heights,
            }
        )

    def create_job(self, data: dict) -> None:
        url = (data.get("url") or "").strip()
        mode = data.get("mode") or "mp4"
        quality = str(data.get("quality") or "1080")
        playlist = bool(data.get("playlist"))

        if not url:
            self.send_error_json("Paste a URL first")
            return
        if mode not in ("mp4", "mp3"):
            self.send_error_json("Unknown mode")
            return
        if mode == "mp3" and not CONFIG.ffmpeg:
            self.send_error_json("MP3 needs ffmpeg. Set its path in settings.")
            return

        job = Job(url, mode, quality, playlist)
        with JOBS_LOCK:
            JOBS[job.id] = job
        enqueue(job)
        self.send_json(job.snapshot(), 201)

    def cancel_job(self, job_id: str) -> None:
        job = JOBS.get(job_id)
        if not job:
            self.send_error_json("No such job", 404)
            return

        job.cancelled = True
        was_waiting = drop_from_queue(job)

        if job.proc and job.proc.poll() is None:
            # run_job finishes the cancellation once the process dies, which is
            # also where the partial files get swept up.
            job.proc.terminate()
        elif was_waiting or job.status == "queued":
            job.status = "cancelled"
            job.stage = "cancelled before start"
            job.queue_pos = 0
            job.publish()
            refresh_queue_positions()

        self.send_json({"ok": True})

    def stream_events(self, job_id: str) -> None:
        job = JOBS.get(job_id)
        if not job:
            self.send_error_json("No such job", 404)
            return

        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        q = job.subscribe()
        try:
            while True:
                try:
                    snap = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(b"data: " + json.dumps(snap).encode("utf-8") + b"\n\n")
                self.wfile.flush()
                if snap["status"] in ("done", "error", "cancelled"):
                    break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            job.unsubscribe(q)

    def locate_job_file(self, job_id: str, query: str) -> Path | None:
        """Resolve a job's finished file, or answer the request and return None.

        Shared by every route that touches a produced file, so the containment
        check lives in exactly one place.
        """
        job = JOBS.get(job_id)
        if not job or not job.files:
            self.send_error_json("This job has no finished file", 404)
            return None

        params = urllib.parse.parse_qs(query)
        try:
            index = int(params.get("i", ["0"])[0])
        except ValueError:
            index = 0
        if not 0 <= index < len(job.files):
            self.send_error_json("No such file", 404)
            return None

        path = Path(job.files[index]).resolve()
        outdir = Path(CONFIG.outdir).expanduser().resolve()
        if outdir not in path.parents:
            self.send_error_json("File is outside the output folder", 403)
            return None
        if not path.is_file():
            self.send_error_json("File is gone — moved or deleted", 404)
            return None
        return path

    def serve_job_file(self, job_id: str, query: str) -> None:
        # Only meaningful when the browser is not on the machine that holds the
        # file; locally the download already landed in the output folder.
        path = self.locate_job_file(job_id, query)
        if path is not None:
            self.send_file(path)

    def launch_job_file(self, job_id: str, query: str, action: str) -> None:
        path = self.locate_job_file(job_id, query)
        if path is None:
            return
        try:
            open_path(path) if action == "open" else reveal_path(path)
        except OSError as exc:
            self.send_error_json(f"Could not open that file: {exc}", 500)
            return
        self.send_json({"ok": True})

    def open_folder(self) -> None:
        outdir = Path(CONFIG.outdir).expanduser()
        try:
            outdir.mkdir(parents=True, exist_ok=True)
            open_path(outdir)
        except OSError as exc:
            self.send_error_json(f"Could not open folder: {exc}", 500)
            return
        self.send_json({"ok": True})


def main() -> int:
    parser = argparse.ArgumentParser(description="YouTube to MP4/MP3 web app.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    url = f"http://{args.host}:{args.port}"

    status = CONFIG.status()
    print(f"ytweb running at {url}")
    print(f"  yt-dlp : {status['ytdlp'] or 'NOT FOUND - set it in the web UI'}")
    print(f"  ffmpeg : {status['ffmpeg'] or 'not found (MP3 and >720p disabled)'}")
    print(f"  output : {status['outdir']}")
    print(f"  slots  : {status['maxConcurrent']} concurrent")
    print(f"  cookies: {status['cookiesBrowser'] or 'off (age-gated videos will fail)'}")

    caps = status["capabilities"]
    if caps.get("missing"):
        print(f"  WARNING: this yt-dlp lacks {', '.join(caps['missing'])}")
        print("           progress or merging may misbehave — see the web UI.")
    elif caps.get("checked"):
        print("  probe  : all required yt-dlp flags present")
    print("Press Ctrl+C to stop.")

    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
