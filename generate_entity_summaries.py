#!/usr/bin/env python3
"""
Génération de résumés contextuels par entité (personne / organisation) via Claude API.
====================================================================

Pour chaque entité de entities.json, génère 2-3 phrases qui décrivent comment
l'entité apparaît dans les enquêtes AEOW (pas qui elle est en général — ça
c'est le rôle des notes éditoriales du curation.json).

Stocké dans entity_summaries.json (consommé par /entities/index.html).

Usage :
    export ANTHROPIC_API_KEY=sk-ant-...
    python generate_entity_summaries.py
    python generate_entity_summaries.py --only-missing  # ne régénère que ceux sans résumé
    python generate_entity_summaries.py --entity prigozhin  # une seule (debug)
    python generate_entity_summaries.py --min-mentions 3  # seulement les entités ≥ 3×
    python generate_entity_summaries.py --model haiku
    python generate_entity_summaries.py --dry-run

Coût indicatif : 0.50 - 1.50 USD pour 750 entités avec Haiku.
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
ENTITIES_FILE = Path("entities.json")
SUMMARIES_FILE = Path("entity_summaries.json")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
}

# Prompt système : focus "comment l'entité apparaît dans les enquêtes"
# (différent du prompt lieux qui se concentre sur "pourquoi le lieu apparaît")
SYSTEM_PROMPT_FR = """Tu es un assistant pour un projet de cartographie d'enquêtes journalistiques sur l'influence russe (Wagner, Africa Corps, opérations hybrides) en Afrique, en Europe et au-delà.

Pour chaque entité (personne ou organisation), tu reçois une liste d'enquêtes (titre + excerpt) qui la mentionnent. Tu dois écrire un court résumé contextuel de 2 phrases (60-100 mots au total) qui répond à : « Comment cette entité apparaît dans les enquêtes ? »

L'objectif n'est PAS de décrire qui est l'entité (cela existe déjà ailleurs), mais de **synthétiser ses apparitions concrètes** dans le corpus AEOW : quels théâtres / opérations / activités sont documentés, dans quelles enquêtes, sur quelles périodes.

RÈGLES STRICTES :
- Écris UNIQUEMENT en français. Toutes les enquêtes te sont fournies en EN ou FR mais le résumé est toujours en français.
- Reste FACTUEL et DESCRIPTIF. Pas d'adjectifs spéculatifs, pas d'analyse politique.
- N'invente RIEN qui ne soit pas dans les excerpts fournis. En cas de doute, reste vague.
- Ne nomme aucun témoin, source, journaliste, ou personne qui apparaîtrait comme "victime", "survivant", ou "lanceur d'alerte". Tu peux nommer les figures publiques officielles.
- Mentionne 1 à 3 thèmes ou théâtres récurrents (ex : "déploiement au Mali", "opérations d'influence via X", "logistique Wagner en Afrique de l'Ouest").
- Si une seule enquête mentionne l'entité, fais une phrase courte (30-50 mots) qui résume sa mention.
- Pas de formulation "selon les enquêtes" ou "il semble que" : va droit au fait. Commence directement par le sujet.
- Pas de conclusion morale, pas d'éditorialisation. Juste les faits cartographiés.
- Format de sortie : juste le texte du résumé, sans préambule, sans guillemets, sans titre."""

SYSTEM_PROMPT_EN = """You are an assistant for a mapping project of journalistic investigations into Russian influence (Wagner, Africa Corps, hybrid operations) in Africa, Europe and beyond.

For each entity (person or organization), you receive a list of investigations (title + excerpt) mentioning it. Write a short contextual summary of 2 sentences (60-100 words total) answering: "How does this entity appear in the investigations?"

The goal is NOT to describe who the entity is (that exists elsewhere), but to **synthesize its concrete appearances** in the AEOW corpus: which theaters / operations / activities are documented, in which investigations, over which periods.

