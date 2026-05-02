#!/usr/bin/env python3
"""
INPACT Investigation Mapper — Pipeline d'extraction
====================================================

Récupère les articles via l'API REST WordPress.com,
extrait les lieux mentionnés (NER multilingue FR/EN/RU/AR),
les géocode via Nominatim, applique 3 filtres automatiques anti-bruit,
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
# Préfixes typiques de noms qui ne sont pas des "lieux d'enquête" pertinents.
STREET_BUILDING_PREFIXES = (
    # rues
    "rue ", "avenue ", "boulevard ", "place ", "impasse ", "allée ", "allee ",
    "chemin ", "quai ", "route ", "voie ", "cours ", "sentier ",
    "street ", "road ", "square ", "lane ",
    "calle ", "plaza ", "avenida ",
    "улица ", "проспект ", "площадь ",  # russe
    # bâtiments
    "hôtel ", "hotel ", "palais ", "palace ", "stade ", "stadium ",
    "aéroport ", "airport ", "gare ", "station ", "ambassade ", "embassy ",
    "musée ", "museum ", "tour ", "cathédrale ", "cathedral ", "mosquée ", "mosque ",
    "université ", "university ", "lycée ", "collège ",
    "restaurant ", "café ", "bar ", "club ",
    "monsieur ", "madame ", "saint-", "sainte-",  # noms propres mal taggés
)

# Types Nominatim qu'on garde (les autres = filtrés)
# Liste basée sur la doc Nominatim : https://nominatim.org/release-docs/develop/api/Output/
ALLOWED_NOMINATIM_TYPES = {
    # places (le plus important)
    "country", "state", "region", "province", "county", "district",
    "city", "town", "village", "hamlet", "municipality", "neighbourhood",
    "suburb", "quarter", "borough", "locality", "island", "archipelago",
    "continent", "ocean", "sea", "bay",
    # boundary
    "administrative", "national_park", "protected_area",
    # natural / désigné mais utile pour zones de conflit
    "peak", "mountain_range", "desert", "plateau", "valley",
}

# Catégories blacklist par défaut (organisations, médias, mois...)
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
    """Extrait les lieux d'un article (compatibilité). Pour aussi extraire
    les personnes/orgs en une seule passe NER, utiliser extract_entities()."""
    places, _ = extract_places_and_entities(article, ner, blacklist, aliases, use_prefix_filter)
    return places


def extract_places_and_entities(article: Article, ner: NERModels,
                                 blacklist: set[str], aliases: dict[str, str],
                                 use_prefix_filter: bool) -> tuple[set[str], list[tuple[str, str]]]:
    """Une seule passe NER pour les lieux ET les personnes/organisations.
    Retourne (set des lieux canoniques, liste de (name, type) pour les entités).
    Les entités gardent leur casse d'origine pour préserver les noms propres."""
    nlp = ner.get(article.lang)
    if nlp is None:
        return set(), []

    places: set[str] = set()
    entity_hits: list[tuple[str, str]] = []  # (name, type) pour personnes/orgs
    text = article.text
    for start in range(0, len(text), 800_000):
        chunk = text[start:start + 800_000]
        doc = nlp(chunk)
        for ent in doc.ents:
            label = ent.label_
            # Lieux
            if label in {"GPE", "LOC"}:
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
            # Personnes / organisations
            elif label in {"PERSON", "PER", "ORG"}:
                name = normalize_name(ent.text)
                if not (MIN_PLACE_LEN <= len(name) <= MAX_PLACE_LEN):
                    continue
                # Type unifié : PERSON pour personnes (PER ou PERSON selon modèle), ORG pour orgs
                ent_type = "PERSON" if label in {"PERSON", "PER"} else "ORG"
                entity_hits.append((name, ent_type))
    return places, entity_hits


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
        "type": hit.get("type", ""),       # ex: "city", "village", "highway"
        "class": hit.get("class", ""),     # ex: "place", "boundary", "amenity"
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
# Entities (PERSON / ORG) - extraction et curation
# ----------------------------------------------------------------------
ENTITIES_FILE = Path("entities.json")
ENTITIES_CURATION = Path("entities_curation.json")  # JSON plutôt que YAML pour rester sans dep

