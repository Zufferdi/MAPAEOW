#!/usr/bin/env python3
"""
INPACT Investigation Mapper — Pipeline d'extraction
====================================================

Récupère les articles via l'API REST WordPress.com,
extrait les lieux mentionnés (NER multilingue FR/EN/RU/AR),
les géocode via Nominatim, applique 3 filtres automatiques anti-bruit,
enrichit les articles avec les PDFs Google Drive qu'ils linkent,
et produit `data.json`.

Usage :
    python pipeline.py
    python pipeline.py --refresh           # ignore le cache d'articles
    python pipeline.py --regeocode         # ré-interroge Nominatim
    python pipeline.py --min-mentions 1    # garder même les lieux mentionnés 1× (par défaut 2)
    python pipeline.py --no-prefix-filter  # désactive le filtre rues/bâtiments
    python pipeline.py --no-type-filter    # désactive le filtre par type Nominatim
"""

from __future__ import annotations

import argparse
import html as html_module
import io
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
REPORTS_CACHE = CACHE_DIR / "reports.json"  # texte extrait des PDFs Google Drive
ALIASES_FILE = Path("place_aliases.json")
OUTPUT_FILE = Path("data.json")

# Regex pour détecter les liens Google Drive dans le HTML des articles
# Match : /file/d/{ID}/view, /file/d/{ID}/edit, /open?id={ID}, /uc?id={ID}
DRIVE_LINK_RE = re.compile(
    r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:export=download&)?id=)([a-zA-Z0-9_-]{20,})"
)
MAX_PDF_SIZE = 50 * 1024 * 1024  # 50 Mo : sécurité contre PDFs énormes

# ----------------------------------------------------------------------
# Filtres anti-bruit
# ----------------------------------------------------------------------
STREET_BUILDING_PREFIXES = (
    "rue ", "avenue ", "boulevard ", "place ", "impasse ", "allée ", "allee ",
    "chemin ", "quai ", "route ", "voie ", "cours ", "sentier ",
    "street ", "road ", "square ", "lane ",
    "calle ", "plaza ", "avenida ",
    "улица ", "проспект ", "площадь ",
    "hôtel ", "hotel ", "palais ", "palace ", "stade ", "stadium ",
    "aéroport ", "airport ", "gare ", "station ", "ambassade ", "embassy ",
    "musée ", "museum ", "tour ", "cathédrale ", "cathedral ", "mosquée ", "mosque ",
    "université ", "university ", "lycée ", "collège ",
    "restaurant ", "café ", "bar ", "club ",
    "monsieur ", "madame ", "saint-", "sainte-",
)

ALLOWED_NOMINATIM_TYPES = {
    "country", "state", "region", "province", "county", "district",
    "city", "town", "village", "hamlet", "municipality", "neighbourhood",
    "suburb", "quarter", "borough", "locality", "island", "archipelago",
    "continent", "ocean", "sea", "bay",
    "administrative", "national_park", "protected_area",
    "peak", "mountain_range", "desert", "plateau", "valley",
}

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
            cats[c["id"]] = html_module.unescape(c["name"])
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
        title=title, url=post["link"], date=post["date"][:10],
        category=category, lang=lang, excerpt=excerpt, text=text,
    )


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for s in soup(["script", "style"]):
        s.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))


# ----------------------------------------------------------------------
# Google Drive — extraction des PDFs liés dans les articles
# ----------------------------------------------------------------------
def load_reports_cache() -> dict:
    """Cache des textes déjà extraits, keyé par ID Drive."""
    if REPORTS_CACHE.exists():
        return json.loads(REPORTS_CACHE.read_text("utf-8"))
    return {}


def save_reports_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    REPORTS_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), "utf-8")


