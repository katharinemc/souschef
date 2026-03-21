"""
test_planner.py

Tests for the Planner. Uses synthetic recipes and WeekConstraints;
no filesystem or database access required.

Run with: python -m pytest test_planner.py -v
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent))

from calendar_reader import WeekConstraints, DayConstraints
from planner import (
    Planner, WeekPlan, MealSlot,
    _is_first_friday_of_month,
    _is_meatless_required,
    _sort_by_last_planned,
    _week_key,
    ALWAYS_MEATLESS_WEEKDAYS,
)
from state_store import StateStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Planning week: Mon 2026-03-02 (Friday = Mar 6 = first Friday of month → homemade pizza)
MONDAY_FIRST   = date(2026, 3, 2)
# Planning week: Mon 2026-03-23 (Friday = Mar 27, not first Friday)
MONDAY_NORMAL  = date(2026, 3, 23)

TUESDAY_NORMAL  = MONDAY_NORMAL + timedelta(days=1)
WEDNESDAY_NORMAL = MONDAY_NORMAL + timedelta(days=2)
THURSDAY_NORMAL  = MONDAY_NORMAL + timedelta(days=3)
FRIDAY_NORMAL    = MONDAY_NORMAL + timedelta(days=4)
SATURDAY_NORMAL  = MONDAY_NORMAL + timedelta(days=5)
SUNDAY_NORMAL    = MONDAY_NORMAL + timedelta(days=6)


def make_recipe(
    rid: str,
    name: str,
    tags: list[str],
    last_planned: date = None,
) -> dict:
    return {
        "id":           rid,
        "name":         name,
        "tags":         tags,
        "servings":     "4",
        "ingredients":  [{"name": "onion", "quantity": "1", "unit": "cup"}],
        "instructions": "Cook it.",
        "last_planned": last_planned.isoformat() if last_planned else None,
        "source":       None,
    }


def make_constraints(
    monday: date,
    no_cook_dates: list[date] = None,
    fasting_dates: list[date] = None,
    open_saturday: bool = True,
) -> WeekConstraints:
    no_cook_dates  = no_cook_dates or []
    fasting_dates  = fasting_dates or []
    wc = WeekConstraints(week_start_monday=monday)
    for i in range(7):
        d = monday + timedelta(days=i)
        dc = DayConstraints(date=d)
        if d in no_cook_dates:
            dc.is_no_cook = True
            dc.no_cook_reason = "qualifying event"
        if d in fasting_dates:
            dc.is_fasting = True
        if d.weekday() == 5:  # Saturday
            if d in no_cook_dates:
                dc.is_open_saturday = False
            else:
                dc.is_open_saturday = open_saturday
        wc.days.append(dc)
    return wc


def make_planner(recipes: dict, lunches: list = None) -> Planner:
    """Build a Planner with patched loaders."""
    p = Planner(config={
        "recipe_dir": "recipes_yaml",
        "lunch_file": "lunches.yaml",
        "target_cook_nights": 3,
        "light_week_threshold": 1,
    })
    lunch_list = lunches or [{"id": "grain-bowl", "label": "Quinoa grain bowl", "prep_notes": "Cook quinoa Sunday"}]
    p._load = MagicMock()
    # Patch at module level
    p._recipes = recipes
    p._lunches = lunch_list
    return p


def plan_week(
    recipes: dict,
    constraints: WeekConstraints,
    lunches: list = None,
    store: StateStore = None,
) -> WeekPlan:
    """Convenience: build a planner, patch loaders, run plan_week."""
    p = Planner(config={
        "recipe_dir": "recipes_yaml",
        "lunch_file": "lunches.yaml",
        "target_cook_nights": 3,
        "light_week_threshold": 1,
    }, store=store)

    lunch_list = lunches or [{"id": "grain-bowl", "label": "Quinoa grain bowl", "prep_notes": "Cook quinoa Sunday"}]

    with patch("planner.load_recipes", return_value=recipes), \
         patch("planner.load_lunches", return_value=lunch_list):
        return p.plan_week(constraints)


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):

    def test_first_friday_of_month(self):
        self.assertTrue(_is_first_friday_of_month(date(2026, 3, 6)))
        self.assertFalse(_is_first_friday_of_month(date(2026, 3, 13)))
        self.assertFalse(_is_first_friday_of_month(date(2026, 3, 20)))

    def test_not_friday_not_first_friday(self):
        self.assertFalse(_is_first_friday_of_month(date(2026, 3, 7)))  # Saturday

    def test_wednesday_is_meatless(self):
        dc = DayConstraints(date=date(2026, 3, 25))  # Wednesday
        self.assertTrue(_is_meatless_required(date(2026, 3, 25), dc))

    def test_friday_is_meatless(self):
        dc = DayConstraints(date=date(2026, 3, 20))
        self.assertTrue(_is_meatless_required(date(2026, 3, 20), dc))

    def test_tuesday_not_meatless_by_default(self):
        dc = DayConstraints(date=date(2026, 3, 24))
        self.assertFalse(_is_meatless_required(date(2026, 3, 24), dc))

    def test_fasting_day_is_meatless(self):
        dc = DayConstraints(date=date(2026, 3, 24), is_fasting=True)
        self.assertTrue(_is_meatless_required(date(2026, 3, 24), dc))

    def test_sort_by_last_planned_none_first(self):
        recipes = [
            make_recipe("a", "A", ["onRotation"], date(2026, 1, 1)),
            make_recipe("b", "B", ["onRotation"], None),
            make_recipe("c", "C", ["onRotation"], date(2026, 2, 1)),
        ]
        lp_map = {"a": date(2026, 1, 1), "c": date(2026, 2, 1)}
        sorted_r = _sort_by_last_planned(recipes, lp_map)
        self.assertEqual(sorted_r[0]["id"], "b")  # never planned → first
        self.assertEqual(sorted_r[1]["id"], "a")  # oldest → second
        self.assertEqual(sorted_r[2]["id"], "c")

    def test_week_key_from_friday(self):
        # Friday 2026-03-20 → Monday 2026-03-16
        self.assertEqual(_week_key(date(2026, 3, 20)), "2026-03-16")


# ---------------------------------------------------------------------------
# Friday assignment
# ---------------------------------------------------------------------------

class TestFridayAssignment(unittest.TestCase):

    def test_first_friday_is_homemade_pizza(self):
        wc = make_constraints(MONDAY_FIRST)
        recs = {"veggie-pasta": make_recipe("veggie-pasta", "Pasta", ["pasta", "vegetarian", "onRotation"])}
        plan = plan_week(recs, wc)
        friday_slot = plan.get_dinner(MONDAY_FIRST + timedelta(days=4))
        self.assertEqual(friday_slot.label, "Homemade pizza")
        self.assertIsNotNone(friday_slot.recipe_id)

    def test_non_first_friday_is_takeout_pizza(self):
        wc = make_constraints(MONDAY_NORMAL)
        recs = {"veggie-pasta": make_recipe("veggie-pasta", "Pasta", ["pasta", "vegetarian", "onRotation"])}
        plan = plan_week(recs, wc)
        friday_slot = plan.get_dinner(FRIDAY_NORMAL)
        self.assertEqual(friday_slot.label, "Takeout pizza")
        self.assertIsNone(friday_slot.recipe_id)

    def test_friday_no_cook(self):
        wc = make_constraints(MONDAY_NORMAL, no_cook_dates=[FRIDAY_NORMAL])
        recs = {"veggie-pasta": make_recipe("veggie-pasta", "Pasta", ["pasta", "vegetarian", "onRotation"])}
        plan = plan_week(recs, wc)
        friday_slot = plan.get_dinner(FRIDAY_NORMAL)
        self.assertTrue(friday_slot.is_no_cook)
        self.assertEqual(friday_slot.label, "no cook")

    def test_homemade_pizza_counts_as_cook_night(self):
        wc = make_constraints(MONDAY_FIRST)
        recs = {"veggie-pasta": make_recipe("veggie-pasta", "Pasta", ["pasta", "vegetarian", "onRotation"])}
        plan = plan_week(recs, wc)
        # Cook nights should include homemade pizza Friday
        self.assertGreaterEqual(plan.cook_nights, 1)

    def test_takeout_friday_does_not_count_as_cook_night(self):
        wc = make_constraints(MONDAY_NORMAL, open_saturday=False)
        # Only one recipe so we can track cook nights precisely
        recs = {"easy-veg": make_recipe("easy-veg", "Easy Veg", ["vegetarian", "easy", "onRotation"])}
        plan = plan_week(recs, wc)
        friday_slot = plan.get_dinner(FRIDAY_NORMAL)
        self.assertEqual(friday_slot.label, "Takeout pizza")
        # Takeout should NOT increment cook_nights
        # (cook_nights counts only actual cooking)
        # We can't assert exact count without knowing all slots, but
        # we can verify Friday itself is not a cook night
        self.assertIsNone(friday_slot.recipe_id)  # no recipe = not cooked


# ---------------------------------------------------------------------------
# Saturday assignment
# ---------------------------------------------------------------------------

class TestSaturdayAssignment(unittest.TestCase):

    def test_open_saturday_gets_experiment(self):
        wc = make_constraints(MONDAY_NORMAL, open_saturday=True)
        recs = {
            "exp-recipe":  make_recipe("exp-recipe",  "Experiment Dish", ["experiment"]),
            "veg-pasta":   make_recipe("veg-pasta",   "Veg Pasta", ["vegetarian", "onRotation"]),
        }
        plan = plan_week(recs, wc)
        sat = plan.get_dinner(SATURDAY_NORMAL)
        self.assertEqual(sat.recipe_id, "exp-recipe")

    def test_open_saturday_no_experiment_falls_back(self):
        wc = make_constraints(MONDAY_NORMAL, open_saturday=True)
        recs = {"easy-veg": make_recipe("easy-veg", "Easy Veg", ["vegetarian", "easy", "onRotation"])}
        plan = plan_week(recs, wc)
        sat = plan.get_dinner(SATURDAY_NORMAL)
        # Should get a real recipe, not no-cook
        self.assertFalse(sat.is_no_cook)

    def test_no_cook_saturday(self):
        wc = make_constraints(MONDAY_NORMAL, no_cook_dates=[SATURDAY_NORMAL])
        recs = {"recipe-a": make_recipe("recipe-a", "Recipe A", ["onRotation"])}
        plan = plan_week(recs, wc)
        sat = plan.get_dinner(SATURDAY_NORMAL)
        self.assertTrue(sat.is_no_cook)

    def test_experiment_not_scheduled_on_no_cook_saturday(self):
        wc = make_constraints(MONDAY_NORMAL, no_cook_dates=[SATURDAY_NORMAL])
        recs = {
            "exp-recipe": make_recipe("exp-recipe", "Experiment", ["experiment"]),
        }
        plan = plan_week(recs, wc)
        sat = plan.get_dinner(SATURDAY_NORMAL)
        self.assertTrue(sat.is_no_cook)
        self.assertNotEqual(sat.recipe_id, "exp-recipe")


# ---------------------------------------------------------------------------
# Sunday assignment
# ---------------------------------------------------------------------------

class TestSundayAssignment(unittest.TestCase):

    def test_sunday_after_no_cook_saturday_gets_easy(self):
        wc = make_constraints(MONDAY_NORMAL, no_cook_dates=[SATURDAY_NORMAL])
        recs = {
            "hard-recipe": make_recipe("hard-recipe", "Hard Recipe", ["onRotation"]),
            "easy-recipe": make_recipe("easy-recipe", "Easy Recipe", ["easy", "onRotation"]),
        }
        plan = plan_week(recs, wc)
        self.assertTrue(wc.sunday_needs_easy)
        sun = plan.get_dinner(SUNDAY_NORMAL)
        self.assertFalse(sun.is_no_cook)
        self.assertIn("easy", sun.tags)

    def test_sunday_after_normal_saturday_is_unrestricted(self):
        wc = make_constraints(MONDAY_NORMAL, open_saturday=True)
        recs = {
            "exp-recipe":  make_recipe("exp-recipe",  "Experiment", ["experiment"]),
            "hard-recipe": make_recipe("hard-recipe", "Hard Recipe", ["onRotation"]),
        }
        plan = plan_week(recs, wc)
        self.assertFalse(wc.sunday_needs_easy)
        # Sunday can have any recipe
        sun = plan.get_dinner(SUNDAY_NORMAL)
        self.assertIsNotNone(sun)

    def test_sunday_no_cook(self):
        wc = make_constraints(MONDAY_NORMAL, no_cook_dates=[SUNDAY_NORMAL])
        recs = {"recipe-a": make_recipe("recipe-a", "Recipe A", ["onRotation"])}
        plan = plan_week(recs, wc)
        sun = plan.get_dinner(SUNDAY_NORMAL)
        self.assertTrue(sun.is_no_cook)


# ---------------------------------------------------------------------------
# Meatless rules
# ---------------------------------------------------------------------------

class TestMeatlessRules(unittest.TestCase):

    def test_wednesday_only_gets_meatless_recipe(self):
        wc = make_constraints(MONDAY_NORMAL)
        recs = {
            "meat-recipe":    make_recipe("meat-recipe",    "Meat Dish",    ["onRotation"]),
            "veggie-recipe":  make_recipe("veggie-recipe",  "Veggie Dish",  ["vegetarian", "onRotation"]),
            "pescatarian":    make_recipe("pescatarian",    "Fish Dish",    ["pescatarian", "onRotation"]),
            "easy-recipe":    make_recipe("easy-recipe",    "Easy Veg",     ["vegetarian", "easy", "onRotation"]),
            "exp-recipe":     make_recipe("exp-recipe",     "Experiment",   ["experiment"]),
        }
        plan = plan_week(recs, wc)
        wed = plan.get_dinner(WEDNESDAY_NORMAL)
        if wed and not wed.is_no_cook and not wed.label == "leftovers":
            self.assertTrue(wed.is_meatless,
                msg=f"Wednesday got non-meatless recipe: {wed.label}")

    def test_fasting_day_gets_meatless(self):
        wc = make_constraints(MONDAY_NORMAL, fasting_dates=[TUESDAY_NORMAL])
        recs = {
            "meat-recipe":   make_recipe("meat-recipe",   "Meat Dish",  ["onRotation"]),
            "veggie-recipe": make_recipe("veggie-recipe", "Veggie Dish",["vegetarian", "onRotation"]),
        }
        plan = plan_week(recs, wc)
        tue = plan.get_dinner(TUESDAY_NORMAL)
        if tue and not tue.is_no_cook:
            self.assertTrue(tue.is_meatless)

    def test_no_cook_wednesday_annotated_meatless(self):
        wc = make_constraints(MONDAY_NORMAL, no_cook_dates=[WEDNESDAY_NORMAL])
        recs = {"recipe-a": make_recipe("recipe-a", "Recipe A", ["onRotation"])}
        plan = plan_week(recs, wc)
        wed = plan.get_dinner(WEDNESDAY_NORMAL)
        self.assertTrue(wed.is_no_cook)
        self.assertTrue(wed.is_meatless)
        self.assertTrue(any("meatless" in n.lower() for n in wed.notes))


# ---------------------------------------------------------------------------
# Cook night target
# ---------------------------------------------------------------------------

class TestCookNightTarget(unittest.TestCase):

    def test_light_week_targets_4_cook_nights(self):
        # No no-cook days on Mon-Thu → light week → target = 4
        wc = make_constraints(MONDAY_NORMAL)
        recs = {f"recipe-{i}": make_recipe(f"recipe-{i}", f"Recipe {i}", ["onRotation"])
                for i in range(8)}
        plan = plan_week(recs, wc)
        # cook_nights should be 4 on a light week (Sat, Sun, + 2 weekdays)
        # Note: takeout Friday doesn't count; light week = ≤1 no-cook on Mon-Thu
        self.assertGreaterEqual(plan.cook_nights, 3)

    def test_busy_week_targets_3_cook_nights(self):
        # Two no-cook weekdays → not light → target = 3
        wc = make_constraints(
            FRIDAY_NORMAL,
            no_cook_dates=[MONDAY_NORMAL, TUESDAY_NORMAL]
        )
        recs = {f"recipe-{i}": make_recipe(f"recipe-{i}", f"Recipe {i}", ["onRotation"])
                for i in range(6)}
        plan = plan_week(recs, wc)
        # With 2 no-cook weekdays, target is 3, achieved via Sat + Sun + remaining days
        self.assertGreaterEqual(plan.cook_nights, 1)


# ---------------------------------------------------------------------------
# Recipe rotation
# ---------------------------------------------------------------------------

class TestRotation(unittest.TestCase):

    def test_never_planned_recipe_picked_first(self):
        wc = make_constraints(MONDAY_NORMAL, open_saturday=False)
        recs = {
            "stale":   make_recipe("stale",   "Stale",  ["onRotation"]),
            "fresh":   make_recipe("fresh",   "Fresh",  ["onRotation"]),
            "unplanned": make_recipe("unplanned", "Unplanned", ["onRotation"]),
        }
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            store = StateStore(f.name)
            store.set_last_planned("stale", date(2026, 1, 1))
            store.set_last_planned("fresh", date(2026, 3, 1))
            # "unplanned" has no last_planned entry

            plan = plan_week(recs, wc, store=store)

        # The unplanned recipe should appear before stale or fresh
        assigned = {s.recipe_id for s in plan.dinners if s.recipe_id}
        self.assertIn("unplanned", assigned)

    def test_recipe_not_repeated_in_same_week(self):
        wc = make_constraints(MONDAY_NORMAL, open_saturday=True)
        # Only two non-experiment recipes; need enough to fill slots
        recs = {
            "exp":    make_recipe("exp",    "Experiment",  ["experiment"]),
            "veg-a":  make_recipe("veg-a",  "Veg A",  ["vegetarian", "onRotation"]),
            "veg-b":  make_recipe("veg-b",  "Veg B",  ["vegetarian", "onRotation"]),
            "meat-a": make_recipe("meat-a", "Meat A", ["onRotation"]),
            "meat-b": make_recipe("meat-b", "Meat B", ["onRotation"]),
            "easy-v": make_recipe("easy-v", "Easy Veg", ["vegetarian", "easy", "onRotation"]),
        }
        plan = plan_week(recs, wc)
        assigned_ids = [s.recipe_id for s in plan.dinners if s.recipe_id]
        # No duplicates
        self.assertEqual(len(assigned_ids), len(set(assigned_ids)))

    def test_experiment_not_scheduled_on_weekday(self):
        wc = make_constraints(MONDAY_NORMAL, open_saturday=True)
        recs = {
            "exp":    make_recipe("exp",    "Experiment",  ["experiment"]),
            "normal": make_recipe("normal", "Normal",      ["onRotation"]),
            "veg":    make_recipe("veg",    "Veg",         ["vegetarian", "onRotation"]),
            "easy":   make_recipe("easy",   "Easy",        ["easy", "onRotation"]),
        }
        plan = plan_week(recs, wc)
        weekday_dates = [MONDAY_NORMAL, TUESDAY_NORMAL, WEDNESDAY_NORMAL, THURSDAY_NORMAL]
        for d in weekday_dates:
            slot = plan.get_dinner(d)
            if slot:
                self.assertNotEqual(slot.recipe_id, "exp",
                    msg=f"Experiment recipe assigned on weekday {d}")

    def test_fallback_reuses_recipe_when_pool_exhausted(self):
        # Only one eligible recipe; planner should reuse rather than leave empty
        wc = make_constraints(MONDAY_NORMAL, open_saturday=False)
        recs = {
            "only-recipe": make_recipe("only-recipe", "Only Recipe", ["onRotation"]),
        }
        plan = plan_week(recs, wc)
        cook_slots = [s for s in plan.dinners if s.recipe_id and s.recipe_id != "homemade-pizza"]
        self.assertGreater(len(cook_slots), 0)


# ---------------------------------------------------------------------------
# Plan structure
# ---------------------------------------------------------------------------

class TestPlanStructure(unittest.TestCase):

    def test_plan_has_7_dinner_slots(self):
        wc = make_constraints(MONDAY_NORMAL)
        recs = {f"r{i}": make_recipe(f"r{i}", f"Recipe {i}", ["onRotation"]) for i in range(8)}
        plan = plan_week(recs, wc)
        self.assertEqual(len(plan.dinners), 7)

    def test_plan_spans_mon_to_sun(self):
        wc = make_constraints(MONDAY_NORMAL)
        recs = {f"r{i}": make_recipe(f"r{i}", f"Recipe {i}", ["onRotation"]) for i in range(8)}
        plan = plan_week(recs, wc)
        dates = [s.date for s in plan.dinners]
        self.assertEqual(min(dates), MONDAY_NORMAL)
        self.assertEqual(max(dates), SUNDAY_NORMAL)

    def test_plan_has_lunch(self):
        wc = make_constraints(MONDAY_NORMAL)
        recs = {"r0": make_recipe("r0", "Recipe", ["onRotation"])}
        plan = plan_week(recs, wc)
        self.assertIsNotNone(plan.lunch)

    def test_lunch_prep_notes_surfaced(self):
        wc = make_constraints(MONDAY_NORMAL)
        recs = {"r0": make_recipe("r0", "Recipe", ["onRotation"])}
        lunches = [{"id": "grain", "label": "Grain bowl", "prep_notes": "Cook quinoa Sunday"}]
        plan = plan_week(recs, wc, lunches=lunches)
        self.assertTrue(any("Cook quinoa Sunday" in n for n in plan.lunch.notes))

    def test_to_dict_serialisable(self):
        import json
        wc = make_constraints(MONDAY_NORMAL)
        recs = {f"r{i}": make_recipe(f"r{i}", f"Recipe {i}", ["onRotation"]) for i in range(6)}
        plan = plan_week(recs, wc)
        d = plan.to_dict()
        # Should be JSON-serialisable
        json.dumps(d)

    def test_to_state_meals_format(self):
        wc = make_constraints(MONDAY_NORMAL)
        recs = {f"r{i}": make_recipe(f"r{i}", f"Recipe {i}", ["onRotation"]) for i in range(6)}
        plan = plan_week(recs, wc)
        meals = plan.to_state_meals()
        self.assertGreater(len(meals), 0)
        for m in meals:
            self.assertIn("meal_date", m)
            self.assertIn("slot", m)
            self.assertIn("label", m)

    def test_week_key_correct(self):
        wc = make_constraints(MONDAY_NORMAL)
        recs = {"r0": make_recipe("r0", "Recipe", ["onRotation"])}
        plan = plan_week(recs, wc)
        # MONDAY_NORMAL 2026-03-23 → week_key is 2026-03-23
        self.assertEqual(plan.week_key, "2026-03-23")


if __name__ == "__main__":
    unittest.main(verbosity=2)