# Blacklist par défaut : médias, journalistes connus, partenaires AEOW.
# Extensible via entities_curation.json (key = nom normalisé, value: {keep: false}).
DEFAULT_ENTITY_BLACKLIST = {
    # Médias / agences
    "afp", "reuters", "ap", "bbc", "cnn", "rfi", "tass", "ria novosti",
    "le monde", "the new york times", "the washington post", "the guardian",
    "le figaro", "libération", "mediapart", "rfe/rl", "radio free europe",
    "radio liberty", "der spiegel", "die zeit", "the wall street journal",
    "financial times", "ft", "bloomberg", "voice of america", "voa",
    "courrier international", "france 24", "tv5 monde", "deutsche welle",
    "novaya gazeta", "moscow times", "the moscow times", "meduza",
    "rt", "russia today", "sputnik", "channel one",
    # Partenaires AEOW connus
    "all eyes on wagner", "aeow", "inpact", "openfacto", "open facto",
    "forbidden stories", "bellingcat", "the continent", "eic", "eic.network",
    "european investigative collaborations",
    "dossier centre", "dossier center", "istories", "openDemocracy",
    # Plateformes / outils mentionnés mais pas des "sujets"
    "google", "facebook", "twitter", "x", "instagram", "telegram", "tiktok",
    "youtube", "whatsapp", "signal", "wikipedia", "wikileaks",
    "openstreetmap", "osm", "google maps", "google earth", "google drive",
    "linkedin",
    # Génériques institutionnels (rarement éclairants en eux-mêmes)
    "european union", "union européenne", "ue", "eu", "european commission",
    "commission européenne", "european parliament", "parlement européen",
    "united nations", "nations unies", "onu", "un", "nato", "otan",
    "african union", "union africaine",
}


def normalize_entity_key(name: str) -> str:
    """Clé canonique pour merger les variantes d'une même entité."""
    s = name.strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)  # retire ponctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s


def load_entities_curation() -> dict:
    """Lit entities_curation.json. Le fichier ne contient QUE les entités où
    l'utilisateur a fait une intervention manuelle (γ-light) :
      {
        "key": {
          "keep": false,                    # blacklister
          "aliases": ["alt name 1", ...],   # noms à fusionner sous cette clé
          "canonical_name": "Nom propre",   # forcer un nom d'affichage
          "type": "PERSON" | "ORG",         # forcer le type si ambigu
          "notes": "Texte libre"
        },
        ...
      }
    Les entités absentes de ce fichier sont gérées en mode auto.
    """
    if not ENTITIES_CURATION.exists():
        return {}
    try:
        return json.loads(ENTITIES_CURATION.read_text("utf-8"))
    except Exception as e:
        print(f"⚠  entities_curation.json illisible : {e}")
        return {}


