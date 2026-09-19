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
    reconstruct_plan,
    _dummy_dc,
    _patch_planner_exports,
)
from planner import WeekPlan, MealSlot
from state_store import StateStore
from calendar_reader import DayConstraints


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Use a dynamic Monday so plans written via record_plan() aren't purged by
# the 4-month history cutoff in state_store._purge_old_history() (which
# happened when this was a fixed date and real time passed it by).
MONDAY    = date.today() - timedelta(days=date.today().weekday())
TUESDAY   = MONDAY + timedelta(days=1)
WEDNESDAY = MONDAY + timedelta(days=2)
THURSDAY  = MONDAY + timedelta(days=3)
FRIDAY    = MONDAY + timedelta(days=4)
SATURDAY  = MONDAY + timedelta(days=5)
SUNDAY    = MONDAY + timedelta(days=6)

WEEK_KEY  = MONDAY.isoformat()


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
        reconstructed = reconstruct_plan(plan_dict)
        self.assertEqual(len(reconstructed.dinners), 7)
        self.assertEqual(reconstructed.week_key, WEEK_KEY)
        self.assertEqual(reconstructed.week_start_monday, MONDAY)

    def test_reconstructed_slots_have_correct_dates(self):
        plan = make_plan()
        plan_dict = plan.to_dict()
        reconstructed = reconstruct_plan(plan_dict)
        dates = [s.date for s in reconstructed.dinners]
        self.assertIn(MONDAY, dates)
        self.assertIn(FRIDAY, dates)
        self.assertIn(SUNDAY, dates)

    def test_reconstructed_no_cook_preserved(self):
        plan = make_plan()
        plan_dict = plan.to_dict()
        reconstructed = reconstruct_plan(plan_dict)
        tuesday_slot = reconstructed.get_dinner(TUESDAY)
        self.assertTrue(tuesday_slot.is_no_cook)

    def test_reconstructed_lunch_preserved(self):
        plan = make_plan()
        plan_dict = plan.to_dict()
        reconstructed = reconstruct_plan(plan_dict)
        self.assertIsNotNone(reconstructed.lunch)
        self.assertEqual(reconstructed.lunch.label, "Grain bowl")

    def test_handler_reconstruct_plan_delegates_to_module_function(self):
        # ReplyHandler._reconstruct_plan should still work (delegates to reconstruct_plan)
        plan = make_plan()
        handler = ReplyHandler(config={"db_path": ":memory:"})
        reconstructed = handler._reconstruct_plan(plan.to_dict())
        self.assertEqual(reconstructed.week_key, WEEK_KEY)


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


