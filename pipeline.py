#!/usr/bin/env python3
"""
INPACT Investigation Mapper — Pipeline d'extraction
====================================================

Récupère tous les articles via l'API REST WordPress.com,
extrait les lieux mentionnés (NER multilingue FR/EN/RU/AR),
les géocode via Nominatim, et produit `data.json` consommé par la carte.

Usage :
    pip install -r requirements.txt
    python -m spacy download fr_core_news_lg
    python -m spacy download en_core_web_lg
    python -m spacy download ru_core_news_lg
    python -m spacy download xx_ent_wiki_sm   # fallback arabe + autres

    python pipeline.py
    python pipeline.py --refresh       # ignore le cache d'articles
    python pipeline.py --regeocode     # ré-interroge Nominatim
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
# Le site est hébergé sur WordPress.com, donc on utilise l'API publique
# de WordPress.com plutôt que l'endpoint /wp-json/wp/v2 standard.
# Pour cibler un autre site, change juste la ligne ci-dessous.
WP_API_BASE = "https://public-api.wordpress.com/wp/v2/sites/alleyesonwagner.org"

USER_AGENT = "INPACT-InvestigationMapper/1.0 (research; contact tips.aeow@proton.me)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_DELAY = 1.1
HTTP_TIMEOUT = 30
MIN_PLACE_LEN = 3
MAX_PLACE_LEN = 60

CACHE_DIR = Path("cache")
ARTICLES_CACHE = CACHE_DIR / "articles.json"
GEOCODE_CACHE = CACHE_DIR / "geocode.json"
ALIASES_FILE = Path("place_aliases.json")
OUTPUT_FILE = Path("data.json")

DEFAULT_BLACKLIST = {
    "africa corps", "wagner", "africa", "wagner group", "afrique", "europe",
    "ouest", "est", "nord", "sud", "north", "south", "east", "west",
    "occident", "occidentaux", "western", "europeans",
    "facebook", "twitter", "x", "instagram", "telegram", "tiktok", "youtube",
    "afp", "reuters", "ap", "rfi", "tass",
    "ue", "ua", "un", "onu", "otan", "nato", "eu", "cedeao", "ecowas",
    "minusma", "fla", "jnim", "famas", "fama", "is", "is-s", "iss", "aqim",
    "google", "openstreetmap", "osm",
    "janvier", "février", "mars", "avril", "mai", "juin", "juillet",
    "août", "septembre", "octobre", "novembre", "décembre",
    "россия", "сша", "запад", "европа", "африка", "восток",
    "روسيا", "أمريكا", "أفريقيا",
}

DEFAULT_ALIASES = {
    "Sevare": "Sévaré",
    "Severe": "Sévaré",
    "Bamako Mali": "Bamako",
    "Tin-Zaouatene": "Tin Zaouaten",
    "Tin Zaouatene": "Tin Zaouaten",
    "Côte-d'Ivoire": "Côte d'Ivoire",
    "Cote d'Ivoire": "Côte d'Ivoire",
    "Cote-d'Ivoire": "Côte d'Ivoire",
    "Russian Federation": "Russie",
    "Russia": "Russie",
    "Россия": "Russie",
    "Москва": "Moscou",
    "Moscow": "Moscou",
    "Алабуга": "Alabuga",
    "Burkina-Faso": "Burkina Faso",
    "République du Mali": "Mali",
    "République du Niger": "Niger",
}

# ----------------------------------------------------------------------
# Modèles
# ----------------------------------------------------------------------
@dataclass
class Article:
    id: str
    title: str
    url: str
    date: str
    category: str
    lang: str
    excerpt: str
    text: str = ""

    def to_public(self) -> dict:
        d = asdict(self)
        d.pop("text", None)
        return d


@dataclass
class Place:
    id: str
    name: str
    country: str
    lat: float
    lng: float
    articles: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------
# WP REST API
# ----------------------------------------------------------------------
def fetch_categories() -> dict[int, str]:
    cats = {}
    page = 1
    while True:
        r = requests.get(
            f"{WP_API_BASE}/categories",
            params={"per_page": 100, "page": page, "_fields": "id,name"},
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 400:
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        for c in batch:
            cats[c["id"]] = c["name"]
        page += 1
    return cats


def fetch_all_posts(cache_articles: bool) -> list[dict]:
    if cache_articles and ARTICLES_CACHE.exists():
        print(f"[cache] {ARTICLES_CACHE} chargé")
        return json.loads(ARTICLES_CACHE.read_text("utf-8"))

    posts = []
    page = 1
    print(f"[wp] récupération des articles depuis {WP_API_BASE}/posts")
    while True:
        r = requests.get(
            f"{WP_API_BASE}/posts",
            params={
                "per_page": 100, "page": page,
                "_fields": "id,date,link,slug,title,categories,content,excerpt",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code == 400:
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        posts.extend(batch)
        print(f"  · page {page} : {len(batch)} articles (cumul {len(posts)})")
        if len(batch) < 100:
            break
        page += 1
        time.sleep(0.3)

    CACHE_DIR.mkdir(exist_ok=True)
    ARTICLES_CACHE.write_text(json.dumps(posts, ensure_ascii=False), "utf-8")
    return posts


def post_to_article(post: dict, categories: dict[int, str]) -> Article:
    title = clean_html(post["title"]["rendered"])
    content_html = post["content"]["rendered"]
    text = clean_html(content_html)
    excerpt = clean_html(post.get("excerpt", {}).get("rendered", ""))[:280]
    cat_id = (post.get("categories") or [None])[0]
    category = categories.get(cat_id, "—") if cat_id else "—"
    lang = detect_language(text)
    return Article(
        id=post.get("slug") or str(post["id"]),
        tit
