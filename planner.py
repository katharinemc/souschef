"""
planner.py

Core weekly meal planner. Produces a fully-assigned WeekPlan for the
upcoming Fri–Thu window.

Scheduling order:
  1. Assign Friday (pizza — homemade first Friday of month, takeout otherwise)
  2. Mark no-cook days from WeekConstraints; annotate meatless note where relevant
  3. Assign Saturday (experiment if open + available, else easy/onRotation)
  4. Assign Sunday (easy recipe if sunday_needs_easy, else normal pool)
  5. Fill remaining slots to hit cook-night target (3 default, 4 if light week)
     - Wednesday and fasting days: meatless pool only
     - All other days: full pool
     - Spread evenly across available slots
  6. Select lunch of the week
  7. Return WeekPlan

Recipe selection within each pool is sorted by last_planned ascending
(most overdue first), with None (never planned) sorting first.
"""

import logging
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import yaml

from calendar_reader import WeekConstraints, DayConstraints
from state_store import StateStore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "recipe_dir":        "recipes_yaml",
    "lunch_file":        "lunches.yaml",
    "target_cook_nights": 3,       # default; bumped to 4 on light weeks
    "light_week_threshold": 1,     # no-cook days <= this → week is "light"
    "db_path":           "meal_planner.db",
}

# Days of the week (weekday int) that are always meatless regardless of calendar
ALWAYS_MEATLESS_WEEKDAYS = {2, 4}  # Wednesday=2, Friday=4

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class MealSlot:
    """A single assigned meal slot."""
    date:        date
    slot:        str              # 'dinner' or 'lunch'
    recipe_id:   Optional[str]    # None for no-cook / takeout
    label:       str              # display name
    tags:        list[str]        # recipe tags
    ingredients: list[dict]       # [{name, quantity?, unit?, section?}]
    notes:       list[str] = field(default_factory=list)  # planner annotations
    is_no_cook:  bool = False
    is_meatless: bool = False
    is_fasting:  bool = False

    @property
    def weekday_name(self) -> str:
        return self.date.strftime("%A")


@dataclass
class WeekPlan:
    """
    A fully-assigned meal plan for one Fri–Thu week.

    Attributes:
        week_start_monday:  The Monday opening this plan.
        week_key:           ISO date of the Monday of this calendar week.
        dinners:            Ordered list of MealSlots (Fri–Thu), one per day.
        lunch:              The single lunch-of-the-week MealSlot.
        cook_nights:        Count of actual cook nights (non-takeout, non-no-cook).
        warnings:           Any planner warnings (e.g. recipe pool exhausted).
    """
    week_start_monday: date
    week_key:          str
    dinners:           list[MealSlot] = field(default_factory=list)
    lunch:             Optional[MealSlot] = None
    cook_nights:       int = 0
    warnings:          list[str] = field(default_factory=list)

    def get_dinner(self, d: date) -> Optional[MealSlot]:
        for slot in self.dinners:
            if slot.date == d:
                return slot
        return None

    def to_dict(self) -> dict:
        """Serialise to a plain dict for JSON storage."""
        return {
            "week_start_monday": self.week_start_monday.isoformat(),
            "week_key":          self.week_key,
            "cook_nights":       self.cook_nights,
            "warnings":          self.warnings,
            "dinners": [
                {
                    "date":        s.date.isoformat(),
                    "weekday":     s.weekday_name,
                    "slot":        s.slot,
                    "recipe_id":   s.recipe_id,
                    "label":       s.label,
                    "tags":        s.tags,
                    "notes":       s.notes,
                    "is_no_cook":  s.is_no_cook,
                    "is_meatless": s.is_meatless,
                    "is_fasting":  s.is_fasting,
                    "ingredients": s.ingredients,
                }
                for s in self.dinners
            ],
            "lunch": {
                "lunch_id":   self.lunch.recipe_id,
                "label":      self.lunch.label,
                "notes":      self.lunch.notes,
                "ingredients": self.lunch.ingredients,
            } if self.lunch else None,
        }

    def to_state_meals(self) -> list[dict]:
        """Convert to the flat list format expected by StateStore.record_plan."""
        rows = []
        for s in self.dinners:
            rows.append({
                "meal_date":   s.date.isoformat(),
                "slot":        s.slot,
                "recipe_id":   s.recipe_id,
                "label":       s.label,
                "tags":        s.tags,
                "ingredients": s.ingredients,
            })
        if self.lunch:
            rows.append({
                "meal_date":   self.week_start_monday.isoformat(),
                "slot":        "lunch",
                "recipe_id":   self.lunch.recipe_id,
                "label":       self.lunch.label,
                "tags":        [],
                "ingredients": self.lunch.ingredients,
            })
        return rows


