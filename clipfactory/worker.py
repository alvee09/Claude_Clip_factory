"""Background workers: scan folders, cut clips, and post approved clips.

Video processing and posting run on separate threads so an upload never waits
behind hours of footage being analysed.
"""
import itertools
import json
import os
import queue
import re
import threading
import traceback
from pathlib import Path

from . import config, db, instagram, media, youtube
from .config import VIDEO_EXTS

class JobQueue(queue.PriorityQueue):
    """Clips you asked for (your picks, re-cuts) run before slow background work."""
    PRIORITY = {"recut": 0, "scan": 1, "video": 2, "score": 3}

    def __init__(self):
        super().__init__()
        self._seq = itertools.count()

    def put(self, job, *args, **kwargs):
        super().put((self.PRIORITY.get(job[0], 2), next(self._seq), job), *args, **kwargs)

    def get(self, *args, **kwargs):
        return super().get(*args, **kwargs)[2]


process_q = JobQueue()
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


def _render_clip(clip, video, s, kinds=None):
    """Render the clip's formats; kinds=['vertical'] redoes just the vertical one."""
    folder = Path(s["output_dir"]) / f"{video['id']:04d}_{_slug(Path(video['path']).stem)}"
    paths = {}
    if not kinds:
        kinds = [k for k in ("vertical", "original") if k in s["formats"]] or ["vertical"]
    for kind in kinds:
        out = folder / f"clip{clip['idx']:02d}_{kind}.mp4"
        activity["process"] = f"Rendering clip {clip['idx']} ({kind}) of {Path(video['path']).name}"
        media.render(video["path"], clip["start"], clip["length"], out, kind, s,
                     vertical_mode=clip.get("vertical_mode"), crop_x=clip.get("crop_x"),
                     aspect=clip.get("aspect"))
        paths[f"path_{kind}"] = str(out)
    thumb = folder / f"clip{clip['idx']:02d}_thumb.jpg"
    source = paths.get("path_vertical") or clip.get("path_vertical") \
        or paths.get("path_original") or clip.get("path_original")
    media.thumbnail(source, clip["length"] * 0.65, thumb)
    db.update("clips", clip["id"], status="pending", thumb=str(thumb), **paths)


def new_clip(video, start, length, score, source, s, vertical_mode=None, crop_x=0.5, aspect=None):
    """Insert a clip row (status 'rendering') and return its id. Numbers never repeat
    within a video, so a new clip can't overwrite an older clip's files."""
    idx = (db.one("SELECT MAX(idx) AS m FROM clips WHERE video_id = ?", (video["id"],))["m"] or 0) + 1
    title = _default_title(video["path"], idx)
    return db.x(
        "INSERT INTO clips (video_id, idx, start, length, score, title, description, tags, added, "
        "source, vertical_mode, crop_x, aspect) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (video["id"], idx, start, length, score, title, s["description_template"], s["default_tags"],
         db.now(), source, vertical_mode, crop_x, aspect))


def delete_clip_files(clip, keep_thumb=True, keep_covers=True):
    """Free disk space: delete a clip's rendered videos (and optionally its images)."""
    cols = ["path_vertical", "path_original"] + ([] if keep_thumb else ["thumb"]) + \
        ([] if keep_covers else ["cover_wide", "cover_tall"])
    for col in cols:
        if clip.get(col):
            Path(clip[col]).unlink(missing_ok=True)
    if db.one("SELECT id FROM clips WHERE id = ?", (clip["id"],)):
        db.update("clips", clip["id"], **{c: None for c in cols})
    folder = next((Path(clip[c]).parent for c in ("path_vertical", "path_original", "thumb") if clip.get(c)), None)
    if folder and folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()


# ---------- intensity graph ----------

SCORES = config.DATA / "scores"


def save_score(vid, score):
    SCORES.mkdir(exist_ok=True)
    (SCORES / f"{vid}.json").write_text(json.dumps([round(float(v), 2) for v in score]))


def load_score(vid):
    path = SCORES / f"{vid}.json"
    return json.loads(path.read_text()) if path.exists() else None


