"""
test_email_sender.py

Tests for email formatting and acknowledgment detection.
No Gmail API calls are made.

Run with: python -m pytest test_email_sender.py -v
"""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from email_sender import (
    EmailSender,
    format_plan_email,
    _fmt_date_range,
    _fmt_day_header,
    _slot_label,
    _wrap,
)
from planner import WeekPlan, MealSlot
from grocery_builder import GroceryList, GroceryItem
from stacker import StackingNote


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


def make_slot(
    d: date,
    recipe_id: str = None,
    label: str = "Test Recipe",
    tags: list = None,
    is_no_cook: bool = False,
    is_meatless: bool = False,
    is_fasting: bool = False,
    notes: list = None,
) -> MealSlot:
    return MealSlot(
        date=d, slot="dinner",
        recipe_id=recipe_id,
        label=label,
        tags=tags or [],
        ingredients=[],
        notes=notes or [],
        is_no_cook=is_no_cook,
        is_meatless=is_meatless,
        is_fasting=is_fasting,
    )


def make_plan(dinners=None, cook_nights=3) -> WeekPlan:
    plan = WeekPlan(
        week_start_monday=MONDAY,
        week_key="2026-03-16",
        cook_nights=cook_nights,
    )
    plan.dinners = dinners or [
        make_slot(FRIDAY,    label="Takeout pizza",  tags=["pizza"], is_meatless=True),
        make_slot(SATURDAY,  recipe_id="r1", label="Spiced Rice",    tags=["vegetarian"]),
        make_slot(SUNDAY,    recipe_id="r2", label="Burger Steaks"),
        make_slot(MONDAY,    is_no_cook=True, label="no cook", notes=["qualifying event after 3pm"]),
        make_slot(TUESDAY,   recipe_id="r3", label="Tomato Pasta",   tags=["pasta"]),
        make_slot(WEDNESDAY, recipe_id="r4", label="IP Pasta",       tags=["pasta"], is_meatless=True),
        make_slot(THURSDAY,  label="leftovers"),
    ]
    plan.lunch = MealSlot(
        date=MONDAY, slot="lunch", recipe_id="grain-bowl",
        label="Quinoa grain bowl", tags=[], ingredients=[],
        notes=["Weekend prep: Cook quinoa Sunday"],
    )
    return plan


def make_grocery(with_on_hand=False) -> GroceryList:
    gl = GroceryList(week_start_monday=MONDAY)
    gl.items_by_category = {
        "produce": [GroceryItem("onion", "onion", "produce", "2", None, recipes=["Burger Steaks"])],
        "protein": [GroceryItem("ground beef", "ground beef", "protein", "1", "lb", recipes=["Burger Steaks"])],
        "dairy":   [GroceryItem("cream cheese", "cream cheese", "dairy", "4", "oz", recipes=["Tomato Pasta"])],
        "pantry":  [GroceryItem("basmati rice", "rice", "pantry", "1", "cup", recipes=["Spiced Rice"])],
        "frozen":  [],
        "other":   [],
    }
    if with_on_hand:
        gl.likely_on_hand = [
            GroceryItem("garam masala", "garam masala", "pantry", None, None, recipes=["Spiced Rice"]),
        ]
    return gl


def make_stacking_notes() -> list[StackingNote]:
    return [
        StackingNote(
            kind="BATCH", canonical="rice", meals=["Spiced Rice", "Rice Bowl"],
            message="Tuesday and Saturday both use rice — consider cooking a double batch on Tuesday.",
        ),
    ]


# ---------------------------------------------------------------------------
# Date formatting tests
# ---------------------------------------------------------------------------

class TestDateFormatting(unittest.TestCase):

    def test_same_month_range(self):
        result = _fmt_date_range(date(2026, 3, 23))
        self.assertIn("Mar", result)
        self.assertIn("23", result)
        self.assertIn("29", result)
        # Should not repeat "Mar"
        self.assertEqual(result.count("Mar"), 1)

    def test_cross_month_range(self):
        result = _fmt_date_range(date(2026, 3, 30))
        # Mar 30 – Apr 5
        self.assertIn("Mar", result)
        self.assertIn("Apr", result)

    def test_day_header_includes_weekday(self):
        result = _fmt_day_header(date(2026, 3, 23))
        self.assertIn("Monday", result)
        self.assertIn("Mar", result)


# ---------------------------------------------------------------------------
# Slot label formatting
# ---------------------------------------------------------------------------

