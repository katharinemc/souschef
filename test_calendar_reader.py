"""
test_calendar_reader.py

Tests all constraint logic in CalendarReader without hitting the Google API.
The _fetch_events method is patched to return synthetic event dicts.

Run with: python -m pytest test_calendar_reader.py -v
"""

import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
from calendar_reader import CalendarReader, DayConstraints, WeekConstraints


# ---------------------------------------------------------------------------
# Helpers to build synthetic Google Calendar event dicts
# ---------------------------------------------------------------------------

def make_event(title: str, start: str, end: str) -> dict:
    """
    Build a minimal Google Calendar event dict.
    start/end are ISO datetime strings e.g. '2026-03-20T18:00:00-05:00'
    """
    return {
        "summary": title,
        "start":   {"dateTime": start},
        "end":     {"dateTime": end},
    }


def make_allday_event(title: str, date_str: str) -> dict:
    """Build an all-day event dict."""
    return {
        "summary": title,
        "start":   {"date": date_str},
        "end":     {"date": date_str},
    }


def make_fasting_event(date_str: str) -> dict:
    """5am 30-minute fasting marker."""
    return make_event(
        "fasting",
        f"{date_str}T05:00:00-05:00",
        f"{date_str}T05:30:00-05:00",
    )


# Planning week: Mon 2026-03-23 → Sun 2026-03-29
MONDAY    = date(2026, 3, 23)
TUESDAY   = date(2026, 3, 24)
WEDNESDAY = date(2026, 3, 25)
THURSDAY  = date(2026, 3, 26)
FRIDAY    = date(2026, 3, 27)
SATURDAY  = date(2026, 3, 28)
SUNDAY    = date(2026, 3, 29)


def reader_with_events(events: list[dict]) -> WeekConstraints:
    """Build a CalendarReader, patch _fetch_events, and return WeekConstraints."""
    r = CalendarReader(timezone="America/New_York")
    with patch.object(r, "_fetch_events", return_value=events):
        return r.get_week_constraints(start_monday=MONDAY)


# ---------------------------------------------------------------------------
# Date helper tests
# ---------------------------------------------------------------------------

class TestDateHelpers(unittest.TestCase):

    def test_next_planning_monday_from_thursday(self):
        # Thursday Mar 19 → next week's Monday Mar 23... wait, that's same week
        # Actually: Thursday Mar 19 → Monday Mar 23 (next calendar week)
        thursday = date(2026, 3, 19)
        self.assertEqual(CalendarReader.next_planning_monday(thursday), date(2026, 3, 23))

    def test_next_planning_monday_from_monday(self):
        # Monday Mar 23 → always go to NEXT week's Monday Mar 30
        monday = date(2026, 3, 23)
        self.assertEqual(CalendarReader.next_planning_monday(monday), date(2026, 3, 30))

    def test_next_planning_monday_from_sunday(self):
        # Sunday Mar 22 → next Monday Mar 23
        sunday = date(2026, 3, 22)
        self.assertEqual(CalendarReader.next_planning_monday(sunday), date(2026, 3, 23))

    def test_planning_window(self):
        mon = date(2026, 3, 23)
        start, end = CalendarReader.planning_window(mon)
        self.assertEqual(start, mon)
        self.assertEqual(end, date(2026, 3, 29))


# ---------------------------------------------------------------------------
# Event classification tests
# ---------------------------------------------------------------------------

