"""Talks to a local Ollama server. Nothing leaves the machine."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass

import httpx

from .config import UNCATEGORIZED, Category


class LLMError(RuntimeError):
    pass


@dataclass
class Analysis:
    category: str
    confidence: float
    caption: str
    tags: list[str]


# How each caption-language choice is described to the model. Anything else is passed through as-is.
LANGUAGE_HINTS = {
    "Cantonese": ("written Cantonese as used on Hong Kong social media: Traditional Chinese characters and "
                  "colloquial Cantonese words such as 嘅、咗、喺、好靚、正 (not Mandarin, not Simplified Chinese)"),
    "Traditional Chinese": "standard written Chinese (書面語) in Traditional Chinese characters",
}


def build_prompt(categories: list[Category], language: str, chinese_tags: bool = False,
                 location: str | None = None) -> str:
    lines = "\n".join(f"- {c.name}: {c.description}" if c.description else f"- {c.name}" for c in categories)
    return (
        "You are a photo librarian and social-media photographer.\n"
        "Classify the photo into exactly ONE of these categories:\n"
        f"{lines}\n"
        f"- {UNCATEGORIZED}: none of the above fits\n\n"
        "Then write a short, natural social-media caption (max 20 words) in "
        f"{LANGUAGE_HINTS.get(language, language)}, and suggest 8-12 popular, widely used Instagram hashtags that "
        "describe the subject, scene, mood, colours and photography style. Prefer common "
        "community tags (e.g. goldenhour, cityscape, dogsofinstagram) over invented ones. "
        "Hashtags: lowercase, no spaces, no '#'. "
        + ("Make 3-5 of them popular Traditional Chinese hashtags used by Hong Kong and Taiwan photographers "
           "(e.g. 攝影, 風景攝影, 美食, 香港 if the photo shows Hong Kong); the rest in English. " if chinese_tags else "")
        + "confidence is 0-1: how sure you are about the category."
        + (f"\nThe photo's GPS says it was taken at: {location}. You may name the place in the caption, but "
           "describe only what is visible: never add weather, season or scenery the photo doesn't show, and "
           "ignore the location if the photo clearly doesn't match it." if location else "")
    )


def response_schema(categories: list[Category]) -> dict:
    return {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": [c.name for c in categories] + [UNCATEGORIZED]},
            "confidence": {"type": "number"},
            "caption": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["category", "confidence", "caption", "hashtags"],
    }


class Ollama:
    def __init__(self, host: str, timeout: float = 300):
        # trust_env=False: never route localhost traffic through an HTTP(S)_PROXY.
        self.host = host.rstrip("/")
        self.http = httpx.Client(base_url=self.host, timeout=timeout, trust_env=False)

    def models(self) -> list[dict]:
        """Installed models, with a flag for vision capability."""
        try:
            tags = self.http.get("/api/tags").raise_for_status().json().get("models", [])
        except httpx.HTTPError as e:
            raise LLMError(f"Cannot reach Ollama at {self.host}. Is it running? ({e})") from e
        out = []
        for m in tags:
            caps = []
            try:
                caps = self.http.post("/api/show", json={"model": m["name"]}).json().get("capabilities", [])
            except httpx.HTTPError:
                pass
            out.append({"name": m["name"], "size": m.get("size", 0), "vision": "vision" in caps})
        return out

    def analyze(self, image_jpeg: bytes, model: str, categories: list[Category], language: str = "English",
                chinese_tags: bool = False, location: str | None = None) -> Analysis:
        body = {
            "model": model,
            "stream": False,
            "think": False,
            "format": response_schema(categories),
            "options": {"temperature": 0},
            "messages": [{
                "role": "user",
                "content": build_prompt(categories, language, chinese_tags, location),
                "images": [base64.b64encode(image_jpeg).decode()],
            }],
        }
        r = self.http.post("/api/chat", json=body)
        if r.status_code == 400 and "think" in r.text:  # model without a thinking toggle
            body.pop("think")
            r = self.http.post("/api/chat", json=body)
        if r.status_code != 200:
            raise LLMError(f"Ollama error {r.status_code}: {r.text[:300]}")
        return parse(r.json()["message"]["content"], categories)


def parse(content: str, categories: list[Category]) -> Analysis:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise LLMError(f"Model returned invalid JSON: {content[:200]}") from e
    names = {c.name.lower(): c.name for c in categories}
    category = names.get(str(data.get("category", "")).strip().lower(), UNCATEGORIZED)
    try:
        confidence = min(1.0, max(0.0, float(data.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 0.0
    tags = data.get("hashtags") or []
    if isinstance(tags, str):
        tags = tags.replace(",", " ").split()
    return Analysis(category, confidence, str(data.get("caption", "")).strip(), [str(t) for t in tags])