STRICT RULES:
- Write ONLY in English. All investigations are provided in EN or FR but the summary is always in English.
- Stay FACTUAL and DESCRIPTIVE. No speculative adjectives, no political analysis.
- Do NOT invent anything not in the provided excerpts. When in doubt, stay vague.
- Do not name witnesses, sources, journalists, or anyone appearing as "victim", "survivor" or "whistleblower". You may name public figures.
- Mention 1 to 3 recurring themes or theaters (e.g., "deployment in Mali", "influence operations via X", "Wagner logistics in West Africa").
- If only one article mentions the entity, write a short sentence (30-50 words).
- No "according to investigations" or "it seems that": go straight to the facts. Start directly with the subject.
- No moral conclusions, no editorializing. Just the mapped facts.
- Output format: only the summary text, no preamble, no quotes, no title."""


def build_user_prompt(entity: dict, articles: list[dict]) -> str:
    """Construit le prompt avec les données de l'entité et de ses articles."""
    type_label = "Personne" if entity['type'] == 'PERSON' else "Organisation"
    lines = [
        f"ENTITÉ : {entity['name']}",
        f"TYPE : {type_label}",
        f"NOMBRE D'ENQUÊTES : {len(articles)}",
    ]
    if entity.get("notes"):
        lines.append(f"CONTEXTE GÉNÉRAL CONNU : {entity['notes']}")
    lines.extend([
        "",
        "ENQUÊTES QUI MENTIONNENT CETTE ENTITÉ :",
        "",
    ])
    for i, a in enumerate(articles, 1):
        title = (a.get("title") or "").strip()
        excerpt = (a.get("excerpt") or "").strip()
        category = a.get("category", "—")
        lang = a.get("lang", "—").upper()
        lines.append(f"[{i}] {title} ({category} · {lang})")
        if excerpt:
            lines.append(f"    {excerpt}")
        lines.append("")
    lines.append("Génère le résumé maintenant (texte brut, 2 phrases max, 60-100 mots, en français).")
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
        help="Ne régénère que les entités sans résumé existant dans cette langue")
    parser.add_argument("--entity", default=None,
        help="Génère seulement pour cette entité (par clé ou par nom)")
    parser.add_argument("--min-mentions", type=int, default=2,
        help="Ignore les entités mentionnées moins de N fois (défaut: 2)")
    parser.add_argument("--model", choices=list(MODELS.keys()), default="haiku")
    parser.add_argument("--dry-run", action="store_true",
        help="Affiche le 1er prompt sans appeler l'API")
    parser.add_argument("--lang", choices=["fr", "en"], default="fr")
    parser.add_argument("--limit", type=int, default=None,
        help="Limite à N entités (debug)")
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not args.dry_run and not api_key:
        sys.exit("⚠  Variable d'environnement ANTHROPIC_API_KEY manquante.")

    if not DATA_FILE.exists():
        sys.exit(f"⚠  {DATA_FILE} introuvable. Lance d'abord pipeline.py.")
    if not ENTITIES_FILE.exists():
        sys.exit(f"⚠  {ENTITIES_FILE} introuvable. Lance d'abord pipeline.py.")

    data = json.loads(DATA_FILE.read_text("utf-8"))
    entities_data = json.loads(ENTITIES_FILE.read_text("utf-8"))
    articles_by_id = {a["id"]: a for a in data["articles"]}

    summaries_data = load_existing_summaries()
    summaries = summaries_data.get("summaries", {})

    # Sélection des entités à traiter
    entities = entities_data["entities"]
    if args.entity:
        target = args.entity.lower()
        entities = [e for e in entities if e["key"].lower() == target or e["name"].lower() == target]
        if not entities:
            sys.exit(f"⚠  Entité {args.entity!r} introuvable.")
    if args.min_mentions > 1:
        entities = [e for e in entities if e["count"] >= args.min_mentions]
    if args.only_missing:
        entities = [e for e in entities if not summaries.get(e["key"], {}).get(args.lang)]
    if args.limit:
        entities = entities[:args.limit]

    if not entities:
        print("Rien à générer. Toutes les entités ont déjà un résumé dans cette langue.")
        return

    model = MODELS[args.model]
    system_prompt = SYSTEM_PROMPT_FR if args.lang == "fr" else SYSTEM_PROMPT_EN

    print(f"=== Génération de résumés d'entités ({args.lang.upper()}, modèle: {args.model}) ===")
    print(f"  {len(entities)} entités à traiter")
    if args.dry_run:
        print(f"  MODE DRY-RUN : aucun appel API")
    print()

    total_input = 0
    total_output = 0
    fails = 0
    started = time.time()

    for i, entity in enumerate(entities, 1):
        article_ids = entity.get("articles", [])
        articles = [articles_by_id[aid] for aid in article_ids if aid in articles_by_id]
        if not articles:
            continue

        prompt = build_user_prompt(entity, articles)
        type_letter = "P" if entity['type'] == 'PERSON' else "O"
        print(f"[{i}/{len(entities)}] [{type_letter}] {entity['name']} ({len(articles)} enquêtes)…",
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

            entry = summaries.get(entity["key"], {})
            entry[args.lang] = summary_text
            entry["meta"] = {
                "model": model,
                "generated": time.strftime("%Y-%m-%d"),
                "articles_count": len(articles),
                "input_tokens": in_t,
                "output_tokens": out_t,
            }
            summaries[entity["key"]] = entry

            preview = summary_text[:120].replace("\n", " ")
            print(f"     → {preview}{'...' if len(summary_text) > 120 else ''}")
            print(f"       ({in_t}+{out_t} tokens, ~${estimate_cost_usd(in_t, out_t, model):.4f})")

            # Sauvegarde progressive (toutes les 20 entités, plus fréquent que pour les lieux car volume ×4)
            if i % 20 == 0:
                summaries_data["summaries"] = summaries
                summaries_data["_meta"] = {
                    "generated": time.strftime("%Y-%m-%d"),
                    "model": model,
                    "total_entities": len(summaries),
                }
                save_summaries(summaries_data)

            time.sleep(0.3)

        except Exception as e:
            fails += 1
            print(f"     ✗ Échec : {e}")

    # Sauvegarde finale
    summaries_data["summaries"] = summaries
    summaries_data["_meta"] = {
        "generated": time.strftime("%Y-%m-%d"),
        "model": model,
        "total_entities": len(summaries),
    }
    save_summaries(summaries_data)

    elapsed = time.time() - started
    cost = estimate_cost_usd(total_input, total_output, model)
    print()
    print(f"✓ Terminé en {elapsed:.0f}s")
    print(f"  Entités traitées : {len(entities) - fails} / {len(entities)}")
    print(f"  Tokens : {total_input} input + {total_output} output")
    print(f"  Coût estimé : ~${cost:.3f} USD")
    print(f"  → {SUMMARIES_FILE}")


if __name__ == "__main__":
    main()
