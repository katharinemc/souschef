"""
stacker.py

Scans a WeekPlan for ingredient overlap opportunities and produces
advisory stacking notes. All output is informational — the planner
and user decide whether to act on any suggestion.

Three categories of stacking note:

  BATCH    — same ingredient appears in 2+ recipes; cook once, use twice.
             e.g. "Tuesday and Thursday both use jasmine rice —
                   consider cooking a double batch on Tuesday."

  REMNANT  — a recipe uses a small amount of a packaged ingredient,
             leaving likely leftovers; another recipe this week can use them up.
             e.g. "Wednesday calls for 1 tbsp cream cheese;
                   Saturday uses 4 oz. Plan accordingly or buy one block."

  SHARED   — same canned/packaged item appears across recipes in quantities
             that may be covered by one purchase.
             e.g. "Two recipes use canned chickpeas. One can may cover both."

Design notes:
  - Ingredient matching is fuzzy: "jasmine rice", "basmati rice", and "rice"
    are normalised to a common key before comparison.
  - The LLM is NOT used here. All matching is deterministic Python.
    The LLM call for stacking is deferred to a future version where richer
    natural-language notes are needed.
  - Returns a list of StackingNote dataclasses, not strings.
    The email formatter converts these to prose.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from planner import WeekPlan, MealSlot

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Ingredient normalisation
# ---------------------------------------------------------------------------

# Synonyms: map variant strings → canonical key
# Each entry is (pattern, canonical). Patterns are matched as substrings
# against the lowercased ingredient name.
INGREDIENT_SYNONYMS: list[tuple[str, str]] = [
    # Grains
    (r"\brice\b",               "rice"),
    (r"\bpasta\b",              "pasta"),
    (r"\bpenne\b",              "pasta"),
    (r"\bspaghetti\b",          "pasta"),
    (r"\bfettuccine\b",         "pasta"),
    (r"\bquinoa\b",             "quinoa"),
    (r"\bcouscous\b",           "couscous"),
    (r"\bfarro\b",              "farro"),
    (r"\bbulgur\b",             "bulgur"),
    (r"\blentil",               "lentils"),
    # Proteins
    (r"\bchickpea",             "chickpeas"),
    (r"\bgarbanzo",             "chickpeas"),
    (r"\bblack bean",           "black beans"),
    (r"\bkidney bean",          "kidney beans"),
    (r"\bground beef\b",        "ground beef"),
    (r"\bground lamb\b",        "ground lamb"),
    (r"\bground turkey\b",      "ground turkey"),
    (r"\bground chicken\b",     "ground chicken"),
    # Dairy
    (r"\bcream cheese\b",       "cream cheese"),
    (r"\bsour cream\b",         "sour cream"),
    (r"\bheavy cream\b",        "heavy cream"),
    (r"\bwhipping cream\b",     "heavy cream"),
    (r"\bparmesan\b",           "parmesan"),
    (r"\bpecorino\b",           "parmesan"),
    (r"\bmozzarella\b",         "mozzarella"),
    (r"\bfeta\b",               "feta"),
    # Aromatics
    (r"\bgarlic\b",             "garlic"),
    (r"\bonion\b",              "onion"),
    (r"\bshallot\b",            "shallot"),
    (r"\bginger\b",             "ginger"),
    # Canned / pantry
    (r"\bdiced tomato",         "canned diced tomatoes"),
    (r"\bcrushed tomato",       "canned crushed tomatoes"),
    (r"\btomato paste\b",       "tomato paste"),
    (r"\bmarinara\b",           "marinara sauce"),
    (r"\bcoconut milk\b",       "coconut milk"),
    (r"\bvegetable broth\b",    "vegetable broth"),
    (r"\bchicken broth\b",      "chicken broth"),
    (r"\bbeef broth\b",         "beef broth"),
    (r"\bbeef stock\b",         "beef broth"),
    (r"\bchicken stock\b",      "chicken broth"),
    (r"\bveg.*stock\b",         "vegetable broth"),
    # Herbs & spices
    (r"\bcilantro\b",           "cilantro"),
    (r"\bparsley\b",            "parsley"),
    (r"\bbasil\b",              "basil"),
    (r"\bthyme\b",              "thyme"),
    (r"\brosemary\b",           "rosemary"),
    (r"\bcumin\b",              "cumin"),
    (r"\bpaprika\b",            "paprika"),
    (r"\bturmeric\b",           "turmeric"),
    (r"\bcoriander\b",          "coriander"),
    (r"\bgaram masala\b",       "garam masala"),
    # Produce
    (r"\bspinach\b",            "spinach"),
    (r"\bkale\b",               "kale"),
    (r"\bzucchini\b",           "zucchini"),
    (r"\beggplant\b",           "eggplant"),
    (r"\bbell pepper\b",        "bell pepper"),
    (r"\bcabbage\b",            "cabbage"),
    (r"\bbroccoli\b",           "broccoli"),
    (r"\bcauliflower\b",        "cauliflower"),
    (r"\blemon\b",              "lemon"),
    (r"\blime\b",               "lime"),
]

# Ingredients too generic to be useful stacking signals
IGNORE_INGREDIENTS = {
    "salt", "pepper", "black pepper", "water", "oil", "olive oil",
    "sugar", "flour", "butter", "egg", "eggs", "ice",
    "salt and pepper", "kosher salt", "sea salt",
}

# Unit-to-ml for volume comparisons (used for REMNANT detection)
UNIT_ML: dict[str, float] = {
    "tsp":        5.0,
    "teaspoon":   5.0,
    "teaspoons":  5.0,
    "tbsp":      15.0,
    "tablespoon": 15.0,
    "tablespoons":15.0,
    "cup":       240.0,
    "cups":      240.0,
    "fl oz":      30.0,
    "fl_oz":      30.0,
    "oz":         30.0,   # approximate for liquids
    "ml":          1.0,
    "l":        1000.0,
}

# Typical retail package sizes in ml — if total recipe usage is below this,
# there will be leftovers that another recipe might use.
PACKAGE_SIZES_ML: dict[str, float] = {
    "cream cheese":      225.0,   # 8 oz block
    "sour cream":        454.0,   # 16 oz container
    "heavy cream":       240.0,   # 1 cup / half-pint
    "coconut milk":      400.0,   # 1 can
    "tomato paste":      170.0,   # 6 oz can
    "marinara sauce":    680.0,   # 24 oz jar
}


def normalise_ingredient(name: str) -> Optional[str]:
    """
    Map an ingredient name to a canonical key, or return None if it
    should be ignored (too generic to be a useful stacking signal).
    """
    lower = name.lower().strip()

    # Strip trailing descriptors: "divided", "room temperature", etc.
    lower = re.sub(
        r"\b(divided|room temperature|at room temperature|fresh|dried|"
        r"chopped|minced|diced|sliced|grated|shredded|melted|softened|"
        r"rinsed|drained|patted dry|optional)\b",
        "", lower
    ).strip()

    if lower in IGNORE_INGREDIENTS:
        return None
    # Check generic ignore patterns
    for ig in IGNORE_INGREDIENTS:
        if lower == ig:
            return None

    for pattern, canonical in INGREDIENT_SYNONYMS:
        if re.search(pattern, lower):
            return canonical

    # Fall back to the cleaned name itself
    return lower if lower else None


def parse_quantity_ml(quantity: Optional[str], unit: Optional[str]) -> Optional[float]:
    """
    Convert a quantity + unit pair to millilitres.
    Returns None if conversion is not possible.
    """
    if not quantity or not unit:
        return None
    unit_lower = unit.lower().strip()
    factor = UNIT_ML.get(unit_lower)
    if factor is None:
        return None
    # Parse quantity, handling fractions and unicode vulgar fractions
    qty_str = quantity.strip()
    qty_str = qty_str.replace("¼", "0.25").replace("½", "0.5").replace("¾", "0.75")
    qty_str = qty_str.replace("\u2009", " ")  # thin space
    try:
        if "/" in qty_str:
            parts = qty_str.split()
            val = 0.0
            for part in parts:
                if "/" in part:
                    num, den = part.split("/")
                    val += float(num) / float(den)
                else:
                    val += float(part)
        else:
            val = float(qty_str)
        return val * factor
    except (ValueError, ZeroDivisionError):
        return None


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class IngredientOccurrence:
    """One ingredient from one meal slot."""
    canonical:  str
    raw_name:   str
    recipe_id:  Optional[str]
    label:      str
    meal_date:  object            # date
    quantity:   Optional[str]
    unit:       Optional[str]
    quantity_ml: Optional[float]  # None if unconvertible


@dataclass
class StackingNote:
    """
    A single stacking advisory note.

    Attributes:
        kind:       'BATCH', 'REMNANT', or 'SHARED'
        canonical:  The normalised ingredient name this note is about
        meals:      The meal labels involved (e.g. ["Tuesday", "Thursday"])
        message:    Human-readable advisory note
    """
    kind:      str
    canonical: str
    meals:     list[str]
    message:   str

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------------
# Stacker
# ---------------------------------------------------------------------------

class Stacker:
    """
    Analyses a WeekPlan and produces StackingNote advisories.

    Usage:
        stacker = Stacker()
        notes = stacker.analyse(plan)
        for note in notes:
            print(note)
    """

    # REMNANT threshold: if a recipe uses less than this fraction of
    # a standard package, flag it as leaving remnants
    REMNANT_THRESHOLD = 0.6

    def analyse(self, plan: WeekPlan) -> list[StackingNote]:
        """
        Analyse a WeekPlan and return all stacking notes, sorted by kind
        then by canonical ingredient name.
        """
        occurrences = self._extract_occurrences(plan)
        notes: list[StackingNote] = []

        notes.extend(self._find_batch_opportunities(occurrences))
        notes.extend(self._find_remnant_opportunities(occurrences))
        notes.extend(self._find_shared_canned(occurrences))

        # Deduplicate by message
        seen: set[str] = set()
        deduped = []
        for n in notes:
            if n.message not in seen:
                seen.add(n.message)
                deduped.append(n)

        deduped.sort(key=lambda n: (n.kind, n.canonical))
        log.info("Stacker found %d notes for week of %s", len(deduped), plan.week_start_monday)
        return deduped

    # -----------------------------------------------------------------------
    # Occurrence extraction
    # -----------------------------------------------------------------------

    def _extract_occurrences(self, plan: WeekPlan) -> list[IngredientOccurrence]:
        occurrences = []
        for slot in plan.dinners:
            if slot.is_no_cook or not slot.recipe_id:
                continue
            for ing in slot.ingredients:
                canonical = normalise_ingredient(ing.get("name", ""))
                if canonical is None:
                    continue
                qty_ml = parse_quantity_ml(ing.get("quantity"), ing.get("unit"))
                occurrences.append(IngredientOccurrence(
                    canonical=canonical,
                    raw_name=ing.get("name", ""),
                    recipe_id=slot.recipe_id,
                    label=slot.label,
                    meal_date=slot.date,
                    quantity=ing.get("quantity"),
                    unit=ing.get("unit"),
                    quantity_ml=qty_ml,
                ))
        return occurrences

    # -----------------------------------------------------------------------
    # BATCH: same ingredient in 2+ recipes
    # -----------------------------------------------------------------------

    def _find_batch_opportunities(
        self, occurrences: list[IngredientOccurrence]
    ) -> list[StackingNote]:
        """
        Flag ingredients that appear in 2+ distinct recipes.
        Only emit a BATCH note for ingredients that make sense to batch-cook
        (grains, legumes — not spices or aromatics used in small amounts).
        """
        BATCH_CANDIDATES = {
            "rice", "quinoa", "farro", "bulgur", "couscous", "lentils",
            "pasta", "chickpeas", "black beans", "kidney beans",
        }

        by_canonical: dict[str, list[IngredientOccurrence]] = {}
        for occ in occurrences:
            by_canonical.setdefault(occ.canonical, []).append(occ)

        notes = []
        for canonical, occs in by_canonical.items():
            if canonical not in BATCH_CANDIDATES:
                continue
            # Must appear in 2+ distinct recipes
            recipe_ids = list(dict.fromkeys(o.recipe_id for o in occs))
            if len(recipe_ids) < 2:
                continue

            # Order by meal date
            occs_sorted = sorted(occs, key=lambda o: o.meal_date)
            # Pick the earlier meal as the batch-cook day
            first = occs_sorted[0]
            rest  = occs_sorted[1:]

            meal_names = [o.label for o in occs_sorted]
            day_names  = [o.meal_date.strftime("%A") for o in occs_sorted]

            # Build a natural list: "Tuesday and Thursday"
            day_list = _natural_list(day_names)
            msg = (
                f"{day_list} both use {canonical} — "
                f"consider cooking a double batch on {first.meal_date.strftime('%A')}."
            )
            notes.append(StackingNote(
                kind="BATCH",
                canonical=canonical,
                meals=meal_names,
                message=msg,
            ))
        return notes

    # -----------------------------------------------------------------------
    # REMNANT: small usage of a packaged item leaves leftovers
    # -----------------------------------------------------------------------

    def _find_remnant_opportunities(
        self, occurrences: list[IngredientOccurrence]
    ) -> list[StackingNote]:
        """
        For ingredients with known package sizes, check if any recipe uses
        a small fraction of the package while another recipe in the week
        also uses it — signal to plan usage together.
        """
        notes = []
        by_canonical: dict[str, list[IngredientOccurrence]] = {}
        for occ in occurrences:
            if occ.canonical not in PACKAGE_SIZES_ML:
                continue
            by_canonical.setdefault(occ.canonical, []).append(occ)

        for canonical, occs in by_canonical.items():
            pkg_ml = PACKAGE_SIZES_ML[canonical]
            recipe_ids = list(dict.fromkeys(o.recipe_id for o in occs))
            if len(recipe_ids) < 2:
                # Only one recipe — check if it uses a small fraction of package
                occ = occs[0]
                if occ.quantity_ml and occ.quantity_ml < pkg_ml * self.REMNANT_THRESHOLD:
                    msg = (
                        f"{occ.label} calls for {_qty_str(occ)} {canonical} — "
                        f"you'll have most of a package left. "
                        f"Consider another recipe this week that uses {canonical}."
                    )
                    notes.append(StackingNote(
                        kind="REMNANT",
                        canonical=canonical,
                        meals=[occ.label],
                        message=msg,
                    ))
            else:
                # Multiple recipes use this ingredient — note compatibility
                occs_sorted = sorted(occs, key=lambda o: o.meal_date)
                total_ml = sum(o.quantity_ml for o in occs_sorted if o.quantity_ml)
                labels = [o.label for o in occs_sorted]
                day_names = [o.meal_date.strftime("%A") for o in occs_sorted]
                day_list = _natural_list(day_names)

                if total_ml and total_ml <= pkg_ml * 1.1:
                    # Combined usage fits in one package
                    msg = (
                        f"{day_list} both use {canonical}. "
                        f"Combined usage fits in one package — buy one and plan accordingly."
                    )
                else:
                    msg = (
                        f"{day_list} both use {canonical}. "
                        f"Plan your purchase around combined usage this week."
                    )
                notes.append(StackingNote(
                    kind="REMNANT",
                    canonical=canonical,
                    meals=labels,
                    message=msg,
                ))
        return notes

    # -----------------------------------------------------------------------
    # SHARED: canned/packaged item appears in multiple recipes
    # -----------------------------------------------------------------------

    def _find_shared_canned(
        self, occurrences: list[IngredientOccurrence]
    ) -> list[StackingNote]:
        """
        Flag canned or pantry staples that appear in 2+ recipes where a
        single purchase might cover both.
        """
        SHARED_CANDIDATES = {
            "canned diced tomatoes",
            "canned crushed tomatoes",
            "chickpeas",
            "black beans",
            "kidney beans",
            "coconut milk",
            "tomato paste",
        }

        by_canonical: dict[str, list[IngredientOccurrence]] = {}
        for occ in occurrences:
            if occ.canonical not in SHARED_CANDIDATES:
                continue
            by_canonical.setdefault(occ.canonical, []).append(occ)

        notes = []
        for canonical, occs in by_canonical.items():
            recipe_ids = list(dict.fromkeys(o.recipe_id for o in occs))
            if len(recipe_ids) < 2:
                continue
            labels   = list(dict.fromkeys(o.label for o in occs))
            day_names = list(dict.fromkeys(o.meal_date.strftime("%A") for o in occs))
            day_list = _natural_list(day_names)
            msg = (
                f"Two recipes this week use {canonical} ({_natural_list(labels)}). "
                f"One purchase may cover both — check quantities."
            )
            notes.append(StackingNote(
                kind="SHARED",
                canonical=canonical,
                meals=labels,
                message=msg,
            ))
        return notes


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _natural_list(items: list[str]) -> str:
    """['A', 'B', 'C'] → 'A, B, and C'"""
    items = list(dict.fromkeys(items))  # deduplicate, preserve order
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _qty_str(occ: IngredientOccurrence) -> str:
    """Format a quantity + unit from an occurrence, e.g. '1 tbsp'."""
    parts = []
    if occ.quantity:
        parts.append(occ.quantity)
    if occ.unit:
        parts.append(occ.unit)
    return " ".join(parts) if parts else "some"