class TestSlotLabel(unittest.TestCase):

    def test_basic_label(self):
        slot = make_slot(MONDAY, recipe_id="r1", label="Spiced Rice")
        self.assertIn("Spiced Rice", _slot_label(slot))

    def test_pasta_tag_shown(self):
        slot = make_slot(TUESDAY, recipe_id="r1", label="Tomato Pasta", tags=["pasta"])
        label = _slot_label(slot)
        self.assertIn("[pasta]", label)

    def test_taco_tag_shown(self):
        slot = make_slot(TUESDAY, recipe_id="r1", label="Tacos", tags=["taco"])
        label = _slot_label(slot)
        self.assertIn("[taco]", label)

    def test_fasting_flag_shown(self):
        slot = make_slot(TUESDAY, recipe_id="r1", label="Veg Dish", is_fasting=True)
        label = _slot_label(slot)
        self.assertIn("fasting", label)

    def test_no_cook_label(self):
        slot = make_slot(MONDAY, is_no_cook=True, label="no cook",
                         notes=["qualifying event after 3pm"])
        label = _slot_label(slot)
        self.assertIn("no cook", label)
        self.assertIn("qualifying event", label)

    def test_meatless_flag_not_shown_on_wednesday(self):
        # Wednesday meatless is implied — no badge needed
        slot = make_slot(WEDNESDAY, recipe_id="r1", label="Veg Pasta",
                         is_meatless=True, tags=["vegetarian"])
        label = _slot_label(slot)
        self.assertNotIn("⚑ meatless day", label)

    def test_meatless_flag_shown_on_non_standing_days(self):
        # Tuesday meatless (fasting day) — should show flag
        slot = make_slot(TUESDAY, recipe_id="r1", label="Veg Dish",
                         is_meatless=True, is_fasting=True)
        label = _slot_label(slot)
        self.assertIn("fasting", label)


# ---------------------------------------------------------------------------
# Word wrap
# ---------------------------------------------------------------------------

class TestWordWrap(unittest.TestCase):

    def test_short_line_unchanged(self):
        result = _wrap("Short line.", width=80)
        self.assertEqual(result, ["Short line."])

    def test_long_line_wrapped(self):
        text = "• " + "word " * 20
        result = _wrap(text, width=40)
        self.assertGreater(len(result), 1)
        for line in result:
            self.assertLessEqual(len(line), 45)  # allow slight overage on last word

    def test_continuation_indented(self):
        text = "• " + "word " * 20
        result = _wrap(text, width=40, indent="  ")
        # Should produce multiple lines when text exceeds width
        self.assertGreater(len(result), 1)
        # All continuation lines (not the first) should be indented
        for line in result[1:]:
            self.assertTrue(
                line.startswith("  "),
                msg=f"Continuation line not indented: {repr(line)}"
            )


# ---------------------------------------------------------------------------
# Email body structure
# ---------------------------------------------------------------------------

class TestEmailBodyStructure(unittest.TestCase):

    def setUp(self):
        self.plan    = make_plan()
        self.grocery = make_grocery()
        self.notes   = make_stacking_notes()

    def test_subject_contains_date_range(self):
        subject, _ = format_plan_email(self.plan, self.grocery, self.notes)
        self.assertIn("Mar", subject)
        self.assertIn("cook nights", subject)

    def test_subject_revised_tag(self):
        subject, _ = format_plan_email(self.plan, self.grocery, self.notes, is_revision=True)
        self.assertIn("REVISED", subject)

    def test_subject_not_revised_by_default(self):
        subject, _ = format_plan_email(self.plan, self.grocery, self.notes)
        self.assertNotIn("REVISED", subject)

    def test_body_contains_all_days(self):
        _, body = format_plan_email(self.plan, self.grocery, self.notes)
        for day in ["Friday", "Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday"]:
            self.assertIn(day, body)

    def test_body_contains_recipe_names(self):
        _, body = format_plan_email(self.plan, self.grocery, self.notes)
        self.assertIn("Spiced Rice", body)
        self.assertIn("Burger Steaks", body)
        self.assertIn("Tomato Pasta", body)

    def test_body_contains_no_cook_label(self):
        _, body = format_plan_email(self.plan, self.grocery, self.notes)
        self.assertIn("no cook", body)

    def test_body_contains_lunch(self):
        _, body = format_plan_email(self.plan, self.grocery, self.notes)
        self.assertIn("Quinoa grain bowl", body)
        self.assertIn("Cook quinoa Sunday", body)

    def test_body_contains_stacking_notes(self):
        _, body = format_plan_email(self.plan, self.grocery, self.notes)
        self.assertIn("STACKING", body)
        # Note may be wrapped across lines — normalise whitespace before checking
        normalised = " ".join(body.split())
        self.assertIn("double batch", normalised)

    def test_body_contains_grocery_sections(self):
        _, body = format_plan_email(self.plan, self.grocery, self.notes)
        self.assertIn("GROCERY LIST", body)
        self.assertIn("PRODUCE", body)
        self.assertIn("PROTEIN", body)
        self.assertIn("DAIRY", body)
        self.assertIn("PANTRY", body)

    def test_body_contains_grocery_items(self):
        _, body = format_plan_email(self.plan, self.grocery, self.notes)
        self.assertIn("onion", body)
        self.assertIn("ground beef", body)
        self.assertIn("cream cheese", body)
        self.assertIn("basmati rice", body)

    def test_body_contains_likely_on_hand_section(self):
        grocery_with_on_hand = make_grocery(with_on_hand=True)
        _, body = format_plan_email(self.plan, grocery_with_on_hand, self.notes)
        self.assertIn("LIKELY ON HAND", body)
        self.assertIn("garam masala", body)

    def test_no_on_hand_section_when_empty(self):
        _, body = format_plan_email(self.plan, self.grocery, self.notes)
        self.assertNotIn("LIKELY ON HAND", body)

    def test_body_contains_reply_instructions(self):
        _, body = format_plan_email(self.plan, self.grocery, self.notes)
        self.assertIn("Reply to make changes", body)
        self.assertIn("Okay thanks", body)

    def test_no_stacking_section_when_empty(self):
        _, body = format_plan_email(self.plan, self.grocery, [])
        self.assertNotIn("STACKING", body)

    def test_cook_nights_in_subject(self):
        plan = make_plan(cook_nights=4)
        subject, _ = format_plan_email(plan, self.grocery, self.notes)
        self.assertIn("4 cook nights", subject)

    def test_warnings_in_body(self):
        plan = make_plan()
        plan.warnings = ["No suitable recipe found for 2026-03-26 — marked as leftovers."]
        _, body = format_plan_email(plan, self.grocery, self.notes)
        self.assertIn("No suitable recipe", body)


