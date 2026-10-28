"""Local web UI + JSON API. Binds to 127.0.0.1 only."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import asdict
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from . import export, geo, images
from .config import (DEFAULT_CATEGORIES, DEFAULT_CATEGORIES_ZH, UNCATEGORIZED, Config, app_home, config_path,
                     load_config, save_config)
from .llm import LLMError, Ollama
from .pipeline import Runner
from .store import Store, record

STATIC = Path(__file__).parent / "static"


class State:
    def __init__(self):
        self.cfg = load_config()
        self.store = Store(app_home() / "insight.db")
        self.runner = Runner(self.store)
        self.folder: Path | None = None
        self.photos: list[Path] = []
        self.known: set[str] = set()
        self.source = "folder"  # "folder" (scanned) or "files" (hand-picked)

    def records(self, paths=None) -> list[dict]:
        return [record(p, self.store.get(p), self.cfg) for p in (paths if paths is not None else self.photos)]

    def open_folder(self, folder: Path, recursive: bool) -> None:
        self.folder = folder.expanduser().resolve()
        self.photos = images.scan(self.folder, recursive)
        self.known = {str(p) for p in self.photos}
        self.source = "folder"
        self.read_gps()

    def read_gps(self) -> None:
        """Read GPS for photos not seen before, so locations show even before analysis."""
        for p in self.photos:
            self.store.ensure_gps(p)

    def open_files(self, paths: list[Path], append: bool) -> int:
        """Open hand-picked photos (from any folders). Returns how many were skipped as unsupported."""
        keep = [p.expanduser().resolve() for p in paths]
        ok = [p for p in keep if p.is_file() and p.suffix.lower() in images.EXTENSIONS]
        current = self.photos if append else []
        self.photos = list(dict.fromkeys(current + ok))
        self.known = {str(p) for p in self.photos}
        if self.photos:
            self.folder = Path(os.path.commonpath([str(p.parent) for p in self.photos]))
        self.source = "files"
        self.read_gps()
        return len(keep) - len(ok)

    def subset(self, paths: list[str] | None) -> list[Path]:
        """The selected photos in grid order, or all photos when nothing is selected."""
        if paths is None:
            return self.photos
        wanted = set(paths)
        return [p for p in self.photos if str(p) in wanted]


app = FastAPI(title="Photo Insight")
S = State()


@app.middleware("http")
async def require_app_header(request: Request, call_next):
    # Any page on the internet can POST to localhost; a custom header forces a CORS
    # preflight that we never approve, so only our own UI can change things.
    if request.method not in ("GET", "HEAD") and request.headers.get("x-photo-insight") != "1":
        return JSONResponse({"detail": "Missing X-Photo-Insight header"}, status_code=403)
    return await call_next(request)


def _need_folder():
    if not S.photos:
        raise HTTPException(400, "Open a folder or choose photos first")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/static/{name}")
def static(name: str):
    path = (STATIC / name).resolve()
    if path.parent != STATIC.resolve() or not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/api/status")
def status():
    try:
        models = Ollama(S.cfg.settings.ollama_host, timeout=10).models()
        ollama = {"ok": True, "models": models}
    except LLMError as e:
        ollama = {"ok": False, "error": str(e), "models": []}
    return {"ollama": ollama, "config": S.cfg.to_dict(), "folder": str(S.folder) if S.folder else None,
            "job": S.runner.state.snapshot(), "uncategorized": UNCATEGORIZED, "geo": geo.status(),
            "exiftool": export.exiftool_path() is not None,
            "starter_categories": {"en": [asdict(c) for c in DEFAULT_CATEGORIES],
                                   "yue": [asdict(c) for c in DEFAULT_CATEGORIES_ZH]}}


@app.put("/api/config")
def put_config(body: dict):
    if S.runner.busy:
        raise HTTPException(409, "Wait for the running batch to finish")
    try:
        cfg = Config.from_dict(body)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    S.cfg = cfg
    save_config(cfg)
    return cfg.to_dict()


@app.post("/api/pick-folder")
def pick_folder():
    if sys.platform != "darwin":
        raise HTTPException(501, "Folder picker is macOS-only; type the path instead")
    r = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose a photo folder")'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return {"path": None}
    return {"path": r.stdout.strip()}


CHOOSE_FILES = """
activate
set fs to choose file with prompt "Choose photos (⌘-click or Shift-click to pick several)" of type {"public.image"} with multiple selections allowed
set out to ""
repeat with f in fs
    set out to out & POSIX path of f & linefeed
