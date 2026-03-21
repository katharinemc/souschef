"""
paprika_import.py

Converts a Paprika bulk export (.paprikarecipes) directly to YAML recipe files,
replacing the .txt → recipe_loader.py path entirely.

A .paprikarecipes file is a ZIP archive containing individual .paprikarecipe
files, each of which is a gzip-compressed JSON blob.

Usage:
    python paprika_import.py --input MyRecipes.paprikarecipes --output ./recipes_yaml
    python paprika_import.py --input MyRecipes.paprikarecipes --output ./recipes_yaml --force
    python paprika_import.py --input MyRecipes.paprikarecipes --list   # preview only

The --list flag prints recipe names and their mapped tags without writing files.

Integration with main.py:
    python main.py ingest --input MyRecipes.paprikarecipes
    (main.py detects .paprikarecipes extension and routes to this module)
"""

import argparse
import gzip
import json
import logging
import sys
import zipfile
from pathlib import Path
from typing import Optional

import yaml

# Reuse ingredient parsing and slug generation from recipe_loader
from recipe_loader import (
    parse_ingredients_block,
    parse_servings,
    parse_time,
    slugify,
    ingredient_to_dict,
    TAG_MAP,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tag mapping from Paprika categories
# ---------------------------------------------------------------------------

def map_categories_to_tags(categories: list[str]) -> list[str]:
    """
    Convert Paprika category strings to taxonomy tags.
    Unknowns are silently dropped.

    Paprika categories may be plain words ("Pasta", "Vegetarian") or
    underscore-prefixed ("_pasta", "_vegetarian") — handle both.
    """
    tags = []
    for cat in (categories or []):
        cleaned = cat.strip().lstrip("_").lower()
        mapped = TAG_MAP.get(cleaned)
        if mapped and mapped not in tags:
            tags.append(mapped)
    return tags


# ---------------------------------------------------------------------------
# JSON → Recipe
# ---------------------------------------------------------------------------

def parse_paprika_json(data: dict) -> dict:
    """
    Convert a Paprika recipe JSON dict to our YAML recipe schema.

    Returns a recipe dict ready for yaml.dump().
    """
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Recipe has no name")

    recipe_id = slugify(name)

    # Tags from categories list (much cleaner than .txt parsing)
    tags = map_categories_to_tags(data.get("categories") or [])

    # Servings
    servings = parse_servings(data.get("servings") or "")

    # Times — Paprika stores these as plain strings
    prep_time = parse_time(data.get("prep_time") or "")
    cook_time = parse_time(data.get("cook_time") or "")

    # Source — prefer source_url, fall back to source field
    source = (data.get("source_url") or data.get("source") or "").strip() or None

    # Ingredients — still a raw string in the JSON, use existing parser
    ingredients_raw = data.get("ingredients") or ""
    ingredients = parse_ingredients_block(ingredients_raw)

    # Instructions
    instructions = (data.get("directions") or "").strip()

    # Notes — Paprika has a dedicated notes field; append to instructions
    # if present so nothing is lost
    notes = (data.get("notes") or "").strip()

    return {
        "id":           recipe_id,
        "name":         name,
        "tags":         tags,
        "servings":     servings,
        "prep_time":    prep_time,
        "cook_time":    cook_time,
        "source":       source,
        "last_planned": None,
        "ingredients":  [ingredient_to_dict(i) for i in ingredients],
        "instructions": instructions,
        "notes":        notes or None,
    }


# ---------------------------------------------------------------------------
# .paprikarecipes unpacker
# ---------------------------------------------------------------------------

def iter_recipes(paprikarecipes_path: Path):
    """
    Yield (filename, recipe_dict) for every recipe in a .paprikarecipes file.
    Skips entries that fail to parse and logs a warning.
    """
    with zipfile.ZipFile(paprikarecipes_path, "r") as zf:
        entries = [e for e in zf.namelist() if e.endswith(".paprikarecipe")]
        log.info("Found %d recipe(s) in %s", len(entries), paprikarecipes_path.name)

        for entry in entries:
            try:
                compressed = zf.read(entry)
                raw_json   = gzip.decompress(compressed)
                data       = json.loads(raw_json)
                recipe     = parse_paprika_json(data)
                yield entry, recipe
            except Exception as e:
                log.warning("Skipping %s: %s", entry, e)


# ---------------------------------------------------------------------------
# File writer
# ---------------------------------------------------------------------------

def write_recipe(recipe: dict, output_dir: Path, force: bool) -> Optional[Path]:
    """Write one recipe dict to a YAML file. Returns path on success, None if skipped."""
    out_path = output_dir / f"{recipe['id']}.yaml"
    if out_path.exists() and not force:
        log.info("Skipping %-40s (already exists — use --force to overwrite)", out_path.name)
        return None
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(recipe, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    log.info("Written: %s", out_path.name)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert a Paprika .paprikarecipes bulk export to YAML recipe files."
    )
    parser.add_argument(
        "--input", "-i", required=True,
        help="Path to the .paprikarecipes export file"
    )
    parser.add_argument(
        "--output", "-o", default="recipes_yaml",
        help="Output directory for .yaml files (default: recipes_yaml)"
    )
    parser.add_argument(
        "--force", "-f", action="store_true",
        help="Overwrite existing YAML files"
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List recipes and their mapped tags without writing files"
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    input_path = Path(args.input)
    if not input_path.exists():
        log.error("File not found: %s", input_path)
        sys.exit(1)
    if input_path.suffix.lower() != ".paprikarecipes":
        log.error("Expected a .paprikarecipes file, got: %s", input_path.suffix)
        sys.exit(1)

    if args.list:
        print(f"\nRecipes in {input_path.name}:\n")
        count = 0
        for _, recipe in iter_recipes(input_path):
            tags_str = ", ".join(recipe["tags"]) if recipe["tags"] else "(no tags mapped)"
            print(f"  {recipe['name']:<45}  [{tags_str}]")
            count += 1
        print(f"\n{count} recipe(s) total.\n")
        return

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped = 0
    failed  = 0

    for _, recipe in iter_recipes(input_path):
        try:
            result = write_recipe(recipe, output_dir, force=args.force)
            if result:
                written += 1
            else:
                skipped += 1
        except Exception as e:
            log.error("Failed to write %s: %s", recipe.get("id", "?"), e)
            failed += 1

    log.info(
        "Done. %d written, %d skipped (already exist), %d failed.",
        written, skipped, failed,
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
