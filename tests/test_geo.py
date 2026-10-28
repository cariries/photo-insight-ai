import json
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image

from photo_insight import export, geo, images
from photo_insight.config import Config
from photo_insight.llm import Analysis
from photo_insight.store import Store, record, signature


@pytest.fixture
def places(tmp_path, monkeypatch):
    """A tiny offline place index around Tsim Sha Tsui and Shibuya (no network)."""
    monkeypatch.setenv("PHOTO_INSIGHT_HOME", str(tmp_path / "home"))
    geo.reset()
    d = geo.geo_dir()
    rows = [  # kind, lat, lon, cc, admin1, fclass, fcode, pop, en, zh
        ("city", 22.27966, 114.16281, "HK", "", "P", "PPLC", 7396076, "Hong Kong", "香港"),
        ("city", 22.29436, 114.17166, "HK", "KYT", "P", "PPL", 24557, "Tsim Sha Tsui", "尖沙咀"),
        ("area", 22.29436, 114.17166, "HK", "KYT", "P", "PPL", 24557, "Tsim Sha Tsui", "尖沙咀"),
        ("area", 22.2931, 114.1723, "HK", "KYT", "P", "PPL", 4002, "The Waterfront", ""),
        ("landmark", 22.2935, 114.1719, "HK", "KYT", "S", "MUS", 0, "Hong Kong Museum of Art", "香港藝術館"),
        ("city", 35.6895, 139.69171, "JP", "40", "P", "PPLC", 9733276, "Tokyo", "東京"),
        ("city", 35.6565, 139.6985, "JP", "40", "P", "PPL", 1818, "Sakuragaokachō", ""),
        ("area", 35.6618, 139.704, "JP", "40", "A", "ADM2", 243883, "Shibuya-ku", "澀谷區"),
    ]
    with open(d / "places.tsv", "w", encoding="utf-8") as f:
        f.writelines("\t".join(map(str, r)) + "\n" for r in rows)
    (d / "admin1.tsv").write_text("HK.KYT\tYau Tsim Mong\t油尖旺區\nJP.40\tTokyo\t東京都\n", encoding="utf-8")
    (d / "meta.json").write_text(json.dumps({"countries": ["HK", "JP"], "built": "2026-10-27", "places": len(rows)}))
    yield d
    geo.reset()


def geotagged(path: Path, lat: float, lon: float) -> Path:
    def dms(x):
        x = abs(x); d = int(x); m = int((x - d) * 60)
        return (Fraction(d), Fraction(m), Fraction(round(((x - d) * 60 - m) * 60, 4)).limit_denominator(10000))
    im = Image.new("RGB", (32, 24))
    ex = im.getexif()
    ex[0x8825] = {1: "N" if lat >= 0 else "S", 2: dms(lat), 3: "E" if lon >= 0 else "W", 4: dms(lon)}
    im.save(path, exif=ex)
    return path


def test_read_gps(tmp_path):
    p = geotagged(tmp_path / "a.jpg", -33.8568, 151.2153)  # southern/eastern hemisphere signs
    assert images.gps(p) == pytest.approx((-33.8568, 151.2153), abs=1e-4)
    Image.new("RGB", (8, 8)).save(tmp_path / "b.jpg")
    assert images.gps(tmp_path / "b.jpg") is None


def test_lookup_prefers_big_city_and_real_district(places):
    p = geo.lookup(22.2936, 114.1719)
    assert (p.city.en, p.area.en, p.landmark.zh, p.region.zh) == ("Hong Kong", "Tsim Sha Tsui", "香港藝術館", "油尖旺區")
    assert p.to_dict()["label_zh"] == "尖沙咀，香港"
    assert geo.hashtags(p, chinese=True) == ["tsimshatsui", "hongkong", "尖沙咀", "香港"]
    assert "hongkongmuseumofart" in geo.hashtags(p, landmark=True)

    jp = geo.lookup(35.6595, 139.7005)
    assert (jp.city.en, jp.area.en, jp.country.zh) == ("Tokyo", "Shibuya-ku", "日本")
    assert geo.hashtags(jp)[:2] == ["shibuya", "tokyo"]  # "-ku" suffix dropped
    assert geo.lookup(0.5, -160.0) is None  # middle of the ocean


def test_chinese_name_preference_and_conversion():
    alt = [  # id, geonameid, lang, name, preferred, short, colloquial, historic
        ["1", "100", "zh", "尖沙嘴", "", "", "", ""],
        ["2", "100", "zh-CN", "尖沙咀", "", "", "", ""],
        ["3", "200", "zh", "九龙", "", "", "", ""],
        ["4", "300", "ja", "渋谷区", "", "", "", ""],
        ["5", "300", "ja", "しぶや", "", "", "", ""],
        ["6", "400", "zh-HK", "旺角", "", "", "", "1"],  # historic: ignored
    ]
    names = geo._chinese_names(alt)
    assert names == {"100": "尖沙咀", "200": "九龍", "300": "澀谷區"}


