# MAPAEOW — Cartographie des enquêtes All Eyes On Wagner

Carte web interactive et bilingue qui agrège les ~100 enquêtes publiées sur
[alleyesonwagner.org](https://alleyesonwagner.org) (et leurs rapports Google Drive)
et les visualise par lieu sur une carte mondiale. ~200 lieux indexés, mis à jour
automatiquement toutes les semaines.

**Live** : [https://zufferdi.github.io/MAPAEOW/](https://zufferdi.github.io/MAPAEOW/)

---

## Pour les utilisateurs (journalistes, chercheurs)

Voir [`USAGE.md`](./USAGE.md) pour le mode d'emploi.

---

## Pour les développeurs et la maintenance

### Architecture

```
MAPAEOW/
├── index.html              # Application complète (HTML+CSS+JS, 4000+ lignes, autonome)
├── data.json               # Données générées par le pipeline
├── pipeline.py             # Scraper WP + NER + géocodage + extraction PDF
├── place_aliases.json      # Curation manuelle (aliases, blacklist, overrides)
├── requirements.txt
├── og-image.png            # Image OpenGraph (partage réseaux sociaux)
├── favicon.ico, *.png      # Favicons logo INPACT
├── .github/workflows/
│   └── update-data.yml     # Run hebdomadaire (lundi 4h UTC)
└── cache/
    ├── articles.json       # Cache brut WP REST API
    ├── geocode.json        # Cache Nominatim (un appel par lieu unique)
    └── reports.json        # Cache des PDFs Google Drive extraits
```

`index.html` est volontairement **autonome** (pas de bundler, pas de framework).
Toutes les libs viennent de CDN (Leaflet + leaflet.heat + Google Fonts).

### Pipeline de données

```
WordPress API
     ↓
   Articles (HTML brut)
     ↓ (extraction des liens drive.google.com)
   PDFs Drive téléchargés + texte concaténé
     ↓
   NER spaCy multilingue (FR / EN / RU + xx_ent_wiki_sm fallback)
     ↓
   3 filtres anti-bruit (préfixes rues, fréquence ≥2, type Nominatim)
     ↓
   Aliases & blacklist & overrides (place_aliases.json, curation manuelle)
     ↓
   Nominatim (1 req/s, cache persistent)
     ↓
   data.json (consommé par index.html)
```

### Lancer le pipeline en local

```bash
pip install -r requirements.txt
python -m spacy download fr_core_news_lg
python -m spacy download en_core_web_lg
python -m spacy download ru_core_news_lg
python -m spacy download xx_ent_wiki_sm

python pipeline.py
```

Options utiles :

```bash
python pipeline.py --refresh           # ignore le cache d'articles
python pipeline.py --regeocode         # ré-interroge Nominatim
python pipeline.py --min-mentions 1    # garder même les lieux mentionnés 1×
python pipeline.py --no-prefix-filter  # désactive le filtre rues/bâtiments
python pipeline.py --no-type-filter    # désactive le filtre par type Nominatim
```

Servir la carte localement :

```bash
python -m http.server 8000
# puis ouvrir http://localhost:8000
```

### GitHub Actions

Le workflow `update-data.yml` tourne automatiquement chaque lundi à 4h UTC, et
peut être déclenché manuellement via Actions → Update map data → Run workflow.
Trois inputs disponibles :

- `regeocode` : ignorer le cache géocodage (utile après ajout d'un override)
- `refresh` : ignorer le cache articles (utile si erreurs de scraping)
- `min_mentions` : seuil minimum de mentions par lieu (défaut : 2)

Le commit automatique met à jour `data.json` et les fichiers du cache.

### Curer les données

`place_aliases.json` est édité à la main. C'est le levier principal pour la
qualité du dataset.

```json
{
  "aliases": {
    "Sevare": "Sévaré",
    "Russia": "Russie",
    "Bois Rouge": "Bangui"
  },
  "blacklist": [
    "wagner",
    "africa corps",
    "sahel"
  ],
  "overrides": {
    "Molkino": {"lat": 44.866, "lng": 39.343, "country": "Russie"},
    "Concord": {"lat": 59.93, "lng": 30.35, "country": "Russie"}
  }
}
```

- **`aliases`** : variantes orthographiques (FR ↔ EN, accents, apostrophes…) qui
  fusionnent vers une forme canonique. Appliqué après extraction NER, avant
  géocodage.
- **`blacklist`** : faux positifs (organisations, fragments, ethnonymes pris pour
  des lieux par le NER). Appliqué pendant l'extraction.
- **`overrides`** : coordonnées forcées pour les lieux que Nominatim géocode mal
  (bases militaires absentes d'OSM, homonymes ambigus, sociétés à l'adresse
  connue…). Appliqué à la place de l'appel Nominatim ; **bypass aussi le filtre
  `min_mentions`** (utile pour forcer un lieu peu mentionné mais important).

Après édition, relancer le workflow avec `regeocode=true` pour que les nouveaux
overrides soient pris en compte.

### Stratégies de filtrage du bruit

Le pipeline applique trois filtres successifs pour éliminer les faux positifs :

1. **Préfixes** (avant NER) : rejette les noms commençant par "rue", "hôtel",
   "ambassade", "stadium"… (cf. `STREET_BUILDING_PREFIXES`)
2. **Fréquence** (après NER) : ne garde que les lieux mentionnés ≥ 2 fois
   (sauf overrides), ce qui élimine ~75% des candidats sans appel Nominatim.
3. **Type Nominatim** (après géocodage) : rejette les `restaurant`, `highway`,
   `building`… ne garde que les `country / city / town / village / region…`
   (cf. `ALLOWED_NOMINATIM_TYPES`)

Les statistiques de chaque filtre sont visibles dans `data.json._meta.filter_stats`.

### Mode bilingue (FR / EN)

L'utilisateur choisit la langue sur la landing. Côté front :
- L'UI bascule en FR ou EN
- Un filtre langue est appliqué automatiquement (`state.filters.languages = {fr}`)
- La carte affiche alors uniquement les markers/heatmap des articles FR
- L'utilisateur peut désactiver le filtre langue pour voir tout

Côté logique : `state.filters.languages` filtre les `articles` au niveau de
chaque lieu, donc la taille des markers reflète dynamiquement le compte filtré.

### Pays vs lieux précis

Par défaut, les **pays** (Russie, Mali, France…) sont **masqués** sur la carte —
seuls les ~120 lieux précis (villes, sites, régions identifiables) sont
visibles. L'utilisateur peut activer le bouton "Pays" (ou raccourci `P`) pour
les afficher.

Détection côté front : `isCountry(place)` compare `place.name` et `place.country`
(insensible à la casse, gère les préfixes type "États-Unis" vs
"États-Unis d'Amérique").

### Crédits

- Carte : [Leaflet](https://leafletjs.com), tuiles
  [CartoDB Dark Matter](https://carto.com/attributions),
  [OpenStreetMap](https://www.openstreetmap.org/copyright)
- Heatmap : [Leaflet.heat](https://github.com/Leaflet/Leaflet.heat)
- NER : [spaCy](https://spacy.io)
- Géocodage : [Nominatim / OSM](https://nominatim.org)
- Extraction PDF : [pypdf](https://pypdf.readthedocs.io/)
- Polices : Instrument Serif, Manrope, JetBrains Mono (Google Fonts)

### Licence et usage

Code sous licence MIT. Les enquêtes et rapports cartographiés appartiennent à
leurs auteurs respectifs (All Eyes On Wagner, INPACT). La carte fait des liens
vers les articles originaux et n'en reproduit pas le contenu.
