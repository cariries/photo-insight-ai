# Photo Insight

Batch-classify photos into **your own categories** and add **popular photography hashtags**.
Runs fully offline: the model is local (Ollama) and nothing leaves your machine.

## Quick start

```bash
# 1. A local vision model (one-off)
ollama pull gemma4:e4b          # default: ~3 s/photo on an M2 Pro. qwen3-vl also works (~20 s/photo)

# 2. Install
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 3. Run the web UI (opens http://127.0.0.1:8765)
.venv/bin/photo-insight serve
```

Command line (no UI; add `--embed` to write metadata into sorted copies):

```bash
.venv/bin/photo-insight run ~/Pictures/Trip --csv trip.csv --xmp --organize copy
.venv/bin/photo-insight run ~/Pictures/Trip --organize copy --group location_category --level area --names zh
```

## What it does

1. **Open a folder** (subfolders included), or **Choose photos…** to pick individual photos. Use **+ Add photos** to add more from other folders. JPEG, PNG, WebP, TIFF and HEIC are supported.
   - Tick photos to limit Analyze, Sort, XMP and CSV to the selection; Shift-click selects a range. With nothing ticked, actions apply to all photos.
   - Click a photo for a large preview: ←/→ to browse, Space to select.
   - **Remove from list** / **Clear all** take photos off the working list only. Files on disk and saved results are kept.
2. **Analyze.** Each photo is resized to 1920 px on the longest edge (configurable; originals are never touched) and sent to the local model. The model returns one category, a confidence, a caption and descriptive hashtags.
3. **Hashtags** = the category's base tags + the model's descriptive tags + general popular tags (optional). Tags are deduped and capped at 20 by default (Instagram's limit is 30).
4. **Review.** Change the category, caption or hashtags on any card. Low-confidence photos go to *Uncategorized*.
5. **Apply:**
   - **Sort into folders**: copy (default) or move into `<folder>/_sorted/`. Folders can be by category, by GPS location (`香港/尖沙咀`, `Japan/Tokyo/Shibuya-ku`), or both nested (location → category, or category → location). Location depth is country, city or district, and place names can be English or 中文. Photos without GPS go to *No location / 未知地點*. Sorting by location alone works before analysis. The dialog previews the folders and counts before anything is copied.
     With **Copy**, tick *Write hashtags, caption and location into the copies* to embed them in the sorted copies (XMP + IPTC, via [exiftool](https://exiftool.org): `brew install exiftool`). Lightroom, Apple Photos, Windows, Google Photos and macOS Spotlight can then search them. Pixels are not re-encoded, existing keywords and camera data (incl. GPS) are kept, and the **originals are never changed**. BMP can't hold metadata and is skipped.
   - **Write XMP keywords**: writes `photo.xmp` sidecars (Lightroom, Capture One, darktable). Sidecars written by another app are never overwritten.
   - **Export CSV**, or **Copy caption + hashtags** for posting.

## 廣東話 / Chinese

- Switch the interface between **English** and **廣東話** in the top-right corner. It follows your browser language until you pick one.
- In **Categories → Edit**:
  - Set **Caption language** to 廣東話 (colloquial, e.g. 「好靚呀！」) or 繁體中文（書面語）.
  - Tick **Also suggest Chinese hashtags** to mix Traditional Chinese tags (#攝影 #風景攝影) with the English ones.
  - **Use starter categories → 中文** loads 風景、人像、街拍、美食、動物、建築、微距及靜物、夜景.
- Category folders, XMP keywords and CSV all support Chinese. The CSV opens correctly in Excel.

## Location tags from GPS

If a photo has GPS coordinates (most phone photos do), Photo Insight names the place, fully offline:

- 📍 **尖沙咀，香港 / Tsim Sha Tsui, Hong Kong** shown on each card, with the nearest landmark in the tooltip and preview.
- **Location hashtags**, most specific first: `#tsimshatsui #hongkong`, plus `#尖沙咀 #香港` when Chinese hashtags are on. Turn on *nearest landmark* for e.g. `#victoriapeak`; it's off by default because you may only have been nearby.
- The place is given to the model as context, so captions can name it.
- XMP sidecars get City / State / Country / Location fields (Lightroom and Capture One show them), and the CSV gets location and latitude/longitude columns.

This needs place data, downloaded once from [GeoNames](https://www.geonames.org/) (CC BY 4.0, about 15–30 MB): **Categories → Edit → Location → Download**, or

```bash
.venv/bin/photo-insight geo download --countries HK MO TW JP   # world cities + district/landmark detail
.venv/bin/photo-insight geo lookup 22.2936 114.1719            # test a coordinate
```

Every town worldwide (1,000+ residents) is covered. Countries in `--countries` also get districts, landmarks and proper Chinese names. Add any ISO code you travel to (e.g. `KR TH FR`).

> Privacy: location hashtags show where a photo was taken when you post it. Untick them for photos taken at home.

Results are cached in `~/.photo-insight/insight.db`. Re-opening a folder is instant, and **Analyze** only processes new or changed photos. A photo is marked *Outdated* when you change the model or the category definitions.

Configuration (categories, model, limits) is in `~/.photo-insight/config.yaml`. Edit it in the UI or by hand.

**Local data… / 本機資料…** (bottom of the sidebar) shows what the app stores and clears any of it: analysis results and edits, the thumbnail cache, settings and categories (reset to defaults), or the downloaded place data. Your photos, sorted folders and XMP files are never deleted.

## Tests

```bash
.venv/bin/python -m pytest -q
```
