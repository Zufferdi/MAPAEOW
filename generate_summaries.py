#!/usr/bin/env python3
"""
Génération de résumés thématiques par lieu via Claude API.
====================================================================

Pour chaque lieu de data.json, génère une phrase synthétique de 2-3 phrases
qui décrit factuellement les sujets des enquêtes qui le mentionnent.
Les résumés sont stockés dans place_summaries.json (consommé par index.html).

Usage :
    export ANTHROPIC_API_KEY=sk-ant-...
    python generate_summaries.py
    python generate_summaries.py --only-missing      # ne régénère que les lieux sans résumé
    python generate_summaries.py --place molkino     # un seul lieu (debug)
    python generate_summaries.py --min-articles 2    # ignore les lieux à 1 article
    python generate_summaries.py --model haiku       # haiku (par défaut) | sonnet
    python generate_summaries.py --dry-run           # affiche prompts sans appeler

Coût indicatif (189 lieux × ~750 input tokens × ~80 output tokens) :
  - Claude Haiku 4.5 : ~0.20 USD
  - Claude Sonnet 4.6 : ~0.60 USD
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

DATA_FILE = Path("data.json")
SUMMARIES_FILE = Path("place_summaries.json")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}

# Prompt système : strict, descriptif, sans hallucination, sans sensibilités politiques
SYSTEM_PROMPT_FR = """Tu es un assistant pour un projet de cartographie d'enquêtes journalistiques sur l'influence russe (Wagner, Africa Corps, opérations hybrides) en Afrique, en Europe et au-delà.

Pour chaque lieu, tu reçois une liste d'enquêtes (titre + excerpt) qui le mentionnent. Tu dois écrire un court résumé thématique de 2 phrases (60-100 mots au total) qui répond à : « Pourquoi ce lieu apparaît dans ces enquêtes ? »

