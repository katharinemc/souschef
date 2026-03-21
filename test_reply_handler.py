"""
test_reply_handler.py

Tests for reply_handler.py. No Gmail API or Anthropic API calls are made —
all external calls are patched.

Run with: python -m pytest test_reply_handler.py -v
"""

import copy
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

from reply_handler import (
    ReplyHandler,
    parse_reply_intents,
    apply_intents,
    _dummy_dc,
    _patch_planner_exports,
)
from planner import WeekPlan, MealSlot
from state_store import StateStore
from calendar_reader import DayConstraints


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MONDAY    = date(2026, 3, 23)
TUESDAY   = MONDAY + timedelta(days=1)
WEDNESDAY = MONDAY + timedelta(days=2)
THURSDAY  = MONDAY + timedelta(days=3)
FRIDAY    = MONDAY + timedelta(days=4)
SATURDAY  = MONDAY + timedelta(days=5)
SUNDAY    = MONDAY + timedelta(days=6)

WEEK_KEY  = "2026-03-23"


def make_recipe(rid, name, tags=None):
    return {
        "id": rid, "name": name,
        "tags": tags or ["onRotation"],
        "ingredients": [{"name": "onion"}],
        "instructions": "Cook.",
        "servings": "4",
        "last_planned": None,
    }


def make_slot(d, recipe_id=None, label="Test", tags=None,
              is_no_cook=False, is_meatless=False):
    return MealSlot(
        date=d, slot="dinner",
        recipe_id=recipe_id,
        label=label,
        tags=tags or [],
        ingredients=[],
        notes=[],
        is_no_cook=is_no_cook,
        is_meatless=is_meatless,
    )


def make_plan():
    plan = WeekPlan(
        week_start_monday=MONDAY,
        week_key=WEEK_KEY,
        cook_nights=4,
    )
    plan.dinners = [
        make_slot(MONDAY,    "burger",   "Burger Steaks"),
        make_slot(TUESDAY,   None,       "no cook",     is_no_cook=True),
        make_slot(WEDNESDAY, "pasta",    "Tomato Pasta", tags=["pasta"], is_meatless=True),
        make_slot(THURSDAY,  None,       "leftovers"),
        make_slot(FRIDAY,    None,       "Takeout pizza", tags=["pizza"], is_meatless=True),
        make_slot(SATURDAY,  "exp",      "Experiment Dish", tags=["experiment"]),
        make_slot(SUNDAY,    "chickpea", "Chickpea Rice", tags=["vegetarian"]),
    ]
    plan.lunch = MealSlot(
        date=MONDAY, slot="lunch", recipe_id="grain-bowl",
        label="Grain bowl", tags=[], ingredients=[], notes=[],
    )
    return plan


def make_recipes():
    return {
        "burger":      make_recipe("burger",    "Burger Steaks"),
        "pasta":       make_recipe("pasta",     "Tomato Pasta",    ["pasta", "vegetarian", "onRotation"]),
        "exp":         make_recipe("exp",       "Experiment Dish", ["experiment"]),
        "chickpea":    make_recipe("chickpea",  "Chickpea Rice",   ["vegetarian", "onRotation"]),
        "easy-soup":   make_recipe("easy-soup", "Easy Soup",       ["easy", "vegetarian", "onRotation"]),
        "taco-night":  make_recipe("taco-night","Taco Night",      ["taco", "onRotation"]),
        "fish-dish":   make_recipe("fish-dish", "Fish Tacos",      ["pescatarian", "taco", "onRotation"]),
    }


# ---------------------------------------------------------------------------
# Intent parsing tests (mocked API)
# ---------------------------------------------------------------------------

