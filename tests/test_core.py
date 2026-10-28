import json
from pathlib import Path

import pytest
from PIL import Image

from photo_insight import export, hashtags, images
from photo_insight.config import UNCATEGORIZED, Config, load_config, save_config
from photo_insight.llm import Analysis, parse
from photo_insight.store import Store, record


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_INSIGHT_HOME", str(tmp_path / "home"))


def make_photo(path: Path, size=(4000, 3000)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (120, 160, 200)).save(path)
    return path


def test_normalize_and_merge():
    assert hashtags.normalize("#Golden Hour!") == "goldenhour"
    assert hashtags.normalize("香港") == "香港"
    tags = hashtags.merge(["landscapephotography"], ["#LandscapePhotography", "sunset", "love", "2024"], [], 10)
    assert tags == ["landscapephotography", "sunset"]
    assert len(hashtags.merge([], [f"t{i}" for i in range(50)], hashtags.GENERAL_HASHTAGS, 20)) == 20


def test_parse_maps_unknown_category_and_clamps():
    cfg = Config()
    a = parse(json.dumps({"category": "landscape", "confidence": 3, "caption": " hi ", "hashtags": "a, b"}), cfg.categories)
    assert (a.category, a.confidence, a.caption, a.tags) == ("Landscape", 1.0, "hi", ["a", "b"])
    assert parse('{"category":"Spaceships","confidence":0.9,"caption":"","hashtags":[]}', cfg.categories).category == UNCATEGORIZED


def test_config_roundtrip_and_validation():
    cfg = load_config()
    cfg.categories[0].name = "Hiking"
    save_config(cfg)
    assert load_config().categories[0].name == "Hiking"
    with pytest.raises(ValueError):
        Config.from_dict({"categories": []})


def test_resize_caps_longest_edge(tmp_path):
    from io import BytesIO
    p = make_photo(tmp_path / "big.jpg", (3840, 2160))
    im = Image.open(BytesIO(images.jpeg_bytes(p, 1920)))
    assert max(im.size) == 1920


def test_scan_skips_hidden_and_sorted(tmp_path):
    make_photo(tmp_path / "a.jpg", (10, 10))
    make_photo(tmp_path / "sub" / "b.png", (10, 10))
    make_photo(tmp_path / "_sorted" / "Food" / "c.jpg", (10, 10))
    make_photo(tmp_path / ".cache" / "d.jpg", (10, 10))
    (tmp_path / "notes.txt").write_text("x")
    assert [p.name for p in images.scan(tmp_path)] == ["a.jpg", "b.png"]


def test_store_overrides_low_confidence_and_export(tmp_path):
    cfg = Config()
    store = Store(tmp_path / "db.sqlite")
    p = make_photo(tmp_path / "photos" / "x.jpg", (10, 10))
    from photo_insight.store import signature
    sig = signature(cfg)
    assert store.needs_analysis(p, sig)
    store.save_analysis(p, Analysis("Food", 0.9, "Yum", ["ramen", "#Foodie"]), "m", sig, False)
    assert not store.needs_analysis(p, sig)
    r = record(p, store.get(p), cfg)
    assert r["category"] == "Food" and r["hashtags"][:3] == ["foodphotography", "foodie", "ramen"]

    store.set_overrides(p, category="Night", tags=["#Neon", "neon", "city"])
    r = record(p, store.get(p), cfg)
    assert (r["category"], r["hashtags"], r["edited"]) == ("Night", ["neon", "city"], True)

    store.save_analysis(p, Analysis("Food", 0.1, "?", []), "m", sig, True)
    assert record(p, store.get(p), cfg)["category"] == UNCATEGORIZED

    store.set_overrides(p, category="Food")
    r = record(p, store.get(p), cfg)
    assert export.write_sidecars([r]) == {"written": 1, "skipped_existing": 0}
    assert "<rdf:li>foodphotography</rdf:li>" in p.with_suffix(".xmp").read_text()
    pairs = export.organize([r], tmp_path / "out", "copy")
    assert pairs[0][1] == tmp_path / "out" / "Food" / "x.jpg" and p.exists()
    assert (tmp_path / "out" / "Food" / "x.xmp").exists()
    assert "Food" in export.to_csv([r])


def test_sidecar_never_overwrites_foreign_xmp(tmp_path):
    p = make_photo(tmp_path / "y.jpg", (10, 10))
    p.with_suffix(".xmp").write_text("<lightroom edits/>")
    r = {"status": "done", "path": str(p), "caption": "", "hashtags": ["a"], "category": "Food"}
    assert export.write_sidecars([r]) == {"written": 0, "skipped_existing": 1}
    assert p.with_suffix(".xmp").read_text() == "<lightroom edits/>"


def test_cantonese_prompt_and_chinese_tags():
    from photo_insight.llm import build_prompt
    cfg = Config()
    p = build_prompt(cfg.categories, "Cantonese", chinese_tags=True)
    assert "Cantonese" in p and "Traditional Chinese hashtags" in p
    assert "Traditional Chinese hashtags" not in build_prompt(cfg.categories, "English")
    assert hashtags.general_for(True, True)[-3:] == hashtags.GENERAL_HASHTAGS_ZH
    assert hashtags.general_for(True, False) == hashtags.GENERAL_HASHTAGS
    assert hashtags.merge(["風景攝影"], ["#香港", "sunset"], [], 5) == ["風景攝影", "香港", "sunset"]


def test_csv_has_bom_for_excel():
    r = {"status": "done", "name": "a.jpg", "path": "/a.jpg", "category": "風景", "confidence": 0.9,
         "caption": "好靚", "hashtags": ["攝影"]}
    assert export.to_csv([r]).startswith("\ufeff") and "#攝影" in export.to_csv([r])


def test_large_images_allowed_and_oversize_message(tmp_path, monkeypatch):
    assert Image.MAX_IMAGE_PIXELS >= 200_000_000  # 200 MP phone photos must open
    p = make_photo(tmp_path / "huge.jpg", (1000, 1000))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)  # simulate a file far over the limit
    with pytest.raises(ValueError, match="megapixels"):
        images.jpeg_bytes(p, 1920)