def build_entities(articles: list, place_to_articles: dict[str, list[str]],
                    ner: NERModels, blacklist: set[str], aliases: dict[str, str],
                    use_prefix_filter: bool, min_mentions: int) -> dict:
    """Génère le dataset entities en réutilisant la passe NER faite pour les lieux.
    Mais comme `extract_places` ne retourne que les lieux, on relance NER ici.
    Pour économiser : on demande UNE passe avec les deux types d'extraction."""
    curation = load_entities_curation()
    # Reverse alias map : pour chaque variante listée dans curation, retrouve la clé canonique
    alias_to_key: dict[str, str] = {}
    for key, info in curation.items():
        for alt in info.get("aliases", []):
            alias_to_key[normalize_entity_key(alt)] = key
        # Le nom canonique lui-même
        if info.get("canonical_name"):
            alias_to_key[normalize_entity_key(info["canonical_name"])] = key

    # Compteurs
    entity_data: dict[str, dict] = {}  # key -> {name, type, count, articles, langs}
    print("\n[entities] extraction des personnes et organisations…")
    for art in tqdm(articles):
        _, hits = extract_places_and_entities(art, ner, blacklist, aliases, use_prefix_filter)
        # Dédupe par article : on compte 1 mention par article même si l'entité apparaît 5x
        seen_in_article: dict[str, str] = {}  # key -> type (premier vu)
        for name, etype in hits:
            norm = normalize_entity_key(name)
            if not norm:
                continue
            # Skip si dans la blacklist d'entités
            if norm in DEFAULT_ENTITY_BLACKLIST:
                continue
            # Résolution alias via curation
            canonical_key = alias_to_key.get(norm, norm)
            # Skip si l'entité curée est marquée keep=false
            curated = curation.get(canonical_key, {})
            if curated.get("keep") is False:
                continue
            if canonical_key not in seen_in_article:
                seen_in_article[canonical_key] = etype

        for canonical_key, etype in seen_in_article.items():
            curated = curation.get(canonical_key, {})
            display_name = curated.get("canonical_name") or _restore_display_name(canonical_key, hits)
            forced_type = curated.get("type")
            if canonical_key not in entity_data:
                entity_data[canonical_key] = {
                    "key": canonical_key,
                    "name": display_name,
                    "type": forced_type or etype,
                    "count": 0,
                    "articles": [],
                    "langs": {},
                    "notes": curated.get("notes", ""),
                    "curated": bool(curated),
                }
            ent = entity_data[canonical_key]
            ent["count"] += 1
            ent["articles"].append(art.id)
            ent["langs"][art.lang] = ent["langs"].get(art.lang, 0) + 1

    # Filtre fréquence : on garde seulement les entités mentionnées au moins min_mentions fois,
    # SAUF si l'entité a été curée (note ou alias définis)
    before = len(entity_data)
    filtered = {
        k: v for k, v in entity_data.items()
        if v["count"] >= min_mentions or v["curated"]
    }
    dropped = before - len(filtered)
    print(f"[entities] {before} entités candidates, {dropped} écartées par fréquence (<{min_mentions}), "
          f"{len(filtered)} retenues")

    # Calcul des lieux co-mentionnés pour chaque entité
    # Un lieu est "co-mentionné" avec une entité s'il apparaît dans un des articles où l'entité est mentionnée
    place_articles_set: dict[str, set[str]] = {p: set(ids) for p, ids in place_to_articles.items()}
    for key, ent in filtered.items():
        ent_articles = set(ent["articles"])
        cooc: list[tuple[str, int]] = []
        for place, art_ids in place_articles_set.items():
            shared = len(ent_articles & art_ids)
            if shared > 0:
                cooc.append((place, shared))
        cooc.sort(key=lambda x: -x[1])
        # On garde le top 30 pour ne pas exploser le JSON
        ent["co_places"] = [{"place": p, "count": c} for p, c in cooc[:30]]

    # Calcul des dates first/last seen
    article_dates = {a.id: a.date for a in articles}
    for ent in filtered.values():
        dates = [article_dates.get(aid) for aid in ent["articles"]]
        dates = [d for d in dates if d]
        if dates:
            ent["first_seen"] = min(dates)
            ent["last_seen"] = max(dates)
        else:
            ent["first_seen"] = ent["last_seen"] = None

    return filtered


def _restore_display_name(key: str, hits: list[tuple[str, str]]) -> str:
    """Choisit un display name correctement capitalisé parmi les variantes vues."""
    candidates = [name for name, _ in hits if normalize_entity_key(name) == key]
    if not candidates:
        return key.title()
    # On prend la variante la plus longue (souvent la plus complète : "Yevgeny Prigozhin" > "Prigozhin")
    # Si égalité, celle avec le plus de majuscules (= mieux capitalisée)
    candidates.sort(key=lambda s: (-len(s), -sum(1 for c in s if c.isupper())))
    return candidates[0]


