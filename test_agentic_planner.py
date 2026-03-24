"""
test_agentic_planner.py

Run with: python -m pytest test_agentic_planner.py -v
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

from calendar_reader import WeekConstraints, DayConstraints
from state_store import StateStore


MONDAY = date(2026, 3, 23)


def make_constraints(monday=MONDAY, open_saturday=True) -> WeekConstraints:
    wc = WeekConstraints(week_start_monday=monday)
    for i in range(7):
        d = monday + timedelta(days=i)
        dc = DayConstraints(date=d)
        if d.weekday() == 5:   # Saturday
            dc.is_open_saturday = open_saturday
        wc.days.append(dc)
    return wc


VEGETARIAN_RECIPE = {
    "id": "spiced-rice",
    "name": "Spiced Rice with Crispy Chickpeas",
    "tags": ["vegetarian", "onRotation"],
    "servings": "4",
    "ingredients": [{"name": "rice", "quantity": "1", "unit": "cup"}],
    "cook_time": "30",
    "prep_time": "10",
}

MEAT_RECIPE = {
    "id": "hamburger-steaks",
    "name": "Hamburger Steaks",
    "tags": ["onRotation"],
    "servings": "4",
    "ingredients": [{"name": "ground beef", "quantity": "1", "unit": "lb"}],
    "cook_time": "30",
    "prep_time": "10",
}

EXPERIMENT_RECIPE = {
    "id": "merguez",
    "name": "Homemade Merguez",
    "tags": ["experiment"],
    "servings": "4",
    "ingredients": [{"name": "ground lamb", "quantity": "1", "unit": "lb"}],
    "cook_time": "45",
    "prep_time": "20",
}

PIZZA_RECIPE = {
    "id": "homemade-pizza",
    "name": "Homemade pizza",
    "tags": ["pizza"],
    "servings": "4",
    "ingredients": [],
    "cook_time": "60",
    "prep_time": "30",
}


class TestAgenticPlannerTools(unittest.TestCase):
    """Test each tool function in isolation without running the agent loop."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = StateStore(self.tmp.name)
        self.cfg = {
            "recipe_dir": "recipes_yaml",
            "lunch_file":  "lunches.yaml",
            "db_path":     self.tmp.name,
        }
        self.constraints = make_constraints()

    def tearDown(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _make_planner_with_recipes(self, recipes):
        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)
        planner._constraints = self.constraints
        planner._assigned = {}
        planner._lunch = None
        planner._rationale = ""
        planner._finalized = False
        planner.recipes = {r["id"]: r for r in recipes}
        planner.lunches = []
        return planner

    # --- assign_meal constraint validation ---

    def test_assign_meat_to_wednesday_returns_error(self):
        planner = self._make_planner_with_recipes([MEAT_RECIPE])
        result = planner._tool_assign_meal("Wednesday", "hamburger-steaks")
        self.assertFalse(result["ok"])
        self.assertIn("meatless", result["error"].lower())

    def test_assign_meat_to_friday_returns_error(self):
        planner = self._make_planner_with_recipes([MEAT_RECIPE])
        result = planner._tool_assign_meal("Friday", "hamburger-steaks")
        self.assertFalse(result["ok"])
        self.assertIn("meatless", result["error"].lower())

    def test_assign_vegetarian_to_wednesday_succeeds(self):
        planner = self._make_planner_with_recipes([VEGETARIAN_RECIPE])
        result = planner._tool_assign_meal("Wednesday", "spiced-rice")
        self.assertTrue(result["ok"])
        self.assertEqual(result["day"], "Wednesday")

    def test_assign_experiment_to_non_saturday_returns_error(self):
        planner = self._make_planner_with_recipes([EXPERIMENT_RECIPE])
        result = planner._tool_assign_meal("Monday", "merguez")
        self.assertFalse(result["ok"])
        self.assertIn("experiment", result["error"].lower())

    def test_assign_experiment_to_saturday_succeeds(self):
        planner = self._make_planner_with_recipes([EXPERIMENT_RECIPE])
        result = planner._tool_assign_meal("Saturday", "merguez")
        self.assertTrue(result["ok"])

    def test_assign_pizza_to_non_friday_returns_error(self):
        planner = self._make_planner_with_recipes([PIZZA_RECIPE])
        result = planner._tool_assign_meal("Monday", "homemade-pizza")
        self.assertFalse(result["ok"])
        self.assertIn("friday", result["error"].lower())

    def test_assign_same_day_twice_returns_error(self):
        planner = self._make_planner_with_recipes([MEAT_RECIPE, VEGETARIAN_RECIPE])
        planner._tool_assign_meal("Monday", "hamburger-steaks")
        result = planner._tool_assign_meal("Monday", "spiced-rice")
        self.assertFalse(result["ok"])
        self.assertIn("already assigned", result["error"].lower())

    def test_assign_unknown_recipe_returns_error(self):
        planner = self._make_planner_with_recipes([])
        result = planner._tool_assign_meal("Monday", "no-such-recipe")
        self.assertFalse(result["ok"])

    # --- assign_no_cook ---

    def test_assign_no_cook_succeeds(self):
        planner = self._make_planner_with_recipes([])
        result = planner._tool_assign_no_cook("Tuesday", "KRM event after 3pm")
        self.assertTrue(result["ok"])
        self.assertEqual(result["label"], "no cook")
        self.assertIn("Tuesday", planner._assigned)

    # --- assign_note ---

    def test_assign_note_out(self):
        planner = self._make_planner_with_recipes([])
        result = planner._tool_assign_note("Tuesday", "Dinner at Sarah's", "out")
        self.assertTrue(result["ok"])
        self.assertEqual(result["note_type"], "out")
        slot = planner._assigned["Tuesday"]
        self.assertEqual(slot.note_type, "out")
        self.assertEqual(slot.label, "Dinner at Sarah's")

    def test_assign_note_cook(self):
        planner = self._make_planner_with_recipes([])
        result = planner._tool_assign_note("Sunday", "Waffles for dinner", "cook")
        self.assertTrue(result["ok"])
        slot = planner._assigned["Sunday"]
        self.assertEqual(slot.note_type, "cook")

    # --- finalize_plan ---

    def test_finalize_plan_sets_flag_and_rationale(self):
        planner = self._make_planner_with_recipes([])
        self.assertFalse(planner._finalized)
        result = planner._tool_finalize_plan("Busy week — kept it simple.")
        self.assertTrue(result["ok"])
        self.assertTrue(planner._finalized)
        self.assertEqual(planner._rationale, "Busy week — kept it simple.")


if __name__ == "__main__":
    unittest.main()
