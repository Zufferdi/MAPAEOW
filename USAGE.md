# Mode d'emploi — Cartographie All Eyes On Wagner

> **Une carte interactive de toutes les enquêtes publiées sur l'influence
> russe en Afrique, en Europe et au-delà.**
>
> Chaque point sur la carte est un lieu mentionné dans une enquête.
> Cliquez dessus pour lire les enquêtes correspondantes.

→ Accéder à la carte : [https://zufferdi.github.io/MAPAEOW/](https://zufferdi.github.io/MAPAEOW/)

---

## 5 choses à savoir pour démarrer

### 1. Les points = des lieux concrets

Bamako, Bangui, Saint-Pétersbourg, Molkino, Tripoli… Chaque point représente
un endroit mentionné dans une ou plusieurs enquêtes. **Plus le point est gros,
plus l'endroit est mentionné fréquemment.**

Les points avec un **halo orange** sont les lieux les plus mentionnés. Ceux
avec un **liseré vert** sont des lieux *émergents* (qui apparaissent
beaucoup récemment dans les enquêtes).

### 2. La heatmap = la chaleur du sujet

La couche orange/jaune/rouge en arrière-plan révèle les **zones où se
concentre la couverture**. Très utile pour saisir d'un coup d'œil les
théâtres principaux : Mali, Centrafrique, Syrie, Ukraine.

### 3. Cliquer ouvre les enquêtes

Cliquez sur n'importe quel point : la sidebar à gauche s'ouvre et liste
toutes les enquêtes qui mentionnent ce lieu, triées de la plus récente à
la plus ancienne. Cliquez sur une enquête pour l'ouvrir dans un nouvel
onglet.

### 4. Pays vs lieux précis

Par défaut, la carte montre **les lieux précis** (villes, bases, sites).
Les **pays entiers** (Russie, Mali, France…) sont masqués pour ne pas
encombrer.

Cliquez sur le bouton **« Pays »** dans la barre de filtres (ou tapez `P`)
pour les faire apparaître. C'est utile pour la vue macro.

### 5. Tout est filtrable

Tu peux croiser plusieurs filtres en même temps :

- **Catégorie** : AEOW, Hybrid Warfare, Russia & Partners, Strategic Arms…
- **Langue** : Français, English (les enquêtes existent dans les deux)
- **Période** : glisse sur la timeline en bas pour filtrer une année,
  un trimestre, etc.
- **Pays / lieux précis** : voir point 4

La taille des points et la heatmap s'adaptent en temps réel aux filtres.
Le bouton « Réinitialiser » remet tout à zéro.

---

## Fonctions avancées

### Liens entre lieux

Activez le toggle **« Liens »** dans le header. La carte affiche alors les
**co-occurrences** : si deux lieux sont mentionnés ensemble dans plusieurs
enquêtes, ils sont reliés par un trait. Plus le trait est gras, plus la
co-occurrence est forte.

Très utile pour révéler les **axes** : Moscou ↔ Bangui, Bamako ↔ Kidal,
Saint-Pétersbourg ↔ Khartoum, etc.

### Visite guidée

Cliquez sur **« Visite »** dans le header (ou tapez `T`). La carte vous
emmène automatiquement sur les 6 lieux les plus mentionnés, avec une pause
de quelques secondes sur chacun. Bon point de départ pour découvrir l'outil.

### Mode comparaison

Cliquez sur **« Comparer »** dans le header (ou tapez `C`). Vous pouvez
sélectionner deux périodes différentes sur la timeline et voir les
**différences** entre elles : quels lieux gagnent en mentions, lesquels
en perdent. Utile pour suivre l'évolution d'un sujet (par exemple, le
basculement de l'attention de la Syrie vers le Mali).

### Statistiques

Cliquez sur l'onglet **« Stats »** dans la sidebar (ou tapez `S`). Vous
verrez :
- Le total d'enquêtes et de lieux dans la vue actuelle
- L'activité par mois sur la période sélectionnée
- Le top des catégories
- Le top des pays
- Les lieux émergents

Tous les chiffres reflètent les **filtres actifs**, donc utilisez les
filtres pour zoomer sur un thème ou une période avant de regarder les
stats.

### Recherche

La barre de recherche en haut permet de **localiser un lieu précis**
("Molkino", "Bangui", "Donbass"…). Appuyez sur Entrée ou cliquez sur la
suggestion pour zoomer dessus.

### Partage par lien

L'URL change automatiquement quand vous filtrez ou sélectionnez un lieu.
Pour **partager une vue précise** avec un collègue, copiez juste l'URL
(ou utilisez le bouton "Copier le lien" dans la sidebar quand un lieu
est sélectionné). À l'ouverture, votre destinataire verra la carte dans
le même état que vous.

Exemple : `https://zufferdi.github.io/MAPAEOW/#cat=Hybrid+Warfare&from=2024-01&to=2024-12`
ouvre la carte filtrée sur la catégorie Hybrid Warfare en 2024.

---

## Raccourcis clavier

| Touche | Action |
|--------|--------|
| `/` | Ouvrir la recherche |
| `Esc` | Fermer la sidebar / annuler une recherche |
| `H` | Toggle heatmap |
| `M` | Toggle markers |
| `L` | Toggle liens de co-occurrence |
| `T` | Lancer / arrêter la visite guidée |
| `C` | Activer / désactiver le mode comparaison |
| `S` | Toggle onglet statistiques |
| `R` | Réinitialiser tous les filtres |
| `P` | Afficher / cacher les pays |
| `?` | Afficher cette liste dans la carte |

---

## Comment c'est fait

La carte est mise à jour **toutes les semaines** automatiquement. Le
processus :

1. Récupération de **toutes les enquêtes** publiées sur alleyesonwagner.org
2. Téléchargement automatique des **rapports PDF** liés via Google Drive
3. Extraction des **noms de lieux** par reconnaissance d'entités nommées
   (NER), en français, anglais et russe
4. **Géocodage** des lieux via OpenStreetMap (Nominatim)
5. Filtrage et **curation manuelle** des faux positifs

Le tout est entièrement reproductible et maintenu sur
[GitHub](https://github.com/Zufferdi/MAPAEOW).

---

## Limites à connaître

- **Couverture** : la carte ne reflète **que ce qui est publié sur
  alleyesonwagner.org**. Les enquêtes d'autres médias (Le Monde, RFI,
  Bellingcat…) ne sont pas indexées.
- **Géocodage automatique** : malgré les filtres et la curation, il peut
  rester des erreurs ponctuelles sur des homonymes (un nom de ville qui
  existe dans plusieurs pays). Si vous en repérez une, signalez-la sur
  le repo.
- **Lieux non-géocodables** : certaines bases ou sites mentionnés dans les
  rapports ne sont pas dans OpenStreetMap et n'apparaissent donc pas. La
  curation manuelle (`place_aliases.json`) en force certains, mais pas
  tous.
- **Régions vagues** : "le Sahel", "la Méditerranée", "l'Afrique de
  l'Ouest" sont volontairement filtrés pour ne pas encombrer la carte.
  Seuls les lieux identifiables sont retenus.

---

## Crédits et liens

- **Enquêtes** : [All Eyes On Wagner](https://alleyesonwagner.org)
- **Écosystème** : [INPACT](https://inpact-network.com)
- **Carte** : [Leaflet](https://leafletjs.com) +
  [CartoDB](https://carto.com/) (tuiles) +
  [OpenStreetMap](https://www.openstreetmap.org)
- **Code source** : [github.com/Zufferdi/MAPAEOW](https://github.com/Zufferdi/MAPAEOW)

Pour toute question, suggestion ou correction : ouvrez une issue sur le
repo GitHub.
