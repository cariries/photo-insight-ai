"""Offline reverse geocoding: GPS coordinates -> landmark / area / city / country, in English and Chinese.

Place data comes from GeoNames (CC BY 4.0) and is downloaded once into <app home>/geo:
  - cities1000: every town with 1,000+ people, worldwide (English names)
  - per-country detail packs: neighbourhoods and landmarks, plus Chinese names
Lookups never touch the network.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import threading
import time
import zipfile
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

import httpx

from .config import app_home

BASE_URL = "https://download.geonames.org/export/dump/"
DEFAULT_COUNTRIES = ["HK", "MO", "TW", "JP"]
COUNTRIES_ZH = json.loads((Path(__file__).parent / "data" / "countries_zh.json").read_text(encoding="utf-8"))

# Landmark feature codes worth naming, with how far away (km) a photo can be and still be "at" it.
LANDMARKS = {
    "S": ({"MUS", "MNMT", "TMPL", "SHRN", "PGDA", "CH", "MSQE", "CSTL", "PAL", "TOWR", "STDM", "ZOO", "OBPT",
           "LTHSE", "AMTH", "THTR", "OPRA", "SQR", "GDN", "RUIN", "HSTS", "BDG", "AIRP", "RSTN", "MTRO", "MALL",
           "MKT", "UNIV", "PIER", "CTRS", "BLDG"}, 0.3),
    "L": ({"PRK", "AMUS", "RESN", "PRT", "RES"}, 0.8),
    "T": ({"PK", "MT", "HLL", "ISL", "PT", "CAPE", "BCH", "CLF", "VLC", "PEN"}, 1.5),
    "H": ({"BAY", "LK", "WTRF", "HBR", "RSV", "COVE"}, 1.5),
}
AREA_KM = 2.5         # district/neighbourhood search radius (detail packs)
AREA_MIN_POP = 10000  # ...preferring real districts over the many tiny named blocks/estates
CITY_KM = 30.0        # town search radius (cities1000), ranked by population and distance

# Language tags for Chinese names, most preferred first (Hong Kong style Traditional Chinese).
_ZH_LANGS = ["zh-HK", "yue", "zh-Hant", "zh-TW", "zh", "zh-Hans", "zh-CN"]
_HAN = re.compile(r"^[㐀-鿿豈-﫿·]+$")


def geo_dir() -> Path:
    d = app_home() / "geo"
    d.mkdir(exist_ok=True)
    return d


# Where GeoNames' Chinese name differs from what Hong Kong people actually write.
ZH_OVERRIDES = {"尖沙嘴": "尖沙咀", "尖沙嘴東": "尖沙咀東"}
_ZH_CHARS = str.maketrans({"渋": "澀", "澁": "澀"})  # Japanese forms OpenCC leaves alone


@lru_cache(maxsize=2)
def _converter(config: str):
    try:
        import opencc

        return opencc.OpenCC(config)  # building it loads dictionaries (~40 ms): do it once
    except Exception:  # pragma: no cover - conversion is a nicety
        return None


def _to_hk(text: str, japanese: bool = False) -> str:
    """Simplified (or Japanese shinjitai, e.g. 渋谷) -> Hong Kong Traditional Chinese."""
    for config in (["jp2t"] if japanese else []) + ["s2hk"]:
        c = _converter(config)
        text = c.convert(text) if c else text
    text = text.translate(_ZH_CHARS)
    return ZH_OVERRIDES.get(text, text)


# ---------------------------------------------------------------- download / build

class DownloadState:
    def __init__(self):
        self.status = "idle"  # idle | running | done | failed
        self.step = ""
        self.error = None

    def snapshot(self) -> dict:
        return dict(self.__dict__)


DOWNLOAD = DownloadState()


def _fetch(client: httpx.Client, name: str, refresh: bool = False) -> bytes:
    """Download one GeoNames file (kept under geo/raw so rebuilding or adding a country is cheap)."""
    cached = geo_dir() / "raw" / name.replace("/", "_")
    if cached.exists() and not refresh:
        return cached.read_bytes()
    for attempt in range(3):
        try:
            r = client.get(BASE_URL + name)
            r.raise_for_status()
            break
        except httpx.TransportError:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    cached.parent.mkdir(exist_ok=True)
    cached.write_bytes(r.content)
    return r.content


def _zip_lines(data: bytes, member: str):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        with z.open(member) as f:
            for line in io.TextIOWrapper(f, encoding="utf-8"):
                yield line.rstrip("\n").split("\t")


def _chinese_names(alt_rows) -> dict[str, str]:
    """geonameid -> best Traditional Chinese name, from a GeoNames alternatenames file."""
    best: dict[str, tuple[tuple, str, bool]] = {}
    for r in alt_rows:
        if len(r) < 8 or r[7] == "1" or r[6] == "1":  # historic / colloquial
            continue
        lang, name = r[2], r[3]
        if lang in _ZH_LANGS:
            rank = (_ZH_LANGS.index(lang), r[4] != "1", len(name))
        elif lang == "ja" and _HAN.match(name):  # kanji-only Japanese name as a last resort
            rank = (len(_ZH_LANGS), r[4] != "1", len(name))
        else:
            continue
        if r[1] not in best or rank < best[r[1]][0]:
            best[r[1]] = (rank, name, lang == "ja")
    return {gid: _to_hk(name, japanese) for gid, (_, name, japanese) in best.items()}


def download(countries: list[str] | None = None, refresh: bool = False) -> dict:
    """Download GeoNames data and build the local index. Safe to re-run (replaces the index).

    refresh=True re-downloads files already fetched (to pick up GeoNames updates).
    """
    countries = sorted({c.strip().upper() for c in (countries or DEFAULT_COUNTRIES) if re.fullmatch(r"\s*[A-Za-z]{2}\s*", c)})
    st = DOWNLOAD
    st.status, st.error = "running", None
    try:
        with httpx.Client(timeout=120, follow_redirects=True) as client:  # honours HTTPS_PROXY
            st.step = "cities1000.zip"
            cities = _fetch(client, "cities1000.zip", refresh)
            st.step = "admin1CodesASCII.txt"
            admin1_raw = _fetch(client, "admin1CodesASCII.txt", refresh).decode("utf-8")
            packs = {}
            for cc in countries:
                st.step = f"{cc}.zip"
                main = _fetch(client, f"{cc}.zip", refresh)
                st.step = f"alternatenames/{cc}.zip"
                alt = _fetch(client, f"alternatenames/{cc}.zip", refresh)
                packs[cc] = (main, alt)

        st.step = "building index"
        zh: dict[str, str] = {}
        for cc, (_, alt) in packs.items():
            zh.update(_chinese_names(_zip_lines(alt, f"{cc}.txt")))

        rows = []  # kind, lat, lon, cc, admin1, fclass, fcode, population, name_en, name_zh
        for r in _zip_lines(cities, "cities1000.txt"):
            rows.append(("city", r[4], r[5], r[8], r[10], r[6], r[7], r[14] or "0", r[1], zh.get(r[0], "")))
        for cc, (main, _) in packs.items():
            for r in _zip_lines(main, f"{cc}.txt"):
                fclass, fcode = r[6], r[7]
                if fclass == "P" or (fclass == "A" and fcode in ("ADM2", "ADM3")):
                    kind = "area"
                elif fclass in LANDMARKS and fcode in LANDMARKS[fclass][0]:
                    kind = "landmark"
                else:
                    continue
                rows.append((kind, r[4], r[5], r[8], r[10], fclass, fcode, r[14] or "0", r[1], zh.get(r[0], "")))

        admin1 = []
        for line in admin1_raw.splitlines():
            code, name, _, gid = line.split("\t")
            admin1.append((code, name, zh.get(gid, "")))

        d = geo_dir()
        with open(d / "places.tsv.tmp", "w", encoding="utf-8", newline="") as f:
            csv.writer(f, delimiter="\t", lineterminator="\n").writerows(rows)
        with open(d / "admin1.tsv", "w", encoding="utf-8", newline="") as f:
            csv.writer(f, delimiter="\t", lineterminator="\n").writerows(admin1)
        (d / "places.tsv.tmp").replace(d / "places.tsv")
        meta = {"countries": countries, "built": time.strftime("%Y-%m-%d"), "places": len(rows)}
        (d / "meta.json").write_text(json.dumps(meta))
        reset()
        st.status, st.step = "done", ""
        return meta
    except Exception as e:
        st.status, st.error = "failed", f"{type(e).__name__}: {e}"
        raise


def status() -> dict:
    meta = geo_dir() / "meta.json"
    info = json.loads(meta.read_text()) if meta.exists() else None
    return {"available": info is not None, "info": info, "download": DOWNLOAD.snapshot(),
            "default_countries": DEFAULT_COUNTRIES}


def download_async(countries: list[str] | None) -> None:
    if DOWNLOAD.status == "running":
        raise RuntimeError("Download already running")
    DOWNLOAD.status = "running"

    def run():
        try:
            download(countries)
        except Exception:
            pass  # recorded in DOWNLOAD

    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- lookup

@dataclass
class Name:
    en: str
    zh: str = ""


@dataclass
class Place:
    country_code: str
    country: Name
    region: Name | None = None
    city: Name | None = None
    area: Name | None = None
    landmark: Name | None = None
    landmark_km: float | None = None

    def parts(self, include_landmark: bool = False) -> list[Name]:
        """Most specific first, duplicates removed (e.g. city 'Hong Kong' and country 'Hong Kong')."""
        out, seen = [], set()
        for n in ([self.landmark] if include_landmark else []) + [self.area, self.city, self.country]:
            if n and n.en.lower() not in seen:
                seen.add(n.en.lower())
                out.append(n)
        return out

    def describe(self) -> str:
        text = ", ".join(n.en for n in self.parts())
        if self.landmark:
            text += f" (near {self.landmark.en})"
        return text

    def to_dict(self) -> dict:
        d = asdict(self)
        d["label_en"] = ", ".join(n.en for n in self.parts())
        d["label_zh"] = "，".join(n.zh or n.en for n in self.parts())
        return d


def _km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(min(1.0, math.sqrt(a)))


class Index:
    CELL = 0.5  # degrees (~55 km), so a 30 km search only needs neighbouring cells

    def __init__(self, folder: Path):
        self.cells: dict[tuple[int, int], list[tuple]] = {}
        with open(folder / "places.tsv", encoding="utf-8", newline="") as f:
            for kind, lat, lon, cc, a1, fclass, fcode, pop, en, zh in csv.reader(f, delimiter="\t"):
                lat, lon = float(lat), float(lon)
                self.cells.setdefault(self._cell(lat, lon), []).append(
                    (kind, lat, lon, cc, a1, fclass, en, zh, int(pop or 0)))
        self.admin1 = {}
        with open(folder / "admin1.tsv", encoding="utf-8", newline="") as f:
            for code, en, zh in csv.reader(f, delimiter="\t"):
                self.admin1[code] = Name(en, zh)

    def _cell(self, lat, lon):
        return (math.floor(lat / self.CELL), math.floor(lon / self.CELL))

    def lookup(self, lat: float, lon: float) -> Place | None:
        cy, cx = self._cell(lat, lon)
        # Lower score wins. Kinds: city | area (significant district) | small (fallback area) | landmark
        best: dict[str, tuple[float, tuple, float]] = {}

        def offer(kind, score, row, d):
            if kind not in best or score < best[kind][0]:
                best[kind] = (score, row, d)

        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for row in self.cells.get((cy + dy, cx + dx), ()):
                    kind, plat, plon, _, _, fclass, _, _, pop = row
                    d = _km(lat, lon, plat, plon)
                    if kind == "city" and d <= CITY_KM:
                        offer("city", -max(pop, 1) / (d + 1) ** 2, row, d)  # big nearby city beats tiny closer town
                    elif kind == "area" and d <= AREA_KM:
                        if pop >= AREA_MIN_POP:
                            offer("area", d, row, d)
                        elif pop > 0 and d <= 1.0:
                            offer("small", d, row, d)
                    elif kind == "landmark" and d <= LANDMARKS[fclass][1]:
                        offer("landmark", d / LANDMARKS[fclass][1], row, d)  # relative to the feature's reach
        if "area" not in best and "small" in best:
            best["area"] = best["small"]
        anchor = best.get("area") or best.get("city") or best.get("landmark")
        if not anchor:
            return None
        cc, a1 = anchor[1][3], anchor[1][4]
        name = lambda k: Name(best[k][1][6], best[k][1][7]) if k in best else None  # noqa: E731
        return Place(
            country_code=cc,
            country=Name(_COUNTRY_EN.get(cc, cc), COUNTRIES_ZH.get(cc, "")),
            region=self.admin1.get(f"{cc}.{a1}"),
            city=name("city"),
            area=name("area"),
            landmark=name("landmark"),
            landmark_km=round(best["landmark"][2], 2) if "landmark" in best else None,
        )


# English country names for the handful GeoNames calls differently from how people tag them.
_COUNTRY_EN = {
    "HK": "Hong Kong", "MO": "Macau", "TW": "Taiwan", "CN": "China", "JP": "Japan", "KR": "South Korea",
    "US": "USA", "GB": "UK", "TH": "Thailand", "SG": "Singapore", "MY": "Malaysia", "VN": "Vietnam",
    "FR": "France", "IT": "Italy", "ES": "Spain", "DE": "Germany", "CH": "Switzerland", "AU": "Australia",
    "NZ": "New Zealand", "CA": "Canada", "ID": "Indonesia", "PH": "Philippines", "IS": "Iceland",
    "NO": "Norway", "GR": "Greece", "PT": "Portugal", "NL": "Netherlands", "AT": "Austria", "CZ": "Czechia",
    "TR": "Turkey", "AE": "UAE", "IN": "India", "MV": "Maldives", "KH": "Cambodia", "MM": "Myanmar",
}

def hashtags(place: Place | None, landmark: bool = False, chinese: bool = False) -> list[str]:
    """Location hashtags, most specific first: ['shibuya', 'tokyo', 'japan', '澀谷區', '東京', '日本']."""
    if place is None:
        return []
    parts = place.parts(include_landmark=landmark)
    tags = [_tag_en(n.en) for n in parts]
    if chinese:
        tags += [n.zh for n in parts if n.zh]
    return [t for t in tags if t]


_SUFFIX = re.compile(r"(?i)[\s-]+(ku|shi|district|city|ward|prefecture)$")


def _tag_en(name: str) -> str:
    import unicodedata

    name = _SUFFIX.sub("", name)
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()  # Dōgenzaka -> Dogenzaka
    return re.sub(r"[^a-z0-9]", "", ascii_.lower())


_index: Index | None = None
_index_lock = threading.Lock()


def reset() -> None:
    global _index
    with _index_lock:
        _index = None
    _cached_lookup.cache_clear()


def index() -> Index | None:
    global _index
    if _index is None and (geo_dir() / "places.tsv").exists():
        with _index_lock:
            if _index is None:
                _index = Index(geo_dir())
    return _index


@lru_cache(maxsize=4096)
def _cached_lookup(lat: float, lon: float) -> Place | None:
    idx = index()
    return idx.lookup(lat, lon) if idx else None


def lookup(lat: float | None, lon: float | None) -> Place | None:
    if lat is None or lon is None:
        return None
    return _cached_lookup(round(lat, 5), round(lon, 5))  # ~1 m; keeps the cache effective
