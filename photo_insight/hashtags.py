"""Hashtag normalisation and merging of model-suggested tags with curated popular ones."""
from __future__ import annotations

import re
import unicodedata

# Widely used, genre-neutral photography hashtags.
GENERAL_HASHTAGS = [
    "photography", "photooftheday", "picoftheday", "instagood", "photographylovers",
    "photographer", "igers", "photographyeveryday", "capturedmoments", "visualsoflife",
]

# General Chinese photography tags, added when Chinese hashtags are on. Kept location-neutral:
# place tags like #香港 come from the model only when the photo shows it.
GENERAL_HASHTAGS_ZH = ["攝影", "影相", "攝影日常"]

# Tags that are popular but say nothing about a photo; keep them out of model suggestions
# so the slots go to descriptive tags (the general list above already covers them).
_FILLER = {"instagram", "insta", "love", "like", "likeforlike", "follow", "followme", "l4l", "f4f", "beautiful"}

_STRIP = re.compile(r"[^\w]+", re.UNICODE)


def normalize(tag: str) -> str:
    """'#Golden Hour!' -> 'goldenhour'. Keeps non-Latin letters (e.g. 香港)."""
    tag = unicodedata.normalize("NFKC", str(tag)).strip().lstrip("#")
    return _STRIP.sub("", tag).replace("_", "").lower()


def merge(category_tags: list[str], model_tags: list[str], general: list[str], limit: int,
          location: list[str] = ()) -> list[str]:
    """Category base tags first, then location (from GPS), the model's descriptive tags, general popular ones."""
    out: list[str] = []
    seen: set[str] = set()

    def add(tags, skip_filler=False):
        for t in tags:
            n = normalize(t)
            if not n or n.isdigit() or len(n) > 40 or n in seen or (skip_filler and n in _FILLER):
                continue
            seen.add(n)
            out.append(n)

    add(category_tags)
    add(location)
    add(model_tags, skip_filler=True)
    add(general)
    return out[: max(0, limit)]


def general_for(include_general: bool, chinese: bool) -> list[str]:
    return (GENERAL_HASHTAGS if include_general else []) + (GENERAL_HASHTAGS_ZH if include_general and chinese else [])


def format_tags(tags: list[str]) -> str:
    return " ".join(f"#{t}" for t in tags)
