"""
test_output_formatter.py

Run with: python -m pytest test_output_formatter.py -v
"""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from planner import WeekPlan, MealSlot
from grocery_builder import GroceryList, GroceryItem
from stacker import StackingNote


MONDAY = date(2026, 3, 23)


def make_slot(d, rid=None, label="Test Meal", tags=None,
              is_no_cook=False, is_meatless=False, is_fasting=False,
              notes=None, note_type=None, note_text=None):
    return MealSlot(
        date=d, slot="dinner",
        recipe_id=rid, label=label,
        tags=tags or [],
        ingredients=[],
        notes=notes or [],
        is_no_cook=is_no_cook,
        is_meatless=is_meatless,
        is_fasting=is_fasting,
        note_type=note_type,
        note_text=note_text,
    )


def make_plan(rationale="") -> WeekPlan:
    plan = WeekPlan(
        week_start_monday=MONDAY,
        week_key="2026-03-23",
        cook_nights=3,
        rationale=rationale,
    )
    for i in range(7):
        d = MONDAY + timedelta(days=i)
        plan.dinners.append(make_slot(d, rid=f"recipe-{i}", label=f"Meal {i}"))
    plan.lunch = make_slot(MONDAY, rid="lunch-1", label="Quinoa bowl")
    plan.lunch.slot = "lunch"
    return plan


def make_grocery() -> GroceryList:
    g = GroceryList(week_start_monday=MONDAY)
    g.items_by_category = {
        "produce": [GroceryItem("onion", "onion", "produce", "1", None)],
        "protein": [],
        "dairy": [],
        "pantry": [],
        "frozen": [],
        "other": [],
    }
    g.likely_on_hand = []
    return g


class TestFormatPlan(unittest.TestCase):
    def setUp(self):
        from output_formatter import format_plan
        self.format_plan = format_plan

    def test_returns_string(self):
        plan = make_plan()
        result = self.format_plan(plan, make_grocery(), [])
        self.assertIsInstance(result, str)

    def test_contains_all_seven_day_names(self):
        plan = make_plan()
        result = self.format_plan(plan, make_grocery(), [])
        for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]:
            self.assertIn(day, result)

    def test_rationale_section_appears_when_nonempty(self):
        plan = make_plan(rationale="Busy week — scheduled accordingly.")
        result = self.format_plan(plan, make_grocery(), [])
        self.assertIn("RATIONALE", result)
        self.assertIn("Busy week", result)

    def test_rationale_section_absent_when_empty(self):
        plan = make_plan(rationale="")
        result = self.format_plan(plan, make_grocery(), [])
        self.assertNotIn("RATIONALE", result)

    def test_out_note_displays_out_flag(self):
        plan = make_plan()
        plan.dinners[1] = make_slot(
            MONDAY + timedelta(days=1),
            label="Dinner at Sarah's",
            is_no_cook=True,
            note_type="out",
            note_text="Dinner at Sarah's",
        )
        result = self.format_plan(plan, make_grocery(), [])
        self.assertIn("[out]", result)
        self.assertIn("Dinner at Sarah's", result)

    def test_cook_note_displays_cook_flag(self):
        plan = make_plan()
        plan.dinners[6] = make_slot(
            MONDAY + timedelta(days=6),
            label="Waffles for dinner",
            note_type="cook",
            note_text="Waffles for dinner",
        )
        result = self.format_plan(plan, make_grocery(), [])
        self.assertIn("[cook", result)
        self.assertIn("Waffles for dinner", result)

    def test_would_have_been_meatless_on_no_cook_wednesday(self):
        plan = make_plan()
        wednesday = MONDAY + timedelta(days=2)  # Wednesday
        plan.dinners[2] = make_slot(
            wednesday,
            label="no cook",
            is_no_cook=True,
            is_meatless=True,
            notes=["Would have been meatless"],
        )
        result = self.format_plan(plan, make_grocery(), [])
        self.assertIn("[would have been meatless]", result)

    def test_grocery_list_produce_section(self):
        plan = make_plan()
        result = self.format_plan(plan, make_grocery(), [])
        self.assertIn("PRODUCE", result)
        self.assertIn("onion", result)

    def test_likely_on_hand_appears_only_when_nonempty(self):
        plan = make_plan()
        grocery = make_grocery()

        result_no_hand = self.format_plan(plan, grocery, [])
        self.assertNotIn("LIKELY ON HAND", result_no_hand)

        grocery.likely_on_hand = [
            GroceryItem("garam masala", "garam masala", "pantry", None, None)
        ]
        result_with_hand = self.format_plan(plan, grocery, [])
        self.assertIn("LIKELY ON HAND", result_with_hand)
        self.assertIn("garam masala", result_with_hand)

    def test_stacking_notes_appear(self):
        plan = make_plan()
        notes = [StackingNote(kind="BATCH", canonical="rice",
                              meals=["Meal 1", "Meal 3"],
                              message="Thursday and Saturday both use rice.")]
        result = self.format_plan(plan, make_grocery(), notes)
        self.assertIn("STACKING NOTES", result)
        self.assertIn("Thursday and Saturday both use rice.", result)

    def test_stacking_notes_absent_when_empty(self):
        plan = make_plan()
        result = self.format_plan(plan, make_grocery(), [])
        self.assertNotIn("STACKING NOTES", result)


class TestPrintPlan(unittest.TestCase):
    def test_print_plan_calls_print(self):
        from output_formatter import print_plan, format_plan
        plan = make_plan()
        with patch("builtins.print") as mock_print:
            print_plan(plan, make_grocery(), [])
        mock_print.assert_called_once()
        args = mock_print.call_args[0][0]
        self.assertIsInstance(args, str)
        self.assertIn("MEAL PLAN", args)


if __name__ == "__main__":
    unittest.main()
