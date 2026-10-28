"""Command line: `photo-insight serve` for the UI, `photo-insight run <folder>` for headless batches."""
from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from . import export, images
from .config import app_home, config_path, load_config
from .hashtags import format_tags
from .llm import LLMError, Ollama
from .pipeline import Runner
from .store import Store, record


def cmd_serve(args) -> None:
    import uvicorn

    url = f"http://127.0.0.1:{args.port}"
    print(f"Photo Insight running at {url}  (config: {config_path()})")
    if not args.no_browser:
        webbrowser.open(url)
    uvicorn.run("photo_insight.server:app", host="127.0.0.1", port=args.port, log_level="warning")


def cmd_run(args) -> int:
    cfg = load_config()
    if args.model:
        cfg.settings.model = args.model
    try:
        Ollama(cfg.settings.ollama_host, timeout=10).models()
    except LLMError as e:
        print(e, file=sys.stderr)
        return 1
    folder = Path(args.folder).expanduser().resolve()
    paths = images.scan(folder, not args.no_recursive)
    print(f"{len(paths)} photos in {folder} · model {cfg.settings.model} · {len(cfg.categories)} categories")
    store = Store(app_home() / "insight.db")
    runner = Runner(store)

    def progress(st):
        n = st.done + st.skipped + st.errors
        eta = f" · ~{round((st.total - n) * st.seconds_per_photo)}s left" if st.seconds_per_photo else ""
        print(f"\r[{n}/{st.total}] analysed {st.done}, cached {st.skipped}, errors {st.errors}{eta}   ",
              end="", flush=True)

    runner.start(paths, cfg, force=args.force, on_progress=progress)
    try:
        runner.wait()
    except KeyboardInterrupt:
        runner.cancel()
        runner.wait()
    print()
    if runner.state.status == "failed":
        print(f"Failed: {runner.state.message}", file=sys.stderr)
        return 1

    recs = [record(p, store.get(p), cfg) for p in paths]
    for r in recs:
        if r["status"] == "done":
            print(f"{r['name']:<32} {r['category']:<20} {r['confidence']:.2f}  {format_tags(r['hashtags'][:6])} …")
        elif r["status"] == "error":
            print(f"{r['name']:<32} ERROR {r['error']}")
    if args.csv:
        Path(args.csv).write_text(export.to_csv(recs), encoding="utf-8")
        print(f"CSV written to {args.csv}")
    if args.xmp:
        print("XMP sidecars:", export.write_sidecars(recs))
    if args.organize:
        dest = Path(args.dest).expanduser().resolve() if args.dest else folder / "_sorted"
        if args.embed and args.organize != "copy":
            print("--embed only works with --organize copy (originals are never changed)", file=sys.stderr)
            return 2
        pairs = export.organize(recs, dest, args.organize, args.group, args.level, args.names)
        if args.organize == "move":
            for src, target in pairs:
                store.rename(src, target)
        print(f"{'Copied' if args.organize == 'copy' else 'Moved'} {len(pairs)} photos into {dest}")
        if args.embed:
            by_path = {r["path"]: r for r in recs}
            print("Embedded metadata:", export.embed_metadata([(t, by_path[str(s)]) for s, t in pairs]))
    return 0


def cmd_geo(args) -> int:
    from . import geo

    if args.geo_cmd == "download":
        print(f"Downloading GeoNames place data for {' '.join(args.countries)} (+ world cities)…")
        meta = geo.download(args.countries, refresh=args.refresh)
        print(f"Done: {meta['places']:,} places in {geo.geo_dir()}")
        return 0
    place = geo.lookup(args.lat, args.lon)
    if place is None:
        print("No place found" + ("" if geo.index() else " (run `photo-insight geo download` first)"))
        return 1
    d = place.to_dict()
    print(d["label_en"], "|", d["label_zh"], "| near:", place.landmark and place.landmark.en)
    print("hashtags:", " ".join("#" + t for t in geo.hashtags(place, landmark=True, chinese=True)))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="photo-insight", description=__doc__)
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="Open the web UI")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-browser", action="store_true")
    r = sub.add_parser("run", help="Analyse a folder from the command line")
    r.add_argument("folder")
    r.add_argument("--model", help="Ollama model (default from config)")
    r.add_argument("--force", action="store_true", help="Re-analyse even if cached")
    r.add_argument("--no-recursive", action="store_true")
    r.add_argument("--csv", metavar="FILE")
    r.add_argument("--xmp", action="store_true", help="Write .xmp keyword sidecars")
    r.add_argument("--organize", choices=["copy", "move"], help="Sort into <dest>/<Category>/")
    r.add_argument("--dest", help="Destination for --organize (default <folder>/_sorted)")
    r.add_argument("--group", choices=export.GROUPS, default="category",
                   help="Folder layout: by category, by GPS location, or both nested")
    r.add_argument("--level", choices=export.LEVELS, default="city", help="How deep location folders go")
    r.add_argument("--names", choices=["en", "zh"], default="en", help="Language for place folder names")
    r.add_argument("--embed", action="store_true",
                   help="With --organize copy: write hashtags, caption and location into the copies (needs exiftool)")
    g = sub.add_parser("geo", help="Offline place data for GPS location tags")
    gsub = g.add_subparsers(dest="geo_cmd", required=True)
    gd = gsub.add_parser("download", help="Download GeoNames data (world cities + detail for some countries)")
    gd.add_argument("--countries", nargs="+", default=["HK", "MO", "TW", "JP"], metavar="CC",
                    help="ISO country codes with district/landmark detail and Chinese names")
    gd.add_argument("--refresh", action="store_true", help="Re-download files fetched before")
    gl = gsub.add_parser("lookup", help="Look up coordinates")
    gl.add_argument("lat", type=float)
    gl.add_argument("lon", type=float)
    args = ap.parse_args(argv)
    if args.cmd == "geo":
        return cmd_geo(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd in (None, "serve"):
        if args.cmd is None:
            args = ap.parse_args(["serve"])
        cmd_serve(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
