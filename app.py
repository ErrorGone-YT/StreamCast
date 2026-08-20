"""StreamCast — single-owner 24/7 YouTube streaming panel."""
import time
import uuid
from functools import wraps
from pathlib import Path

from flask import (
    Flask, abort, flash, jsonify, redirect, render_template,
    request, session, url_for,
)
from werkzeug.utils import secure_filename

import config
import db
from encoder import worker as encoder_worker
from streamer import manager

app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024
# Reload templates on change so UI edits show up without a restart.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True


# --- Auth -------------------------------------------------------------------
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        # Password disabled -> open access (no login screen).
        if not config.REQUIRE_LOGIN:
            return view(*args, **kwargs)
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if not config.REQUIRE_LOGIN:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        if request.form.get("password") == config.OWNER_PASSWORD:
            session["authed"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Wrong password", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --- Dashboard --------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    streams = db.list_streams()
    for s in streams:
        s["live"] = manager.is_live(s["id"])
    return render_template("dashboard.html", streams=streams)


# --- Stream CRUD ------------------------------------------------------------
@app.route("/stream/create", methods=["GET", "POST"])
@login_required
def stream_create():
    if request.method == "POST":
        name = request.form.get("name", "").strip() or "Untitled stream"
        sid = db.create_stream(
            name,
            rtmp_key=request.form.get("rtmp_key", "").strip(),
            youtube_url=request.form.get("youtube_url", "").strip(),
        )
        return redirect(url_for("stream_detail", stream_id=sid))
    return render_template("stream_edit.html", stream=None)


@app.route("/stream/<int:stream_id>")
@login_required
def stream_detail(stream_id):
    stream = db.get_stream(stream_id)
    if not stream:
        abort(404)
    videos = db.list_videos(stream_id)
    status = manager.status(stream_id)
    return render_template(
        "stream.html", stream=stream, videos=videos, status=status,
    )


@app.route("/stream/edit/<int:stream_id>", methods=["GET", "POST"])
@login_required
def stream_edit(stream_id):
    stream = db.get_stream(stream_id)
    if not stream:
        abort(404)
    if request.method == "POST":
        db.update_stream(
            stream_id,
            name=request.form.get("name", "").strip() or stream["name"],
            rtmp_key=request.form.get("rtmp_key", "").strip(),
            youtube_url=request.form.get("youtube_url", "").strip(),
            loop_queue=1 if request.form.get("loop_queue") else 0,
        )
        flash("Saved", "ok")
        return redirect(url_for("stream_detail", stream_id=stream_id))
    return render_template("stream_edit.html", stream=stream)


@app.route("/stream/delete/<int:stream_id>", methods=["POST"])
@login_required
def stream_delete(stream_id):
    manager.stop_stream(stream_id)
    for v in db.list_videos(stream_id):
        _remove_video_files(v)
    db.delete_stream(stream_id)
    flash("Stream deleted", "ok")
    return redirect(url_for("dashboard"))


# --- Start / stop / schedule ------------------------------------------------
@app.route("/stream/start/<int:stream_id>", methods=["POST"])
@login_required
def stream_start(stream_id):
    ok, msg = manager.start_stream(stream_id)
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("stream_detail", stream_id=stream_id))


@app.route("/stream/stop/<int:stream_id>", methods=["POST"])
@login_required
def stream_stop(stream_id):
    ok, msg = manager.stop_stream(stream_id)
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("stream_detail", stream_id=stream_id))


@app.route("/stream/apply/<int:stream_id>", methods=["POST"])
@login_required
def stream_apply(stream_id):
    ok, msg = manager.apply_now(stream_id)
    flash(msg, "ok" if ok else "error")
    return redirect(url_for("stream_detail", stream_id=stream_id))


@app.route("/stream/schedule/<int:stream_id>", methods=["POST"])
@login_required
def stream_schedule(stream_id):
    when = request.form.get("scheduled_at", "").strip()
    if when:
        # HTML datetime-local -> unix ts (server local time).
        try:
            ts = time.mktime(time.strptime(when, "%Y-%m-%dT%H:%M"))
            db.update_stream(stream_id, scheduled_at=ts)
            flash("Scheduled", "ok")
        except ValueError:
            flash("Bad date", "error")
    else:
        db.update_stream(stream_id, scheduled_at=None)
        flash("Schedule cleared", "ok")
    return redirect(url_for("stream_detail", stream_id=stream_id))


# --- Upload / delete video --------------------------------------------------
def _remove_video_files(video):
    if video["stored_name"]:
        (config.UPLOAD_DIR / video["stored_name"]).unlink(missing_ok=True)
    if video["encoded_name"]:
        (config.ENCODED_DIR / video["encoded_name"]).unlink(missing_ok=True)


@app.route("/upload/<int:stream_id>", methods=["POST"])
@login_required
def upload(stream_id):
    if not db.get_stream(stream_id):
        abort(404)
    files = request.files.getlist("video")
    added = 0
    for f in files:
        if not f or not f.filename:
            continue
        ext = Path(f.filename).suffix.lower()
        if ext not in config.ALLOWED_EXT:
            flash(f"{f.filename}: unsupported type", "error")
            continue
        stored = f"{uuid.uuid4().hex}{ext}"
        f.save(config.UPLOAD_DIR / stored)
        db.add_video(stream_id, secure_filename(f.filename), stored)
        added += 1
    if added:
        flash(f"{added} video(s) queued for encoding", "ok")
    return redirect(url_for("stream_detail", stream_id=stream_id))


@app.route("/video/delete/<int:video_id>", methods=["POST"])
@login_required
def video_delete(video_id):
    video = db.get_video(video_id)
    if not video:
        abort(404)
    _remove_video_files(video)
    db.delete_video(video_id)
    return redirect(url_for("stream_detail", stream_id=video["stream_id"]))


# --- JSON API (live polling) ------------------------------------------------
@app.route("/api/video_statuses/<int:stream_id>")
@login_required
def api_video_statuses(stream_id):
    videos = db.list_videos(stream_id)
    return jsonify([
        {
            "id": v["id"], "status": v["status"], "error": v["error_msg"],
            "duration": round(v["duration"] or 0, 1),
        }
        for v in videos
    ])


@app.route("/api/stream_status/<int:stream_id>")
@login_required
def api_stream_status(stream_id):
    return jsonify(manager.status(stream_id))


@app.route("/api/reorder", methods=["POST"])
@login_required
def api_reorder():
    data = request.get_json(silent=True) or {}
    stream_id = data.get("stream_id")
    order = data.get("order", [])
    if stream_id is None:
        return jsonify({"ok": False}), 400
    db.reorder_videos(int(stream_id), [int(i) for i in order])
    return jsonify({"ok": True})


# --- Boot -------------------------------------------------------------------
def bootstrap():
    db.init_db()
    encoder_worker.start()
    manager.start_scheduler()


bootstrap()


if __name__ == "__main__":
    # Dev server. Use gunicorn in production (see README).
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True)