def write_entities_outputs(entities: dict, articles_count: int) -> None:
    """Écrit entities.json (consommé par UI) et un template entities_curation.json
    si pas encore présent."""
    out = {
        "_meta": {
            "generated": time.strftime("%Y-%m-%d"),
            "entities_count": len(entities),
            "articles_count": articles_count,
        },
        "entities": sorted(entities.values(), key=lambda e: -e["count"]),
    }
    ENTITIES_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), "utf-8")
    print(f"✓ {ENTITIES_FILE} écrit ({len(entities)} entités)")

    # Génère un template de curation aidant si pas existant
    if not ENTITIES_CURATION.exists():
        template = {
            "_doc": "Curation manuelle des entités (γ-light). Format : key -> {keep, aliases, canonical_name, type, notes}. Une entité absente de ce fichier est gérée en mode auto. Exemple :",
            "_example": {
                "prigozhin": {
                    "keep": True,
                    "aliases": ["Yevgeny Prigozhin", "Evgueni Prigojine", "Пригожин"],
                    "canonical_name": "Yevgeny Prigozhin",
                    "type": "PERSON",
                    "notes": "Fondateur de Wagner Group, mort en août 2023."
                }
            }
        }
        ENTITIES_CURATION.write_text(json.dumps(template, ensure_ascii=False, indent=2), "utf-8")
        print(f"  ↳ {ENTITIES_CURATION} template créé (édite-le pour enrichir)")


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
    entity_curation = load_entities_curation()
    # Reverse alias map pour entités
    entity_alias_to_key: dict[str, str] = {}
    for key, info in entity_curation.items():
        if not isinstance(info, dict):
            continue
        for alt in info.get("aliases", []):
            entity_alias_to_key[normalize_entity_key(alt)] = key
        if info.get("canonical_name"):
            entity_alias_to_key[normalize_entity_key(info["canonical_name"])] = key

    # Étape 1 : extraction NER (lieux + entités personnes/orgs en une seule passe)
    place_to_articles: dict[str, list[str]] = {}
    entity_data: dict[str, dict] = {}  # key -> agrégat
    print("\n[ner] extraction des lieux et entités par article…")
    for art in tqdm(articles):
        places, entity_hits = extract_places_and_entities(
            art, ner, blacklist, aliases, use_prefix_filter
        )
        # Lieux
        for place in places:
            place_to_articles.setdefault(place, []).append(art.id)
        # Entités : dédup par article (1 mention par article max)
        seen_keys: dict[str, str] = {}  # key -> type (premier vu)
        seen_names: dict[str, str] = {}  # key -> nom le mieux capitalisé vu
        for name, etype in entity_hits:
            norm = normalize_entity_key(name)
            if not norm or norm in DEFAULT_ENTITY_BLACKLIST:
                continue
            canonical_key = entity_alias_to_key.get(norm, norm)
            curated = entity_curation.get(canonical_key, {})
            if isinstance(curated, dict) and curated.get("keep") is False:
                continue
            if canonical_key not in seen_keys:
                seen_keys[canonical_key] = etype
                seen_names[canonical_key] = name
            else:
                # Garde la variante la plus longue / la mieux capitalisée
                prev = seen_names[canonical_key]
                if (len(name), sum(1 for c in name if c.isupper())) > \
                   (len(prev), sum(1 for c in prev if c.isupper())):
                    seen_names[canonical_key] = name
        for canonical_key, etype in seen_keys.items():
            curated = entity_curation.get(canonical_key, {})
            curated = curated if isinstance(curated, dict) else {}
            if canonical_key not in entity_data:
                entity_data[canonical_key] = {
                    "key": canonical_key,
                    "name": curated.get("canonical_name") or seen_names[canonical_key],
                    "type": curated.get("type") or etype,
                    "count": 0,
                    "articles": [],
                    "langs": {},
                    "notes": curated.get("notes", ""),
                    "curated": bool(curated),
                }
            ent = entity_data[canonical_key]
            ent["count"] += 1
            ent["articles"].append(art.id)
            ent["langs"][art.lang] = ent["langs"].get(art.lang, 0) + 1
            # Met à jour le name si on rencontre une variante mieux capitalisée
            if not curated.get("canonical_name"):
                cur = ent["name"]
                cand = seen_names[canonical_key]
                if (len(cand), sum(1 for c in cand if c.isupper())) > \
                   (len(cur), sum(1 for c in cur if c.isupper())):
                    ent["name"] = cand
    print(f"[ner] {len(place_to_articles)} lieux uniques · {len(entity_data)} entités candidates")

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
    rejected_by_type: list[tuple[str, str, str]] = []  # (name, class, type)
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
            # Filtre type Nominatim : on ne garde que villes / régions / pays
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
        # Échantillon des 10 premiers pour info
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

    # ============================================================
    # Sortie entités
    # ============================================================
    print("\n[entities] finalisation…")
    # Filtre fréquence (mêmes règles que pour les lieux : >=min_mentions, sauf curées)
    before_ent = len(entity_data)
    filtered_entities = {
        k: v for k, v in entity_data.items()
        if v["count"] >= min_mentions or v["curated"]
    }
    dropped_ent = before_ent - len(filtered_entities)
    print(f"[entities] {dropped_ent} entités écartées (mentionnées < {min_mentions} fois et non curées)")

    # Calcul des dates first/last seen
    article_dates = {a.id: a.date for a in articles}
    # Calcul des lieux co-mentionnés (lieux qui apparaissent dans les articles où l'entité est citée)
    place_articles_set: dict[str, set[str]] = {p: set(ids) for p, ids in place_to_articles.items()}
    geocoded_names = {pl.name: pl for pl in geocoded}  # pour ne garder que les lieux finalement géocodés
    for ent in filtered_entities.values():
        ent_articles = set(ent["articles"])
        # Co-mentions
        cooc: list[tuple[str, int]] = []
        for place, art_ids in place_articles_set.items():
            if place not in geocoded_names:
                continue  # ignore les lieux écartés par filtre type
            shared = len(ent_articles & art_ids)
            if shared > 0:
                cooc.append((place, shared))
        cooc.sort(key=lambda x: -x[1])
        ent["co_places"] = [
            {"place": p, "place_id": geocoded_names[p].id, "count": c}
            for p, c in cooc[:30]
        ]
        # First / last seen
        dates = [article_dates.get(aid) for aid in ent["articles"]]
        dates = [d for d in dates if d]
        ent["first_seen"] = min(dates) if dates else None
        ent["last_seen"] = max(dates) if dates else None

    # Co-mentions entité↔entité (top 10 par entité, pour suggestions de fiches liées)
    for ent in filtered_entities.values():
        ent_articles = set(ent["articles"])
        cooc_e: list[tuple[str, str, int]] = []
        for other_key, other_ent in filtered_entities.items():
            if other_key == ent["key"]:
                continue
            shared = len(ent_articles & set(other_ent["articles"]))
            if shared > 0:
                cooc_e.append((other_key, other_ent["name"], shared))
        cooc_e.sort(key=lambda x: -x[2])
        ent["co_entities"] = [
            {"key": k, "name": n, "count": c} for k, n, c in cooc_e[:10]
        ]

    # Écriture entities.json
    entities_out = {
        "_meta": {
            "generated": time.strftime("%Y-%m-%d"),
            "source": WP_API_BASE,
            "articles_count": len(articles),
            "entities_count": len(filtered_entities),
            "filter_stats": {
                "candidates": before_ent,
                "dropped_by_frequency": dropped_ent,
            },
            "type_breakdown": {
                "PERSON": sum(1 for e in filtered_entities.values() if e["type"] == "PERSON"),
                "ORG": sum(1 for e in filtered_entities.values() if e["type"] == "ORG"),
            },
        },
        "entities": sorted(filtered_entities.values(), key=lambda e: -e["count"]),
    }
    ENTITIES_FILE.write_text(json.dumps(entities_out, ensure_ascii=False, indent=2), "utf-8")
    print(f"✓ {ENTITIES_FILE} écrit ({len(filtered_entities)} entités)")

    # Crée un template de curation si pas existant
    if not ENTITIES_CURATION.exists():
        template = {
            "_doc": "Curation manuelle des entités (γ-light). Format : key -> {keep, aliases, canonical_name, type, notes}. Une entité absente de ce fichier est gérée en mode auto.",
            "_example_prigozhin": {
                "keep": True,
                "aliases": ["Yevgeny Prigozhin", "Evgueni Prigojine", "Пригожин"],
                "canonical_name": "Yevgeny Prigozhin",
                "type": "PERSON",
                "notes": "Fondateur de Wagner Group, mort en août 2023."
            }
        }
        ENTITIES_CURATION.write_text(json.dumps(template, ensure_ascii=False, indent=2), "utf-8")
        print(f"  ↳ {ENTITIES_CURATION} template créé")


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
