"""
test_agentic_planner.py

Tests for AgenticPlanner — single-call architecture (ADR-001).

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


# ---------------------------------------------------------------------------
# Helper: build a minimal submit_plan input for a full week
# ---------------------------------------------------------------------------

def _full_plan_input(constraints: WeekConstraints, recipes: dict) -> dict:
    """Build a valid submit_plan input assigning every day."""
    monday = constraints.week_start_monday
    # Find a vegetarian and a meat recipe for valid assignment
    veg_id = next((r["id"] for r in recipes.values() if "vegetarian" in (r.get("tags") or [])), None)
    meat_id = next((r["id"] for r in recipes.values() if "vegetarian" not in (r.get("tags") or [])), None)

    dinners = []
    for i in range(7):
        d = monday + timedelta(days=i)
        weekday = d.weekday()
        dc = next(dc for dc in constraints.days if dc.date == d)

        if dc.is_no_cook:
            dinners.append({"date": d.isoformat(), "type": "no_cook"})
        elif weekday in (2, 4) and veg_id:  # Wed/Fri: meatless
            dinners.append({"date": d.isoformat(), "type": "recipe", "recipe_id": veg_id})
        elif meat_id:
            dinners.append({"date": d.isoformat(), "type": "recipe", "recipe_id": meat_id})
        else:
            dinners.append({"date": d.isoformat(), "type": "no_cook"})

    return {"rationale": "Test rationale.", "dinners": dinners}


# ---------------------------------------------------------------------------
# Tests: _parse_dinner_slot (constraint validation)
# ---------------------------------------------------------------------------

class TestParseDinnerSlot(unittest.TestCase):
    """Test constraint enforcement in _parse_dinner_slot."""

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

    def _make_planner(self, recipes):
        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)
        planner.recipes = {r["id"]: r for r in recipes}
        planner.lunches = []
        return planner

    def _dc_for_weekday(self, weekday: int) -> DayConstraints:
        """Return the DayConstraints for the given weekday (0=Mon)."""
        monday = self.constraints.week_start_monday
        d = monday + timedelta(days=weekday)
        return next(dc for dc in self.constraints.days if dc.date == d)

    # --- meatless constraint ---

    def test_meat_on_wednesday_overrides_to_no_cook(self):
        planner = self._make_planner([MEAT_RECIPE])
        dc = self._dc_for_weekday(2)  # Wednesday
        slot = planner._parse_dinner_slot(dc, "recipe", {"recipe_id": "hamburger-steaks"})
        self.assertTrue(slot.is_no_cook)

    def test_meat_on_friday_overrides_to_no_cook(self):
        planner = self._make_planner([MEAT_RECIPE])
        dc = self._dc_for_weekday(4)  # Friday
        slot = planner._parse_dinner_slot(dc, "recipe", {"recipe_id": "hamburger-steaks"})
        self.assertTrue(slot.is_no_cook)

    def test_vegetarian_on_wednesday_succeeds(self):
        planner = self._make_planner([VEGETARIAN_RECIPE])
        dc = self._dc_for_weekday(2)  # Wednesday
        slot = planner._parse_dinner_slot(dc, "recipe", {"recipe_id": "spiced-rice"})
        self.assertFalse(slot.is_no_cook)
        self.assertEqual(slot.recipe_id, "spiced-rice")

    def test_meat_on_fasting_day_overrides_to_no_cook(self):
        planner = self._make_planner([MEAT_RECIPE])
        dc = self._dc_for_weekday(0)  # Monday
        dc.is_fasting = True
        slot = planner._parse_dinner_slot(dc, "recipe", {"recipe_id": "hamburger-steaks"})
        self.assertTrue(slot.is_no_cook)

    # --- unknown recipe ---

    def test_unknown_recipe_id_falls_back_to_no_cook(self):
        planner = self._make_planner([])
        dc = self._dc_for_weekday(0)  # Monday
        slot = planner._parse_dinner_slot(dc, "recipe", {"recipe_id": "no-such-recipe"})
        self.assertTrue(slot.is_no_cook)

    # --- note slots ---

    def test_note_out(self):
        planner = self._make_planner([])
        dc = self._dc_for_weekday(1)  # Tuesday
        slot = planner._parse_dinner_slot(dc, "note", {
            "note_text": "Dinner at Sarah's", "note_type": "out"
        })
        self.assertEqual(slot.note_type, "out")
        self.assertEqual(slot.label, "Dinner at Sarah's")

    def test_note_cook(self):
        planner = self._make_planner([])
        dc = self._dc_for_weekday(6)  # Sunday
        slot = planner._parse_dinner_slot(dc, "note", {
            "note_text": "Waffles for dinner", "note_type": "cook"
        })
        self.assertEqual(slot.note_type, "cook")

    # --- no_cook slots ---

    def test_no_cook_includes_qualifying_event_in_notes(self):
        planner = self._make_planner([])
        dc = self._dc_for_weekday(1)  # Tuesday
        dc.qualifying_events = ["KRM Soccer 6:30pm"]
        slot = planner._parse_dinner_slot(dc, "no_cook", {})
        self.assertIn("KRM Soccer 6:30pm", slot.notes)

    def test_no_cook_on_meatless_day_notes_would_have_been_meatless(self):
        planner = self._make_planner([])
        dc = self._dc_for_weekday(2)  # Wednesday
        slot = planner._parse_dinner_slot(dc, "no_cook", {})
        self.assertIn("Would have been meatless", slot.notes)


# ---------------------------------------------------------------------------
# Tests: plan_week (full loop with mocked Anthropic client)
# ---------------------------------------------------------------------------

class TestAgenticPlannerLoop(unittest.TestCase):

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

    def _make_submit_plan_response(self, plan_input: dict):
        block = MagicMock()
        block.type = "tool_use"
        block.name = "submit_plan"
        block.input = plan_input

        usage = MagicMock()
        usage.input_tokens = 1234
        usage.output_tokens = 567

        response = MagicMock()
        response.stop_reason = "tool_use"
        response.content = [block]
        response.usage = usage
        return response

    def _make_recipes(self):
        return {
            "hamburger-steaks": MEAT_RECIPE,
            "spiced-rice":      VEGETARIAN_RECIPE,
        }

    @patch('agentic_planner.anthropic')
    @patch('agentic_planner.load_recipes')
    @patch('agentic_planner.load_lunches', return_value=[])
    def test_single_api_call(self, mock_lunches, mock_recipes, mock_anthropic):
        """Exactly one API call is made."""
        RECIPES = self._make_recipes()
        mock_recipes.return_value = RECIPES
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        plan_input = _full_plan_input(self.constraints, RECIPES)
        mock_client.messages.create.return_value = self._make_submit_plan_response(plan_input)

        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)
        planner.plan_week(self.constraints)

        self.assertEqual(mock_client.messages.create.call_count, 1)

    @patch('agentic_planner.anthropic')
    @patch('agentic_planner.load_recipes')
    @patch('agentic_planner.load_lunches', return_value=[])
    def test_full_plan_has_seven_days(self, mock_lunches, mock_recipes, mock_anthropic):
        RECIPES = self._make_recipes()
        mock_recipes.return_value = RECIPES
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        plan_input = _full_plan_input(self.constraints, RECIPES)
        plan_input["rationale"] = "Test rationale."
        mock_client.messages.create.return_value = self._make_submit_plan_response(plan_input)

        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)
        plan = planner.plan_week(self.constraints)

        self.assertEqual(len(plan.dinners), 7)
        self.assertEqual(plan.rationale, "Test rationale.")

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
    def test_fallback_when_no_tool_call_in_response(self, mock_lunches, mock_recipes, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        usage = MagicMock()
        usage.input_tokens = 100
        usage.output_tokens = 10
        bad_response = MagicMock()
        bad_response.stop_reason = "end_turn"
        bad_response.content = []
        bad_response.usage = usage
        mock_client.messages.create.return_value = bad_response

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
    def test_missing_days_filled_with_leftovers(self, mock_lunches, mock_recipes, mock_anthropic):
        """Days omitted from Claude's response are filled with leftovers."""
        RECIPES = self._make_recipes()
        mock_recipes.return_value = RECIPES
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        monday = self.constraints.week_start_monday
        # Only assign Monday — all other days missing
        partial_plan = {
            "rationale": "Partial.",
            "dinners": [
                {"date": monday.isoformat(), "type": "recipe", "recipe_id": "hamburger-steaks"},
            ],
        }
        mock_client.messages.create.return_value = self._make_submit_plan_response(partial_plan)

        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)
        plan = planner.plan_week(self.constraints)

        self.assertEqual(len(plan.dinners), 7)
        leftovers = [s for s in plan.dinners if s.label == "leftovers"]
        self.assertEqual(len(leftovers), 6)

    @patch('agentic_planner.anthropic')
    @patch('agentic_planner.load_recipes')
    @patch('agentic_planner.load_lunches', return_value=[])
    def test_rationale_preserved(self, mock_lunches, mock_recipes, mock_anthropic):
        RECIPES = self._make_recipes()
        mock_recipes.return_value = RECIPES
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        plan_input = _full_plan_input(self.constraints, RECIPES)
        plan_input["rationale"] = "Busy week with lots of KRM events."
        mock_client.messages.create.return_value = self._make_submit_plan_response(plan_input)

        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)
        plan = planner.plan_week(self.constraints)

        self.assertEqual(plan.rationale, "Busy week with lots of KRM events.")


if __name__ == "__main__":
    unittest.main()
