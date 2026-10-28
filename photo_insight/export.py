"""Applying results: sort into category folders, XMP sidecars, CSV."""
from __future__ import annotations

import csv
import io
import re
import shutil
from pathlib import Path
from xml.sax.saxutils import escape

from .hashtags import format_tags

_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def safe_folder_name(name: str) -> str:
    return _UNSAFE.sub("-", name).strip(" .") or "Unnamed"


def _free_path(target: Path) -> Path:
    if not target.exists():
        return target
    i = 1
    while (alt := target.with_name(f"{target.stem} ({i}){target.suffix}")).exists():
        i += 1
    return alt


GROUPS = ("category", "location", "location_category", "category_location")
LEVELS = ("country", "city", "area")
NO_LOCATION = {"en": "No location", "zh": "未知地點"}


def location_parts(rec: dict, level: str = "city", names: str = "en") -> list[str]:
    """Nested folder names for a photo's place, e.g. ['Japan', 'Tokyo', 'Shibuya-ku'], down to `level`.

    Repeats are skipped, so Hong Kong is 'Hong Kong/Tsim Sha Tsui', not 'Hong Kong/Hong Kong/Tsim Sha Tsui'.
    """
    loc = rec.get("location")
    if not loc:
        return [NO_LOCATION.get(names, NO_LOCATION["en"])]
    parts, seen = [], set()
    for key in LEVELS[: LEVELS.index(level) + 1]:
        n = loc.get(key)
        if not n or n["en"].lower() in seen:
            continue
        seen.add(n["en"].lower())
        parts.append((n.get("zh") or n["en"]) if names == "zh" else n["en"])
    return parts


def folder_for(rec: dict, group: str = "category", level: str = "city", names: str = "en") -> list[str] | None:
    """Folder path (as parts) under the destination, or None if the photo can't be sorted this way yet."""
    if group not in GROUPS:
        raise ValueError(f"group must be one of {', '.join(GROUPS)}")
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    needs_category = group != "location"
    if needs_category and rec["status"] != "done":
        return None
    place = location_parts(rec, level, names)
    if group == "category":
        parts = [rec["category"]]
    elif group == "location":
        parts = place
    elif group == "location_category":
        parts = place + [rec["category"]]
    else:
        parts = [rec["category"]] + place
    return [safe_folder_name(p) for p in parts]


def plan(records: list[dict], group: str = "category", level: str = "city", names: str = "en") -> dict:
    """Dry run: how many photos land in each folder, and how many can't be sorted yet."""
    folders: dict[str, int] = {}
    skipped = 0
    for r in records:
        parts = folder_for(r, group, level, names)
        if parts is None:
            skipped += 1
            continue
        key = "/".join(parts)
        folders[key] = folders.get(key, 0) + 1
    return {"folders": sorted(folders.items(), key=lambda kv: (-kv[1], kv[0])), "skipped": skipped}


def organize(records: list[dict], dest_root: Path, mode: str = "copy", group: str = "category",
             level: str = "city", names: str = "en") -> list[tuple[Path, Path]]:
    """Copy or move photos into dest_root/<folders by category and/or place>/. Returns (source, target) pairs."""
    if mode not in ("copy", "move"):
        raise ValueError("mode must be 'copy' or 'move'")
    moved = []
    for r in records:
        parts = folder_for(r, group, level, names)
        if parts is None:
            continue
        src = Path(r["path"])
        folder = dest_root.joinpath(*parts)
        folder.mkdir(parents=True, exist_ok=True)
        if src.parent == folder:
            continue
        target = _free_path(folder / src.name)
        if mode == "copy":
            shutil.copy2(src, target)
        else:
            shutil.move(src, target)
        sidecar = xmp_path(src)
        if sidecar.exists():
            (shutil.copy2 if mode == "copy" else shutil.move)(sidecar, xmp_path(target))
        moved.append((src, target))
    return moved


# ---------------------------------------------------------------- embedding (exiftool)

EMBED_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff", ".png", ".webp"}


def exiftool_path() -> str | None:
    found = shutil.which("exiftool")
    if found:
        return found
    for p in ("/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool"):  # GUI launches may lack Homebrew's PATH
        if Path(p).exists():
            return p
    return None


_BASE_ARGS = ("-overwrite_original", "-P", "-charset", "iptc=UTF8", "-IPTC:CodedCharacterSet=UTF8")


def embed_args(rec: dict) -> list[str]:
    """exiftool arguments writing keywords, caption, category and location as XMP + IPTC.

    Keywords are removed-then-added so ones already in the file aren't duplicated; existing keywords stay.
    """
    one_line = lambda v: " ".join(str(v).split())  # noqa: E731  (argfiles are line based)
    a = list(_BASE_ARGS)
    for t in rec.get("hashtags") or []:
        for tag in ("XMP-dc:Subject", "IPTC:Keywords"):
            a += [f"-{tag}-={t}", f"-{tag}+={t}"]
    if rec.get("caption"):
        cap = one_line(rec["caption"])
        a += [f"-XMP-dc:Description={cap}", f"-IPTC:Caption-Abstract={cap}"]
    if rec.get("status") == "done" and rec.get("category"):
        h = f"Photo Insight|{one_line(rec['category'])}"
        a += [f"-XMP-lr:HierarchicalSubject-={h}", f"-XMP-lr:HierarchicalSubject+={h}"]
    loc = rec.get("location") or {}
    en = lambda k: (loc.get(k) or {}).get("en")  # noqa: E731
    fields = {
        ("XMP-photoshop:City", "IPTC:City"): en("city") or en("area"),
        ("XMP-photoshop:State", "IPTC:Province-State"): en("region"),
        ("XMP-photoshop:Country", "IPTC:Country-PrimaryLocationName"): en("country"),
        ("XMP-iptcCore:CountryCode", "IPTC:Country-PrimaryLocationCode"): loc.get("country_code"),
        ("XMP-iptcCore:Location", "IPTC:Sub-location"): en("area") or en("landmark"),
    }
    for tags, value in fields.items():
        if value:
            a += [f"-{t}={one_line(value)}" for t in tags]
    return a


