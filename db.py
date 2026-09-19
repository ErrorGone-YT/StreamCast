"""SQLite data layer for StreamCast.

Two tables:
  streams  — one broadcast target (name, RTMP key, YouTube URL, schedule, state)
  videos   — files queued under a stream, with encode status and play order
"""
import sqlite3
import time
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    rtmp_key      TEXT DEFAULT '',
    youtube_url   TEXT DEFAULT '',
    loop_queue    INTEGER DEFAULT 1,      -- 1 = repeat the queue forever (24/7)
    is_live       INTEGER DEFAULT 0,
    pid           INTEGER,                -- ffmpeg process id when live
    scheduled_at  REAL,                   -- unix ts for a planned start, or NULL
    created_at    REAL NOT NULL,
    shuffle       INTEGER DEFAULT 0,      -- 1 = random play enabled
    stream_type   TEXT DEFAULT 'video',   -- 'video' | 'music'
    loop_video_id INTEGER,                -- music: video row looped as the background
    mix_video_audio INTEGER DEFAULT 0,    -- music: mix the background video's own sound
    video_volume  REAL DEFAULT 0.5        -- music: background video sound level (0..1)
);

CREATE TABLE IF NOT EXISTS videos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id     INTEGER NOT NULL,
    orig_name     TEXT NOT NULL,
    stored_name   TEXT NOT NULL,          -- filename inside uploads/
    encoded_name  TEXT,                   -- filename inside encoded/ once ready
    status        TEXT DEFAULT 'waiting_encode',  -- waiting_encode|encoding|completed|error
    error_msg     TEXT DEFAULT '',
    duration      REAL DEFAULT 0,
    progress      REAL DEFAULT 0,
    encode_pid    INTEGER,
    position      INTEGER DEFAULT 0,      -- play order within the stream
    kind          TEXT DEFAULT 'video',   -- 'video' | 'audio' (music stream tracks)
    created_at    REAL NOT NULL,
    FOREIGN KEY (stream_id) REFERENCES streams(id) ON DELETE CASCADE
);
"""

def _connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

@contextmanager
def get_db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def migrate_db():
    """Ensures all necessary columns exist in the database."""
    with get_db() as db:
        try:
            db.execute("ALTER TABLE videos ADD COLUMN progress REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE videos ADD COLUMN encode_pid INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE streams ADD COLUMN shuffle INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE streams ADD COLUMN stream_type TEXT DEFAULT 'video'")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE streams ADD COLUMN loop_video_id INTEGER")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE streams ADD COLUMN mix_video_audio INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE streams ADD COLUMN video_volume REAL DEFAULT 0.5")
        except sqlite3.OperationalError:
            pass
        try:
            db.execute("ALTER TABLE videos ADD COLUMN kind TEXT DEFAULT 'video'")
        except sqlite3.OperationalError:
            pass

def init_db():
    config.ensure_dirs()
    with get_db() as db:
        db.executescript(SCHEMA)
        migrate_db()

# --- Streams ----------------------------------------------------------------
def create_stream(name, rtmp_key="", youtube_url="", stream_type="video"):
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO streams (name, rtmp_key, youtube_url, stream_type, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, rtmp_key, youtube_url, stream_type, time.time()),
        )
        return cur.lastrowid

def get_stream(stream_id):
    with get_db() as db:
        return db.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()

def list_streams():
    with get_db() as db:
        rows = db.execute("SELECT * FROM streams ORDER BY created_at DESC").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["video_count"] = db.execute(
                "SELECT COUNT(*) c FROM videos WHERE stream_id = ?", (r["id"],)
            ).fetchone()["c"]
            result.append(d)
        return result

def update_stream(stream_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_db() as db:
        db.execute(
            f"UPDATE streams SET {cols} WHERE id = ?",
            (*fields.values(), stream_id),
        )

def delete_stream(stream_id):
    with get_db() as db:
        db.execute("DELETE FROM streams WHERE id = ?", (stream_id,))

def set_live(stream_id, is_live, pid=None):
    update_stream(stream_id, is_live=1 if is_live else 0, pid=pid)

# --- Videos -----------------------------------------------------------------
def add_video(stream_id, orig_name, stored_name, kind="video"):
    with get_db() as db:
        pos = db.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 p FROM videos WHERE stream_id = ?",
            (stream_id,),
        ).fetchone()["p"]
        cur = db.execute(
            "INSERT INTO videos (stream_id, orig_name, stored_name, kind, position, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (stream_id, orig_name, stored_name, kind, pos, time.time()),
        )
        return cur.lastrowid

def get_video(video_id):
    with get_db() as db:
        return db.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()

def list_videos(stream_id):
    with get_db() as db:
        return db.execute(
            "SELECT * FROM videos WHERE stream_id = ? ORDER BY position, id",
            (stream_id,),
        ).fetchall()

def list_ready_videos(stream_id, kind=None):
    """Completed files in play order — this is the actual 24/7 playlist."""
    query = ("SELECT * FROM videos WHERE stream_id = ? AND status = 'completed'")
    args = [stream_id]
    if kind:
        query += " AND kind = ?"
        args.append(kind)
    query += " ORDER BY position, id"
    with get_db() as db:
        return db.execute(query, args).fetchall()

def update_video(video_id, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with get_db() as db:
        db.execute(
            f"UPDATE videos SET {cols} WHERE id = ?",
            (*fields.values(), video_id),
        )

def delete_video(video_id):
    with get_db() as db:
        db.execute("DELETE FROM videos WHERE id = ?", (video_id,))

def reorder_videos(stream_id, ordered_ids):
    with get_db() as db:
        for pos, vid in enumerate(ordered_ids):
            db.execute(
                "UPDATE videos SET position = ? WHERE id = ? AND stream_id = ?",
                (pos, vid, stream_id),
            )

def next_encode_job():
    """Oldest video still waiting to be normalized."""
    with get_db() as db:
        return db.execute(
            "SELECT * FROM videos WHERE status = 'waiting_encode' "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()