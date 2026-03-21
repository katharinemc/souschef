"""
recipe_loader.py

Converts Paprika .txt recipe exports to structured YAML files.

Usage:
    python recipe_loader.py --input ./recipes_raw --output ./recipes_yaml
    python recipe_loader.py --input ./recipes_raw/single_recipe.txt --output ./recipes_yaml

Each .txt file produces one .yaml file in the output directory.
Existing YAML files are not overwritten unless --force is passed.
"""

import re
import sys
import argparse
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tag taxonomy
# ---------------------------------------------------------------------------

# Maps lowercase normalized Paprika tags -> canonical taxonomy tags.
# Unknowns are silently dropped.
TAG_MAP: dict[str, str] = {
    "onrotation":   "onRotation",
    "on_rotation":  "onRotation",
    "experiment":   "experiment",
    "easy":         "easy",
    "vegetarian":   "vegetarian",
    "pescatarian":  "pescatarian",
    "vegan":        "vegan",
    "pasta":        "pasta",
    "taco":         "taco",
    "tacos":        "taco",
    "freezer":      "freezer",
    "pizza":        "pizza",
}


def normalize_tag(raw: str) -> Optional[str]:
    """Map a raw Paprika tag to a canonical taxonomy tag, or None if unknown."""
    cleaned = raw.strip().lstrip("_").lower()
    return TAG_MAP.get(cleaned)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Ingredient:
    name: str
    quantity: Optional[str] = None
    unit: Optional[str] = None
    section: Optional[str] = None   # e.g. "Crust", "Filling"


@dataclass
class Recipe:
    name: str
    id: str
    tags: list[str] = field(default_factory=list)
    servings: Optional[str] = None
    prep_time: Optional[str] = None
    cook_time: Optional[str] = None
    source: Optional[str] = None
    ingredients: list[Ingredient] = field(default_factory=list)
    instructions: str = ""
    last_planned: Optional[str] = None   # populated by the planner, not here


# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Convert a recipe name to a kebab-case id slug."""
    s = name.lower()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


# ---------------------------------------------------------------------------
# Ingredient parsing
# ---------------------------------------------------------------------------

# Units we recognize for structured parsing.
UNITS = {
    "teaspoon", "teaspoons", "tsp",
    "tablespoon", "tablespoons", "tbsp",
    "cup", "cups",
    "ounce", "ounces", "oz",
    "pound", "pounds", "lb", "lbs",
    "gram", "grams", "g",
    "kilogram", "kilograms", "kg",
    "milliliter", "milliliters", "ml",
    "liter", "liters", "l",
    "pinch", "pinches",
    "clove", "cloves",
    "slice", "slices",
    "piece", "pieces",
    "can", "cans",
    "jar", "jars",
    "bag", "bags",
    "package", "packages",
    "bunch", "bunches",
    "sprig", "sprigs",
    "stalk", "stalks",
    "quart", "quarts",
    "pint", "pints",
    "gallon", "gallons",
    "stick", "sticks",
}

# Matches a leading fraction/number like: 1, 1/2, 1 1/2, ¼, 1½
QTY_PATTERN = re.compile(
    r"^([\d/\s\u00BC-\u00BE\u2150-\u215E]+(?:\s+[\d/\u00BC-\u00BE\u2150-\u215E]+)?)"
)

# Grocery-store ad noise: lines containing price patterns
AD_PATTERN = re.compile(r"\$[\d.]+|for \d+ item|expires in \d+ day", re.IGNORECASE)

# Grocery ad product lines: brand name + package size, no leading cooking quantity
# e.g. "Bertolli Original Extra Virgin Olive Oil 16.9 Fl Oz"
# e.g. "Great Value Pure Ground Black Pepper 3 Oz"
AD_PRODUCT_PATTERN = re.compile(
    r"^[A-Z][A-Za-z\s]+\b\d+(\.\d+)?\s*(fl\s*oz|oz|ml|liter|lb|g|kg|count|ct)\s*$",
    re.IGNORECASE
)

# Section header: a line that contains only capitalized words and no numbers
SECTION_HEADER_PATTERN = re.compile(r"^[A-Z][A-Za-z\s/]+:?\s*$")


def is_ad_line(line: str) -> bool:
    if AD_PATTERN.search(line):
        return True
    # Catch brand-name + package-size lines with no leading cooking quantity
    # e.g. "Bertolli Original Extra Virgin Olive Oil 16.9 Fl Oz"
    stripped = line.strip()
    if AD_PRODUCT_PATTERN.match(stripped) and not QTY_PATTERN.match(stripped):
        return True
    return False


def is_section_header(line: str) -> bool:
    """Return True if this line looks like an ingredient subsection header."""
    stripped = line.strip().rstrip(":")
    if not stripped:
        return False
    # All words start with uppercase, no digits
    if re.search(r"\d", stripped):
        return False
    words = stripped.split()
    if len(words) > 4:
        return False
    return all(w[0].isupper() for w in words if w)


