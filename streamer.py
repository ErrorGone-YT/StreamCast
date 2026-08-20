"""24/7 streaming engine.

For each live stream we run one ffmpeg process that reads a concat playlist of
the already-normalized clips and pushes it to YouTube via RTMP. Because every
clip shares the same codec/resolution/fps/GOP, we stream with `-c copy` (no
re-encode) — cheap on CPU and seamless between clips.

Hot reload: instead of looping one fixed playlist forever, the supervisor plays
the queue in "blocks" (a playlist that repeats the current queue up to
RELOAD_BLOCK_SECONDS). When a block ends, it rebuilds the playlist from the
*current* database state and starts the next block. So adding videos or
reordering the queue while live is picked up automatically at the next block —
no manual stop/start. `apply_now()` forces an immediate block restart.

A watchdog restarts ffmpeg if it dies (network blip, YouTube reset), so the
channel self-heals.
"""
import subprocess
import threading
import time
import uuid

import config
import db


class _Runner:
    """Owns one ffmpeg process + watchdog for a single stream."""

    def __init__(self, stream_id):
        self.stream_id = stream_id
        self.proc = None
        self.playlist_path = None
        self._stop = threading.Event()
        self._reload = threading.Event()   # set to force an immediate block restart
        self._thread = None
        self.last_error = ""

        # now-playing tracking (computed from wall-clock, since -re plays realtime)
        self._block_started = 0.0          # monotonic time the current block began
        self._block_videos = []            # list of {id, duration} in play order
        self._block_total = 0.0            # summed duration of one queue pass

    # -- playlist ------------------------------------------------------------
    def _build_playlist(self):
        """Rebuild from current DB state. Returns (path, videos, one_pass_dur)."""
        videos = db.list_ready_videos(self.stream_id)
        if not videos:
            return None, [], 0.0

        stream = db.get_stream(self.stream_id)
        loop = bool(stream and stream["loop_queue"])

        one_pass = sum((v["duration"] or 0) for v in videos)
        # Repeat the queue enough times to fill a block, so short queues don't
        # force a YouTube reconnect every few seconds. If looping is off, play
        # the queue exactly once.
        repeats = 1
        if loop and one_pass > 0:
            repeats = max(1, int(config.RELOAD_BLOCK_SECONDS // one_pass) or 1)

        lines = ["ffconcat version 1.0"]
        for _ in range(repeats):
            for v in videos:
                path = (config.ENCODED_DIR / v["encoded_name"]).resolve()
                safe = str(path).replace("\\", "/").replace("'", "'\\''")
                lines.append(f"file '{safe}'")

        pl = config.STORAGE_DIR / f"playlist_{self.stream_id}_{uuid.uuid4().hex}.txt"
        pl.write_text("\n".join(lines) + "\n", encoding="utf-8")
        meta = [{"id": v["id"], "duration": v["duration"] or 0} for v in videos]
        return pl, meta, one_pass

    def _cleanup_playlist(self):
        if not self.playlist_path:
            return
        for _ in range(10):
            try:
                self.playlist_path.unlink(missing_ok=True)
                break
            except (PermissionError, OSError):
                time.sleep(0.3)
        self.playlist_path = None

    # -- ffmpeg command ------------------------------------------------------
    def _ffmpeg_cmd(self, stream):
        rtmp_url = f"{config.RTMP_BASE}/{stream['rtmp_key']}"
        return [
            config.FFMPEG, "-hide_banner", "-loglevel", "warning",
            "-re",                      # read at native rate = real-time push
            "-f", "concat", "-safe", "0",
            "-i", str(self.playlist_path),
            "-c", "copy",               # no re-encode: clips are pre-normalized
            "-f", "flv",
            "-flvflags", "no_duration_filesize",
            rtmp_url,
        ]

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        stream = db.get_stream(self.stream_id)
        if not stream or not stream["rtmp_key"]:
            self.last_error = "No RTMP key set"
            return False
        # Validate there is at least one ready video before spawning a thread.
        pl, meta, one_pass = self._build_playlist()
        if pl is None:
            self.last_error = "No ready videos in queue"
            return False
        self.playlist_path, self._block_videos, self._block_total = pl, meta, one_pass
        self._stop.clear()
        self._reload.clear()
        self._thread = threading.Thread(target=self._supervise, daemon=True)
        self._thread.start()
        return True

    def _spawn(self, stream):
        self.proc = subprocess.Popen(
            self._ffmpeg_cmd(stream),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        self._block_started = time.monotonic()
        db.set_live(self.stream_id, True, pid=self.proc.pid)

    def _supervise(self):
        """Play the queue block by block, rebuilding between blocks."""
        backoff = 2
        while not self._stop.is_set():
            stream = db.get_stream(self.stream_id)
            if not stream:
                break

            # (Re)build the playlist from current queue state for this block.
            if self.playlist_path is None:
                pl, meta, one_pass = self._build_playlist()
                if pl is None:
                    # queue emptied while live — wait and retry, don't die
                    self.last_error = "Queue is empty"
                    time.sleep(5)
                    continue
                self.playlist_path, self._block_videos, self._block_total = pl, meta, one_pass

            self._spawn(stream)

            # Wait for ffmpeg to finish this block, but wake early on reload.
            killed_by_us = False
            while True:
                try:
                    self.proc.wait(timeout=1)
                    break  # ffmpeg exited (block done or crashed)
                except subprocess.TimeoutExpired:
                    if self._stop.is_set() or self._reload.is_set():
                        killed_by_us = True
                        self.proc.terminate()
                        try:
                            self.proc.wait(timeout=8)
                        except subprocess.TimeoutExpired:
                            self.proc.kill()
                        break

            rc = self.proc.returncode
            block_was_reload = self._reload.is_set()
            self._reload.clear()
            self._cleanup_playlist()  # force rebuild next iteration

            if self._stop.is_set():
                break

            if block_was_reload:
                backoff = 2
                continue  # user changed the queue -> straight into a fresh block

            # ffmpeg exited on its own. A clean exit (rc 0) is a normal block/loop
            # boundary; a non-zero code means it faulted (network, bad key, etc).
            if not killed_by_us and rc not in (0, None):
                err = self.proc.stderr.read() if self.proc.stderr else ""
                self.last_error = (err or "").strip()[-300:]
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
            else:
                self.last_error = ""
                backoff = 2
                # Looping disabled + queue finished cleanly -> end the broadcast.
                stream = db.get_stream(self.stream_id)
                if stream and not stream["loop_queue"]:
                    break

        self._cleanup_playlist()
        db.set_live(self.stream_id, False, pid=None)

    def apply_now(self):
        """Force the current block to end and rebuild from the latest queue."""
        self._reload.set()

    def stop(self):
        self._stop.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        self._cleanup_playlist()
        db.set_live(self.stream_id, False, pid=None)

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def now_playing(self):
        """Which video is on air right now, and progress through it.

        Position is derived from elapsed wall-clock (ffmpeg -re plays in real
        time) modulo one queue pass — no ffmpeg log parsing needed.
        """
        if not self._block_videos or self._block_total <= 0:
            return None
        elapsed = (time.monotonic() - self._block_started) % self._block_total
        acc = 0.0
        for v in self._block_videos:
            dur = v["duration"] or 0
            if elapsed < acc + dur:
                return {
                    "video_id": v["id"],
                    "offset": round(elapsed - acc, 1),
                    "duration": round(dur, 1),
                }
            acc += dur
        return None


class StreamManager:
    """Registry of running streams, keyed by stream id."""

    def __init__(self):
        self._runners = {}
        self._lock = threading.Lock()
        self._sched_thread = None
        self._sched_stop = threading.Event()

    def start_stream(self, stream_id):
        with self._lock:
            existing = self._runners.get(stream_id)
            if existing and existing.is_running():
                return True, "Already live"
            runner = _Runner(stream_id)
            ok = runner.start()
            if ok:
                self._runners[stream_id] = runner
                return True, "Started"
            return False, runner.last_error or "Failed to start"

    def stop_stream(self, stream_id):
        with self._lock:
            runner = self._runners.pop(stream_id, None)
        if runner:
            runner.stop()
            return True, "Stopped"
        # Not tracked in memory (e.g. after app restart) — just clear the flag.
        db.set_live(stream_id, False, pid=None)
        return True, "Stopped"

    def apply_now(self, stream_id):
        runner = self._runners.get(stream_id)
        if runner and runner.is_running():
            runner.apply_now()
            return True, "Applying new queue"
        return False, "Stream is not live"

    def is_live(self, stream_id):
        runner = self._runners.get(stream_id)
        return bool(runner and runner.is_running())

    def status(self, stream_id):
        runner = self._runners.get(stream_id)
        if runner and runner.is_running():
            return {
                "live": True,
                "error": runner.last_error,
                "now_playing": runner.now_playing(),
            }
        return {
            "live": False,
            "error": runner.last_error if runner else "",
            "now_playing": None,
        }

    # -- scheduler -----------------------------------------------------------
    def start_scheduler(self):
        if self._sched_thread and self._sched_thread.is_alive():
            return
        self._sched_stop.clear()
        self._sched_thread = threading.Thread(target=self._sched_loop, daemon=True)
        self._sched_thread.start()

    def _sched_loop(self):
        while not self._sched_stop.is_set():
            now = time.time()
            for s in db.list_streams():
                sched = s.get("scheduled_at")
                if sched and sched <= now and not self.is_live(s["id"]):
                    db.update_stream(s["id"], scheduled_at=None)
                    self.start_stream(s["id"])
            time.sleep(10)

    def shutdown(self):
        self._sched_stop.set()
        for sid in list(self._runners.keys()):
            self.stop_stream(sid)


manager = StreamManager()
