"""Local web dashboard + JSON API (http://127.0.0.1:5055)."""
import io
import os
import subprocess
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file
from werkzeug.utils import secure_filename

from . import config, db, instagram, media, worker, youtube

app = Flask(__name__, static_folder=None)
_accounts = {"youtube": None, "instagram": None}


def _err(msg, code=400):
    return jsonify({"error": msg}), code


def _refresh_accounts():
    for key, fn in (("youtube", youtube.channel_name), ("instagram", instagram.account_name)):
        try:
            _accounts[key] = fn()
        except Exception as e:
            _accounts[key] = None
            print(f"[{key}] {e}")


@app.get("/")
def index():
    return send_file(Path(__file__).parent / "static" / "index.html")


@app.get("/api/state")
def state():
    s = config.load()
    s["ig_token"] = "••••" if s["ig_token"] else ""
    volumes = []
    for v in sorted(Path("/Volumes").iterdir()) if Path("/Volumes").exists() else []:
        if v.is_dir() and not v.is_symlink():
            volumes.append(str(v))
    return jsonify({
        "sources": db.q("SELECT * FROM sources ORDER BY id"),
        "videos": db.q("SELECT v.*, (SELECT COUNT(*) FROM clips c WHERE c.video_id = v.id) AS clips "
                       "FROM videos v ORDER BY v.id"),
        "clips": db.q("SELECT c.*, v.path AS video_path FROM clips c JOIN videos v ON v.id = c.video_id "
                      "ORDER BY c.id"),
        "settings": s,
        "accounts": _accounts,
        "youtube_client_ready": youtube.CLIENT_SECRET.exists(),
        "volumes": volumes,
        "activity": worker.activity,
        "queue": {"process": worker.process_q.qsize(), "post": worker.post_q.qsize()},
    })


# ---------- sources ----------

@app.post("/api/sources")
def add_source():
    path = os.path.expanduser((request.json or {}).get("path", "").strip().strip("'\""))
    if not path or not Path(path).is_dir():
        return _err("That folder doesn't exist. Is the drive plugged in?")
    path = str(Path(path).resolve())
    existing = db.one("SELECT id FROM sources WHERE path = ?", (path,))
    sid = existing["id"] if existing else db.x(
        "INSERT INTO sources (path, added) VALUES (?, ?)", (path, db.now()))
    worker.process_q.put(("scan", sid))
    return jsonify({"id": sid})


@app.post("/api/upload")
def upload_videos():
    files = request.files.getlist("files")
    if not files:
        return _err("Choose at least one video file")
    config.UPLOADS.mkdir(exist_ok=True)
    folder = str(config.UPLOADS.resolve())
    existing = db.one("SELECT id FROM sources WHERE path = ?", (folder,))
    sid = existing["id"] if existing else db.x(
        "INSERT INTO sources (path, added) VALUES (?, ?)", (folder, db.now()))
    added, skipped = [], []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in config.VIDEO_EXTS:
            skipped.append(f.filename)
            continue
        name = (secure_filename(Path(f.filename).stem) or "video") + ext
        dest = config.UPLOADS / name
        n = 1
        while dest.exists():  # never overwrite an earlier upload
            dest = config.UPLOADS / f"{Path(name).stem}_{n}{Path(name).suffix}"
            n += 1
        f.save(dest)
        vid = db.x("INSERT INTO videos (source_id, path, size, added) VALUES (?, ?, ?, ?)",
                   (sid, str(dest.resolve()), dest.stat().st_size, db.now()))
        worker.process_q.put(("video", vid))
        added.append(dest.name)
    if not added:
        return _err("Those don't look like video files")
    return jsonify({"added": added, "skipped": skipped})


@app.post("/api/sources/<int:sid>/scan")
def rescan(sid):
    worker.process_q.put(("scan", sid))
    return jsonify({"ok": True})


@app.delete("/api/sources/<int:sid>")
def remove_source(sid):
    # Only forgets the folder; never deletes anything on the drive.
    db.x("DELETE FROM sources WHERE id = ?", (sid,))
    return jsonify({"ok": True})


def _video_or_404(vid):
    video = db.one("SELECT * FROM videos WHERE id = ?", (vid,))
    if not video:
        abort(404)
    return video


_scoring = set()


@app.get("/api/videos/<int:vid>/score")
def video_score(vid):
    """Per-second intensity for the graph. Older videos get theirs built on first request."""
    video = _video_or_404(vid)
    score = worker.load_score(vid)
    if score is None and video["status"] == "done" and vid not in _scoring:
        _scoring.add(vid)
        worker.process_q.put(("score", vid))
    return jsonify({"score": score, "pending": score is None})


@app.get("/media/video/<int:vid>")
def source_video(vid):
    video = _video_or_404(vid)
    if not Path(video["path"]).exists():
        abort(404)
    return send_file(video["path"], conditional=True)