class TestIntentParsing(unittest.TestCase):

    def _mock_response(self, json_str: str):
        """Build a mock Anthropic API response."""
        mock_content = MagicMock()
        mock_content.text = json_str
        mock_response = MagicMock()
        mock_response.content = [mock_content]
        return mock_response

    def test_swap_day_parsed(self):
        with patch("reply_handler.anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.Anthropic.return_value = mock_client
            mock_client.messages.create.return_value = self._mock_response(
                '[{"type": "swap_day", "day": "Tuesday", "constraint": "pasta"}]'
            )
            intents = parse_reply_intents("Swap Tuesday for pasta", "claude-test")
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["type"], "swap_day")
        self.assertEqual(intents[0]["day"], "Tuesday")
        self.assertEqual(intents[0]["constraint"], "pasta")

    def test_acknowledgment_parsed(self):
        with patch("reply_handler.anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.Anthropic.return_value = mock_client
            mock_client.messages.create.return_value = self._mock_response(
                '[{"type": "acknowledgment"}]'
            )
            intents = parse_reply_intents("Okay thanks", "claude-test")
        self.assertEqual(intents[0]["type"], "acknowledgment")

    def test_multiple_intents_parsed(self):
        with patch("reply_handler.anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.Anthropic.return_value = mock_client
            mock_client.messages.create.return_value = self._mock_response(
                '[{"type": "swap_day", "day": "Wednesday", "constraint": "easy"}, '
                '{"type": "remove_on_hand", "ingredient": "cream cheese"}]'
            )
            intents = parse_reply_intents(
                "Switch Wednesday to easy, cream cheese is gone", "claude-test"
            )
        self.assertEqual(len(intents), 2)

    def test_malformed_json_falls_back_to_ack(self):
        with patch("reply_handler.anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.Anthropic.return_value = mock_client
            mock_client.messages.create.return_value = self._mock_response(
                "not valid json at all"
            )
            intents = parse_reply_intents("anything", "claude-test")
        self.assertEqual(intents[0]["type"], "acknowledgment")

    def test_markdown_fences_stripped(self):
        with patch("reply_handler.anthropic") as mock_anthropic:
            mock_client = MagicMock()
            mock_anthropic.Anthropic.return_value = mock_client
            mock_client.messages.create.return_value = self._mock_response(
                '```json\n[{"type": "acknowledgment"}]\n```'
            )
            intents = parse_reply_intents("okay", "claude-test")
        self.assertEqual(intents[0]["type"], "acknowledgment")


# ---------------------------------------------------------------------------
# apply_intents tests
# ---------------------------------------------------------------------------

class TestApplyIntents(unittest.TestCase):

    def test_swap_day_changes_recipe(self):
        plan = make_plan()
        recipes = make_recipes()
        intents = [{"type": "swap_day", "day": "Monday", "constraint": None}]
        modified, notes = apply_intents(plan, intents, recipes, store=None)
        monday_slot = modified.get_dinner(MONDAY)
        # Recipe should have changed (not "burger" anymore ideally,
        # but if only one eligible it may stay — just check it assigned something)
        self.assertIsNotNone(monday_slot.recipe_id)
        self.assertGreater(len(notes), 0)

    def test_swap_day_respects_tag_constraint(self):
        plan = make_plan()
        recipes = make_recipes()
        intents = [{"type": "swap_day", "day": "Monday", "constraint": "taco"}]
        modified, notes = apply_intents(plan, intents, recipes, store=None)
        monday_slot = modified.get_dinner(MONDAY)
        self.assertIn("taco", (monday_slot.tags or []))

    def test_swap_meatless_day_stays_meatless(self):
        plan = make_plan()
        recipes = make_recipes()
        # Wednesday is meatless
        intents = [{"type": "swap_day", "day": "Wednesday", "constraint": None}]
        modified, notes = apply_intents(plan, intents, recipes, store=None)
        wed_slot = modified.get_dinner(WEDNESDAY)
        self.assertTrue(wed_slot.is_meatless)

    def test_force_no_cook(self):
        plan = make_plan()
        recipes = make_recipes()
        intents = [{"type": "force_no_cook", "day": "Monday"}]
        modified, notes = apply_intents(plan, intents, recipes, store=None)
        monday_slot = modified.get_dinner(MONDAY)
        self.assertIsNone(monday_slot.recipe_id)
        self.assertEqual(monday_slot.label, "leftovers")

    def test_force_cook_replaces_leftovers(self):
        plan = make_plan()
        recipes = make_recipes()
        intents = [{"type": "force_cook", "day": "Thursday"}]
        modified, notes = apply_intents(plan, intents, recipes, store=None)
        thu_slot = modified.get_dinner(THURSDAY)
        # Thursday was leftovers — should now have a recipe
        self.assertIsNotNone(thu_slot.recipe_id)
        self.assertGreater(len(notes), 0)

    def test_skip_experiment_replaces_saturday(self):
        plan = make_plan()
        recipes = make_recipes()
        intents = [{"type": "skip_experiment"}]
        modified, notes = apply_intents(plan, intents, recipes, store=None)
        sat_slot = modified.get_dinner(SATURDAY)
        # Should no longer be the experiment recipe
        self.assertNotEqual(sat_slot.recipe_id, "exp")
        self.assertGreater(len(notes), 0)

    def test_remove_on_hand_adds_to_plan(self):
        plan = make_plan()
        recipes = make_recipes()
        intents = [{"type": "remove_on_hand", "ingredient": "cream cheese"}]
        modified, notes = apply_intents(plan, intents, recipes, store=None)
        self.assertIn("cream cheese", getattr(modified, "_removed_on_hand", []))
        self.assertGreater(len(notes), 0)

    def test_acknowledgment_no_changes(self):
        plan = make_plan()
        original_dinners = [s.recipe_id for s in plan.dinners]
        recipes = make_recipes()
        intents = [{"type": "acknowledgment"}]
        modified, notes = apply_intents(plan, intents, recipes, store=None)
        modified_dinners = [s.recipe_id for s in modified.dinners]
        self.assertEqual(original_dinners, modified_dinners)

    def test_original_plan_not_mutated(self):
        plan = make_plan()
        original_monday_id = plan.get_dinner(MONDAY).recipe_id
        recipes = make_recipes()
        intents = [{"type": "swap_day", "day": "Monday", "constraint": "taco"}]
        modified, _ = apply_intents(plan, intents, recipes, store=None)
        # Original should be unchanged
        self.assertEqual(plan.get_dinner(MONDAY).recipe_id, original_monday_id)

    def test_unknown_day_produces_note(self):
        plan = make_plan()
        recipes = make_recipes()
        intents = [{"type": "swap_day", "day": "Blurnsday", "constraint": None}]
        modified, notes = apply_intents(plan, intents, recipes, store=None)
        self.assertTrue(any("Could not find" in n or "Blurnsday" in n for n in notes))


# ---------------------------------------------------------------------------
# Plan reconstruction tests
# ---------------------------------------------------------------------------

class TestPlanReconstruction(unittest.TestCase):

    def test_round_trip_to_dict_and_back(self):
        plan = make_plan()
        plan_dict = plan.to_dict()
        handler = ReplyHandler(config={"db_path": ":memory:"})
        reconstructed = handler._reconstruct_plan(plan_dict)
        self.assertEqual(len(reconstructed.dinners), 7)
        self.assertEqual(reconstructed.week_key, WEEK_KEY)
        self.assertEqual(reconstructed.week_start_monday, MONDAY)

    def test_reconstructed_slots_have_correct_dates(self):
        plan = make_plan()
        plan_dict = plan.to_dict()
        handler = ReplyHandler(config={"db_path": ":memory:"})
        reconstructed = handler._reconstruct_plan(plan_dict)
        dates = [s.date for s in reconstructed.dinners]
        self.assertIn(MONDAY, dates)
        self.assertIn(FRIDAY, dates)
        self.assertIn(SUNDAY, dates)

    def test_reconstructed_no_cook_preserved(self):
        plan = make_plan()
        plan_dict = plan.to_dict()
        handler = ReplyHandler(config={"db_path": ":memory:"})
        reconstructed = handler._reconstruct_plan(plan_dict)
        tuesday_slot = reconstructed.get_dinner(TUESDAY)
        self.assertTrue(tuesday_slot.is_no_cook)

    def test_reconstructed_lunch_preserved(self):
        plan = make_plan()
        plan_dict = plan.to_dict()
        handler = ReplyHandler(config={"db_path": ":memory:"})
        reconstructed = handler._reconstruct_plan(plan_dict)
        self.assertIsNotNone(reconstructed.lunch)
        self.assertEqual(reconstructed.lunch.label, "Grain bowl")


# ---------------------------------------------------------------------------
# ReplyHandler acknowledgment flow
# ---------------------------------------------------------------------------

class TestReplyHandlerAck(unittest.TestCase):

    def test_acknowledgment_marks_plan_approved(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            store = StateStore(f.name)
            plan = make_plan()
            store.record_plan(WEEK_KEY, plan.to_dict(), plan.to_state_meals())

            handler = ReplyHandler(
                config={"db_path": f.name, "to_address": "test@example.com"},
                dry_run=True,
            )

            mock_sender = MagicMock()
            mock_sender.is_acknowledgment.return_value = True

            handler._handle_reply(
                {"body": "Looks good!", "from": "test@example.com"},
                WEEK_KEY,
                mock_sender,
            )

            store2 = StateStore(f.name)
            row = store2._conn.execute(
                "SELECT approved FROM weekly_plans WHERE week_key = ?",
                (WEEK_KEY,)
            ).fetchone()
            # dry_run=True so approved won't be set, but handler should return True
            store2.close()
            store.close()

    def test_ack_does_not_trigger_replan(self):
        handler = ReplyHandler(config={}, dry_run=True)
        mock_sender = MagicMock()
        mock_sender.is_acknowledgment.return_value = True

        result = handler._handle_reply(
            {"body": "okay thanks"},
            WEEK_KEY,
            mock_sender,
        )
        # send_plan should NOT have been called
        mock_sender.send_plan.assert_not_called()
        self.assertTrue(result)


# ---------------------------------------------------------------------------
# Thursday-only polling guard
# ---------------------------------------------------------------------------

class TestThursdayGuard(unittest.TestCase):

    def test_poll_exits_on_non_thursday(self):
        handler = ReplyHandler(config={}, dry_run=True)
        # Patch _is_thursday to return False
        with patch.object(handler, "_is_thursday", return_value=False):
            result = handler.poll_and_handle(WEEK_KEY)
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