class TestEventClassification(unittest.TestCase):

    def setUp(self):
        self.r = CalendarReader(timezone="America/New_York")

    def test_krm_prefix_is_qualifying(self):
        self.assertTrue(self.r._is_qualifying("KRM Birthday Dinner"))

    def test_family_prefix_is_qualifying(self):
        self.assertTrue(self.r._is_qualifying("Family Game Night"))

    def test_sgm_is_ignored(self):
        self.assertTrue(self.r._should_ignore("SGM Board Meeting"))

    def test_sgm_not_qualifying(self):
        self.assertFalse(self.r._is_qualifying("SGM Board Meeting"))

    def test_unrelated_event_not_qualifying(self):
        self.assertFalse(self.r._is_qualifying("Dentist Appointment"))

    def test_unrelated_event_not_ignored(self):
        self.assertFalse(self.r._should_ignore("Dentist Appointment"))

    def test_prefix_matching_is_case_insensitive(self):
        self.assertTrue(self.r._is_qualifying("krm dinner"))
        self.assertTrue(self.r._is_qualifying("FAMILY reunion"))
        self.assertTrue(self.r._should_ignore("sgm meeting"))

    def test_after_3pm_true(self):
        event = make_event("KRM Dinner", "2026-03-27T18:00:00-05:00", "2026-03-27T20:00:00-05:00")
        self.assertTrue(self.r._is_after_3pm(event))

    def test_after_3pm_exactly_3pm(self):
        event = make_event("KRM Dinner", "2026-03-27T15:00:00-05:00", "2026-03-27T16:00:00-05:00")
        self.assertTrue(self.r._is_after_3pm(event))

    def test_not_after_3pm(self):
        event = make_event("KRM Lunch", "2026-03-27T12:00:00-05:00", "2026-03-27T13:00:00-05:00")
        self.assertFalse(self.r._is_after_3pm(event))

    def test_all_day_event_not_after_3pm(self):
        event = make_allday_event("KRM Birthday", "2026-03-20")
        self.assertFalse(self.r._is_after_3pm(event))

    def test_duration_over_2hrs(self):
        event = make_event("Family Event", "2026-03-28T10:00:00-05:00", "2026-03-28T13:00:00-05:00")
        self.assertTrue(self.r._is_longer_than_2hrs(event))

    def test_duration_exactly_2hrs_not_over(self):
        event = make_event("Family Event", "2026-03-28T10:00:00-05:00", "2026-03-28T12:00:00-05:00")
        self.assertFalse(self.r._is_longer_than_2hrs(event))

    def test_duration_under_2hrs(self):
        event = make_event("Family Event", "2026-03-28T10:00:00-05:00", "2026-03-28T11:00:00-05:00")
        self.assertFalse(self.r._is_longer_than_2hrs(event))

    def test_fasting_event_detected(self):
        event = make_fasting_event("2026-03-24")
        self.assertTrue(self.r._is_fasting_event(event))

    def test_fasting_event_wrong_hour(self):
        event = make_event("fasting", "2026-03-24T08:00:00-05:00", "2026-03-24T08:30:00-05:00")
        self.assertFalse(self.r._is_fasting_event(event))

    def test_fasting_event_wrong_title(self):
        event = make_event("Fast Day", "2026-03-24T05:00:00-05:00", "2026-03-24T05:30:00-05:00")
        self.assertFalse(self.r._is_fasting_event(event))

    def test_fasting_allday_event_not_detected(self):
        # All-day events have no dateTime, so shouldn't match
        event = make_allday_event("fasting", "2026-03-24")
        self.assertFalse(self.r._is_fasting_event(event))


# ---------------------------------------------------------------------------
# Weekday no-cook rule tests
# ---------------------------------------------------------------------------

class TestWeekdayNoCook(unittest.TestCase):

    def test_krm_after_3pm_is_no_cook(self):
        events = [make_event("KRM Dinner Party", "2026-03-27T18:30:00-05:00", "2026-03-27T21:00:00-05:00")]
        wc = reader_with_events(events)
        friday = wc.get(FRIDAY)
        self.assertTrue(friday.is_no_cook)

    def test_krm_before_3pm_is_not_no_cook(self):
        events = [make_event("KRM Lunch", "2026-03-27T12:00:00-05:00", "2026-03-27T13:30:00-05:00")]
        wc = reader_with_events(events)
        friday = wc.get(FRIDAY)
        self.assertFalse(friday.is_no_cook)

    def test_sgm_after_3pm_does_not_trigger_no_cook(self):
        events = [make_event("SGM Board Meeting", "2026-03-23T17:00:00-05:00", "2026-03-23T19:00:00-05:00")]
        wc = reader_with_events(events)
        monday = wc.get(MONDAY)
        self.assertFalse(monday.is_no_cook)

    def test_unrelated_event_after_3pm_does_not_trigger_no_cook(self):
        events = [make_event("Dentist", "2026-03-23T16:00:00-05:00", "2026-03-23T17:00:00-05:00")]
        wc = reader_with_events(events)
        monday = wc.get(MONDAY)
        self.assertFalse(monday.is_no_cook)

    def test_family_event_after_3pm_triggers_no_cook(self):
        events = [make_event("Family Reunion", "2026-03-24T19:00:00-05:00", "2026-03-24T22:00:00-05:00")]
        wc = reader_with_events(events)
        tuesday = wc.get(TUESDAY)
        self.assertTrue(tuesday.is_no_cook)

    def test_no_cook_reason_populated(self):
        events = [make_event("KRM Dinner", "2026-03-27T18:00:00-05:00", "2026-03-27T20:00:00-05:00")]
        wc = reader_with_events(events)
        self.assertIsNotNone(wc.get(FRIDAY).no_cook_reason)

    def test_no_events_means_normal_day(self):
        wc = reader_with_events([])
        for dc in wc.days:
            if dc.date.weekday() != 5:  # skip Saturday (open_saturday check)
                self.assertFalse(dc.is_no_cook)


# ---------------------------------------------------------------------------
# Saturday rule tests
# ---------------------------------------------------------------------------

