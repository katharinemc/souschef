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
from planner import WeekPlan
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


class TestAgenticPlannerLoop(unittest.TestCase):
    """Test the agent loop using a mocked Anthropic client."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = StateStore(self.tmp.name)
        self.cfg = {
            "recipe_dir": "recipes_yaml",
            "lunch_file":  "lunches.yaml",
            "db_path":     self.tmp.name,
            "model":       "claude-opus-4-5",
        }
        self.constraints = make_constraints()

    def tearDown(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _make_tool_use_response(self, tool_name, tool_id, tool_input):
        """Helper: build a mock API response with a single tool_use block."""
        block = MagicMock()
        block.type = "tool_use"
        block.name = tool_name
        block.id = tool_id
        block.input = tool_input

        response = MagicMock()
        response.stop_reason = "tool_use"
        response.content = [block]
        return response

    def _make_end_turn_response(self):
        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = []
        return response

    @patch('agentic_planner.anthropic')
    @patch('agentic_planner.load_recipes', return_value={})
    @patch('agentic_planner.load_lunches', return_value=[])
    def test_finalize_plan_stops_loop(self, mock_lunches, mock_recipes, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        call_count = [0]
        def side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return self._make_tool_use_response(
                    "finalize_plan", "call_1",
                    {"rationale": "Simple week — no complex decisions."}
                )
            return self._make_end_turn_response()

        mock_client.messages.create.side_effect = side_effect

        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)
        plan = planner.plan_week(self.constraints)

        self.assertEqual(plan.rationale, "Simple week — no complex decisions.")
        # Only 1 API call needed (first call returns finalize_plan)
        self.assertEqual(mock_client.messages.create.call_count, 1)

    @patch('agentic_planner.anthropic')
    @patch('agentic_planner.load_recipes', return_value={})
    @patch('agentic_planner.load_lunches', return_value=[])
    def test_fallback_on_api_error(self, mock_lunches, mock_recipes, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.side_effect = RuntimeError("API connection refused")

        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)

        with patch('planner.Planner') as mock_planner_class:
            mock_fallback = MagicMock()
            mock_fallback.plan_week.return_value = WeekPlan(
                week_start_monday=MONDAY,
                week_key="2026-03-23",
            )
            mock_planner_class.return_value = mock_fallback

            plan = planner.plan_week(self.constraints)

        mock_fallback.plan_week.assert_called_once()
        self.assertTrue(any("fallback" in w.lower() for w in plan.warnings))

    @patch('agentic_planner.anthropic')
    @patch('agentic_planner.load_recipes', return_value={})
    @patch('agentic_planner.load_lunches', return_value=[])
    def test_30_call_limit_triggers_fallback(self, mock_lunches, mock_recipes, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        # Always return a tool_use response (never finalize_plan → hits limit)
        def side_effect(**kwargs):
            return self._make_tool_use_response(
                "get_calendar_constraints", "call_x", {}
            )
        mock_client.messages.create.side_effect = side_effect

        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)

        with patch('planner.Planner') as mock_planner_class:
            mock_fallback = MagicMock()
            mock_fallback.plan_week.return_value = WeekPlan(
                week_start_monday=MONDAY,
                week_key="2026-03-23",
            )
            mock_planner_class.return_value = mock_fallback
            plan = planner.plan_week(self.constraints)

        mock_fallback.plan_week.assert_called_once()

    @patch('agentic_planner.anthropic')
    @patch('agentic_planner.load_recipes')
    @patch('agentic_planner.load_lunches', return_value=[])
    def test_full_plan_has_seven_days(self, mock_lunches, mock_recipes, mock_anthropic):
        """Agent assigns all 7 days then finalizes."""
        RECIPES = {
            "hamburger-steaks": MEAT_RECIPE,
            "spiced-rice":      VEGETARIAN_RECIPE,
        }
        mock_recipes.return_value = RECIPES
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        # Simulate agent: assign Mon, Tue, Thu as recipes, rest no-cook, finalize
        call_seq = [0]
        def make_seq(**kwargs):
            call_seq[0] += 1
            n = call_seq[0]
            if n == 1:
                return self._make_tool_use_response("assign_meal", f"c{n}",
                    {"day": "Monday", "recipe_id": "hamburger-steaks"})
            elif n == 2:
                return self._make_tool_use_response("assign_no_cook", f"c{n}",
                    {"day": "Tuesday"})
            elif n == 3:
                return self._make_tool_use_response("assign_meal", f"c{n}",
                    {"day": "Wednesday", "recipe_id": "spiced-rice"})
            elif n == 4:
                return self._make_tool_use_response("assign_meal", f"c{n}",
                    {"day": "Thursday", "recipe_id": "hamburger-steaks"})
            elif n == 5:
                return self._make_tool_use_response("assign_no_cook", f"c{n}",
                    {"day": "Friday"})
            elif n == 6:
                return self._make_tool_use_response("assign_no_cook", f"c{n}",
                    {"day": "Saturday"})
            elif n == 7:
                return self._make_tool_use_response("assign_no_cook", f"c{n}",
                    {"day": "Sunday"})
            elif n == 8:
                return self._make_tool_use_response("finalize_plan", f"c{n}",
                    {"rationale": "Test rationale."})
            return self._make_end_turn_response()

        mock_client.messages.create.side_effect = make_seq

        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)
        plan = planner.plan_week(self.constraints)

        # All 7 days should be present (unassigned filled with leftovers)
        self.assertEqual(len(plan.dinners), 7)
        day_names = {s.weekday_name for s in plan.dinners}
        expected = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
        self.assertEqual(day_names, expected)
        self.assertEqual(plan.rationale, "Test rationale.")


if __name__ == "__main__":
    unittest.main()
