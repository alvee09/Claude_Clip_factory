"""SQLite storage for sources, videos and clips."""
import sqlite3
import time

from .config import DATA

DB_PATH = DATA / "clipfactory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    added REAL NOT NULL,
    last_scan TEXT             -- what the last scan found, for the dashboard
);
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    path TEXT UNIQUE NOT NULL,
    size INTEGER,
    duration REAL,
    width INTEGER,
    height INTEGER,
    status TEXT NOT NULL DEFAULT 'new',   -- new, analyzing, rendering, done, error, skipped
    progress REAL DEFAULT 0,
    error TEXT,
    added REAL NOT NULL,
    fingerprint TEXT           -- size + hash of the first and last MB: same file under any path
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    video_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    start REAL NOT NULL,
    length REAL NOT NULL,
    score REAL,
    path_vertical TEXT,
    path_original TEXT,
    thumb TEXT,
    title TEXT,
    description TEXT,
    tags TEXT,
    -- rendering, pending, posting, posted, post_failed, rejected, error
    status TEXT NOT NULL DEFAULT 'rendering',
    yt_short_url TEXT,
    yt_video_url TEXT,
    ig_reel_url TEXT,
    post_error TEXT,
    added REAL NOT NULL,
    posted_at REAL,
    vertical_mode TEXT,        -- blur / crop; NULL = use the default from settings
    crop_x REAL DEFAULT 0.5,   -- for crop: 0 = left edge, 0.5 = center, 1 = right edge
    source TEXT DEFAULT 'auto', -- auto = found by the app, manual = picked on the graph
    cover_wide TEXT,           -- your 16:9 thumbnail (used for YouTube)
    cover_tall TEXT,           -- your 9:16 cover
    cover_t REAL,              -- seconds into the clip of the cover's frame (used for Instagram)
    aspect TEXT                -- 9:16 / 4:5 / 1:1; NULL = use the default from settings
);
"""

# Columns added after the first release, for databases created before them.
MIGRATIONS = [
    ("clips", "vertical_mode", "TEXT"),
    ("clips", "crop_x", "REAL DEFAULT 0.5"),
    ("clips", "source", "TEXT DEFAULT 'auto'"),
    ("clips", "cover_wide", "TEXT"),
    ("clips", "cover_tall", "TEXT"),
    ("clips", "cover_t", "REAL"),
    ("clips", "aspect", "TEXT"),
    ("videos", "fingerprint", "TEXT"),
    ("sources", "last_scan", "TEXT"),
]


def connect():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init():
    with connect() as con:
        con.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def q(sql, args=()):
    with connect() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


def one(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def x(sql, args=()):
    with connect() as con:
        return con.execute(sql, args).lastrowid


def update(table, row_id, **fields):
    cols = ", ".join(f"{k} = ?" for k in fields)
    x(f"UPDATE {table} SET {cols} WHERE id = ?", (*fields.values(), row_id))


def now():
    return time.time()