class TestSaturdayRules(unittest.TestCase):

    def test_no_events_saturday_is_open(self):
        wc = reader_with_events([])
        saturday = wc.get(SATURDAY)
        self.assertTrue(saturday.is_open_saturday)
        self.assertFalse(saturday.is_no_cook)

    def test_qualifying_event_over_2hrs_is_no_cook_saturday(self):
        events = [make_event("Family Reunion", "2026-03-28T10:00:00-05:00", "2026-03-28T14:00:00-05:00")]
        wc = reader_with_events(events)
        saturday = wc.get(SATURDAY)
        self.assertTrue(saturday.is_no_cook)
        self.assertFalse(saturday.is_open_saturday)

    def test_qualifying_event_under_2hrs_saturday_still_open(self):
        events = [make_event("KRM Brunch", "2026-03-28T10:00:00-05:00", "2026-03-28T11:00:00-05:00")]
        wc = reader_with_events(events)
        saturday = wc.get(SATURDAY)
        self.assertFalse(saturday.is_no_cook)
        self.assertTrue(saturday.is_open_saturday)

    def test_sgm_event_over_2hrs_does_not_block_saturday(self):
        events = [make_event("SGM All-Day Retreat", "2026-03-28T09:00:00-05:00", "2026-03-28T18:00:00-05:00")]
        wc = reader_with_events(events)
        saturday = wc.get(SATURDAY)
        self.assertFalse(saturday.is_no_cook)
        self.assertTrue(saturday.is_open_saturday)

    def test_unrelated_event_over_2hrs_does_not_block_saturday(self):
        events = [make_event("Kids Soccer Tournament", "2026-03-28T08:00:00-05:00", "2026-03-28T14:00:00-05:00")]
        wc = reader_with_events(events)
        saturday = wc.get(SATURDAY)
        # Non-qualifying event should not block Saturday
        self.assertFalse(saturday.is_no_cook)
        self.assertTrue(saturday.is_open_saturday)

    def test_no_cook_saturday_triggers_sunday_needs_easy(self):
        events = [make_event("Family Wedding", "2026-03-28T11:00:00-05:00", "2026-03-28T17:00:00-05:00")]
        wc = reader_with_events(events)
        self.assertTrue(wc.sunday_needs_easy)

    def test_open_saturday_does_not_trigger_sunday_needs_easy(self):
        wc = reader_with_events([])
        self.assertFalse(wc.sunday_needs_easy)


# ---------------------------------------------------------------------------
# Fasting day tests
# ---------------------------------------------------------------------------

class TestFastingDays(unittest.TestCase):

    def test_fasting_marker_sets_fasting_flag(self):
        events = [make_fasting_event("2026-03-24")]
        wc = reader_with_events(events)
        tuesday = wc.get(TUESDAY)
        self.assertTrue(tuesday.is_fasting)

    def test_fasting_does_not_set_no_cook(self):
        events = [make_fasting_event("2026-03-24")]
        wc = reader_with_events(events)
        tuesday = wc.get(TUESDAY)
        self.assertFalse(tuesday.is_no_cook)

    def test_fasting_and_no_cook_can_coexist(self):
        events = [
            make_fasting_event("2026-03-24"),
            make_event("KRM Dinner", "2026-03-24T19:00:00-05:00", "2026-03-24T21:00:00-05:00"),
        ]
        wc = reader_with_events(events)
        tuesday = wc.get(TUESDAY)
        self.assertTrue(tuesday.is_fasting)
        self.assertTrue(tuesday.is_no_cook)

    def test_multiple_fasting_days(self):
        events = [
            make_fasting_event("2026-03-23"),
            make_fasting_event("2026-03-25"),
        ]
        wc = reader_with_events(events)
        self.assertTrue(wc.get(MONDAY).is_fasting)
        self.assertTrue(wc.get(WEDNESDAY).is_fasting)
        self.assertFalse(wc.get(TUESDAY).is_fasting)

    def test_fasting_days_list(self):
        events = [make_fasting_event("2026-03-24")]
        wc = reader_with_events(events)
        self.assertEqual(len(wc.fasting_days()), 1)


# ---------------------------------------------------------------------------
# WeekConstraints helper tests
# ---------------------------------------------------------------------------

class TestWeekConstraints(unittest.TestCase):

    def test_all_7_days_present(self):
        wc = reader_with_events([])
        self.assertEqual(len(wc.days), 7)

    def test_days_span_mon_to_sun(self):
        wc = reader_with_events([])
        self.assertEqual(wc.days[0].date, MONDAY)
        self.assertEqual(wc.days[-1].date, SUNDAY)

    def test_no_cook_days_list(self):
        events = [
            make_event("KRM Dinner", "2026-03-27T18:00:00-05:00", "2026-03-27T20:00:00-05:00"),
            make_event("Family Party", "2026-03-23T19:00:00-05:00", "2026-03-23T22:00:00-05:00"),
        ]
        wc = reader_with_events(events)
        self.assertEqual(len(wc.no_cook_days()), 2)

    def test_open_saturday_accessor(self):
        wc = reader_with_events([])
        self.assertIsNotNone(wc.open_saturday())
        self.assertEqual(wc.open_saturday().date, SATURDAY)

    def test_summary_returns_string(self):
        wc = reader_with_events([])
        s = wc.summary()
        self.assertIsInstance(s, str)
        self.assertIn("2026-03-23", s)

    def test_weekday_name(self):
        wc = reader_with_events([])
        self.assertEqual(wc.days[0].weekday_name, "Monday")
        self.assertEqual(wc.days[1].weekday_name, "Tuesday")


if __name__ == "__main__":
    unittest.main(verbosity=2)