def score_video(vid):
    """Build the graph for a video processed before graphs were saved."""
    video = db.one("SELECT * FROM videos WHERE id = ?", (vid,))
    if not video or load_score(vid) is not None or not Path(video["path"]).exists():
        return
    activity["process"] = f"Building graph for {Path(video['path']).name}"
    info = media.probe(video["path"])
    save_score(vid, media.intensity_curve(video["path"], info))


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
        save_score(vid, score)
        moments = media.pick_moments(score, info["duration"], s)

        db.update("videos", vid, status="rendering", progress=0.8)
        # Re-processing replaces automatic clips you haven't acted on yet; your own picks,
        # posted and rejected clips stay.
        stale = "video_id = ? AND status IN ('rendering', 'error', 'pending') AND COALESCE(source, 'auto') = 'auto'"
        for old in db.q(f"SELECT * FROM clips WHERE {stale}", (vid,)):
            delete_clip_files(old, keep_thumb=False, keep_covers=False)
        db.x(f"DELETE FROM clips WHERE {stale}", (vid,))
        for n, (start, length, sc) in enumerate(moments, 1):
            cid = new_clip(video, start, length, sc, "auto", s)
            _render_clip(db.one("SELECT * FROM clips WHERE id = ?", (cid,)), video, s)
            db.update("videos", vid, progress=round(0.8 + 0.2 * n / len(moments), 3))
        db.update("videos", vid, status="done", progress=1)
    except Exception as e:
        traceback.print_exc()
        db.update("videos", vid, status="error", error=str(e)[:500])


def recut_clip(clip_id, kinds=None):
    clip = db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    video = db.one("SELECT * FROM videos WHERE id = ?", (clip["video_id"],))
    try:
        _render_clip(clip, video, config.load(), kinds)
    except Exception as e:
        traceback.print_exc()
        db.update("clips", clip_id, status="error", post_error=f"Re-cut failed: {e}"[:500])


# ---------- posting ----------

def _tags(clip):
    return [t.strip().lstrip("#") for t in (clip["tags"] or "").split(",") if t.strip()]


def post_clip(clip_id, targets):
    clip = db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    s = config.load()
    if not (clip["path_vertical"] or clip["path_original"]):
        # files were deleted after an earlier post; cut it again first
        video = db.one("SELECT * FROM videos WHERE id = ?", (clip["video_id"],))
        try:
            if not video or not Path(video["path"]).exists():
                raise RuntimeError("The source video is gone (is the drive plugged in?)")
            activity["post"] = f"Re-cutting clip #{clip_id} before posting"
            _render_clip(clip, video, s)
        except Exception as e:
            traceback.print_exc()
            db.update("clips", clip_id, status="post_failed", post_error=f"Couldn't re-cut the clip: {e}"[:1000])
            return
        clip = db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    tags = _tags(clip)
    errors, notes = [], []
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
                cover = clip["cover_wide"] if not short else clip["cover_tall"] or clip["cover_wide"]
                if cover and not Path(cover).exists():
                    cover = None
                url, warning = youtube.upload(path, clip["title"], clip["description"] or "", tags,
                                              s["yt_privacy"], s["yt_category"], short, thumbnail=cover)
                if warning:
                    notes.append(f"{target}: {warning}")
            elif target == "ig_reel":
                path = clip["path_vertical"] or clip["path_original"]
                caption = clip["title"]
                if clip["description"]:
                    caption += "\n\n" + clip["description"]
                if tags:
                    caption += "\n\n" + " ".join("#" + re.sub(r"\W+", "", t) for t in tags)
                offset = clip["cover_t"] * 1000 if clip.get("cover_t") is not None else None
                url = instagram.upload_reel(path, caption, thumb_offset_ms=offset)
            else:
                continue
            db.update("clips", clip_id, **{f"{target}_url": url})
        except Exception as e:
            traceback.print_exc()
            errors.append(f"{target}: {e}")
    if errors:
        db.update("clips", clip_id, status="post_failed", post_error="\n".join(errors + notes)[:1000])
    else:
        db.update("clips", clip_id, status="posted", post_error="\n".join(notes)[:1000] or None, posted_at=db.now())
        delete_clip_files(clip)  # it's on YouTube/Instagram now; free the space


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
            elif kind == "score":
                score_video(*args)
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
