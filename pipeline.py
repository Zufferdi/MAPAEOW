#!/usr/bin/env python3
"""
INPACT Investigation Mapper — Pipeline d'extraction
====================================================

Récupère tous les articles de https://alleyesonwagner.org via l'API REST
WordPress, extrait les lieux mentionnés (NER multilingue FR/EN/RU/AR),
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
    python pipeline.py --site URL      # change de site source

Pour corriger des géocodages erronés ou filtrer des faux positifs,
édite `place_aliases.json` (créé au premier run).
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
DEFAULT_SITE = "https://alleyesonwagner.org"
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
def fetch_categories(site: str) -> dict[int, str]:
    cats = {}
    page = 1
    while True:
        r = requests.get(
            f"{site}/wp-json/wp/v2/categories",
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


def fetch_all_posts(site: str, cache_articles: bool) -> list[dict]:
    if cache_articles and ARTICLES_CACHE.exists():
        print(f"[cache] {ARTICLES_CACHE} chargé")
        return json.loads(ARTICLES_CACHE.read_text("utf-8"))

    posts = []
    page = 1
    print(f"[wp] récupération des articles depuis {site}/wp-json/wp/v2/posts")
    while True:
        r = requests.get(
            f"{site}/wp-json/wp/v2/posts",
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
        title=title, url=post["link"], date=post["date"][:10],
        category=category, lang=lang, excerpt=excerpt, text=text,
    )


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for s in soup(["script", "style"]):
        s.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


# ----------------------------------------------------------------------
# Détection de langue : script-based pour ru/ar, lexicale pour fr/en
# ----------------------------------------------------------------------
def detect_language(text: str) -> str:
    sample = text[:8000]
    if not sample:
        return "en"
    cyrillic = sum(1 for c in sample if "\u0400" <= c <= "\u04ff")
    arabic = sum(1 for c in sample if "\u0600" <= c <= "\u06ff")
    total_letters = sum(1 for c in sample if c.isalpha()) or 1
    if cyrillic / total_letters > 0.3:
        return "ru"
    if arabic / total_letters > 0.3:
        return "ar"
    s = sample.lower()
    fr = sum(s.count(w) for w in (" le ", " la ", " des ", " une ", " est ", " avec ", " dans ", " pour "))
    en = sum(s.count(w) for w in (" the ", " and ", " of ", " is ", " with ", " in ", " has ", " for "))
    return "fr" if fr >= en else "en"


# ----------------------------------------------------------------------
# NER : modèles multilingues chargés à la demande
# ----------------------------------------------------------------------
class NERModels:
    """Charge les modèles spaCy à la demande, garde un cache en mémoire."""
    def __init__(self):
        self._cache: dict[str, object] = {}
        try:
            import spacy
            self.spacy = spacy
        except ImportError:
            sys.exit("⚠  installe spaCy : pip install spacy")

    def get(self, lang: str):
        if lang in self._cache:
            return self._cache[lang]
        candidates = {
            "fr": ["fr_core_news_lg", "fr_core_news_md"],
            "en": ["en_core_web_lg", "en_core_web_md"],
            "ru": ["ru_core_news_lg", "ru_core_news_md"],
            "ar": ["xx_ent_wiki_sm"],  # spaCy n'a pas de modèle arabe natif, fallback multilingue
        }.get(lang, ["xx_ent_wiki_sm"])
        for name in candidates:
            try:
                model = self.spacy.load(name)
                model.max_length = 2_000_000
                self._cache[lang] = model
                print(f"[ner] modèle {name} chargé pour lang={lang}")
                return model
            except OSError:
                continue
        print(f"⚠  aucun modèle disponible pour lang={lang}, articles sautés")
        print(f"   essaie : python -m spacy download {candidates[0]}")
        self._cache[lang] = None
        return None


def normalize_name(name: str) -> str:
    name = name.strip().strip(".,;:!?\"'()[]{}«»").strip()
    return re.sub(r"\s+", " ", name)


def slugify(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name).encode("ASCII", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", nfkd.lower()).strip("-") or f"x{abs(hash(name))%99999}"


def extract_places(article: Article, ner: NERModels, blacklist: set[str], aliases: dict[str, str]) -> set[str]:
    nlp = ner.get(article.lang)
    if nlp is None:
        return set()
    places: set[str] = set()
    text = article.text
    for start in range(0, len(text), 800_000):
        chunk = text[start:start + 800_000]
        doc = nlp(chunk)
        for ent in doc.ents:
            if ent.label_ not in {"GPE", "LOC"}:
                continue
            name = normalize_name(ent.text)
            if not (MIN_PLACE_LEN <= len(name) <= MAX_PLACE_LEN):
                continue
            if name.lower() in blacklist:
                continue
            if name.isdigit() or re.match(r"^\d", name):
                continue
            canon = aliases.get(name, name)
            places.add(canon)
    return places


# ----------------------------------------------------------------------
# Géocodage
# ----------------------------------------------------------------------
def load_geocode_cache() -> dict:
    if GEOCODE_CACHE.exists():
        return json.loads(GEOCODE_CACHE.read_text("utf-8"))
    return {}


def save_geocode_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    GEOCODE_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")


def geocode(name: str, cache: dict) -> dict | None:
    key = name.lower()
    if key in cache:
        return cache[key]
    try:
        r = requests.get(
            NOMINATIM_URL,
            params={"q": name, "format": "json", "limit": 1, "addressdetails": 1, "accept-language": "fr,en"},
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT,
        )
        time.sleep(NOMINATIM_DELAY)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  ⚠  géocodage {name!r} : {e}")
        cache[key] = None
        return None
    if not data:
        cache[key] = None
        return None
    hit = data[0]
    addr = hit.get("address", {})
    country = addr.get("country") or addr.get("country_code", "").upper() or "—"
    result = {
        "lat": float(hit["lat"]), "lng": float(hit["lon"]),
        "country": country, "display": hit.get("display_name", name),
    }
    cache[key] = result
    return result


# ----------------------------------------------------------------------
# Aliases
# ----------------------------------------------------------------------
def load_aliases_and_blacklist() -> tuple[dict[str, str], set[str], dict[str, dict]]:
    if not ALIASES_FILE.exists():
        ALIASES_FILE.write_text(
            json.dumps({
                "_doc": "aliases (variante->canonique), blacklist (faux positifs), overrides (lat/lng forcés)",
                "aliases": DEFAULT_ALIASES,
                "blacklist": sorted(DEFAULT_BLACKLIST),
                "overrides": {},
            }, ensure_ascii=False, indent=2),
            "utf-8",
        )
    cfg = json.loads(ALIASES_FILE.read_text("utf-8"))
    aliases = cfg.get("aliases", {})
    blacklist = {b.lower() for b in cfg.get("blacklist", [])} | DEFAULT_