def parse_ingredient_line(line: str, section: Optional[str]) -> Optional[Ingredient]:
    """
    Parse a single ingredient line into an Ingredient.
    Returns None if the line should be skipped (ad, footnote, etc).
    """
    line = line.strip()
    if not line:
        return None
    # Strip inline price suffixes BEFORE ad check, so "1 lb pasta ($0.89)" isn't dropped
    line = re.sub(r"\s*\(\$[\d.]+\)", "", line).strip()
    if is_ad_line(line):
        return None
    # Footnote markers like "* If unavailable..."
    if line.startswith("*"):
        return None
    # Trailing comma-separated format from some Paprika exports: "1 1/2 pounds, beef"
    # Normalise to "1 1/2 pounds beef"
    line = re.sub(r",\s+", " ", line)

    qty_match = QTY_PATTERN.match(line)
    quantity = None
    unit = None
    name = line

    if qty_match:
        quantity = qty_match.group(1).strip()
        rest = line[qty_match.end():].strip()
        # Check if next token is a unit
        tokens = rest.split(None, 1)
        if tokens and tokens[0].lower().rstrip(".") in UNITS:
            unit = tokens[0]
            name = tokens[1] if len(tokens) > 1 else ""
        else:
            name = rest

    name = name.strip().strip(",").strip()
    if not name:
        return None

    return Ingredient(name=name, quantity=quantity, unit=unit, section=section)


def parse_ingredients_block(block: str) -> list[Ingredient]:
    """Parse the full ingredients block, handling subsection headers."""
    ingredients = []
    current_section = None

    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        if is_section_header(line):
            current_section = line.rstrip(":")
            continue
        ingredient = parse_ingredient_line(line, current_section)
        if ingredient:
            ingredients.append(ingredient)

    return ingredients


# ---------------------------------------------------------------------------
# Servings / time parsing
# ---------------------------------------------------------------------------

def parse_servings(raw: str) -> Optional[str]:
    """Extract a clean servings string, e.g. '4' or '6'."""
    if not raw:
        return None
    # Strip leading labels
    cleaned = re.sub(r"^(servings?|serves?|yield[s]?)\s*[:.]?\s*", "", raw, flags=re.IGNORECASE)
    # Strip trailing noise like "(Scaled 2x)"
    cleaned = re.sub(r"\(scaled.*?\)", "", cleaned, flags=re.IGNORECASE).strip()
    return cleaned or None


def parse_time(raw: str) -> Optional[str]:
    if not raw:
        return None
    return raw.strip()


# ---------------------------------------------------------------------------
# Tag parsing
# ---------------------------------------------------------------------------

def parse_tags(raw_line: str) -> list[str]:
    """
    Parse a Paprika tag line like '_pasta, InstantPot' or 'Dessert'.
    Returns only tags that exist in our taxonomy.
    """
    parts = re.split(r"[,\s]+", raw_line)
    tags = []
    for part in parts:
        mapped = normalize_tag(part)
        if mapped and mapped not in tags:
            tags.append(mapped)
    return tags


# ---------------------------------------------------------------------------
# Full file parser
# ---------------------------------------------------------------------------

# Section headers we look for (all uppercase variants handled via re.IGNORECASE)
SECTION_RE = re.compile(
    r"^(Ingredients|Directions|Instructions|Notes?|Nutrition|Source)\s*:?\s*$",
    re.IGNORECASE
)

# Inline source line: "Source: https://..."  (URL on same line, no newline before it)
INLINE_SOURCE_RE = re.compile(r"^Source\s*:\s*(\S+)", re.IGNORECASE)

# Metadata line: "Prep Time: 20 minutes | Cook Time: 20 minutes | Servings: 4"
META_RE = re.compile(
    r"(?:Prep\s*Time\s*:\s*([^|]+))?.*?"
    r"(?:Cook\s*Time\s*:\s*([^|]+))?.*?"
    r"(?:Servings?\s*:\s*(.+))?",
    re.IGNORECASE
)


