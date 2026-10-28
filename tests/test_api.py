import importlib

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pathlib import Path

from photo_insight.llm import Analysis

H = {"X-Photo-Insight": "1"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PHOTO_INSIGHT_HOME", str(tmp_path / "home"))
    from photo_insight import server
    importlib.reload(server)
    return TestClient(server.app), server


def photo(path, color=(10, 20, 30)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), color).save(path)
    return str(path)


def test_choose_files_append_and_skip_unsupported(client, tmp_path):
    c, _ = client
    a = photo(tmp_path / "trip" / "a.jpg")
    b = photo(tmp_path / "home2" / "b.png")
    raw = tmp_path / "trip" / "c.cr3"
    raw.write_bytes(b"raw")
    r = c.post("/api/files", json={"paths": [a, str(raw)]}, headers=H).json()
    assert (r["source"], r["skipped"], [p["name"] for p in r["photos"]]) == ("files", 1, ["a.jpg"])
    r = c.post("/api/files", json={"paths": [b, a], "append": True}, headers=H).json()
    assert [p["name"] for p in r["photos"]] == ["a.jpg", "b.png"]
    assert r["folder"] == str(tmp_path)  # common parent, used for the default _sorted destination
    assert c.get("/api/thumb", params={"path": b, "size": 1600}).status_code == 200


def test_actions_apply_to_selection_only(client, tmp_path):
    c, server = client
    paths = [photo(tmp_path / f"p{i}.jpg", (i * 40, 0, 0)) for i in range(3)]
    c.post("/api/folder", json={"path": str(tmp_path)}, headers=H)
    from photo_insight.store import signature
    sig = signature(server.S.cfg)
    for p in paths:
        server.S.store.save_analysis(Path(p), Analysis("Food", 0.9, "x", ["a"]), "m", sig, False)

    r = c.post("/api/sidecars", json={"paths": [paths[1]]}, headers=H).json()
    assert r["written"] == 1
    assert [Path(p).with_suffix(".xmp").exists() for p in paths] == [False, True, False]

    csv = c.post("/api/export.csv", json={"paths": paths[:2]}, headers=H).text
    assert csv.count("\n") == 3  # header + 2 rows

    r = c.post("/api/organize", json={"mode": "copy", "paths": [paths[2]]}, headers=H).json()
    assert r["count"] == 1 and (tmp_path / "_sorted" / "Food" / "p2.jpg").exists()

    assert server.S.subset(None) == server.S.photos
    assert server.S.subset(["/not/known.jpg"]) == [] and server.S.subset([]) == []


def test_remove_and_clear_list_keeps_files(client, tmp_path):
    c, _ = client
    paths = [photo(tmp_path / f"r{i}.jpg") for i in range(3)]
    c.post("/api/folder", json={"path": str(tmp_path)}, headers=H)
    r = c.post("/api/remove", json={"paths": [paths[0]]}, headers=H).json()
    assert r["removed"] == 1 and [p["name"] for p in r["photos"]] == ["r1.jpg", "r2.jpg"]
    assert c.get("/api/thumb", params={"path": paths[0]}).status_code == 404  # no longer served
    r = c.post("/api/remove", json={}, headers=H).json()
    assert r["removed"] == 2 and r["photos"] == [] and r["folder"] is None
    assert all(Path(p).exists() for p in paths)
    assert "yue" in c.get("/api/status").json()["starter_categories"]


def test_clear_local_data(client, tmp_path):
    c, server = client
    p = photo(tmp_path / "pics" / "a.jpg")
    c.post("/api/folder", json={"path": str(tmp_path / "pics")}, headers=H)
    from photo_insight.store import signature
    server.S.store.save_analysis(Path(p), Analysis("Food", 0.9, "x", ["a"]), "m", signature(server.S.cfg), False)
    server.S.store.set_overrides(Path(p), caption="mine")
    c.get("/api/thumb", params={"path": p})
    cfg = server.S.cfg.to_dict()
    cfg["categories"] = [{"name": "Only", "description": "", "hashtags": []}]
    c.put("/api/config", json=cfg, headers=H)

    d = c.get("/api/data").json()
    assert (d["results"]["analysed"], d["results"]["edited"], d["thumbs"]["files"], d["settings"]["categories"]) == (1, 1, 1, 1)
    assert c.post("/api/data/clear", json={"results": True}).status_code == 403  # needs the app header

    d = c.post("/api/data/clear", json={"results": True, "thumbs": True}, headers=H).json()
    assert d["results"]["analysed"] == 0 and d["thumbs"]["files"] == 0
    assert d["settings"]["categories"] == 1  # untouched
    assert c.get("/api/photos").json()["photos"][0]["status"] == "pending"
    assert Path(p).exists()  # photo files are never deleted

    d = c.post("/api/data/clear", json={"settings": True}, headers=H).json()
    assert d["settings"]["categories"] == 8


def test_embed_args_no_duplicates_and_one_line():
    from photo_insight import export
    args = export.embed_args({"status": "done", "category": "Food", "caption": "line one\nline two",
                              "hashtags": ["a"], "location": None})
    assert "-XMP-dc:Subject-=a" in args and "-XMP-dc:Subject+=a" in args  # remove-then-add: no duplicates
    assert "-XMP-dc:Description=line one line two" in args


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="exiftool not installed")
def test_sort_copy_embeds_metadata_and_leaves_original(client, tmp_path):
    import hashlib
    import subprocess
    c, server = client
    p = photo(tmp_path / "pics" / "a.jpg")
    before = hashlib.md5(Path(p).read_bytes()).hexdigest()
    c.post("/api/folder", json={"path": str(tmp_path / "pics")}, headers=H)
    from photo_insight.store import signature
    server.S.store.save_analysis(Path(p), Analysis("Food", 0.9, "好味 ramen", ["ramen", "拉麵"]), "m",
                                 signature(server.S.cfg), False)

    assert c.post("/api/organize", json={"mode": "move", "embed": True}, headers=H).status_code == 400
    r = c.post("/api/organize", json={"mode": "copy", "embed": True}, headers=H).json()
    assert r["embedded"]["embedded"] == 1 and r["embedded"]["failed"] == 0
    copy = tmp_path / "pics" / "_sorted" / "Food" / "a.jpg"
    out = subprocess.run(["exiftool", "-s3", "-XMP-dc:Subject", "-XMP-dc:Description", str(copy)],
                         capture_output=True, text=True).stdout
    assert "ramen" in out and "拉麵" in out and "好味 ramen" in out
    assert hashlib.md5(Path(p).read_bytes()).hexdigest() == before  # original untouched


@pytest.mark.skipif(not __import__("shutil").which("exiftool"), reason="exiftool not installed")
def test_embed_skips_photos_with_nothing_to_write(tmp_path):
    from photo_insight import export
    p = Path(photo(tmp_path / "x.jpg"))
    r = export.embed_metadata([(p, {"status": "pending", "hashtags": [], "location": None})])
    assert (r["embedded"], r["nothing"]) == (0, 1)
