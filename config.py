"""
StreamCast configuration.

Everything is driven by environment variables so the same code runs on your
laptop (localhost) and on a VPS without edits. Copy .env.example to .env and
adjust, or export the variables in your shell / systemd unit.
"""
import os
from pathlib import Path


def _env(name, default=None):
    val = os.environ.get(name)
    return val if val not in (None, "") else default


# --- Paths ------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = Path(_env("STREAMCAST_STORAGE", str(BASE_DIR / "storage")))
UPLOAD_DIR = STORAGE_DIR / "uploads"      # raw user uploads
ENCODED_DIR = STORAGE_DIR / "encoded"     # normalized, ready-to-stream files
DB_PATH = Path(_env("STREAMCAST_DB", str(STORAGE_DIR / "streamcast.db")))

# --- Auth (single owner, no registration) -----------------------------------
# Login page checks this single password. Change it before exposing the app!
OWNER_PASSWORD = _env("STREAMCAST_PASSWORD", "changeme")
SECRET_KEY = _env("STREAMCAST_SECRET", "dev-secret-change-me")

# Set STREAMCAST_REQUIRE_LOGIN=1 to turn the password back on. Off by default,
# so the panel opens straight to the dashboard with no login screen.
# WARNING: leave this OFF only on a private/local machine. Anyone who can reach
# the URL gets full control (and your stream keys) when login is disabled.
REQUIRE_LOGIN = _env("STREAMCAST_REQUIRE_LOGIN", "0") == "1"

# --- Streaming --------------------------------------------------------------
# Default YouTube ingest endpoint. Users only paste their stream KEY in the UI.
RTMP_BASE = _env("STREAMCAST_RTMP_BASE", "rtmp://a.rtmp.youtube.com/live2")

# Target format all uploads are normalized to (keeps the 24/7 stream seamless).
TARGET_WIDTH = int(_env("STREAMCAST_WIDTH", "1920"))
TARGET_HEIGHT = int(_env("STREAMCAST_HEIGHT", "1080"))
TARGET_FPS = int(_env("STREAMCAST_FPS", "30"))
VIDEO_BITRATE = _env("STREAMCAST_VBITRATE", "4500k")
AUDIO_BITRATE = _env("STREAMCAST_ABITRATE", "128k")
GOP_SECONDS = 2  # keyframe interval; YouTube recommends 2s

# Uploads at or above this fps are rejected (matches the "blocks 60fps!" rule).
MAX_FPS = int(_env("STREAMCAST_MAX_FPS", "60"))

MAX_UPLOAD_MB = int(_env("STREAMCAST_MAX_UPLOAD_MB", "8192"))  # 8 GB
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v"}

# When a stream is live, queue edits are picked up at the end of the current
# playback block. Short queues are repeated inside one block up to this many
# seconds so we don't reconnect to YouTube too often. Lower = faster pickup of
# queue changes but more frequent reconnects.
RELOAD_BLOCK_SECONDS = int(_env("STREAMCAST_RELOAD_BLOCK_SECONDS", "900"))

# ffmpeg / ffprobe binaries (override if not on PATH)
FFMPEG = _env("STREAMCAST_FFMPEG", "ffmpeg")
FFPROBE = _env("STREAMCAST_FFPROBE", "ffprobe")


def ensure_dirs():
    for d in (STORAGE_DIR, UPLOAD_DIR, ENCODED_DIR):
        d.mkdir(parents=True, exist_ok=True)