def embed_metadata(items: list[tuple[Path, dict]]) -> dict:
    """Write metadata into the given files (sorted copies) with one exiftool run. Pixels are not re-encoded."""
    import subprocess
    import tempfile

    tool = exiftool_path()
    if not tool:
        raise RuntimeError("exiftool is not installed (brew install exiftool)")
    supported = [(p, r) for p, r in items if p.suffix.lower() in EMBED_EXT]
    todo = [(p, r) for p, r in supported if len(embed_args(r)) > len(_BASE_ARGS)]  # skip: nothing to write
    result = {"embedded": 0, "failed": 0, "unsupported": len(items) - len(supported),
              "nothing": len(supported) - len(todo), "errors": []}
    if not todo:
        return result
    lines: list[str] = []
    for path, rec in todo:
        lines += embed_args(rec) + [str(path), "-execute"]
    with tempfile.TemporaryDirectory() as tmp:  # removed automatically afterwards
        argfile = Path(tmp) / "embed.args"
        argfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
        proc = subprocess.run([tool, "-@", str(argfile)], capture_output=True, text=True, timeout=600)
    result["embedded"] = sum(int(n) for n in re.findall(r"(\d+) image files? updated", proc.stdout))
    result["failed"] = len(todo) - result["embedded"]
    result["errors"] = [ln for ln in proc.stderr.splitlines() if ln.startswith("Error")][:5]
    return result


def xmp_path(photo: Path) -> Path:
    return photo.with_suffix(".xmp")  # Lightroom / Capture One convention


def _location_xml(loc: dict | None) -> str:
    """IPTC/Photoshop location fields, which Lightroom and Capture One show as City / State / Country."""
    if not loc:
        return ""
    fields = [
        ("photoshop:City", (loc.get("city") or loc.get("area") or {}).get("en")),
        ("photoshop:State", (loc.get("region") or {}).get("en")),
        ("photoshop:Country", (loc.get("country") or {}).get("en")),
        ("Iptc4xmpCore:CountryCode", loc.get("country_code")),
        ("Iptc4xmpCore:Location", (loc.get("area") or loc.get("landmark") or {}).get("en")),
    ]
    return "".join(f"\n   <{k}>{escape(v)}</{k}>" for k, v in fields if v)


def xmp_document(caption: str, tags: list[str], category: str, location: dict | None = None) -> str:
    subjects = "".join(f"<rdf:li>{escape(t)}</rdf:li>" for t in tags)
    return f"""<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Photo Insight">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:lr="http://ns.adobe.com/lightroom/1.0/"
    xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/"
    xmlns:Iptc4xmpCore="http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/">
   <dc:description><rdf:Alt><rdf:li xml:lang="x-default">{escape(caption)}</rdf:li></rdf:Alt></dc:description>
   <dc:subject><rdf:Bag>{subjects}</rdf:Bag></dc:subject>
   <lr:hierarchicalSubject><rdf:Bag><rdf:li>Photo Insight|{escape(category)}</rdf:li></rdf:Bag></lr:hierarchicalSubject>{_location_xml(location)}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def write_sidecars(records: list[dict], overwrite: bool = False) -> dict:
    """Write keyword sidecars. Existing sidecars are left alone unless we wrote them (they may hold edits)."""
    written, skipped = 0, 0
    for r in records:
        if r["status"] != "done":
            continue
        target = xmp_path(Path(r["path"]))
        if target.exists() and not (overwrite or 'x:xmptk="Photo Insight"' in target.read_text(errors="ignore")):
            skipped += 1
            continue
        target.write_text(xmp_document(r["caption"], r["hashtags"], r["category"], r.get("location")), encoding="utf-8")
        written += 1
    return {"written": written, "skipped_existing": skipped}


def to_csv(records: list[dict]) -> str:
    buf = io.StringIO()
    buf.write("\ufeff")  # BOM so Excel opens Chinese captions/hashtags as UTF-8
    w = csv.writer(buf)
    w.writerow(["file", "path", "category", "confidence", "caption", "hashtags",
                "location", "location_zh", "latitude", "longitude"])
    for r in records:
        if r["status"] == "done":
            loc, gps = r.get("location") or {}, r.get("gps") or ["", ""]
            w.writerow([r["name"], r["path"], r["category"], f'{r["confidence"]:.2f}', r["caption"],
                        format_tags(r["hashtags"]), loc.get("label_en", ""), loc.get("label_zh", ""), *gps])
    return buf.getvalue()