# ---------------------------------------------------------------------------
# Acknowledgment detection
# ---------------------------------------------------------------------------

class TestAcknowledgmentDetection(unittest.TestCase):

    def setUp(self):
        self.sender = EmailSender(config={"to_address": "test@example.com"}, dry_run=True)

    def test_okay_thanks(self):
        self.assertTrue(self.sender.is_acknowledgment("Okay thanks"))

    def test_looks_good(self):
        self.assertTrue(self.sender.is_acknowledgment("Looks good!"))

    def test_thumbs_up_emoji(self):
        self.assertTrue(self.sender.is_acknowledgment("👍"))

    def test_thanks(self):
        self.assertTrue(self.sender.is_acknowledgment("Thanks"))

    def test_sounds_good(self):
        self.assertTrue(self.sender.is_acknowledgment("Sounds good"))

    def test_done(self):
        self.assertTrue(self.sender.is_acknowledgment("Done"))

    def test_swap_request_not_ack(self):
        self.assertFalse(self.sender.is_acknowledgment(
            "Swap Tuesday for a pasta dish"
        ))

    def test_feedback_not_ack(self):
        self.assertFalse(self.sender.is_acknowledgment(
            "Skip the experiment this week, use an easy recipe"
        ))

    def test_cream_cheese_correction_not_ack(self):
        self.assertFalse(self.sender.is_acknowledgment(
            "The cream cheese is already gone, remove it from likely on hand"
        ))

    def test_ack_with_quoted_reply_text(self):
        # Common email client behaviour: reply with quoted prior message
        body = "Looks good!\n\n> On Thu, Mar 19...\n> MEAL PLAN: Mar 20..."
        self.assertTrue(self.sender.is_acknowledgment(body))

    def test_ack_case_insensitive(self):
        self.assertTrue(self.sender.is_acknowledgment("LOOKS GOOD"))
        self.assertTrue(self.sender.is_acknowledgment("okay thanks"))
        self.assertTrue(self.sender.is_acknowledgment("OKAY THANKS"))

    def test_empty_string_not_ack(self):
        self.assertFalse(self.sender.is_acknowledgment(""))

    def test_whitespace_only_not_ack(self):
        self.assertFalse(self.sender.is_acknowledgment("   "))


# ---------------------------------------------------------------------------
# Dry-run send (no network)
# ---------------------------------------------------------------------------

class TestDryRun(unittest.TestCase):

    def test_dry_run_returns_none(self):
        sender = EmailSender(config={"to_address": "test@example.com"}, dry_run=True)
        plan    = make_plan()
        grocery = make_grocery()
        notes   = make_stacking_notes()
        result = sender.send_plan(plan, grocery, notes)
        self.assertIsNone(result)

    def test_missing_to_address_raises(self):
        sender = EmailSender(config={}, dry_run=True)
        plan    = make_plan()
        grocery = make_grocery()
        with self.assertRaises(ValueError):
            sender.send_plan(plan, grocery, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
