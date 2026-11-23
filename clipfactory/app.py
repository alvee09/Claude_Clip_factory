"""Local web dashboard + JSON API (http://127.0.0.1:5055)."""
import os
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file
from werkzeug.utils import secure_filename

from . import config, db, instagram, worker, youtube

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
    path = clip.get("thumb" if kind == "thumb" else f"path_{kind}")
    if not path or not Path(path).exists():
        abort(404)
    return send_file(path, conditional=True)


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