def test_record_xmp_and_csv_include_location(places, tmp_path):
    cfg = Config()
    store = Store(tmp_path / "db.sqlite")
    p = geotagged(tmp_path / "tst.jpg", 22.2936, 114.1719)
    assert store.ensure_gps(p) == pytest.approx((22.2936, 114.1719), abs=1e-4)
    r = record(p, store.get(p), cfg)
    assert r["status"] == "pending" and r["location"]["label_en"] == "Tsim Sha Tsui, Hong Kong"

    store.save_analysis(p, Analysis("Night", 0.9, "Harbour lights", ["cityscape", "hongkong"]), "m", signature(cfg), False)
    r = record(p, store.get(p), cfg)
    assert r["hashtags"][:4] == ["nightphotography", "longexposure", "tsimshatsui", "hongkong"]
    cfg.settings.location_tags = False
    assert "tsimshatsui" not in record(p, store.get(p), cfg)["hashtags"]

    export.write_sidecars([r])
    xmp = p.with_suffix(".xmp").read_text(encoding="utf-8")
    assert "<photoshop:City>Hong Kong</photoshop:City>" in xmp
    assert "<Iptc4xmpCore:Location>Tsim Sha Tsui</Iptc4xmpCore:Location>" in xmp
    assert "尖沙咀，香港" in export.to_csv([r])


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE photos (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, category TEXT, confidence REAL,"
                " caption TEXT, model_tags TEXT, model TEXT, signature TEXT, analyzed_at REAL, error TEXT,"
                " user_category TEXT, user_tags TEXT, user_caption TEXT)")
    con.commit(); con.close()
    store = Store(db)
    p = geotagged(tmp_path / "x.jpg", 22.3, 114.17)
    assert store.ensure_gps(p) is not None


def _rec(path, loc=None, status="done", category="Food"):
    return {"path": str(path), "name": Path(path).name, "status": status, "category": category, "location": loc}


HK = {"country": {"en": "Hong Kong", "zh": "香港"}, "city": {"en": "Hong Kong", "zh": "香港"},
      "area": {"en": "Tsim Sha Tsui", "zh": "尖沙咀"}, "region": None, "landmark": None, "country_code": "HK"}
JP = {"country": {"en": "Japan", "zh": "日本"}, "city": {"en": "Tokyo", "zh": "東京"},
      "area": {"en": "Shibuya-ku", "zh": ""}, "region": None, "landmark": None, "country_code": "JP"}


def test_location_folders():
    assert export.folder_for(_rec("a.jpg", HK), "location", "area") == ["Hong Kong", "Tsim Sha Tsui"]  # no HK/HK
    assert export.folder_for(_rec("a.jpg", HK), "location", "area", "zh") == ["香港", "尖沙咀"]
    assert export.folder_for(_rec("a.jpg", JP), "location", "area", "zh") == ["日本", "東京", "Shibuya-ku"]  # zh fallback
    assert export.folder_for(_rec("a.jpg", JP), "location", "country") == ["Japan"]
    assert export.folder_for(_rec("a.jpg", JP), "location_category") == ["Japan", "Tokyo", "Food"]
    assert export.folder_for(_rec("a.jpg", JP), "category_location") == ["Food", "Japan", "Tokyo"]
    assert export.folder_for(_rec("a.jpg"), "location", names="zh") == ["未知地點"]
    pending = _rec("b.jpg", JP, status="pending", category=None)
    assert export.folder_for(pending, "location") == ["Japan", "Tokyo"]  # no analysis needed
    assert export.folder_for(pending, "location_category") is None
    with pytest.raises(ValueError):
        export.folder_for(_rec("a.jpg"), "colour")


def test_plan_and_organize_by_location(tmp_path):
    srcs = [tmp_path / f"{n}.jpg" for n in ("a", "b", "c")]
    for s in srcs:
        Image.new("RGB", (8, 8)).save(s)
    recs = [_rec(srcs[0], HK), _rec(srcs[1], JP, status="pending", category=None), _rec(srcs[2])]
    plan = export.plan(recs, "location", "city", "zh")
    assert dict(plan["folders"]) == {"香港": 1, "日本/東京": 1, "未知地點": 1} and plan["skipped"] == 0
    assert export.plan(recs, "location_category")["skipped"] == 1
    pairs = export.organize(recs, tmp_path / "out", "copy", "location", "area", "zh")
    assert sorted(str(t.relative_to(tmp_path / "out")) for _, t in pairs) == ["日本/東京/Shibuya-ku/b.jpg", "未知地點/c.jpg", "香港/尖沙咀/a.jpg"]
    assert all(s.exists() for s in srcs)  # copy keeps originals
