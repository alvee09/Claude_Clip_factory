"""Paths and user settings (stored in data/settings.json)."""
import json
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
SETTINGS_PATH = DATA / "settings.json"
UPLOADS = ROOT / "uploads"  # videos added through the dashboard's upload box

VIDEO_EXTS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".m2ts", ".ts",
    ".webm", ".wmv", ".flv", ".mpg", ".mpeg", ".3gp",
}

DEFAULTS = {
    # Where finished clips are written. The source drive is only ever read.
    "output_dir": str(ROOT / "clips"),
    # Clip picking
    "clip_length": 30,          # seconds per clip
    "minutes_per_clip": 4,      # roughly one clip for every N minutes of footage
    "max_clips_per_video": 8,
    "min_score": 1.2,           # how "intense" a moment must be (higher = pickier)
    # Rendering
    "formats": ["vertical", "original"],
    "vertical_mode": "crop",    # "crop" = zoom to fill, centered on gameplay; "blur" = full frame on blurred background
    "crf": 20,
    "preset": "veryfast",
    # Post defaults
    "default_targets": ["yt_short", "ig_reel"],
    "description_template": "",
    "default_tags": "gaming, highlights, clips",
    "yt_privacy": "public",
    "yt_category": "20",        # 20 = Gaming, 17 = Sports
    # Instagram
    "ig_user_id": "",
    "ig_token": "",
    "ig_api_host": "graph.instagram.com",
    "ig_api_version": "v21.0",
}

_lock = threading.Lock()


def load():
    with _lock:
        data = dict(DEFAULTS)
        if SETTINGS_PATH.exists():
            data.update(json.loads(SETTINGS_PATH.read_text()))
        return data


def save(updates):
    with _lock:
        data = dict(DEFAULTS)
        if SETTINGS_PATH.exists():
            data.update(json.loads(SETTINGS_PATH.read_text()))
        data.update({k: v for k, v in updates.items() if k in DEFAULTS})
        SETTINGS_PATH.write_text(json.dumps(data, indent=2))
        SETTINGS_PATH.chmod(0o600)
        return data
