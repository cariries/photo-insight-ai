"""User settings and self-defined categories, stored as YAML in the app home dir."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

UNCATEGORIZED = "Uncategorized"


def app_home() -> Path:
    home = Path(os.environ.get("PHOTO_INSIGHT_HOME", Path.home() / ".photo-insight"))
    home.mkdir(parents=True, exist_ok=True)
    return home


@dataclass
class Category:
    name: str
    description: str = ""
    hashtags: list[str] = field(default_factory=list)  # base tags always added for this category


@dataclass
class Settings:
    model: str = "gemma4:e4b"
    ollama_host: str = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    max_hashtags: int = 20          # Instagram allows 30; ~20 is a common sweet spot
    include_general_hashtags: bool = True
    min_confidence: float = 0.4     # below this the photo goes to Uncategorized
    image_size: int = 1920          # longest edge sent to the model (originals are never modified)
    language: str = "English"       # caption language: English | Cantonese | Traditional Chinese | free text
    chinese_hashtags: bool = False  # also suggest Traditional Chinese hashtags (e.g. #攝影)
    location_tags: bool = True      # hashtags from GPS: area, city, country (needs place data, see geo.py)
    landmark_tags: bool = False     # also the nearest landmark (can be wrong if you were only nearby)


DEFAULT_CATEGORIES = [
    Category("Landscape", "Scenic outdoor views: mountains, sea, lakes, forests, sky, sunsets",
             ["landscapephotography", "naturephotography"]),
    Category("Portrait", "A person or people are the main subject",
             ["portraitphotography", "portrait"]),
    Category("Street", "Candid everyday life in public urban spaces",
             ["streetphotography", "streetlife"]),
    Category("Food", "Dishes, drinks, cooking, restaurants",
             ["foodphotography", "foodie"]),
    Category("Animals", "Pets, wildlife, birds, insects",
             ["animalphotography"]),
    Category("Architecture", "Buildings, interiors, bridges, cityscapes and urban structures",
             ["architecturephotography", "architecture"]),
    Category("Macro & Still Life", "Close-ups, flowers, objects, product-style shots",
             ["macrophotography", "stilllife"]),
    Category("Night", "Night scenes, city lights, astrophotography",
             ["nightphotography", "longexposure"]),
]


# Starter set for Chinese-speaking users (offered in the UI; never applied automatically).
DEFAULT_CATEGORIES_ZH = [
    Category("風景", "山、海、湖、森林、天空、日落等戶外景色", ["landscapephotography", "風景攝影"]),
    Category("人像", "以人物為主角", ["portraitphotography", "人像攝影"]),
    Category("街拍", "城市街頭嘅日常生活", ["streetphotography", "街拍"]),
    Category("美食", "食物、飲品、煮食、餐廳", ["foodphotography", "美食"]),
    Category("動物", "寵物、野生動物、雀鳥、昆蟲", ["animalphotography", "動物攝影"]),
    Category("建築", "建築物、室內、橋、城市景觀", ["architecturephotography", "建築攝影"]),
    Category("微距及靜物", "特寫、花、物件、產品", ["macrophotography", "微距攝影"]),
    Category("夜景", "夜景、城市燈光、星空", ["nightphotography", "夜景"]),
]


@dataclass
class Config:
    settings: Settings = field(default_factory=Settings)
    categories: list[Category] = field(default_factory=lambda: [Category(**asdict(c)) for c in DEFAULT_CATEGORIES])

    def category(self, name: str) -> Category | None:
        return next((c for c in self.categories if c.name == name), None)

    def to_dict(self) -> dict:
        return {"settings": asdict(self.settings), "categories": [asdict(c) for c in self.categories]}

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        known = Settings.__dataclass_fields__
        settings = Settings(**{k: v for k, v in (data.get("settings") or {}).items() if k in known})
        cats = []
        seen = set()
        for c in data.get("categories") or []:
            name = str(c.get("name", "")).strip()
            if not name or name in seen or name == UNCATEGORIZED:
                continue
            seen.add(name)
            cats.append(Category(name, str(c.get("description", "")).strip(),
                                 [str(t) for t in c.get("hashtags") or []]))
        if not cats:
            raise ValueError("At least one category is required")
        return cls(settings, cats)


def config_path() -> Path:
    return app_home() / "config.yaml"


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        cfg = Config()
        save_config(cfg)
        return cfg
    return Config.from_dict(yaml.safe_load(path.read_text()) or {})


def save_config(cfg: Config) -> None:
    config_path().write_text(yaml.safe_dump(cfg.to_dict(), sort_keys=False, allow_unicode=True))