def parse_paprika_txt(text: str) -> Recipe:
    """
    Parse a Paprika .txt export into a Recipe dataclass.
    
    File structure (loosely):
        Line 1:         Recipe name
        Line 2:         Star rating (ignore)
        Line 3+:        Zero or more tag/category lines, then a metadata line
        Section blocks: Ingredients, Directions, Notes, Nutrition, Source
    """
    lines = text.splitlines()
    if not lines:
        raise ValueError("Empty file")

    # --- Name (line 0) ---
    name = lines[0].strip()
    recipe = Recipe(name=name, id=slugify(name))

    # --- Pre-section header lines (rating, tags, metadata) ---
    # Walk lines until we hit the first section header
    i = 1
    pre_section_lines = []
    while i < len(lines):
        if SECTION_RE.match(lines[i].strip()):
            break
        pre_section_lines.append(lines[i].strip())
        i += 1

    for line in pre_section_lines:
        if not line:
            continue
        # Skip star ratings
        if set(line).issubset({"★", "☆", " "}):
            continue
        # Metadata line containing pipe-delimited time/servings
        if "|" in line or re.search(r"(prep|cook)\s*time", line, re.IGNORECASE):
            m = re.search(r"prep\s*time\s*:\s*([^|]+)", line, re.IGNORECASE)
            if m:
                recipe.prep_time = parse_time(m.group(1).strip())
            m = re.search(r"cook\s*time\s*:\s*([^|]+)", line, re.IGNORECASE)
            if m:
                recipe.cook_time = parse_time(m.group(1).strip())
            m = re.search(r"servings?\s*:\s*(.+)", line, re.IGNORECASE)
            if m:
                recipe.servings = parse_servings(m.group(1).strip())
            continue
        # Standalone servings line
        if re.match(r"^(servings?|serves?|yield)", line, re.IGNORECASE):
            recipe.servings = parse_servings(line)
            continue
        # Tag line (contains underscore-prefixed tags or known category words)
        tags = parse_tags(line)
        if tags:
            for t in tags:
                if t not in recipe.tags:
                    recipe.tags.append(t)
            continue
        # Anything else in the pre-section: could be a plain category like "Dessert"
        # Try it as a tag; if it maps to nothing, silently discard
        # (already handled by parse_tags above)

    # --- Section blocks ---
    current_section = None
    section_lines: dict[str, list[str]] = {
        "ingredients": [],
        "directions": [],
        "notes": [],
        "source": [],
    }

    while i < len(lines):
        line = lines[i]
        m = SECTION_RE.match(line.strip())
        if m:
            header = m.group(1).lower()
            if header in ("directions", "instructions"):
                current_section = "directions"
            elif header == "ingredients":
                current_section = "ingredients"
            elif header in ("note", "notes"):
                current_section = "notes"
            elif header == "source":
                current_section = "source"
            else:
                current_section = None   # nutrition etc — discard
        else:
            # Inline "Source: https://..." line — capture regardless of current section
            inline_src = INLINE_SOURCE_RE.match(line.strip())
            if inline_src:
                section_lines["source"].append(inline_src.group(1).strip())
            elif current_section and current_section in section_lines:
                section_lines[current_section].append(line)
        i += 1

    # --- Ingredients ---
    ingredients_text = "\n".join(section_lines["ingredients"])
    # Strip "Instructions Checklist" noise
    ingredients_text = re.sub(r"Instructions Checklist", "", ingredients_text, flags=re.IGNORECASE)
    recipe.ingredients = parse_ingredients_block(ingredients_text)

    # --- Directions ---
    directions_lines = section_lines["directions"]
    # Strip "Instructions Checklist" noise
    directions_lines = [l for l in directions_lines if "Instructions Checklist" not in l]
    recipe.instructions = "\n".join(directions_lines).strip()

    # --- Source ---
    source_text = " ".join(section_lines["source"]).strip()
    if source_text:
        recipe.source = source_text

    return recipe


# ---------------------------------------------------------------------------
# YAML serialisation
# ---------------------------------------------------------------------------

def ingredient_to_dict(ing: Ingredient) -> dict:
    d: dict = {"name": ing.name}
    if ing.quantity:
        d["quantity"] = ing.quantity
    if ing.unit:
        d["unit"] = ing.unit
    if ing.section:
        d["section"] = ing.section
    return d


def recipe_to_dict(recipe: Recipe) -> dict:
    return {
        "id": recipe.id,
        "name": recipe.name,
        "tags": recipe.tags,
        "servings": recipe.servings,
        "prep_time": recipe.prep_time,
        "cook_time": recipe.cook_time,
        "source": recipe.source,
        "last_planned": recipe.last_planned,
        "ingredients": [ingredient_to_dict(i) for i in recipe.ingredients],
        "instructions": recipe.instructions,
    }


def write_yaml(recipe: Recipe, output_dir: Path) -> Path:
    out_path = output_dir / f"{recipe.id}.yaml"
    data = recipe_to_dict(recipe)
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def process_file(txt_path: Path, output_dir: Path, force: bool) -> bool:
    """Parse one file. Returns True on success."""
    try:
        text = txt_path.read_text(encoding="utf-8")
        recipe = parse_paprika_txt(text)
        out_path = output_dir / f"{recipe.id}.yaml"
        if out_path.exists() and not force:
            log.info("Skipping %s (already exists, use --force to overwrite)", out_path.name)
            return True
        written = write_yaml(recipe, output_dir)
        log.info("Written: %s", written)
        return True
    except Exception as e:
        log.error("Failed to parse %s: %s", txt_path.name, e)
        return False


def main():
    parser = argparse.ArgumentParser(description="Convert Paprika .txt exports to YAML.")
    parser.add_argument("--input", "-i", required=True,
                        help="Path to a .txt file or directory of .txt files")
    parser.add_argument("--output", "-o", required=True,
                        help="Output directory for .yaml files")
    parser.add_argument("--force", "-f", action="store_true",
                        help="Overwrite existing YAML files")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if input_path.is_file():
        files = [input_path]
    elif input_path.is_dir():
        files = sorted(input_path.glob("*.txt"))
        if not files:
            log.error("No .txt files found in %s", input_path)
            sys.exit(1)
    else:
        log.error("Input path does not exist: %s", input_path)
        sys.exit(1)

    results = [process_file(f, output_dir, args.force) for f in files]
    failed = results.count(False)
    log.info("Done. %d/%d recipes converted.", len(results) - failed, len(results))
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