# ---------------------------------------------------------------------------
# Recipe library loader
# ---------------------------------------------------------------------------

def load_recipes(recipe_dir: str | Path) -> dict[str, dict]:
    """
    Load all YAML recipe files from recipe_dir.
    Returns {recipe_id: recipe_dict}.
    """
    recipes = {}
    for path in Path(recipe_dir).glob("*.yaml"):
        with open(path, encoding="utf-8") as f:
            r = yaml.safe_load(f)
        if r and "id" in r:
            recipes[r["id"]] = r
    log.debug("Loaded %d recipes from %s", len(recipes), recipe_dir)
    return recipes


def load_lunches(lunch_file: str | Path) -> list[dict]:
    """Load the manually-maintained lunch YAML file."""
    path = Path(lunch_file)
    if not path.exists():
        log.warning("Lunch file not found: %s", path)
        return []
    with open(path, encoding="utf-8") as f:
        entries = yaml.safe_load(f)
    return entries or []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _week_key(d: date) -> str:
    """ISO date of the Monday of the given week."""
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def _is_first_friday_of_month(d: date) -> bool:
    """True if d is the first Friday of its month."""
    return d.weekday() == 4 and d.day <= 7


def _is_meatless_required(d: date, dc: DayConstraints) -> bool:
    """True if this day requires a meatless recipe."""
    if d.weekday() in ALWAYS_MEATLESS_WEEKDAYS:
        return True
    if dc.is_fasting:
        return True
    return False


def _recipe_is_meatless(recipe: dict) -> bool:
    tags = set(recipe.get("tags") or [])
    return bool(tags & {"vegetarian", "pescatarian", "vegan"})


def _recipe_has_tag(recipe: dict, tag: str) -> bool:
    return tag in (recipe.get("tags") or [])


def _sort_by_last_planned(
    recipes: list[dict],
    last_planned_map: dict[str, date],
) -> list[dict]:
    """
    Sort recipes most-overdue first.
    Never-planned (None) sorts before any date.
    """
    def key(r):
        lp = last_planned_map.get(r["id"])
        if lp is None:
            return date.min
        return lp
    return sorted(recipes, key=key)


