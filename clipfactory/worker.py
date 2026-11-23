"""Background workers: scan folders, cut clips, and post approved clips.

Video processing and posting run on separate threads so an upload never waits
behind hours of footage being analysed.
"""
import os
import queue
import re
import threading
import traceback
from pathlib import Path

from . import config, db, instagram, media, youtube
from .config import VIDEO_EXTS

process_q = queue.Queue()
post_q = queue.Queue()
activity = {"process": "Idle", "post": "Idle"}


# ---------- scanning ----------

def scan_source(source_id):
    src = db.one("SELECT * FROM sources WHERE id = ?", (source_id,))
    root = Path(src["path"])
    if not root.is_dir():
        raise RuntimeError(f"Folder not found: {root} (is the drive plugged in?)")
    out_dir = Path(config.load()["output_dir"]).resolve()
    found = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")
                       and Path(dirpath, d).resolve() != out_dir]
        for name in filenames:
            # "._name" files are macOS metadata twins on exFAT/FAT drives, not videos
            if name.startswith(".") or Path(name).suffix.lower() not in VIDEO_EXTS:
                continue
            path = Path(dirpath, name)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size < 1_000_000:
                continue
            if db.one("SELECT id FROM videos WHERE path = ?", (str(path),)):
                continue
            vid = db.x("INSERT INTO videos (source_id, path, size, added) VALUES (?, ?, ?, ?)",
                       (source_id, str(path), size, db.now()))
            process_q.put(("video", vid))
            found += 1
    return found


# ---------- cutting ----------

def _slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:60] or "video"


def _default_title(video_path, n):
    stem = re.sub(r"[_\-.]+", " ", Path(video_path).stem).strip()
    return f"{stem} – Highlight {n}"


def _render_clip(clip, video, s):
    folder = Path(s["output_dir"]) / f"{video['id']:04d}_{_slug(Path(video['path']).stem)}"
    paths = {}
    kinds = [k for k in ("vertical", "original") if k in s["formats"]] or ["vertical"]
    for i, kind in enumerate(kinds):
        out = folder / f"clip{clip['idx']:02d}_{kind}.mp4"
        activity["process"] = f"Rendering clip {clip['idx']} ({kind}) of {Path(video['path']).name}"
        media.render(video["path"], clip["start"], clip["length"], out, kind, s)
        paths[f"path_{kind}"] = str(out)
    first = paths.get("path_original") or paths["path_vertical"]
    thumb = folder / f"clip{clip['idx']:02d}_thumb.jpg"
    media.thumbnail(paths.get("path_vertical") or first, clip["length"] * 0.65, thumb)
    db.update("clips", clip["id"], status="pending", thumb=str(thumb), **paths)


def process_video(vid):
    video = db.one("SELECT * FROM videos WHERE id = ?", (vid,))
    if not video:
        return
    name = Path(video["path"]).name
    s = config.load()
    try:
        if not Path(video["path"]).exists():
            raise RuntimeError("File not found (is the drive plugged in?)")
        db.update("videos", vid, status="analyzing", progress=0, error=None)
        activity["process"] = f"Analyzing {name}"
        info = media.probe(video["path"])
        db.update("videos", vid, duration=info["duration"],
                  width=info["width"], height=info["height"])
        if not info["has_video"]:
            raise RuntimeError("No video stream in this file")

        def prog(p):
            db.update("videos", vid, progress=round(p * 0.8, 3))
        score = media.intensity_curve(video["path"], info, prog)
        moments = media.pick_moments(score, info["duration"], s)

        db.update("videos", vid, status="rendering", progress=0.8)
        # Re-processing replaces clips you haven't acted on yet; posted/rejected ones stay.
        db.x("DELETE FROM clips WHERE video_id = ? AND status IN ('rendering', 'error', 'pending')", (vid,))
        for n, (start, length, sc) in enumerate(moments, 1):
            cid = db.x(
                "INSERT INTO clips (video_id, idx, start, length, score, title, description, tags, added) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (vid, n, start, length, sc, _default_title(video["path"], n),
                 s["description_template"], s["default_tags"], db.now()))
            _render_clip(db.one("SELECT * FROM clips WHERE id = ?", (cid,)), video, s)
            db.update("videos", vid, progress=round(0.8 + 0.2 * n / len(moments), 3))
        db.update("videos", vid, status="done", progress=1)
    except Exception as e:
        traceback.print_exc()
        db.update("videos", vid, status="error", error=str(e)[:500])


def recut_clip(clip_id):
    clip = db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    video = db.one("SELECT * FROM videos WHERE id = ?", (clip["video_id"],))
    try:
        _render_clip(clip, video, config.load())
    except Exception as e:
        traceback.print_exc()
        db.update("clips", clip_id, status="error", post_error=f"Re-cut failed: {e}"[:500])


# ---------- posting ----------

def _tags(clip):
    return [t.strip().lstrip("#") for t in (clip["tags"] or "").split(",") if t.strip()]


def post_clip(clip_id, targets):
    clip = db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    s = config.load()
    tags = _tags(clip)
    errors = []
    for target in targets:
        if clip.get(f"{target}_url"):
            continue  # already posted there; never double-post
        activity["post"] = f"Posting clip #{clip_id} → {target}"
        try:
            if target in ("yt_short", "yt_video"):
                short = target == "yt_short"
                path = clip["path_vertical"] if short else clip["path_original"]
                if not path:
                    raise RuntimeError("That format wasn't rendered (check Settings → formats)")
                url = youtube.upload(path, clip["title"], clip["description"] or "", tags,
                                     s["yt_privacy"], s["yt_category"], short)
            elif target == "ig_reel":
                path = clip["path_vertical"] or clip["path_original"]
                caption = clip["title"]
                if clip["description"]:
                    caption += "\n\n" + clip["description"]
                if tags:
                    caption += "\n\n" + " ".join("#" + re.sub(r"\W+", "", t) for t in tags)
                url = instagram.upload_reel(path, caption)
            else:
                continue
            db.update("clips", clip_id, **{f"{target}_url": url})
        except Exception as e:
            traceback.print_exc()
            errors.append(f"{target}: {e}")
    if errors:
        db.update("clips", clip_id, status="post_failed", post_error="\n".join(errors)[:1000])
    else:
        db.update("clips", clip_id, status="posted", post_error=None, posted_at=db.now())


# ---------- threads ----------

def _loop(q, key):
    while True:
        job = q.get()
        try:
            kind, *args = job
            if kind == "video":
                process_video(*args)
            elif kind == "recut":
                recut_clip(*args)
            elif kind == "scan":
                activity[key] = "Scanning folder"
                scan_source(*args)
            elif kind == "post":
                post_clip(*args)
        except Exception:
            traceback.print_exc()
        finally:
            activity[key] = "Idle"
            q.task_done()


def start():
    # Resume anything interrupted by a restart.
    db.x("UPDATE videos SET status = 'new' WHERE status IN ('analyzing', 'rendering')")
    db.x("UPDATE clips SET status = 'pending', post_error = 'Posting was interrupted — approve again to retry' "
         "WHERE status = 'posting'")
    for v in db.q("SELECT id FROM videos WHERE status = 'new' ORDER BY id"):
        process_q.put(("video", v["id"]))
    for c in db.q("SELECT id FROM clips WHERE status = 'rendering' AND video_id IN "
                  "(SELECT id FROM videos WHERE status = 'done')"):
        process_q.put(("recut", c["id"]))
    threading.Thread(target=_loop, args=(process_q, "process"), daemon=True).start()
    threading.Thread(target=_loop, args=(post_q, "post"), daemon=True).start()