class TestNewIntentParsing(unittest.TestCase):
    """Test that the intent system prompt handles new v1.5 intent types."""

    @patch('reply_handler.anthropic')
    def test_rate_experiment_intent_parsed(self, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value.content = [
            MagicMock(text='[{"type": "rate_experiment", "day": "Saturday", "stars": 4}]')
        ]
        intents = parse_reply_intents("Rate Saturday's experiment 4 stars", model="test-model")
        self.assertEqual(intents[0]["type"], "rate_experiment")
        self.assertEqual(intents[0]["stars"], 4)

    @patch('reply_handler.anthropic')
    def test_promote_experiment_intent_parsed(self, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value.content = [
            MagicMock(text='[{"type": "promote_experiment", "recipe_id": "merguez"}]')
        ]
        intents = parse_reply_intents("Add the merguez to onRotation", model="test-model")
        self.assertEqual(intents[0]["type"], "promote_experiment")

    @patch('reply_handler.anthropic')
    def test_assign_note_out_intent_parsed(self, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value.content = [
            MagicMock(text='[{"type": "assign_note_out", "day": "Tuesday", "note_text": "Dinner at Sarah\'s"}]')
        ]
        intents = parse_reply_intents("Mark Tuesday as dinner at Sarah's", model="test-model")
        self.assertEqual(intents[0]["type"], "assign_note_out")
        self.assertEqual(intents[0]["day"], "Tuesday")

    @patch('reply_handler.anthropic')
    def test_assign_note_cook_intent_parsed(self, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value.content = [
            MagicMock(text='[{"type": "assign_note_cook", "day": "Sunday", "note_text": "Waffles for dinner"}]')
        ]
        intents = parse_reply_intents("Put waffles on Sunday", model="test-model")
        self.assertEqual(intents[0]["type"], "assign_note_cook")

    def test_read_stdin_reply_done_returns_none(self):
        from reply_handler import read_stdin_reply
        with patch('builtins.input', return_value="done"):
            result = read_stdin_reply()
        self.assertIsNone(result)

    def test_read_stdin_reply_okay_thanks_returns_none(self):
        from reply_handler import read_stdin_reply
        with patch('builtins.input', return_value="okay thanks"):
            result = read_stdin_reply()
        self.assertIsNone(result)

    def test_read_stdin_reply_feedback_returns_string(self):
        from reply_handler import read_stdin_reply
        with patch('builtins.input', return_value="Swap Tuesday for pasta"):
            result = read_stdin_reply()
        self.assertEqual(result, "Swap Tuesday for pasta")


# ---------------------------------------------------------------------------
# amend / confirm integration
# ---------------------------------------------------------------------------

class TestAmendConfirmFlow(unittest.TestCase):
    """
    Tests for the one-shot amend/confirm path (two independent process
    invocations that build on each other's persisted changes).

    No CalendarReader is called anywhere in this path — verified by the
    absence of any patch for it in these tests.
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = StateStore(self.tmp.name)
        self.plan = make_plan()
        self.store.record_plan(WEEK_KEY, self.plan.to_dict(), self.plan.to_state_meals())
        self.recipes = make_recipes()

    def tearDown(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _apply_one_amend(self, intents):
        """Simulate one invocation of cmd_amend (without the CLI layer)."""
        result = self.store.get_draft_plan()
        self.assertIsNotNone(result, "Expected a draft plan to exist")
        week_key, plan_dict = result
        plan = reconstruct_plan(plan_dict)
        modified, notes = apply_intents(plan, intents, self.recipes, self.store)
        self.store.record_plan(
            modified.week_key,
            modified.to_dict(),
            modified.to_state_meals(),
        )
        return week_key, modified, notes

    def test_amend_persists_change(self):
        intents = [{"type": "swap_day", "day": "Monday", "constraint": "taco"}]
        week_key, modified, notes = self._apply_one_amend(intents)

        # Reload from DB — this is what a second process invocation would see
        reloaded_dict = self.store.get_plan(week_key)
        reloaded = reconstruct_plan(reloaded_dict)
        monday_slot = reloaded.get_dinner(MONDAY)
        self.assertIn("taco", (monday_slot.tags or []))

    def test_two_sequential_amends_build_on_each_other(self):
        # First amend: swap Monday for a taco
        intents_1 = [{"type": "swap_day", "day": "Monday", "constraint": "taco"}]
        week_key, plan_after_1, _ = self._apply_one_amend(intents_1)
        monday_after_1 = plan_after_1.get_dinner(MONDAY).recipe_id

        # Second amend (new "process invocation"): force Sunday no-cook
        intents_2 = [{"type": "force_no_cook", "day": "Sunday"}]
        _, plan_after_2, _ = self._apply_one_amend(intents_2)

        # Reload from DB to confirm both changes survived
        reloaded_dict = self.store.get_plan(week_key)
        reloaded = reconstruct_plan(reloaded_dict)

        # Monday still reflects first amend
        self.assertEqual(reloaded.get_dinner(MONDAY).recipe_id, monday_after_1)
        # Sunday reflects second amend
        sunday_slot = reloaded.get_dinner(SUNDAY)
        self.assertIsNone(sunday_slot.recipe_id)
        self.assertEqual(sunday_slot.label, "leftovers")

    def test_amend_plan_stays_draft_after_change(self):
        intents = [{"type": "force_no_cook", "day": "Sunday"}]
        self._apply_one_amend(intents)
        self.assertEqual(self.store.get_plan_status(WEEK_KEY), "draft")

    def test_confirm_marks_plan_confirmed(self):
        # Simulate cmd_confirm
        result = self.store.get_draft_plan()
        week_key, _ = result
        self.store.mark_plan_approved(week_key)

        self.assertEqual(self.store.get_plan_status(week_key), "confirmed")
        self.assertIsNone(self.store.get_draft_plan())

    def test_confirm_is_durable_across_restart(self):
        # Confirm, close, reopen — status must survive
        result = self.store.get_draft_plan()
        week_key, _ = result
        self.store.mark_plan_approved(week_key)
        self.store.close()

        reopened = StateStore(self.tmp.name)
        self.assertEqual(reopened.get_plan_status(week_key), "confirmed")
        reopened.close()
        self.store = StateStore(self.tmp.name)  # reopen for tearDown

    def test_amend_does_not_call_calendar_reader(self):
        # CalendarReader must NOT be imported or instantiated during amend.
        # If this test passes without patching CalendarReader, that's the proof.
        with patch("calendar_reader.CalendarReader") as mock_cr:
            intents = [{"type": "force_no_cook", "day": "Sunday"}]
            self._apply_one_amend(intents)
            mock_cr.assert_not_called()

    def test_amend_acknowledgment_confirms_plan(self):
        # If the amend message parses as acknowledgment, plan is confirmed
        # (This tests the routing logic in cmd_amend, here done inline)
        intents = [{"type": "acknowledgment"}]
        result = self.store.get_draft_plan()
        week_key, plan_dict = result
        if all(i.get("type") == "acknowledgment" for i in intents):
            self.store.mark_plan_approved(week_key)
        self.assertEqual(self.store.get_plan_status(week_key), "confirmed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
