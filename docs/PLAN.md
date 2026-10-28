# Photo Insight: plan and decisions

## Goal
Offline batch classification of photos into user-defined categories, plus popular photography hashtags, using a local LLM.

## Epics
- **E1 MVP (done 2026-10-27):** Ollama vision client with JSON-schema output; user categories (name, description, base hashtags); batch runner with progress, cancel and cache; web UI (grid, edit, filter); sort into folders; XMP sidecars; CSV; CLI.
- **E1.1 (done 2026-10-27):** pick individual photos (macOS dialog) and add more; tick-box selection that scopes every action; large preview with keyboard navigation; remove from list / clear all.
- **E1.2 (done 2026-10-27):** Cantonese. 廣東話 UI (`static/i18n.js`); captions in 廣東話 or 繁體中文; optional Chinese hashtags; 中文 starter categories; UTF-8 BOM in CSV for Excel.
- **E1.3 (done 2026-10-27):** GPS location tags. EXIF GPS reading, offline reverse geocoding (GeoNames), location hashtags (EN + 中文), place context in the caption prompt, XMP City/State/Country/Location, CSV columns.
- **E1.4 (done 2026-10-27):** sort by location: category / location / location→category / category→location; depth country/city/district; EN or 中文 folder names; dry-run preview in the dialog; location-only sorting needs no analysis.
- **E1.5 (done 2026-10-27):** Local data dialog: shows sizes and clears results/edits, thumbnails, settings, place data individually (with confirmation). Never touches photos, sorted folders or XMP.
- **E1.6 (done 2026-10-27):** embed hashtags/caption/category/location into *sorted copies* (copy mode only) with exiftool, one process per batch via an argfile. Verified: pixel checksum unchanged, GPS kept, existing keywords kept without duplicates, macOS Spotlight importer reads keywords/caption/city.
- **E2 Hashtag quality:** curated per-genre popular tag library (refreshable list); banned/shadow-banned tag filter; per-platform presets (Instagram 30 / Threads / Xiaohongshu); "top N most used across batch".
- **E3 Formats & metadata:** RAW (CR3/NEF/ARW via embedded preview); write keywords into JPEG/HEIC (exiftool) as an option; use EXIF (date, GPS, lens) as hints (#hongkong, #35mm).
- **E4 Multi-label & smart folders:** optional secondary categories; rules (e.g. confidence < x → review queue); duplicate/near-duplicate detection.
- **E5 Packaging:** one-click macOS app (py2app/briefcase), first-run model download helper.

## Decisions
- **Stack:** Python + FastAPI + vanilla JS, served on 127.0.0.1. Python because of local file access, Pillow/HEIC and easy packaging. No build step.
- **Local LLM:** Ollama. Default model `gemma4:e4b`: ~3 s/photo warm on an M2 Pro, accurate on samples. `qwen3-vl` works but is ~5x slower. Model is selectable in the UI. `think:false`, `temperature:0`, structured output (enum of category names).
- **Resize:** 1920 px longest edge before sending (user's call, 2026-10-27). Benchmarked against 768 px on a 4K photo: same result, similar warm latency on gemma4. Configurable 512–4096.
- **Safety:** originals are never modified. Sorting defaults to copy, foreign XMP sidecars are never overwritten, and mutating API calls need an `X-Photo-Insight` header (blocks cross-site POSTs to localhost).
- **Photo picking:** native macOS dialog via `osascript` (keeps real paths, so sorting and XMP work in place). Browser drag-and-drop was deferred because browsers only expose file contents, not paths.
- **Cantonese:** the caption prompt asks for colloquial written Cantonese (嘅、咗、喺) in Traditional characters, which gemma4 does well. The UI language is a per-browser preference (localStorage), separate from caption language. Chinese general tags are location-neutral (#攝影, not #香港); place tags only come from the model.
- **Large photos (2026-10-27):** Pillow's default decompression-bomb limit (~179 MP) rejected a user's 200 MP phone photo. Raised to 1 GP, since inputs are the user's own files, with a clear error above that. Concurrent full decodes are capped at 2 so huge PNG/TIFF/HEIC thumbnails can't exhaust memory. A 200 MP JPEG resizes in ~0.3 s via draft(); a 200 MP PNG takes ~2.5 s and ~1.7 GB peak.
- **Reverse geocoding (2026-10-27):** offline GeoNames data, since online geocoders would break the offline promise. World `cities1000` plus per-country detail packs (default HK MO TW JP), indexed into a 0.5° grid (~280k places, loads in 0.3 s, lookups ~1 ms). City = biggest-nearby (population / (d+1)²), so you get #tokyo, not a 1,800-person block. Area = nearest district with 10k+ people, or an ADM2/ADM3 district (Shibuya, Xinyi). Chinese names come from per-country alternatenames (zh-HK first), converted to HK Traditional with OpenCC (`s2hk`, `jp2t` for Japanese forms) plus a small override list (尖沙嘴→尖沙咀). Landmark hashtags are off by default because "nearest landmark" is often only nearby. Raw downloads are kept in `geo/raw` and downloads retry 3x (flaky connections seen in HK).
- **Embedding (2026-10-27):** user chose "copies only" over writing into originals. exiftool over Pillow because it writes XMP+IPTC losslessly across JPEG/HEIC/PNG/TIFF/WebP and preserves the rest of the metadata. Keywords use remove-then-add (`-=` then `+=`) to avoid duplicates. HEIC gets XMP only (IPTC isn't supported there), which is what Photos/Lightroom read.
- **HK proxy:** Ollama HTTP client uses `trust_env=False` so the shell's `https_proxy` doesn't intercept localhost.
