"""
grocery_builder.py

Builds a categorised grocery list from a WeekPlan.

Responsibilities:
  - Aggregate all ingredients across the week's cook nights
  - Group by grocery category (produce, protein, dairy, pantry, frozen, other)
  - Flag ingredients likely already on hand from the prior week's plan
  - Combine duplicate ingredients (same canonical name) and sum quantities
    where units match; flag as "check quantity" where they don't
  - Return a GroceryList dataclass ready for the email formatter

Leftover / on-hand logic:
  The StateStore provides the last 2 weeks of ingredient history.
  An ingredient is flagged "likely on hand" if:
    - It appeared in a planned meal in the past 7 days, AND
    - It is not a highly perishable item (fresh produce, meat)
  The user confirms or corrects this in their reply email.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from planner import WeekPlan
from stacker import normalise_ingredient, parse_quantity_ml, UNIT_ML
from state_store import StateStore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category map
# ---------------------------------------------------------------------------

# Each entry: (canonical_pattern, category)
# Checked in order; first match wins.
CATEGORY_RULES: list[tuple[str, str]] = [
    # Produce
    (r"\bonion\b",          "produce"),
    (r"\bshallot\b",        "produce"),
    (r"\bgarlic\b",         "produce"),
    (r"\bginger\b",         "produce"),
    (r"\bspinach\b",        "produce"),
    (r"\bkale\b",           "produce"),
    (r"\bcabbage\b",        "produce"),
    (r"\bbroccoli\b",       "produce"),
    (r"\bcauliflower\b",    "produce"),
    (r"\bzucchini\b",       "produce"),
    (r"\beggplant\b",       "produce"),
    (r"\btomato\b(?!.*paste)",         "produce"),
    (r"\bell pepper\b",     "produce"),
    (r"\bcarrot\b",         "produce"),
    (r"\bcelery\b",         "produce"),
    (r"\bcucumber\b",       "produce"),
    (r"\bpotato\b",         "produce"),
    (r"\bsweet potato\b",   "produce"),
    (r"\blemon\b",          "produce"),
    (r"\blime\b",           "produce"),
    (r"\bavocado\b",        "produce"),
    (r"\bdate\b",           "produce"),
    (r"\bherb\b",           "produce"),
    (r"\bparsley\b",        "produce"),
    (r"\bcilantro\b",       "produce"),
    (r"\bbasil\b",          "produce"),
    (r"\bchive\b",          "produce"),
    (r"\bdill\b",           "produce"),
    (r"\bmint\b",           "produce"),
    (r"\bthyme\b",          "produce"),
    (r"\brosemary\b",       "produce"),
    (r"\bscallion\b",       "produce"),
    (r"\bleek\b",           "produce"),
    (r"\bfennel\b",         "produce"),
    (r"\bjalapeno\b",       "produce"),
    (r"\bchile\b",          "produce"),
    (r"\bpeppers?\b",       "produce"),
    (r"\bmushroom\b",       "produce"),
    (r"\basparagus\b",      "produce"),
    (r"\bgreen bean\b",     "produce"),
    (r"\bpea\b",            "produce"),
    (r"\bcorn\b",           "produce"),
    (r"\bbok choy\b",       "produce"),
    (r"\bchard\b",          "produce"),
    (r"\barugula\b",        "produce"),
    (r"\blettuce\b",        "produce"),
    (r"\bapple\b",          "produce"),
    (r"\bpear\b",           "produce"),
    (r"\bgrape\b",          "produce"),
    (r"\bberr",             "produce"),
    (r"\bfresh\b.*\bjuice", "produce"),
    # Protein
    (r"\bground beef\b",    "protein"),
    (r"\bground lamb\b",    "protein"),
    (r"\bground turkey\b",  "protein"),
    (r"\bground chicken\b", "protein"),
    (r"\bground pork\b",    "protein"),
    (r"\bchicken\b",        "protein"),
    (r"\bturkey\b",         "protein"),
    (r"\bpork\b",           "protein"),
    (r"\blamb\b",           "protein"),
    (r"\bbeef\b",           "protein"),
    (r"\bsalmon\b",         "protein"),
    (r"\btuna\b",           "protein"),
    (r"\bshrimp\b",         "protein"),
    (r"\bcod\b",            "protein"),
    (r"\btilapia\b",        "protein"),
    (r"\bhalibut\b",        "protein"),
    (r"\bsausage\b",        "protein"),
    (r"\bbacon\b",          "protein"),
    (r"\bham\b",            "protein"),
    (r"\bprosciutto\b",     "protein"),
    (r"\bpancetta\b",       "protein"),
    (r"\bsardine\b",        "protein"),
    (r"\banchovie\b",       "protein"),
    (r"\btofu\b",           "protein"),
    (r"\btempeh\b",         "protein"),
    (r"\begg\b",            "protein"),
    # Dairy
    (r"\bmilk\b",           "dairy"),
    (r"\bcream\b",          "dairy"),
    (r"\bbutter\b",         "dairy"),
    (r"\bcheese\b",         "dairy"),
    (r"\byogurt\b",         "dairy"),
    (r"\bkefir\b",          "dairy"),
    (r"\bsour cream\b",     "dairy"),
    (r"\bcottage cheese\b", "dairy"),
    (r"\bricotta\b",        "dairy"),
    (r"\bmascarpone\b",     "dairy"),
    (r"\bghee\b",           "dairy"),
    (r"\bhalf.and.half\b",  "dairy"),
    # Frozen
    (r"\bfrozen\b",         "frozen"),
    # Pantry — grains & pasta
    (r"\brice\b",           "pantry"),
    (r"\bquinoa\b",         "pantry"),
    (r"\bfarro\b",          "pantry"),
    (r"\bbulgur\b",         "pantry"),
    (r"\bcouscous\b",       "pantry"),
    (r"\bpasta\b",          "pantry"),
    (r"\bnoodle\b",         "pantry"),
    (r"\bpenne\b",          "pantry"),
    (r"\bspaghetti\b",      "pantry"),
    (r"\bfettuccine\b",     "pantry"),
    (r"\borzo\b",           "pantry"),
    (r"\bbread\b",          "pantry"),
    (r"\bpanko\b",          "pantry"),
    (r"\bbreadcrumb\b",     "pantry"),
    (r"\btortilla\b",       "pantry"),
    (r"\bpita\b",           "pantry"),
    (r"\bflour\b",          "pantry"),
    (r"\boat\b",            "pantry"),
    # Pantry — canned & jarred
    (r"\bcanned\b",         "pantry"),
    (r"\bchickpea",          "pantry"),
    (r"\bgarbanzo",          "pantry"),
    (r"\bblack bean\b",     "pantry"),
    (r"\bkidney bean\b",    "pantry"),
    (r"\blentil\b",         "pantry"),
    (r"\bwhite bean\b",     "pantry"),
    (r"\bpinto bean\b",     "pantry"),
    (r"\btomato paste\b",   "pantry"),
    (r"\bmarinara\b",       "pantry"),
    (r"\bsalsa\b",          "pantry"),
    (r"\bbroth\b",          "pantry"),
    (r"\bstock\b",          "pantry"),
    (r"\bcoconut milk\b",   "pantry"),
    (r"\bolive\b",          "pantry"),
    (r"\bcaper\b",          "pantry"),
    (r"\bpickle\b",         "pantry"),
    (r"\banchov\b",         "pantry"),
    # Pantry — oils, vinegars, condiments
    (r"\boil\b",            "pantry"),
    (r"\bvinegar\b",        "pantry"),
    (r"\bsoy sauce\b",      "pantry"),
    (r"\btamari\b",         "pantry"),
    (r"\bfish sauce\b",     "pantry"),
    (r"\bworcestershire\b", "pantry"),
    (r"\bhotsauce\b",       "pantry"),
    (r"\bharissa\b",        "pantry"),
    (r"\bsriracha\b",       "pantry"),
    (r"\bmustard\b",        "pantry"),
    (r"\bketchup\b",        "pantry"),
    (r"\bmayonnaise\b",     "pantry"),
    (r"\bhoney\b",          "pantry"),
    (r"\bmaple syrup\b",    "pantry"),
    (r"\bmolasses\b",       "pantry"),
    (r"\bjam\b",            "pantry"),
    # Pantry — spices & dry goods
    (r"\bspice\b",          "pantry"),
    (r"\bsalt\b",           "pantry"),
    (r"\bpepper\b",         "pantry"),
    (r"\bcumin\b",          "pantry"),
    (r"\bpaprika\b",        "pantry"),
    (r"\bturmeric\b",       "pantry"),
    (r"\bcoriander\b",      "pantry"),
    (r"\bcinnamon\b",       "pantry"),
    (r"\bclove\b",          "pantry"),
    (r"\bnumeg\b",          "pantry"),
    (r"\bgaram masala\b",   "pantry"),
    (r"\bcurry\b",          "pantry"),
    (r"\bcayenne\b",        "pantry"),
    (r"\bchili\b",          "pantry"),
    (r"\bitalian seasoning","pantry"),
    (r"\bbay leaf\b",       "pantry"),
    (r"\bred pepper flake", "pantry"),
    (r"\bsoup mix\b",       "pantry"),
    (r"\bcocoa\b",          "pantry"),
    (r"\bchocolate\b",      "pantry"),
    (r"\bvanilla\b",        "pantry"),
    (r"\bbaking soda\b",    "pantry"),
    (r"\bbaking powder\b",  "pantry"),
    (r"\bsugar\b",          "pantry"),
    (r"\bnut\b",            "pantry"),
    (r"\bpistachio\b",      "pantry"),
    (r"\bwalnut\b",         "pantry"),
    (r"\balmond\b",         "pantry"),
    (r"\bpine nut\b",       "pantry"),
    (r"\bdried fruit\b",    "pantry"),
    (r"\braisin\b",         "pantry"),
    (r"\bcranberr\b",       "pantry"),
    (r"\bwine\b",           "pantry"),
    (r"\bbeer\b",           "pantry"),
    (r"\bstock cube\b",     "pantry"),
]

CATEGORY_ORDER = ["produce", "protein", "dairy", "pantry", "frozen", "other"]

# Perishable items — NOT flagged as "likely on hand" even if they were
# in last week's plan, because they spoil quickly.
PERISHABLE_PATTERNS = [
    r"\bfresh\b",
    r"\bproduce\b",
    r"\bspinach\b", r"\bkale\b", r"\blettuce\b", r"\barugula\b",
    r"\bherb\b", r"\bcilantro\b", r"\bparsley\b", r"\bbasil\b",
    r"\bchive\b", r"\bdill\b", r"\bmint\b",
    r"\bmushroom\b",
    r"\bground beef\b", r"\bground lamb\b", r"\bground turkey\b",
    r"\bground chicken\b", r"\bground pork\b",
    r"\bchicken\b", r"\bturkey\b", r"\bbeef\b", r"\bpork\b",
    r"\blamb\b", r"\bsalmon\b", r"\bshrimp\b", r"\bfish\b",
    r"\bmilk\b", r"\bwhipping cream\b", r"\bheavy cream\b",
    r"\byogurt\b",
]


def categorise(name: str) -> str:
    lower = name.lower()
    for pattern, category in CATEGORY_RULES:
        if re.search(pattern, lower):
            return category
    return "other"


def is_perishable(name: str) -> bool:
    lower = name.lower()
    return any(re.search(p, lower) for p in PERISHABLE_PATTERNS)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class GroceryItem:
    """One item on the grocery list."""
    name:           str              # best display name from the recipes
    canonical:      str              # normalised key used for dedup
    category:       str
    quantity:       Optional[str]    # combined quantity string, or None
    unit:           Optional[str]    # unit, or None if mixed/unknown
    likely_on_hand: bool = False     # flagged from prior week history
    quantity_note:  Optional[str] = None  # e.g. "check quantity — used in 2 recipes"
    recipes:        list[str] = field(default_factory=list)  # which recipes need this


@dataclass
class GroceryList:
    """
    Full grocery list for a week, organised by category.

    Attributes:
        week_start_friday:  The Friday of the planning week.
        items_by_category:  {category: [GroceryItem]} in CATEGORY_ORDER.
        likely_on_hand:     Items flagged as probably already in the pantry.
        all_items:          Flat list of all items (convenience accessor).
    """
    week_start_monday:  date
    items_by_category:  dict[str, list[GroceryItem]] = field(default_factory=dict)
    likely_on_hand:     list[GroceryItem] = field(default_factory=list)

    @property
    def all_items(self) -> list[GroceryItem]:
        items = []
        for cat in CATEGORY_ORDER:
            items.extend(self.items_by_category.get(cat, []))
        return items

    def category_items(self, category: str) -> list[GroceryItem]:
        return self.items_by_category.get(category, [])

    def summary(self) -> str:
        lines = [f"Grocery list — week of {self.week_start_monday}"]
        for cat in CATEGORY_ORDER:
            items = self.items_by_category.get(cat, [])
            if not items:
                continue
            lines.append(f"\n{cat.upper()}")
            for item in items:
                qty = f"{item.quantity} {item.unit}".strip() if item.quantity else ""
                note = f"  [{item.quantity_note}]" if item.quantity_note else ""
                lines.append(f"  - {item.name}{(' — ' + qty) if qty else ''}{note}")
        if self.likely_on_hand:
            lines.append("\nLIKELY ON HAND (confirm before buying)")
            for item in self.likely_on_hand:
                lines.append(f"  - {item.name}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class GroceryBuilder:
    """
    Builds a GroceryList from a WeekPlan.

    Args:
        store: Optional StateStore for leftover / on-hand detection.
               If None, no items are flagged as likely on hand.
    """

    def __init__(self, store: Optional[StateStore] = None):
        self.store = store

    def build(self, plan: WeekPlan) -> GroceryList:
        """
        Build and return a GroceryList for the given WeekPlan.
        """
        # Step 1: collect all raw ingredients from cook-night slots
        raw: list[tuple[str, Optional[str], Optional[str], str]] = []
        # (name, quantity, unit, recipe_label)

        for slot in plan.dinners:
            if slot.is_no_cook or not slot.recipe_id:
                continue
            # Skip homemade pizza — ingredients not in the recipe library yet
            if slot.recipe_id == "homemade-pizza":
                continue
            for ing in slot.ingredients:
                name = ing.get("name", "").strip()
                if not name:
                    continue
                raw.append((
                    name,
                    ing.get("quantity"),
                    ing.get("unit"),
                    slot.label,
                ))

        # Step 2: get prior-week ingredient names for on-hand detection
        prior_ingredient_names: set[str] = set()
        if self.store:
            cutoff = (date.today() - timedelta(days=7)).isoformat()
            recent = self.store.get_recent_ingredients(weeks=2)
            for r in recent:
                if r["meal_date"] < date.today().isoformat():
                    prior_ingredient_names.add(
                        normalise_ingredient(r["name"]) or r["name"].lower()
                    )

        # Step 3: aggregate by canonical name
        aggregated = self._aggregate(raw)

        # Step 4: categorise, flag on-hand, build output
        items_by_category: dict[str, list[GroceryItem]] = {
            cat: [] for cat in CATEGORY_ORDER
        }
        likely_on_hand: list[GroceryItem] = []

        for canonical, data in sorted(aggregated.items(), key=lambda x: x[0]):
            category  = categorise(data["best_name"])
            on_hand   = (
                canonical in prior_ingredient_names
                and not is_perishable(data["best_name"])
            )

            item = GroceryItem(
                name=data["best_name"],
                canonical=canonical,
                category=category,
                quantity=data["quantity"],
                unit=data["unit"],
                likely_on_hand=on_hand,
                quantity_note=data.get("quantity_note"),
                recipes=data["recipes"],
            )

            if on_hand:
                likely_on_hand.append(item)
            else:
                items_by_category.setdefault(category, []).append(item)

        grocery = GroceryList(
            week_start_monday=plan.week_start_monday,
            items_by_category=items_by_category,
            likely_on_hand=likely_on_hand,
        )

        log.info(
            "Grocery list built: %d items, %d likely on hand",
            len(grocery.all_items),
            len(likely_on_hand),
        )
        return grocery

    # -----------------------------------------------------------------------
    # Aggregation
    # -----------------------------------------------------------------------

    def _aggregate(
        self,
        raw: list[tuple[str, Optional[str], Optional[str], str]],
    ) -> dict[str, dict]:
        """
        Group raw ingredient tuples by canonical name.
        Combines quantities where possible; flags mixed units.

        Returns {canonical: {best_name, quantity, unit, quantity_note, recipes}}
        """
        groups: dict[str, list[tuple]] = {}
        best_names: dict[str, str] = {}

        for name, qty, unit, recipe_label in raw:
            canonical = normalise_ingredient(name)
            if canonical is None:
                # Generic ingredient (salt, oil, etc.) — still include it
                # under its cleaned name so the list isn't missing basics
                canonical = name.lower().strip()

            groups.setdefault(canonical, []).append((name, qty, unit, recipe_label))

            # Keep the longest / most descriptive raw name as display name
            existing = best_names.get(canonical, "")
            if len(name) > len(existing):
                best_names[canonical] = name

        result = {}
        for canonical, entries in groups.items():
            recipes = list(dict.fromkeys(e[3] for e in entries))
            best_name = best_names[canonical]

            if len(entries) == 1:
                _, qty, unit, _ = entries[0]
                result[canonical] = {
                    "best_name":     best_name,
                    "quantity":      qty,
                    "unit":          unit,
                    "quantity_note": None,
                    "recipes":       recipes,
                }
                continue

            # Multiple entries — try to combine
            combined = self._combine_quantities(entries)
            result[canonical] = {
                "best_name":     best_name,
                "quantity":      combined["quantity"],
                "unit":          combined["unit"],
                "quantity_note": combined.get("note"),
                "recipes":       recipes,
            }

        return result

    def _combine_quantities(
        self, entries: list[tuple]
    ) -> dict:
        """
        Attempt to sum quantities across entries with compatible units.
        Falls back to a note if units are mixed or quantities are unparseable.
        """
        # Collect (quantity_str, unit) pairs
        pairs = [(e[1], e[2]) for e in entries]

        # All units the same (or all None)?
        units = [p[1] for p in pairs if p[1] is not None]
        def _norm_unit(u):
            if u is None:
                return u
            u = u.lower().strip()
            # normalise plural forms for comparison
            plural_map = {
                "cups": "cup", "tablespoons": "tablespoon", "teaspoons": "teaspoon",
                "tbsps": "tbsp", "tsps": "tsp", "ounces": "oz", "pounds": "lb",
                "lbs": "lb", "grams": "g", "liters": "l", "milliliters": "ml",
                "pinches": "pinch", "cloves": "clove",
            }
            return plural_map.get(u, u)
        unique_units = list(dict.fromkeys(_norm_unit(u) for u in units if u is not None))

        if not units:
            # No units at all — just note count
            return {
                "quantity": None,
                "unit":     None,
                "note":     f"used in {len(entries)} recipes — check quantity",
            }

        if len(unique_units) == 1:
            # All same unit — attempt numeric sum
            total_ml = 0.0
            all_convertible = True
            for qty_str, unit in pairs:
                ml = parse_quantity_ml(qty_str, unit)
                if ml is None:
                    all_convertible = False
                    break
                total_ml += ml

            unit = unique_units[0]
            # Look up factor using both normalised and original unit form
            if all_convertible:
                factor = UNIT_ML.get(unit, UNIT_ML.get(units[0].lower() if units else "", 1.0))
                total_in_unit = total_ml / factor
                # Format nicely
                qty_str = _format_quantity(total_in_unit)
                return {"quantity": qty_str, "unit": unit, "note": None}
            else:
                return {
                    "quantity": None,
                    "unit":     unit,
                    "note":     f"used in {len(entries)} recipes — check quantity",
                }

        # Mixed units — can't combine
        qty_parts = [
            f"{p[0]} {p[1]}".strip() for p in pairs if p[0] or p[1]
        ]
        return {
            "quantity": None,
            "unit":     None,
            "note":     "mixed units — " + ", ".join(qty_parts),
        }


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_quantity(value: float) -> str:
    """Format a float quantity cleanly — avoid '1.0', prefer '1'."""
    if value == int(value):
        return str(int(value))
    # Round to 2 decimal places, strip trailing zeros
    return f"{value:.2f}".rstrip("0").rstrip(".")