def fetch_drive_pdf_text(file_id: str) -> str:
    """Télécharge un PDF public depuis Google Drive et en extrait le texte.
    Retourne '' en cas d'échec (PDF privé, fichier non-PDF, etc.)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        print("⚠  pypdf non installé : pip install pypdf")
        return ""

    base_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    sess = requests.Session()
    try:
        r = sess.get(base_url, timeout=60, allow_redirects=True, stream=True)
        r.raise_for_status()
    except Exception as e:
        print(f"  ⚠  download Drive {file_id!r} échec : {e}")
        return ""

    # Si Drive renvoie une page HTML de confirmation (gros fichiers > 25 Mo)
    ctype = r.headers.get("Content-Type", "")
    if "text/html" in ctype:
        # Extraire le token de confirmation
        body = r.text[:50000]  # limite pour ne pas exploser la mémoire
        token_match = re.search(r'name="confirm"\s+value="([^"]+)"', body) or \
                      re.search(r'confirm=([0-9A-Za-z_-]+)', body)
        if token_match:
            token = token_match.group(1)
            try:
                r = sess.get(f"{base_url}&confirm={token}", timeout=120, allow_redirects=True, stream=True)
                r.raise_for_status()
            except Exception as e:
                print(f"  ⚠  download Drive (confirm) {file_id!r} échec : {e}")
                return ""
        else:
            print(f"  ⚠  Drive renvoie HTML pour {file_id!r} (privé ?)")
            return ""

    # Limite de taille
    content_length = int(r.headers.get("Content-Length") or 0)
    if content_length > MAX_PDF_SIZE:
        print(f"  ⚠  PDF Drive {file_id!r} trop gros ({content_length // 1024 // 1024} Mo), skip")
        return ""

    # Lire le contenu en mémoire (avec garde-fou)
    buf = io.BytesIO()
    total = 0
    for chunk in r.iter_content(chunk_size=64 * 1024):
        buf.write(chunk)
        total += len(chunk)
        if total > MAX_PDF_SIZE:
            print(f"  ⚠  PDF Drive {file_id!r} dépasse {MAX_PDF_SIZE // 1024 // 1024} Mo en streaming, skip")
            return ""

    buf.seek(0)
    # Vérifie que c'est bien un PDF
    head = buf.read(8)
    buf.seek(0)
    if not head.startswith(b"%PDF"):
        print(f"  ⚠  Drive {file_id!r} : pas un PDF (signature : {head[:8]!r})")
        return ""

    # Extraire le texte
    try:
        reader = PdfReader(buf)
        pages = []
        for p in reader.pages:
            try:
                pages.append(p.extract_text() or "")
            except Exception:
                pages.append("")
        text = "\n".join(pages)
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception as e:
        print(f"  ⚠  parsing PDF {file_id!r} échec : {e}")
        return ""


def extract_drive_links(html: str) -> list[str]:
    """Retourne la liste des IDs Drive uniques trouvés dans le HTML."""
    if not html:
        return []
    ids = DRIVE_LINK_RE.findall(html)
    # Déduppe en préservant l'ordre
    seen = set()
    unique = []
    for fid in ids:
        if fid not in seen:
            seen.add(fid)
            unique.append(fid)
    return unique


def enrich_articles_with_drive_reports(articles: list, raw_posts: list[dict],
                                       reports_cache: dict, refresh: bool) -> tuple[int, int]:
    """Pour chaque article qui contient des liens Google Drive, télécharge les PDFs
    et concatène leur texte au texte de l'article. Mise à jour in place.
    Retourne (nb_articles_enrichis, nb_pdfs_extraits)."""
    n_articles = 0
    n_pdfs = 0
    # Map id article → html brut (pour chercher les liens Drive)
    raw_html_by_slug = {}
    for post in raw_posts:
        slug = post.get("slug") or str(post["id"])
        raw_html_by_slug[slug] = post.get("content", {}).get("rendered", "")

    for art in articles:
        raw_html = raw_html_by_slug.get(art.id, "")
        drive_ids = extract_drive_links(raw_html)
        if not drive_ids:
            continue

        added = []
        for fid in drive_ids:
            if fid in reports_cache and not refresh:
                # Déjà extrait
                cached = reports_cache[fid]
                if cached.get("text"):
                    added.append(cached["text"])
            else:
                print(f"[drive] téléchargement PDF {fid} (article : {art.id})")
                text = fetch_drive_pdf_text(fid)
                reports_cache[fid] = {
                    "text": text,
                    "extracted_at": time.strftime("%Y-%m-%d"),
                    "article_slug": art.id,
                    "char_count": len(text),
                }
                if text:
                    added.append(text)
                    n_pdfs += 1
                # Pause courtoisie entre downloads
                time.sleep(1.0)

        if added:
            art.text = (art.text + "\n\n" + "\n\n".join(added)).strip()
            n_articles += 1
            print(f"  + {len(drive_ids)} rapport(s) ajouté(s) à {art.id!r} "
                  f"(+{sum(len(t) for t in added)} chars)")

    return n_articles, n_pdfs


# ----------------------------------------------------------------------
# Détection de langue
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
# NER
# ----------------------------------------------------------------------
class NERModels:
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
            "ar": ["xx_ent_wiki_sm"],
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
        self._cache[lang] = None
        return None


def normalize_name(name: str) -> str:
    name = name.strip().strip(".,;:!?\"'()[]{}«»").strip()
    return re.sub(r"\s+", " ", name)


def slugify(name: str) -> str:
    nfkd = unicodedata.normalize("NFKD", name).encode("ASCII", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", nfkd.lower()).strip("-") or f"x{abs(hash(name))%99999}"


def has_street_or_building_prefix(name: str) -> bool:
    """Filtre 1 : rejette les noms commençant par 'rue', 'hôtel', 'street'..."""
    lower = name.lower()
    return any(lower.startswith(p) for p in STREET_BUILDING_PREFIXES)


def extract_places(article: Article, ner: NERModels, blacklist: set[str], aliases: dict[str, str], use_prefix_filter: bool) -> set[str]:
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
            if use_prefix_filter and has_street_or_building_prefix(name):
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
    """Retourne {lat, lng, country, type, class, display} ou None."""
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
        "lat": float(hit["lat"]),
        "lng": float(hit["lon"]),
        "country": country,
        "display": hit.get("display_name", name),
        "type": hit.get("type", ""),
        "class": hit.get("class", ""),
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
    blacklist = {b.lower() for b in cfg.get("blacklist", [])} | DEFAULT_BLACKLIST
    overrides = cfg.get("overrides", {})
    return aliases, blacklist, overrides


# ----------------------------------------------------------------------
# Pipeline principal
# ----------------------------------------------------------------------
def run(refresh_articles: bool, regeocode: bool, min_mentions: int,
        use_prefix_filter: bool, use_type_filter: bool) -> None:
    CACHE_DIR.mkdir(exist_ok=True)

    raw_posts = fetch_all_posts(cache_articles=not refresh_articles)
    categories = fetch_categories()
    articles = [post_to_article(p, categories) for p in raw_posts]

    lang_counts: dict[str, int] = {}
    for a in articles:
        lang_counts[a.lang] = lang_counts.get(a.lang, 0) + 1
    print(f"[wp] {len(articles)} articles parsés. Langues : "
          f"{', '.join(f'{k}={v}' for k,v in sorted(lang_counts.items()))}")

    # Enrichissement : télécharger les PDFs Google Drive liés dans les articles
    # et concaténer leur texte au texte de l'article (option A : fusion).
    print("\n[drive] recherche de liens Google Drive dans les articles…")
    reports_cache = load_reports_cache()
    n_enriched, n_new_pdfs = enrich_articles_with_drive_reports(
        articles, raw_posts, reports_cache, refresh=refresh_articles
    )
    save_reports_cache(reports_cache)
    cached_count = sum(1 for v in reports_cache.values() if v.get("text"))
    print(f"[drive] {n_enriched} article(s) enrichi(s), {n_new_pdfs} PDF(s) nouvellement extraits, "
          f"{cached_count} PDF(s) en cache au total")

    print(f"\n[filtres] préfixes={'ON' if use_prefix_filter else 'OFF'} · "
          f"types Nominatim={'ON' if use_type_filter else 'OFF'} · "
          f"min mentions={min_mentions}")

    ner = NERModels()
    aliases, blacklist, overrides = load_aliases_and_blacklist()

    # Étape 1 : extraction NER (avec filtre préfixe optionnel)
    place_to_articles: dict[str, list[str]] = {}
    print("\n[ner] extraction des lieux par article…")
    for art in tqdm(articles):
        for place in extract_places(art, ner, blacklist, aliases, use_prefix_filter):
            place_to_articles.setdefault(place, []).append(art.id)
    print(f"[ner] {len(place_to_articles)} lieux uniques extraits")

    # Étape 2 : filtre par fréquence (avant géocodage = on évite des appels Nominatim inutiles)
    before_freq = len(place_to_articles)
    if min_mentions > 1:
        place_to_articles = {
            name: ids for name, ids in place_to_articles.items()
            if len(set(ids)) >= min_mentions or name in overrides
        }
        dropped_freq = before_freq - len(place_to_articles)
        print(f"[filtre fréquence] {dropped_freq} lieux écartés (mentionnés < {min_mentions} fois)")
    else:
        dropped_freq = 0

    # Étape 3 : géocodage + filtre type Nominatim
    geocode_cache = {} if regeocode else load_geocode_cache()
    geocoded: list[Place] = []
    rejected_by_type: list[tuple[str, str, str]] = []
    print(f"\n[geo] géocodage Nominatim (≈1 req/s, {len(place_to_articles)} lieux)…")
    try:
        for name, art_ids in tqdm(sorted(place_to_articles.items())):
            if name in overrides:
                ov = overrides[name]
                geocoded.append(Place(
                    id=slugify(name), name=name, country=ov.get("country", "—"),
                    lat=float(ov["lat"]), lng=float(ov["lng"]),
                    articles=sorted(set(art_ids)),
                ))
                continue
            res = geocode(name, geocode_cache)
            if not res:
                continue
            if use_type_filter:
                osm_type = res.get("type", "")
                if osm_type and osm_type not in ALLOWED_NOMINATIM_TYPES:
                    rejected_by_type.append((name, res.get("class", ""), osm_type))
                    continue
            geocoded.append(Place(
                id=slugify(name), name=name, country=res["country"],
                lat=res["lat"], lng=res["lng"],
                articles=sorted(set(art_ids)),
            ))
    finally:
        save_geocode_cache(geocode_cache)

    print(f"\n[geo] {len(geocoded)} lieux géocodés et retenus")
    if rejected_by_type:
        print(f"[filtre type] {len(rejected_by_type)} lieux écartés (mauvais type Nominatim)")
        sample = rejected_by_type[:10]
        for name, cls, typ in sample:
            print(f"   - {name!r} ({cls}/{typ})")
        if len(rejected_by_type) > 10:
            print(f"   ... et {len(rejected_by_type) - 10} autres")

    # Output
    out = {
        "_meta": {
            "generated": time.strftime("%Y-%m-%d"),
            "source": WP_API_BASE,
            "articles_count": len(articles),
            "places_count": len(geocoded),
            "languages": lang_counts,
            "filters": {
                "min_mentions": min_mentions,
                "prefix_filter": use_prefix_filter,
                "type_filter": use_type_filter,
            },
            "filter_stats": {
                "dropped_by_frequency": dropped_freq,
                "dropped_by_type": len(rejected_by_type),
            },
            "reports": {
                "articles_with_drive_pdfs": n_enriched,
                "pdfs_indexed": cached_count,
            },
        },
        "articles": [a.to_public() for a in articles],
        "places": [asdict(p) for p in sorted(geocoded, key=lambda x: -len(x.articles))],
    }
    OUTPUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), "utf-8")
    print(f"\n✓ {OUTPUT_FILE} écrit ({len(geocoded)} lieux, {len(articles)} enquêtes, "
          f"dont {n_enriched} enrichi(s) par PDF)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--refresh", action="store_true", help="Ignore le cache d'articles")
    parser.add_argument("--regeocode", action="store_true", help="Ignore le cache de géocodage")
    parser.add_argument("--min-mentions", type=int, default=2,
                        help="Garde seulement les lieux mentionnés au moins N fois (défaut: 2)")
    parser.add_argument("--no-prefix-filter", action="store_true",
                        help="Désactive le filtre par préfixe (rues, bâtiments)")
    parser.add_argument("--no-type-filter", action="store_true",
                        help="Désactive le filtre par type Nominatim (villes/régions/pays)")
    args = parser.parse_args()
    run(
        refresh_articles=args.refresh,
        regeocode=args.regeocode,
        min_mentions=max(1, args.min_mentions),
        use_prefix_filter=not args.no_prefix_filter,
        use_type_filter=not args.no_type_filter,
    )


if __name__ == "__main__":
    main()