@app.get("/media/frame/<int:vid>")
def frame(vid):
    """One still from the source video, for the framing preview (w=1920 for thumbnails)."""
    video = _video_or_404(vid)
    try:
        t = max(0.0, float(request.args.get("t", 0)))
        w = min(max(int(request.args.get("w", 960)), 160), 1920)
    except ValueError:
        t, w = 0.0, 960
    out = subprocess.run([media.ffmpeg(), "-v", "error", "-nostdin", "-ss", f"{t:.2f}", "-i", video["path"],
                          "-frames:v", "1", "-vf", f"scale='min({w},iw)':-2", "-q:v", "3" if w > 960 else "4",
                          "-f", "image2", "-c:v", "mjpeg", "-"], capture_output=True)
    if out.returncode != 0 or not out.stdout:
        abort(404)
    return send_file(io.BytesIO(out.stdout), mimetype="image/jpeg", max_age=3600)


@app.post("/api/videos/<int:vid>/clips")
def add_clip(vid):
    """A clip you picked yourself on the graph."""
    video = _video_or_404(vid)
    body = request.json or {}
    try:
        start = max(0.0, float(body["start"]))
        length = min(max(3.0, float(body.get("length", 30))), 180.0)
        crop_x = min(max(float(body.get("crop_x", 0.5)), 0.0), 1.0)
    except (KeyError, TypeError, ValueError):
        return _err("Start and length must be numbers (seconds)")
    if video["duration"]:
        length = min(length, video["duration"])
        start = min(start, max(0.0, video["duration"] - length))
    mode = body.get("vertical_mode")
    if mode not in (None, "blur", "crop"):
        return _err("Unknown vertical style")
    score = worker.load_score(vid) or []
    peak = max(score[int(start):int(start + length)] or [0])
    cid = worker.new_clip(video, round(start, 2), length, peak, "manual", config.load(), mode, crop_x)
    worker.process_q.put(("recut", cid))
    return jsonify({"id": cid})


@app.post("/api/videos/<int:vid>/retry")
def retry_video(vid):
    db.update("videos", vid, status="new", error=None, progress=0)
    worker.process_q.put(("video", vid))
    return jsonify({"ok": True})


# ---------- clips ----------

def _clip_or_404(cid):
    clip = db.one("SELECT * FROM clips WHERE id = ?", (cid,))
    if not clip:
        abort(404)
    return clip


@app.get("/media/clip/<int:cid>/<kind>")
def clip_media(cid, kind):
    clip = _clip_or_404(cid)
    col = kind if kind in ("thumb", "cover_wide", "cover_tall") else f"path_{kind}"
    path = clip.get(col)
    if not path or not Path(path).exists():
        abort(404)
    return send_file(path, conditional=True)


@app.post("/api/clips/<int:cid>/cover")
def save_cover(cid):
    """A thumbnail made in the dashboard: 16:9 goes to YouTube, the frame time to Instagram."""
    clip = _clip_or_404(cid)
    shape = request.form.get("shape")
    f = request.files.get("file")
    if shape not in ("wide", "tall") or not f:
        return _err("Send a thumbnail image and its shape")
    try:
        t = min(max(float(request.form.get("t", 0)), 0.0), clip["length"])
    except ValueError:
        return _err("Frame time must be a number (seconds)")
    data = f.read()
    if not data.startswith(b"\xff\xd8") or len(data) > 2_000_000:  # YouTube's limit is 2 MB
        return _err("The thumbnail must be a JPEG under 2 MB")
    clip_file = clip["path_vertical"] or clip["path_original"]
    if not clip_file:
        return _err("This clip hasn't been cut yet")
    out = Path(clip_file).parent / f"clip{clip['idx']:02d}_cover_{shape}.jpg"
    out.write_bytes(data)
    db.update("clips", cid, **{f"cover_{shape}": str(out), "cover_t": t})
    return jsonify({"ok": True})


@app.delete("/api/clips/<int:cid>/cover")
def remove_cover(cid):
    clip = _clip_or_404(cid)
    for shape in ("wide", "tall"):
        if clip[f"cover_{shape}"]:
            Path(clip[f"cover_{shape}"]).unlink(missing_ok=True)
    db.update("clips", cid, cover_wide=None, cover_tall=None, cover_t=None)
    return jsonify({"ok": True})


def _save_meta(cid, body):
    fields = {k: str(body[k]) for k in ("title", "description", "tags") if k in body}
    if fields:
        db.update("clips", cid, **fields)


@app.post("/api/clips/<int:cid>")
def save_clip(cid):
    _clip_or_404(cid)
    _save_meta(cid, request.json or {})
    return jsonify({"ok": True})