RÈGLES STRICTES :
- Écris UNIQUEMENT en français. Toutes les enquêtes te sont fournies en EN ou FR mais le résumé est toujours en français.
- Reste FACTUEL et DESCRIPTIF. Pas d'adjectifs spéculatifs, pas d'analyse politique.
- N'invente RIEN qui ne soit pas dans les excerpts fournis. En cas de doute, reste vague.
- Ne nomme aucun témoin, source, journaliste, ou personne qui apparaîtrait comme "victime", "survivant", ou "lanceur d'alerte". Tu peux nommer les figures publiques officielles (chefs d'État, dirigeants Wagner, ministres).
- Mentionne 1 à 3 thèmes ou acteurs récurrents (ex : "Wagner Group", "Africa Corps", "réseaux logistiques", "désinformation", "or", "drones turcs"…).
- Si un seul article mentionne le lieu, fais une phrase courte (30-50 mots) qui résume le contenu.
- Pas de formulation "selon les enquêtes" ou "il semble que" : va droit au fait.
- Pas de conclusion morale, pas d'éditorialisation. Juste les faits cartographiés.
- Format de sortie : juste le texte du résumé, sans préambule, sans guillemets, sans titre."""

SYSTEM_PROMPT_EN = """You are an assistant for a mapping project of journalistic investigations into Russian influence (Wagner, Africa Corps, hybrid operations) in Africa, Europe and beyond.

For each location, you receive a list of investigations (title + excerpt) that mention it. Write a short thematic summary of 2 sentences (60-100 words total) answering: "Why does this location appear in these investigations?"

STRICT RULES:
- Write ONLY in English. All investigations are provided in EN or FR but the summary is always in English.
- Stay FACTUAL and DESCRIPTIVE. No speculative adjectives, no political analysis.
- Do NOT invent anything not in the provided excerpts. When in doubt, stay vague.
- Do not name witnesses, sources, journalists, or anyone appearing as "victim", "survivor" or "whistleblower". You may name public figures (heads of state, Wagner leadership, ministers).
- Mention 1 to 3 recurring themes or actors (e.g., "Wagner Group", "Africa Corps", "logistics networks", "disinformation", "gold", "Turkish drones"...).
- If only one article mentions the location, write a short sentence (30-50 words) summarizing its content.
- No "according to investigations" or "it seems that": go straight to the facts.
- No moral conclusions, no editorializing. Just the mapped facts.
- Output format: only the summary text, no preamble, no quotes, no title."""


def build_user_prompt(place: dict, articles: list[dict]) -> str:
    """Construit le prompt user avec les données du lieu et de ses articles."""
    lines = [
        f"LIEU : {place['name']}",
        f"PAYS : {place.get('country', '—')}",
        f"NOMBRE D'ENQUÊTES : {len(articles)}",
        "",
        "ENQUÊTES QUI MENTIONNENT CE LIEU :",
        "",
    ]
    for i, a in enumerate(articles, 1):
        title = (a.get("title") or "").strip()
        excerpt = (a.get("excerpt") or "").strip()
        category = a.get("category", "—")
        lang = a.get("lang", "—").upper()
        lines.append(f"[{i}] {title} ({category} · {lang})")
        if excerpt:
            lines.append(f"    {excerpt}")
        lines.append("")
    lines.append("Génère le résumé maintenant (texte brut, 2 phrases max, 60-100 mots).")
    return "\n".join(lines)


def call_claude(api_key: str, model: str, system_prompt: str, user_prompt: str,
                max_retries: int = 3) -> tuple[str, dict]:
    """Appelle l'API Claude. Retourne (texte, usage_info)."""
    body = {
        "model": model,
        "max_tokens": 200,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.post(API_URL, headers=headers, json=body, timeout=60)
            if r.status_code == 429:
                wait = 2 ** attempt * 5
                print(f"    rate limited, attente {wait}s...", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            text = "".join(b.get("text", "") for b in data.get("content", []))
            usage = data.get("usage", {})
            return text.strip(), usage
        except Exception as e:
            last_err = e
            wait = 2 ** attempt
            print(f"    erreur API (essai {attempt+1}/{max_retries}) : {e}", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Échec après {max_retries} essais : {last_err}")


def estimate_cost_usd(input_tokens: int, output_tokens: int, model: str) -> float:
    """Coût en USD selon le modèle."""
    if "haiku" in model:
        return (input_tokens / 1_000_000) * 1.0 + (output_tokens / 1_000_000) * 5.0
    if "sonnet" in model:
        return (input_tokens / 1_000_000) * 3.0 + (output_tokens / 1_000_000) * 15.0
    return 0.0


def load_existing_summaries() -> dict:
    if SUMMARIES_FILE.exists():
        return json.loads(SUMMARIES_FILE.read_text("utf-8"))
    return {"_meta": {}, "summaries": {}}


def save_summaries(summaries: dict) -> None:
    SUMMARIES_FILE.write_text(
        json.dumps(summaries, ensure_ascii=False, indent=2), "utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only-missing", action="store_true",
        help="Ne régénère que les lieux sans résumé existant")
    parser.add_argument("--place", default=None,
        help="Génère seulement pour ce lieu (par nom ou par id)")
    parser.add_argument("--min-articles", type=int, default=1,
        help="Ignore les lieux mentionnés moins de N fois (défaut: 1)")
    parser.add_argument("--model", choices=list(MODELS.keys()), default="haiku",
        help="Modèle Claude (défaut: haiku)")
    parser.add_argument("--dry-run", action="store_true",
        help="Affiche le 1er prompt sans appeler l'API")
    parser.add_argument("--lang", choices=["fr", "en"], default="fr",
        help="Langue de génération (défaut: fr)")
    parser.add_argument("--limit", type=int, default=None,
        help="Limite à N lieux (debug)")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not args.dry_run and not api_key:
        sys.exit("⚠  Variable d'environnement ANTHROPIC_API_KEY manquante.\n"
                 "    export ANTHROPIC_API_KEY=sk-ant-...")

    if not DATA_FILE.exists():
        sys.exit(f"⚠  {DATA_FILE} introuvable. Lance d'abord pipeline.py.")
    data = json.loads(DATA_FILE.read_text("utf-8"))
    articles_by_id = {a["id"]: a for a in data["articles"]}

    summaries_data = load_existing_summaries()
    summaries = summaries_data.get("summaries", {})

    # Sélection des lieux à traiter
    places = data["places"]
    if args.place:
        target = args.place.lower()
        places = [p for p in places if p["name"].lower() == target or p["id"] == target]
        if not places:
            sys.exit(f"⚠  Lieu {args.place!r} introuvable.")
    if args.min_articles > 1:
        places = [p for p in places if len(p["articles"]) >= args.min_articles]
    if args.only_missing:
        # Skip les lieux pour lesquels on a déjà un résumé dans la même langue
        places = [p for p in places if not summaries.get(p["id"], {}).get(args.lang)]
    if args.limit:
        places = places[:args.limit]

    if not places:
        print("Rien à générer. Tous les lieux ont déjà un résumé dans cette langue.")
        return

    model = MODELS[args.model]
    system_prompt = SYSTEM_PROMPT_FR if args.lang == "fr" else SYSTEM_PROMPT_EN

    print(f"=== Génération de résumés ({args.lang.upper()}, modèle: {args.model}) ===")
    print(f"  {len(places)} lieux à traiter")
    if args.dry_run:
        print(f"  MODE DRY-RUN : aucun appel API")
    print()

    total_input = 0
    total_output = 0
    fails = 0
    started = time.time()

    for i, place in enumerate(places, 1):
        article_ids = place.get("articles", [])
        articles = [articles_by_id[aid] for aid in article_ids if aid in articles_by_id]
        if not articles:
            continue

        prompt = build_user_prompt(place, articles)
        print(f"[{i}/{len(places)}] {place['name']} ({len(articles)} enquêtes)…",
              flush=True)

        if args.dry_run:
            if i == 1:
                print("--- PROMPT DRY-RUN ---")
                print(f"SYSTEM:\n{system_prompt[:300]}...\n")
                print(f"USER:\n{prompt}")
                print("--- /DRY-RUN ---")
            continue

        try:
            summary_text, usage = call_claude(api_key, model, system_prompt, prompt)
            in_t = usage.get("input_tokens", 0)
            out_t = usage.get("output_tokens", 0)
            total_input += in_t
            total_output += out_t

            entry = summaries.get(place["id"], {})
            entry[args.lang] = summary_text
            entry["meta"] = {
                "model": model,
                "generated": time.strftime("%Y-%m-%d"),
                "articles_count": len(articles),
                "input_tokens": in_t,
                "output_tokens": out_t,
            }
            summaries[place["id"]] = entry

            preview = summary_text[:120].replace("\n", " ")
            print(f"     → {preview}{'...' if len(summary_text) > 120 else ''}")
            print(f"       ({in_t}+{out_t} tokens, ~${estimate_cost_usd(in_t, out_t, model):.4f})")

            # Save progressivement (every 10 places)
            if i % 10 == 0:
                summaries_data["summaries"] = summaries
                summaries_data["_meta"] = {
                    "generated": time.strftime("%Y-%m-%d"),
                    "model": model,
                    "total_places": len(summaries),
                }
                save_summaries(summaries_data)

            # Pause polite entre appels
            time.sleep(0.3)

        except Exception as e:
            fails += 1
            print(f"     ✗ Échec : {e}")

    # Sauvegarde finale
    summaries_data["summaries"] = summaries
    summaries_data["_meta"] = {
        "generated": time.strftime("%Y-%m-%d"),
        "model": model,
        "total_places": len(summaries),
    }
    save_summaries(summaries_data)

    elapsed = time.time() - started
    cost = estimate_cost_usd(total_input, total_output, model)
    print()
    print(f"✓ Terminé en {elapsed:.0f}s")
    print(f"  Lieux traités : {len(places) - fails} / {len(places)}")
    print(f"  Tokens : {total_input} input + {total_output} output")
    print(f"  Coût estimé : ~${cost:.3f} USD")
    print(f"  → {SUMMARIES_FILE}")


if __name__ == "__main__":
    main()