end repeat
return out
"""


class FilesBody(BaseModel):
    paths: list[str] | None = None  # None: show the macOS file dialog
    append: bool = False


@app.post("/api/files")
def open_files(body: FilesBody):
    paths = body.paths
    if paths is None:
        if sys.platform != "darwin":
            raise HTTPException(501, "The photo picker is macOS-only for now; open a folder instead")
        r = subprocess.run(["osascript", "-e", CHOOSE_FILES], capture_output=True, text=True)
        if r.returncode != 0:  # user pressed Cancel
            return {"cancelled": True}
        paths = [line for line in r.stdout.splitlines() if line.strip()]
    skipped = S.open_files([Path(p) for p in paths], body.append)
    return {"folder": str(S.folder) if S.folder else None, "source": S.source,
            "skipped": skipped, "photos": S.records()}


class FolderBody(BaseModel):
    path: str
    recursive: bool = True


@app.post("/api/folder")
def open_folder(body: FolderBody):
    try:
        S.open_folder(Path(body.path), body.recursive)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"folder": str(S.folder), "source": S.source, "photos": S.records()}


@app.get("/api/geo")
def geo_status():
    return geo.status()


class GeoDownloadBody(BaseModel):
    countries: list[str] | None = None


@app.post("/api/geo/download")
def geo_download(body: GeoDownloadBody):
    try:
        geo.download_async(body.countries)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return geo.status()


class RemoveBody(BaseModel):
    paths: list[str] | None = None  # None: clear the whole list


@app.post("/api/remove")
def remove(body: RemoveBody):
    """Take photos off the working list. Files on disk and cached results are kept."""
    if S.runner.busy:
        raise HTTPException(409, "Wait for the running batch to finish")
    before = len(S.photos)
    drop = set(body.paths or []) if body.paths is not None else {str(p) for p in S.photos}
    S.photos = [p for p in S.photos if str(p) not in drop]
    S.known = {str(p) for p in S.photos}
    S.source = "files"
    S.folder = Path(os.path.commonpath([str(p.parent) for p in S.photos])) if S.photos else None
    return {"folder": str(S.folder) if S.folder else None, "source": S.source,
            "removed": before - len(S.photos), "photos": S.records()}


@app.get("/api/photos")
def photos():
    return {"folder": str(S.folder) if S.folder else None, "source": S.source, "photos": S.records()}


@app.get("/api/thumb")
def thumb(path: str, size: int = 400):
    if path not in S.known:
        raise HTTPException(404)
    size = 1600 if size > 400 else 400  # grid thumbnail or large preview
    p = Path(path)
    st = p.stat()
    key = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime}|{size}".encode()).hexdigest()
    cache = app_home() / "thumbs" / f"{key}.jpg"
    if not cache.exists():
        cache.parent.mkdir(exist_ok=True)
        try:
            cache.write_bytes(images.jpeg_bytes(p, size, quality=80 if size == 400 else 88))
        except Exception:
            raise HTTPException(415, "Cannot read image")
    return FileResponse(cache, headers={"Cache-Control": "max-age=86400"})


class AnalyzeBody(BaseModel):
    force: bool = False
    paths: list[str] | None = None


@app.post("/api/analyze")
def analyze(body: AnalyzeBody):
    _need_folder()
    paths = S.subset(body.paths)
    try:
        S.runner.start(paths, S.cfg, force=body.force)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return S.runner.state.snapshot()


@app.post("/api/cancel")
def cancel():
    S.runner.cancel()
    return S.runner.state.snapshot()


@app.get("/api/job")
def job():
    changed = [Path(p) for p in S.runner.take_changed()]
    return {"job": S.runner.state.snapshot(), "updated": S.records(changed)}


class PhotoEdit(BaseModel):
    path: str
    category: str | None = None
    hashtags: list[str] | None = None
    caption: str | None = None
    reset: bool = False


@app.patch("/api/photo")
def edit_photo(body: PhotoEdit):
    if body.path not in S.known:
        raise HTTPException(404)
    if body.category is not None and body.category != UNCATEGORIZED and not S.cfg.category(body.category):
        raise HTTPException(400, "Unknown category")
    p = Path(body.path)
    S.store.set_overrides(p, body.category, body.hashtags, body.caption, body.reset)
    return S.records([p])[0]


class OrganizeBody(BaseModel):
    mode: str = "copy"
    dest: str | None = None
    paths: list[str] | None = None
    group: str = "category"  # category | location | location_category | category_location
    level: str = "city"      # country | city | area (how deep location folders go)
    names: str = "en"        # en | zh (folder names for places)
    embed: bool = False      # write hashtags/caption/location into the copies (copy mode only)


class SubsetBody(BaseModel):
    paths: list[str] | None = None


@app.post("/api/organize/plan")
def organize_plan(body: OrganizeBody):
    """Dry run for the Sort dialog: folders that would be created, with photo counts."""
    _need_folder()
    try:
        return export.plan(S.records(S.subset(body.paths)), body.group, body.level, body.names)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/organize")
def organize(body: OrganizeBody):
    _need_folder()
    if S.runner.busy:
        raise HTTPException(409, "Wait for the running batch to finish")
    dest = Path(body.dest).expanduser().resolve() if body.dest else S.folder / "_sorted"
    if body.embed and body.mode != "copy":
        raise HTTPException(400, "Metadata is only written into copies; originals are never changed")
    if body.embed and not export.exiftool_path():
        raise HTTPException(400, "exiftool is not installed (brew install exiftool)")
    records = S.records(S.subset(body.paths))
    try:
        pairs = export.organize(records, dest, body.mode, body.group, body.level, body.names)
    except ValueError as e:
        raise HTTPException(400, str(e))
    embedded = None
    if body.embed:
        by_path = {r["path"]: r for r in records}
        embedded = export.embed_metadata([(target, by_path[str(src)]) for src, target in pairs])
    if body.mode == "move":
        mapping = {src: target for src, target in pairs}
        for src, target in pairs:
            S.store.rename(src, target)
        S.photos = [mapping.get(p, p) for p in S.photos]
        S.known = {str(p) for p in S.photos}
    return {"count": len(pairs), "dest": str(dest), "skipped": len(records) - len(pairs), "embedded": embedded}


@app.post("/api/sidecars")
def sidecars(body: SubsetBody):
    _need_folder()
    return export.write_sidecars(S.records(S.subset(body.paths)))


@app.post("/api/export.csv")
def export_csv(body: SubsetBody):
    _need_folder()
    return PlainTextResponse(export.to_csv(S.records(S.subset(body.paths))), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="photo-insight.csv"'})


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) if path.exists() else 0


@app.get("/api/data")
def data_stats():
    """What Photo Insight keeps in its own folder (never the user's photos)."""
    home = app_home()
    return {
        "home": str(home),
        "results": {**S.store.stats(), "bytes": (home / "insight.db").stat().st_size},
        "thumbs": {"files": len(list((home / "thumbs").glob("*.jpg"))) if (home / "thumbs").exists() else 0,
                   "bytes": _dir_size(home / "thumbs")},
        "settings": {"categories": len(S.cfg.categories), "bytes": _dir_size(config_path())},
        "geo": {"available": geo.status()["available"], "bytes": _dir_size(home / "geo")},
    }


class ClearBody(BaseModel):
    results: bool = False   # analysis results, edits, cached GPS
    thumbs: bool = False    # thumbnail/preview cache
    settings: bool = False  # categories and options -> defaults
    geo: bool = False       # downloaded place data


@app.post("/api/data/clear")
def data_clear(body: ClearBody):
    if S.runner.busy:
        raise HTTPException(409, "Wait for the running batch to finish")
    if body.geo and geo.DOWNLOAD.status == "running":
        raise HTTPException(409, "Wait for the place data download to finish")
    home = app_home()
    if body.results:
        S.store.clear()
    if body.thumbs:
        shutil.rmtree(home / "thumbs", ignore_errors=True)
    if body.settings:
        config_path().unlink(missing_ok=True)
        S.cfg = load_config()  # writes fresh defaults
    if body.geo:
        shutil.rmtree(home / "geo", ignore_errors=True)
        geo.reset()
    if body.results:
        S.read_gps()  # keep showing locations for the photos still open
    return data_stats()


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)
