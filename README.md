# INPACT — Cartographie des Enquêtes

Carte interactive des lieux mentionnés dans les enquêtes publiées sur
[alleyesonwagner.org](https://alleyesonwagner.org).

## Fonctionnalités

- **Heatmap + points** des lieux mentionnés, taille proportionnelle au nombre
  d'enquêtes
- **Sidebar** : clic sur un lieu → liste des enquêtes qui le mentionnent,
  triées par date
- **Filtres par catégorie** (chips multi-select : AEOW, Hybrid Warfare,
  Russia & Partners, Strategic arms and Disruptive Tech)
- **Timeline avec brush-selection** sous la carte : glisse pour sélectionner
  une plage temporelle, drag les poignées pour ajuster, déplace la sélection
  entière, double-clic pour réinitialiser
- **Liens de co-occurrence** (toggle "Liens" dans le header) : chaque paire
  de lieux mentionnés ensemble dans une enquête est reliée par une polyline,
  pondérée par le nombre d'articles partagés. Révèle les axes Russie→Libye→Mali,
  Conakry→Bamako, etc.
- **Recherche** par nom de lieu (Enter pour zoomer)
- **Pipeline NER multilingue** : FR / EN / RU / AR (détection de script
  cyrillique/arabe automatique)

## Démarrer en 30 secondes (avec les données de démo)

La carte fonctionne immédiatement avec un échantillon de données embarqué.

```bash
cd inpact-map
python -m http.server 8000
```

Ouvre [http://localhost:8000](http://localhost:8000).

## Régénérer depuis le site

```bash
# Installer une fois
pip install -r requirements.txt
python -m spacy download fr_core_news_lg
python -m spacy download en_core_web_lg
python -m spacy download ru_core_news_lg
python -m spacy download xx_ent_wiki_sm   # fallback arabe + multilingue

# Lancer
python pipeline.py
```

Le pipeline :
1. Récupère tous les articles via l'API REST WordPress
2. Détecte la langue par script (cyrillique → ru, arabe → ar, sinon fr/en
   par mots usuels)
3. Charge à la demande le bon modèle spaCy par langue
4. Extrait les entités GPE/LOC, applique aliases et blacklist
5. Géocode chaque lieu via Nominatim (≈1 req/s, mis en cache)
6. Écrit `data.json` avec les statistiques par langue

```bash
python pipeline.py --refresh     # force re-scrape des articles
python pipeline.py --regeocode   # force re-géocodage
python pipeline.py --site URL    # cible un autre WordPress
```

Les caches sont dans `cache/` — le second run est quasi instantané.

## Curer les résultats

`place_aliases.json` (créé au premier run) contient :

- **`aliases`** : variantes orthographiques → forme canonique
  (`"Москва": "Moscou"`, `"Sevare": "Sévaré"`)
- **`blacklist`** : faux positifs courants à filtrer (organisations
  taguées comme lieux par le NER : `"Africa Corps"`, `"Wagner Group"`)
- **`overrides`** : coordonnées forcées pour les lieux que Nominatim trouve
  mal (bases militaires, ZI russes…) :
  ```json
  "overrides": {
    "Al Khadim (base aérienne)": {"lat": 32.4, "lng": 21.9, "country": "Libye"}
  }
  ```

Édite, relance `python pipeline.py`.

## Structure

```
inpact-map/
├── index.html              # Carte + filtres + timeline + sidebar (autonome)
├── data.json               # Données générées par le pipeline
├── pipeline.py             # Scraper WP → NER → Géocodeur → JSON
├── place_aliases.json      # Curation manuelle (édité à la main)
├── requirements.txt
└── cache/
    ├── articles.json       # Cache brut WP REST API
    └── geocode.json        # Cache Nominatim
```

`index.html` essaie de charger `./data.json` (servi via HTTP) et tombe sur
les données embarquées en fallback (cas `file://`). Pour la prod, n'importe
quel hébergeur statique fait l'affaire (GitHub Pages, Netlify, S3…).

## Pistes d'évolution

- **Légende** des couleurs heatmap et de la pondération des liens
- **Export** d'une vue filtrée (PNG via `dom-to-image`, ou URL avec
  paramètres pour partager une recherche : `?cat=AEOW&from=2025-01&to=2025-12`)
- **Désambiguïsation Wikidata** pour "Niger" (pays vs fleuve), "Tchad"
  (pays vs lac), etc., via `qwikidata` ou un appel à l'API
- **Mode "axes"** : au lieu de la heatmap, animer l'apparition chronologique
  des lieux (curseur sur la timeline = état de la cartographie à cette date)
- **Co-occurrence triadique** : groupes de 3+ lieux récurrents (révèle des
  schémas plus structurés que les paires)

## Crédits

- Carte : [Leaflet](https://leafletjs.com), tuiles
  [CartoDB Dark Matter](https://carto.com/attributions),
  [OpenStreetMap](https://www.openstreetmap.org/copyright)
- Heatmap : [Leaflet.heat](https://github.com/Leaflet/Leaflet.heat)
- NER : [spaCy](https://spacy.io)
- Géocodage : [Nominatim / OSM](https://nominatim.org)