@app.post("/api/clips/<int:cid>/recut")
def recut(cid):
    clip = _clip_or_404(cid)
    body = request.json or {}
    video = db.one("SELECT duration FROM videos WHERE id = ?", (clip["video_id"],))
    try:
        start = max(0.0, float(body.get("start", clip["start"])))
        length = min(max(3.0, float(body.get("length", clip["length"]))), 180.0)
    except (TypeError, ValueError):
        return _err("Start and length must be numbers (seconds)")
    if video["duration"]:
        start = min(start, max(0.0, video["duration"] - length))
    _save_meta(cid, body)
    db.update("clips", cid, start=start, length=length, status="rendering", post_error=None)
    worker.process_q.put(("recut", cid))
    return jsonify({"ok": True})


@app.post("/api/clips/<int:cid>/style")
def restyle(cid):
    """Change how the vertical version is framed and re-render just that version."""
    clip = _clip_or_404(cid)
    if clip["status"] in ("rendering", "posting"):
        return _err("This clip is busy — try again in a moment")
    body = request.json or {}
    mode = body.get("vertical_mode")
    if mode not in ("blur", "crop"):
        return _err("Unknown vertical style")
    try:
        crop_x = min(max(float(body.get("crop_x", 0.5)), 0.0), 1.0)
    except (TypeError, ValueError):
        return _err("Position must be a number between 0 and 1")
    _save_meta(cid, body)
    db.update("clips", cid, vertical_mode=mode, crop_x=crop_x, status="rendering", post_error=None)
    worker.process_q.put(("recut", cid, ["vertical"]))
    return jsonify({"ok": True})


@app.post("/api/clips/<int:cid>/approve")
def approve(cid):
    clip = _clip_or_404(cid)
    body = request.json or {}
    targets = [t for t in body.get("targets", []) if t in ("yt_short", "yt_video", "ig_reel")]
    if not targets:
        return _err("Pick at least one place to post")
    if any(t.startswith("yt") for t in targets) and not youtube.TOKEN.exists():
        return _err("Connect YouTube in Settings first")
    if "ig_reel" in targets and not config.load()["ig_token"]:
        return _err("Connect Instagram in Settings first")
    if clip["status"] in ("rendering", "posting"):
        return _err("This clip is busy — try again in a moment")
    _save_meta(cid, body)
    db.update("clips", cid, status="posting", post_error=None)
    worker.post_q.put(("post", cid, targets))
    return jsonify({"ok": True})


@app.post("/api/clips/<int:cid>/reject")
def reject(cid):
    _clip_or_404(cid)
    db.update("clips", cid, status="rejected")
    return jsonify({"ok": True})


@app.post("/api/clips/<int:cid>/restore")
def restore(cid):
    _clip_or_404(cid)
    db.update("clips", cid, status="pending")
    return jsonify({"ok": True})


# ---------- settings & accounts ----------

@app.post("/api/settings")
def save_settings():
    body = dict(request.json or {})
    for k in ("clip_length", "minutes_per_clip", "max_clips_per_video", "min_score", "crf"):
        if k in body:
            try:
                body[k] = float(body[k]) if k == "min_score" else int(float(body[k]))
            except (TypeError, ValueError):
                return _err(f"{k} must be a number")
    if body.get("ig_token") == "••••":
        body.pop("ig_token")
    config.save(body)
    if {"ig_token", "ig_user_id", "ig_api_host"} & body.keys():
        _refresh_accounts()
    return jsonify({"ok": True, "accounts": _accounts})


@app.post("/api/youtube/client")
def youtube_client():
    f = request.files.get("file")
    if not f:
        return _err("Choose the client_secret JSON file")
    f.save(youtube.CLIENT_SECRET)
    youtube.CLIENT_SECRET.chmod(0o600)
    return jsonify({"ok": True})


@app.post("/api/youtube/connect")
def youtube_connect():
    try:
        _accounts["youtube"] = youtube.connect()
    except Exception as e:
        return _err(str(e))
    return jsonify({"ok": True, "accounts": _accounts})


@app.post("/api/youtube/disconnect")
def youtube_disconnect():
    youtube.disconnect()
    _accounts["youtube"] = None
    return jsonify({"ok": True})


@app.post("/api/instagram/disconnect")
def instagram_disconnect():
    config.save({"ig_token": "", "ig_user_id": ""})
    _accounts["instagram"] = None
    return jsonify({"ok": True})


@app.post("/api/accounts/refresh")
def accounts_refresh():
    _refresh_accounts()
    return jsonify({"accounts": _accounts})


def main():
    db.init()
    Path(config.load()["output_dir"]).mkdir(parents=True, exist_ok=True)
    worker.start()
    _refresh_accounts()
    port = int(os.environ.get("PORT", 5055))
    print(f"\n  Clip Factory running → http://127.0.0.1:{port}\n")
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)
