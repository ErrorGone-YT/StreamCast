"""Background encoder.

Uploaded files come in every shape (different codecs, resolutions, fps). To play
them back-to-back in a seamless 24/7 stream we normalize each one to a single
target format up front. Then the streamer can concat + copy them with no
real-time transcoding, which keeps CPU low and avoids stutter at cut points.

A single worker thread pulls the oldest 'waiting_encode' video and processes it.
Status transitions: waiting_encode -> encoding -> completed | error.
"""
import json
import subprocess
import threading
import time
import uuid

import config
import db


def ffprobe_info(path):
    """Return (duration_seconds, fps) for a media file."""
    cmd = [
        config.FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate:format=duration",
        "-of", "json", str(path),
    ]
    out = subprocess.check_output(cmd, text=True)
    data = json.loads(out)
    duration = float(data.get("format", {}).get("duration", 0) or 0)
    fps = 0.0
    streams = data.get("streams", [])
    if streams:
        rate = streams[0].get("avg_frame_rate", "0/0")
        try:
            num, den = rate.split("/")
            fps = float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0
    return duration, fps


def _transcode(video, cmd, out_path, total_duration):
    """Run an ffmpeg normalize job, streaming progress into the DB."""
    import re

    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, text=True, bufsize=1)
    db.update_video(video["id"], encode_pid=proc.pid)

    # Regex for 'time=00:00:00.00'
    time_pattern = re.compile(r"time=(\d{2}):(\d{2}):(\d{2})\.(\d{2})")

    try:
        while True:
            line = proc.stderr.readline()
            if not line:
                break

            match = time_pattern.search(line)
            if match and total_duration > 0:
                h, m, s, ms = map(int, match.groups())
                current_time = h * 3600 + m * 60 + s + ms / 100
                progress = (current_time / total_duration) * 100
                db.update_video(video["id"], progress=min(100.0, progress))
    except Exception:
        # Log error or just let it fail
        pass

    proc.wait()

    if proc.returncode != 0:
        out_path.unlink(missing_ok=True)
        db.update_video(video["id"], status="error", error_msg=f"encode failed with code {proc.returncode}")
        return

    try:
        duration, _ = ffprobe_info(out_path)
    except Exception:
        duration = 0

    db.update_video(
        video["id"], status="completed", encoded_name=out_path.name,
        duration=duration, error_msg="", progress=100.0,
    )


def _encode_one(video):
    src = config.UPLOAD_DIR / video["stored_name"]
    if not src.exists():
        db.update_video(video["id"], status="error", error_msg="Source file missing")
        return

    kind = video["kind"] if "kind" in video.keys() else "video"

    # Enforce the 60fps block rule (video only) before spending CPU on encoding.
    try:
        src_duration, src_fps = ffprobe_info(src)
    except Exception as e:  # noqa: BLE001
        db.update_video(video["id"], status="error", error_msg=f"probe failed: {e}")
        return
    if kind != "audio" and src_fps >= config.MAX_FPS:
        db.update_video(
            video["id"], status="error",
            error_msg=f"{src_fps:.0f}fps rejected (max {config.MAX_FPS - 1}fps)",
        )
        return

    db.update_video(video["id"], status="encoding", error_msg="", progress=0.0)

    if kind == "audio":
        # Normalize to CBR MP3 so the concat playlist plays tracks seamlessly.
        out_path = config.ENCODED_DIR / f"{uuid.uuid4().hex}.mp3"
        cmd = [
            config.FFMPEG, "-y", "-i", str(src),
            "-vn",
            "-c:a", "libmp3lame", "-b:a", config.AUDIO_BITRATE,
            "-ar", "44100", "-ac", "2", "-write_xing", "0",
            str(out_path),
        ]
    else:
        out_path = config.ENCODED_DIR / f"{uuid.uuid4().hex}.mp4"

        vf = (
            f"scale={config.TARGET_WIDTH}:{config.TARGET_HEIGHT}:"
            f"force_original_aspect_ratio=decrease,"
            f"pad={config.TARGET_WIDTH}:{config.TARGET_HEIGHT}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={config.TARGET_FPS},format=yuv420p"
        )
        gop = config.TARGET_FPS * config.GOP_SECONDS
        cmd = [
            config.FFMPEG, "-y", "-i", str(src),
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
            "-b:v", config.VIDEO_BITRATE, "-maxrate", config.VIDEO_BITRATE,
            "-bufsize", config.VIDEO_BITRATE,
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", config.AUDIO_BITRATE, "-ar", "48000", "-ac", "2",
            "-movflags", "+faststart",
            str(out_path),
        ]

    _transcode(video, cmd, out_path, src_duration)


class EncoderWorker:
    """One background thread that drains the encode queue."""

    def __init__(self, poll_interval=2.0):
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            job = db.next_encode_job()
            if job is None:
                time.sleep(self.poll_interval)
                continue
            try:
                _encode_one(job)
            except Exception as e:  # noqa: BLE001
                db.update_video(job["id"], status="error", error_msg=str(e))


worker = EncoderWorker()