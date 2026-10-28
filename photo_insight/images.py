"""Finding photos on disk and preparing them for the model / the UI."""
from __future__ import annotations

import io
import threading
from pathlib import Path

from PIL import ExifTags, Image, ImageOps

try:  # HEIC/HEIF from iPhones
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF = True
except ImportError:  # pragma: no cover
    HEIF = False

# Pillow refuses images over ~179 MP as a "decompression bomb" guard for untrusted uploads. These are the
# user's own files, and 200 MP phone cameras exceed that, so allow up to 1 gigapixel (JPEGs are decoded
# at reduced scale via draft(), so even 200 MP stays cheap).
MAX_PIXELS = 1_000_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

# Full-resolution decodes of huge PNG/TIFF/HEIC files can take >1 GB each; the grid requests many thumbnails
# at once, so cap concurrent decodes.
_DECODE_SLOTS = threading.BoundedSemaphore(2)

EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"} | ({".heic", ".heif"} if HEIF else set())
SKIP_DIRS = {"_sorted"}


def scan(folder: Path, recursive: bool = True) -> list[Path]:
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise FileNotFoundError(f"Not a folder: {folder}")
    it = folder.rglob("*") if recursive else folder.iterdir()
    photos = []
    for p in it:
        rel = p.relative_to(folder).parts
        if any(part.startswith(".") or part in SKIP_DIRS for part in rel):
            continue
        if p.is_file() and p.suffix.lower() in EXTENSIONS:
            photos.append(p)
    return sorted(photos)


def jpeg_bytes(path: Path, max_edge: int, quality: int = 85) -> bytes:
    """Load, honour EXIF orientation, downscale and re-encode as JPEG."""
    with _DECODE_SLOTS:
        return _jpeg_bytes(path, max_edge, quality)


def _jpeg_bytes(path: Path, max_edge: int, quality: int) -> bytes:
    try:
        im = Image.open(path)
    except Image.DecompressionBombError:
        raise ValueError(f"Image is larger than {MAX_PIXELS // 1_000_000} megapixels") from None
    with im:
        im.draft("RGB", (max_edge, max_edge))  # fast JPEG decode at reduced scale for 4K+ files
        im = ImageOps.exif_transpose(im)
        im.thumbnail((max_edge, max_edge))
        if im.mode != "RGB":
            im = im.convert("RGB")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        return buf.getvalue()


def gps(path: Path) -> tuple[float, float] | None:
    """(latitude, longitude) from EXIF, or None. Reads metadata only, no pixel decoding."""
    try:
        with Image.open(path) as im:
            info = im.getexif().get_ifd(ExifTags.IFD.GPSInfo)
    except Exception:
        return None
    try:
        def deg(v):
            d, m, s = (float(x) for x in v)
            return d + m / 60 + s / 3600

        lat, lon = deg(info[2]), deg(info[4])
        if str(info.get(1, "N")).upper().startswith("S"):
            lat = -lat
        if str(info.get(3, "E")).upper().startswith("W"):
            lon = -lon
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return round(lat, 6), round(lon, 6)
