"""SQLite cache of analysis results, keyed by absolute file path.

Results are reused while the file (size/mtime), model and category definitions are unchanged,
so re-opening a folder is instant and only new or changed photos go to the model.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from . import geo, hashtags, images
from .config import UNCATEGORIZED, Config
from .llm import Analysis

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    path TEXT PRIMARY KEY,
    size INTEGER, mtime REAL,
    category TEXT, confidence REAL, caption TEXT, model_tags TEXT,
    model TEXT, signature TEXT, analyzed_at REAL, error TEXT,
    user_category TEXT, user_tags TEXT, user_caption TEXT,
    lat REAL, lon REAL, gps_read INTEGER
);
"""
# Columns added after the first release; created on older databases at startup.
MIGRATIONS = {"lat": "REAL", "lon": "REAL", "gps_read": "INTEGER"}


def signature(cfg: Config) -> str:
    """Changes when anything that affects the model's answer changes."""
    s = cfg.settings
    raw = json.dumps([s.model, s.language, s.chinese_hashtags, s.image_size, [(c.name, c.description) for c in cfg.categories]])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


class Store:
    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            have = {r["name"] for r in self.db.execute("PRAGMA table_info(photos)")}
            for col, kind in MIGRATIONS.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE photos ADD COLUMN {col} {kind}")

    def get(self, path: Path) -> sqlite3.Row | None:
        with self.lock:
            return self.db.execute("SELECT * FROM photos WHERE path=?", (str(path),)).fetchone()

    def needs_analysis(self, path: Path, sig: str) -> bool:
        row = self.get(path)
        if row is None or row["error"] or row["signature"] != sig:
            return True
        st = path.stat()
        return row["size"] != st.st_size or abs(row["mtime"] - st.st_mtime) > 1e-3

    def save_analysis(self, path: Path, a: Analysis, model: str, sig: str, clear_overrides: bool) -> None:
        st = path.stat()
        with self.lock, self.db:
            self.db.execute(
                """INSERT INTO photos (path,size,mtime,category,confidence,caption,model_tags,model,signature,analyzed_at,error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,NULL)
                   ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,
                     category=excluded.category, confidence=excluded.confidence, caption=excluded.caption,
                     model_tags=excluded.model_tags, model=excluded.model, signature=excluded.signature,
                     analyzed_at=excluded.analyzed_at, error=NULL""",
                (str(path), st.st_size, st.st_mtime, a.category, a.confidence, a.caption,
                 json.dumps(a.tags, ensure_ascii=False), model, sig, time.time()),
            )
            if clear_overrides:
                self.db.execute("UPDATE photos SET user_category=NULL, user_tags=NULL, user_caption=NULL WHERE path=?",
                                (str(path),))

    def ensure_gps(self, path: Path, refresh: bool = False) -> tuple[float, float] | None:
        """GPS from EXIF, read once per photo and cached (metadata only, ~1 ms)."""
        row = None if refresh else self.get(path)
        if row is not None and row["gps_read"]:
            return (row["lat"], row["lon"]) if row["lat"] is not None else None
        coords = images.gps(path)
        lat, lon = coords or (None, None)
        with self.lock, self.db:
            self.db.execute(
                """INSERT INTO photos (path,lat,lon,gps_read) VALUES (?,?,?,1)
                   ON CONFLICT(path) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, gps_read=1""",
                (str(path), lat, lon))
        return coords

    def save_error(self, path: Path, error: str) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO photos (path,error) VALUES (?,?) ON CONFLICT(path) DO UPDATE SET error=excluded.error",
                (str(path), error[:500]),
            )

    def set_overrides(self, path: Path, category=None, tags=None, caption=None, reset=False) -> None:
        with self.lock, self.db:
            if reset:
                self.db.execute("UPDATE photos SET user_category=NULL, user_tags=NULL, user_caption=NULL WHERE path=?",
                                (str(path),))
                return
            if category is not None:
                self.db.execute("UPDATE photos SET user_category=? WHERE path=?", (category, str(path)))
            if tags is not None:
                norm = [n for n in (hashtags.normalize(t) for t in tags) if n]
                self.db.execute("UPDATE photos SET user_tags=? WHERE path=?",
                                (json.dumps(list(dict.fromkeys(norm)), ensure_ascii=False), str(path)))
            if caption is not None:
                self.db.execute("UPDATE photos SET user_caption=? WHERE path=?", (caption, str(path)))

    def stats(self) -> dict:
        with self.lock:
            r = self.db.execute(
                """SELECT count(*) AS photos, sum(category IS NOT NULL) AS analysed,
                          sum(user_category IS NOT NULL OR user_tags IS NOT NULL OR user_caption IS NOT NULL) AS edited
                   FROM photos""").fetchone()
        return {k: r[k] or 0 for k in ("photos", "analysed", "edited")}

    def clear(self) -> None:
        """Forget every result, edit and cached GPS reading. Photo files are not touched."""
        with self.lock:
            with self.db:
                self.db.execute("DELETE FROM photos")
            self.db.execute("VACUUM")

    def rename(self, old: Path, new: Path) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM photos WHERE path=?", (str(new),))
            self.db.execute("UPDATE photos SET path=? WHERE path=?", (str(new), str(old)))


def record(path: Path, row: sqlite3.Row | None, cfg: Config) -> dict:
    """The effective view of a photo: model output with user overrides and current category tags applied."""
    s = cfg.settings
    rec = {"path": str(path), "name": path.name, "status": "pending", "category": None, "confidence": None,
           "caption": "", "hashtags": [], "edited": False, "error": None, "stale": False,
           "gps": None, "location": None}
    if row is None:
        return rec
    place = None
    if row["lat"] is not None:
        place = geo.lookup(row["lat"], row["lon"])
        rec.update(gps=[row["lat"], row["lon"]], location=place.to_dict() if place else None)
    if row["error"]:
        rec.update(status="error", error=row["error"])
        return rec
    if row["category"] is None:
        return rec
    category = row["user_category"] or row["category"]
    if not row["user_category"] and (row["confidence"] or 0) < s.min_confidence:
        category = UNCATEGORIZED
    cat = cfg.category(category)
    if row["user_tags"] is not None:
        tags = json.loads(row["user_tags"])
    else:
        loc_tags = geo.hashtags(place, s.landmark_tags, s.chinese_hashtags) if s.location_tags else []
        tags = hashtags.merge(cat.hashtags if cat else [], json.loads(row["model_tags"] or "[]"),
                              hashtags.general_for(s.include_general_hashtags, s.chinese_hashtags), s.max_hashtags,
                              location=loc_tags)
    rec.update(
        status="done", category=category, model_category=row["category"], confidence=row["confidence"],
        caption=row["user_caption"] if row["user_caption"] is not None else row["caption"],
        hashtags=tags, model=row["model"],
        edited=any(row[k] is not None for k in ("user_category", "user_tags", "user_caption")),
        stale=row["signature"] != signature(cfg),
    )
    return rec