def _ingredients_from_recipe(recipe: dict) -> list[dict]:
    """Extract ingredient dicts, stripping YAML-only fields."""
    raw = recipe.get("ingredients") or []
    result = []
    for ing in raw:
        d = {"name": ing["name"]}
        if ing.get("quantity"):
            d["quantity"] = ing["quantity"]
        if ing.get("unit"):
            d["unit"] = ing["unit"]
        if ing.get("section"):
            d["section"] = ing["section"]
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class Planner:
    """
    Produces a WeekPlan given a WeekConstraints and a recipe library.

    Args:
        config:  Dict of config values; missing keys fall back to DEFAULT_CONFIG.
        store:   Optional StateStore; if None, rotation history is ignored.
    """

    def __init__(
        self,
        config:   Optional[dict] = None,
        store:    Optional[StateStore] = None,
    ):
        self.cfg   = {**DEFAULT_CONFIG, **(config or {})}
        self.store = store

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    def plan_week(self, constraints: WeekConstraints) -> WeekPlan:
        """
        Produce a WeekPlan for the week described by constraints.
        """
        monday = constraints.week_start_monday

        recipes  = load_recipes(self.cfg["recipe_dir"])
        lunches  = load_lunches(self.cfg["lunch_file"])

        # Last-planned maps from state store (or empty dicts if no store)
        last_planned      = self.store.get_all_last_planned() if self.store else {}
        lunch_last_planned = self.store.get_all_lunch_last_planned() if self.store else {}

        plan = WeekPlan(
            week_start_monday=monday,
            week_key=_week_key(monday),
        )

        # Track which recipe IDs have been assigned this week
        assigned_ids: set[str] = set()

        # Build ordered day list Fri → Thu
        days = [constraints.get(monday + timedelta(days=i)) for i in range(7)]

        # ----------------------------------------------------------------
        # Step 1: Friday — pizza (index 4 in Mon-based week)
        # ----------------------------------------------------------------
        friday    = monday + timedelta(days=4)
        friday_dc = days[4]
        plan.dinners.append(
            self._assign_friday(friday, friday_dc)
        )
        if not friday_dc.is_no_cook and _is_first_friday_of_month(friday):
            plan.cook_nights += 1  # homemade pizza counts

        # ----------------------------------------------------------------
        # Step 2 & 3: Saturday (index 5)
        # ----------------------------------------------------------------
        saturday    = monday + timedelta(days=5)
        saturday_dc = days[5]
        sat_slot = self._assign_saturday(
            saturday, saturday_dc, recipes, last_planned, assigned_ids
        )
        plan.dinners.append(sat_slot)
        saturday_cooked = not sat_slot.is_no_cook and sat_slot.recipe_id is not None
        if saturday_cooked:
            plan.cook_nights += 1
            assigned_ids.add(sat_slot.recipe_id)

        # ----------------------------------------------------------------
        # Step 4: Sunday (index 6)
        # Rule: if Saturday had a recipe, Sunday is always leftovers.
        #       if Saturday was no-cook, Sunday gets an easy recipe.
        # ----------------------------------------------------------------
        sunday    = monday + timedelta(days=6)
        sunday_dc = days[6]
        sun_slot = self._assign_sunday(
            sunday, sunday_dc,
            saturday_was_cook=saturday_cooked,
            needs_easy=constraints.sunday_needs_easy,
            recipes=recipes, last_planned=last_planned, assigned_ids=assigned_ids,
        )
        plan.dinners.append(sun_slot)
        if not sun_slot.is_no_cook and sun_slot.recipe_id:
            plan.cook_nights += 1
            assigned_ids.add(sun_slot.recipe_id)

        # ----------------------------------------------------------------
        # Step 5: Fill Mon–Thu to hit cook-night target
        # ----------------------------------------------------------------
        target = self._cook_night_target(constraints)
        remaining_days = [
            (monday + timedelta(days=i), days[i])
            for i in range(0, 4)  # Mon=0, Tue=1, Wed=2, Thu=3
        ]

        # Spread: shuffle remaining days so we don't always bias toward Monday
        # Use a seeded shuffle based on the week key for reproducibility
        rng = random.Random(plan.week_key)
        rng.shuffle(remaining_days)

        for day_date, dc in remaining_days:
            # Always append a slot for every day
            if dc.is_no_cook:
                slot = self._make_no_cook_slot(day_date, dc)
                plan.dinners.append(slot)
                continue

            if plan.cook_nights >= target:
                # Target met — remaining days get no-cook/leftovers label
                slot = self._make_leftovers_slot(day_date, dc)
                plan.dinners.append(slot)
                continue

            meatless_required = _is_meatless_required(day_date, dc)
            recipe = self._pick_recipe(
                recipes, last_planned, assigned_ids,
                meatless_required=meatless_required,
                prefer_easy=False,
            )

            if recipe is None:
                plan.warnings.append(
                    f"No suitable recipe found for {day_date} — marked as leftovers."
                )
                slot = self._make_leftovers_slot(day_date, dc)
            else:
                slot = self._make_recipe_slot(day_date, dc, recipe)
                assigned_ids.add(recipe["id"])
                plan.cook_nights += 1

            plan.dinners.append(slot)

        # Re-sort dinners into calendar order (Fri → Thu)
        plan.dinners.sort(key=lambda s: s.date)

        # ----------------------------------------------------------------
        # Step 6: Lunch of the week
        # ----------------------------------------------------------------
        plan.lunch = self._assign_lunch(lunches, lunch_last_planned)

        log.info(
            "Plan complete: %d cook nights, %d warnings",
            plan.cook_nights, len(plan.warnings)
        )
        return plan

    # -----------------------------------------------------------------------
    # Slot constructors
    # -----------------------------------------------------------------------

    def _assign_friday(self, d: date, dc: DayConstraints) -> MealSlot:
        if dc.is_no_cook:
            notes = ["Would have been pizza night"]
            if dc.no_cook_reason:
                notes.append(dc.no_cook_reason)
            return MealSlot(
                date=d, slot="dinner",
                recipe_id=None, label="no cook",
                tags=["pizza"], notes=notes,
                ingredients=[], is_no_cook=True,
            )

        homemade = _is_first_friday_of_month(d)
        label = "Homemade pizza" if homemade else "Takeout pizza"
        notes = ["First Friday — homemade pizza night"] if homemade else ["Takeout pizza night"]
        return MealSlot(
            date=d, slot="dinner",
            recipe_id="homemade-pizza" if homemade else None,
            label=label,
            tags=["pizza"],
            notes=notes,
            ingredients=[],
            is_meatless=True,  # Friday is always meatless
        )

    def _assign_saturday(
        self,
        d: date,
        dc: DayConstraints,
        recipes: dict,
        last_planned: dict,
        assigned_ids: set,
    ) -> MealSlot:
        if dc.is_no_cook:
            return self._make_no_cook_slot(d, dc)

        if dc.is_open_saturday:
            # Try experiment recipe first
            experiments = [
                r for r in recipes.values()
                if _recipe_has_tag(r, "experiment")
                and r["id"] not in assigned_ids
            ]
            if experiments:
                candidates = _sort_by_last_planned(experiments, last_planned)
                recipe = candidates[0]
                slot = self._make_recipe_slot(d, dc, recipe)
                slot.notes.insert(0, "Open Saturday — experiment recipe")
                return slot
            else:
                log.debug("No experiment recipes available; falling back to easy/onRotation for Saturday")

        # Fallback: easy recipe preferred, then onRotation
        meatless_required = _is_meatless_required(d, dc)
        recipe = self._pick_recipe(
            recipes, last_planned, assigned_ids,
            meatless_required=meatless_required,
            prefer_easy=True,
        )
        if recipe:
            return self._make_recipe_slot(d, dc, recipe)

        return self._make_leftovers_slot(d, dc)

    def _assign_sunday(
        self,
        d: date,
        dc: DayConstraints,
        saturday_was_cook: bool,
        needs_easy: bool,
        recipes: dict,
        last_planned: dict,
        assigned_ids: set,
    ) -> MealSlot:
        """
        Sunday assignment rules:
          - Calendar no-cook event → no cook slot
          - Saturday had a recipe → leftovers (never cook both weekend days)
          - Saturday was no-cook → easy recipe (recovery meal)
        """
        if dc.is_no_cook:
            return self._make_no_cook_slot(d, dc)

        if saturday_was_cook:
            # Saturday already covered the weekend cook slot
            return self._make_leftovers_slot(d, dc)

        # Saturday was no-cook — Sunday gets an easy recovery recipe
        meatless_required = _is_meatless_required(d, dc)
        recipe = self._pick_recipe(
            recipes, last_planned, assigned_ids,
            meatless_required=meatless_required,
            prefer_easy=True,
            require_easy=needs_easy,
        )

        if recipe is None and needs_easy:
            # Relax easy requirement rather than leave Sunday empty
            recipe = self._pick_recipe(
                recipes, last_planned, assigned_ids,
                meatless_required=meatless_required,
                prefer_easy=False,
            )

        if recipe:
            slot = self._make_recipe_slot(d, dc, recipe)
            if needs_easy:
                slot.notes.insert(0, "Easy recipe — no-cook Saturday recovery")
            return slot

        return self._make_leftovers_slot(d, dc)

    def _assign_lunch(
        self,
        lunches: list[dict],
        lunch_last_planned: dict,
    ) -> Optional[MealSlot]:
        if not lunches:
            return None

        def key(entry):
            lp = lunch_last_planned.get(entry.get("id", ""))
            return lp if lp is not None else date.min

        candidates = sorted(lunches, key=key)
        entry = candidates[0]

        notes = []
        prep = entry.get("prep_notes")
        if prep:
            notes.append(f"Weekend prep: {prep}")

        return MealSlot(
            date=date.today(),   # lunch slot date is nominal
            slot="lunch",
            recipe_id=entry.get("id"),
            label=entry.get("label", ""),
            tags=[],
            ingredients=[],
            notes=notes,
        )

    # -----------------------------------------------------------------------
    # Generic slot factories
    # -----------------------------------------------------------------------

    def _make_no_cook_slot(self, d: date, dc: DayConstraints) -> MealSlot:
        notes = []
        if dc.no_cook_reason:
            notes.append(dc.no_cook_reason)
        if _is_meatless_required(d, dc):
            notes.append("Would have been meatless")
        if dc.is_fasting:
            notes.append("Fasting day")
        return MealSlot(
            date=d, slot="dinner",
            recipe_id=None, label="no cook",
            tags=[], notes=notes,
            ingredients=[],
            is_no_cook=True,
            is_meatless=_is_meatless_required(d, dc),
            is_fasting=dc.is_fasting,
        )

    def _make_leftovers_slot(self, d: date, dc: DayConstraints) -> MealSlot:
        notes = []
        if dc.is_fasting:
            notes.append("Fasting day")
        if _is_meatless_required(d, dc):
            notes.append("Meatless day")
        return MealSlot(
            date=d, slot="dinner",
            recipe_id=None, label="leftovers",
            tags=[], notes=notes,
            ingredients=[],
            is_meatless=_is_meatless_required(d, dc),
            is_fasting=dc.is_fasting,
        )

    def _make_recipe_slot(
        self, d: date, dc: DayConstraints, recipe: dict
    ) -> MealSlot:
        notes = []
        if _recipe_has_tag(recipe, "freezer"):
            notes.append("Freezer-friendly — consider doubling")
        if dc.is_fasting:
            notes.append("Fasting day")

        return MealSlot(
            date=d, slot="dinner",
            recipe_id=recipe["id"],
            label=recipe["name"],
            tags=list(recipe.get("tags") or []),
            notes=notes,
            ingredients=_ingredients_from_recipe(recipe),
            is_meatless=_recipe_is_meatless(recipe) or _is_meatless_required(d, dc),
            is_fasting=dc.is_fasting,
        )

    # -----------------------------------------------------------------------
    # Recipe selection
    # -----------------------------------------------------------------------

    def _pick_recipe(
        self,
        recipes:           dict,
        last_planned:      dict,
        assigned_ids:      set,
        meatless_required: bool = False,
        prefer_easy:       bool = False,
        require_easy:      bool = False,
    ) -> Optional[dict]:
        """
        Select the best recipe from the pool.

        Priority:
          1. Not assigned this week
          2. If meatless_required: must be vegetarian/pescatarian/vegan
          3. If require_easy: must have 'easy' tag
          4. If prefer_easy: easy recipes sorted first
          5. onRotation recipes only (no experiment — those are Saturday-only)
          6. Sort by last_planned ascending (most overdue first)
          7. If pool is empty, fall back to least-recently-planned regardless
        """
        pool = [
            r for r in recipes.values()
            if r["id"] not in assigned_ids
            and not _recipe_has_tag(r, "experiment")  # experiments are Saturday-only
            and not _recipe_has_tag(r, "pizza")        # pizza is Friday-only
        ]

        if meatless_required:
            pool = [r for r in pool if _recipe_is_meatless(r)]

        if require_easy:
            easy_pool = [r for r in pool if _recipe_has_tag(r, "easy")]
            if easy_pool:
                pool = easy_pool
            # if no easy recipes available, fall through to full pool

        # Prefer onRotation; fall back to full pool if empty
        on_rotation = [r for r in pool if _recipe_has_tag(r, "onRotation")]
        working_pool = on_rotation if on_rotation else pool

        if not working_pool:
            # Last resort: allow re-use, pick least recently planned
            fallback = [
                r for r in recipes.values()
                if not _recipe_has_tag(r, "experiment")
                and not _recipe_has_tag(r, "pizza")
            ]
            if meatless_required:
                fallback = [r for r in fallback if _recipe_is_meatless(r)]
            if not fallback:
                return None
            working_pool = fallback

        sorted_pool = _sort_by_last_planned(working_pool, last_planned)

        if prefer_easy and not require_easy:
            # Stable-sort easy recipes to the front without discarding others
            easy = [r for r in sorted_pool if _recipe_has_tag(r, "easy")]
            non_easy = [r for r in sorted_pool if not _recipe_has_tag(r, "easy")]
            sorted_pool = easy + non_easy

        return sorted_pool[0] if sorted_pool else None

    # -----------------------------------------------------------------------
    # Cook-night target
    # -----------------------------------------------------------------------

    def _cook_night_target(self, constraints: WeekConstraints) -> int:
        """
        Return 3 (default) or 4 (light week).
        A light week has <= light_week_threshold no-cook days.
        Friday, Saturday, and Sunday are excluded from this count since
        they have their own fixed assignment logic.
        """
        base_target = self.cfg["target_cook_nights"]
        threshold   = self.cfg["light_week_threshold"]

        weekday_no_cooks = sum(
            1 for dc in constraints.days
            if dc.is_no_cook
            and dc.date.weekday() not in (4, 5, 6)  # exclude Fri/Sat/Sun
        )

        if weekday_no_cooks <= threshold:
            return base_target + 1  # light week → 4 cook nights
        return base_target
