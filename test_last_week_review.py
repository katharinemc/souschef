"""Tests for _review_last_week substitution handling in main.py."""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_recipe(recipe_dir: str, rid: str, name: str) -> None:
    p = Path(recipe_dir) / f"{rid}.yaml"
    p.write_text(yaml.dump({
        "id": rid,
        "name": name,
        "tags": ["onRotation"],
        "ingredients": [],
    }))


def _save_plan(store: StateStore, monday: date, dinners: list[dict]) -> None:
    """Store a minimal plan dict for the given week."""
    week_key = monday.isoformat()
    plan_dict = {
        "week_start_monday": week_key,
        "week_key": week_key,
        "dinners": [
            {
                "date": (monday + timedelta(days=i)).isoformat(),
                "weekday": (monday + timedelta(days=i)).strftime("%A"),
                "recipe_id": d["recipe_id"],
                "label": d["label"],
                "tags": [],
                "ingredients": [],
                "is_no_cook": False,
                "note_type": None,
                "note_text": None,
            }
            for i, d in enumerate(dinners)
        ],
        "lunch": None,
        "cook_nights": len(dinners),
        "rationale": "",
        "warnings": [],
    }
    store.record_plan(week_key, plan_dict, [])


def _mock_claude(payload: dict) -> MagicMock:
    client = MagicMock()
    content = MagicMock()
    content.text = json.dumps(payload)
    client.messages.create.return_value.content = [content]
    return client


def _call_review(store, this_monday, model, recipe_dir, user_input, claude_response):
    from main import _review_last_week
    mock_client = _mock_claude(claude_response)
    with patch("builtins.input", return_value=user_input), \
         patch("anthropic.Anthropic", return_value=mock_client):
        _review_last_week(store, this_monday, model, recipe_dir)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestReviewLastWeekSubstitutions(unittest.TestCase):

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = StateStore(self.db_path)

        self.recipe_dir = tempfile.mkdtemp()
        _write_recipe(self.recipe_dir, "chorizo-and-potato-tacos", "Chorizo Tacos")
        _write_recipe(self.recipe_dir, "hamburger-steaks", "Hamburger Steaks")
        _write_recipe(self.recipe_dir, "chicken-cacciatore", "Chicken Cacciatore")

        self.last_monday = date(2026, 7, 13)
        _save_plan(self.store, self.last_monday, [
            {"recipe_id": "chorizo-and-potato-tacos", "label": "Chorizo Tacos"},
            {"recipe_id": "hamburger-steaks",         "label": "Hamburger Steaks"},
        ])
        self.this_monday = self.last_monday + timedelta(weeks=1)

    def tearDown(self):
        self.store.close()
        os.unlink(self.db_path)

    # -----------------------------------------------------------------------
    # not_cooked behaviour (regression check)
    # -----------------------------------------------------------------------

    def test_not_cooked_clears_recipe_history(self):
        self.store.set_last_planned("chorizo-and-potato-tacos", self.last_monday)
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "we didn't make the tacos",
            {"not_cooked": ["chorizo-and-potato-tacos"], "substitutions": []},
        )
        self.assertIsNone(self.store.get_last_planned("chorizo-and-potato-tacos"))

    # -----------------------------------------------------------------------
    # substitution — library match
    # -----------------------------------------------------------------------

    def test_substitution_library_match_records_last_planned(self):
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "we didn't make tacos, we made chicken cacciatore instead",
            {
                "not_cooked": ["chorizo-and-potato-tacos"],
                "substitutions": [{
                    "original_recipe_id": "chorizo-and-potato-tacos",
                    "recipe_id": "chicken-cacciatore",
                    "free_text": "chicken cacciatore",
                    "cook_date": "2026-07-13",
                }],
            },
        )
        lp = self.store.get_last_planned("chicken-cacciatore")
        self.assertEqual(lp, date(2026, 7, 13))

    def test_substitution_library_match_does_not_record_meal_note(self):
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "made cacciatore instead of tacos",
            {
                "not_cooked": [],
                "substitutions": [{
                    "original_recipe_id": None,
                    "recipe_id": "chicken-cacciatore",
                    "free_text": "chicken cacciatore",
                    "cook_date": "2026-07-13",
                }],
            },
        )
        notes = self.store.get_meal_notes(self.last_monday.isoformat())
        self.assertEqual(notes, [])

    # -----------------------------------------------------------------------
    # substitution — free-text fallback (no library match)
    # -----------------------------------------------------------------------

    def test_substitution_no_match_records_meal_note(self):
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "we made lasagna instead of steaks",
            {
                "not_cooked": ["hamburger-steaks"],
                "substitutions": [{
                    "original_recipe_id": "hamburger-steaks",
                    "recipe_id": None,
                    "free_text": "lasagna",
                    "cook_date": "2026-07-14",
                }],
            },
        )
        notes = self.store.get_meal_notes(self.last_monday.isoformat())
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["note_text"], "lasagna")
        self.assertEqual(notes[0]["note_type"], "cook")
        self.assertEqual(notes[0]["meal_date"], "2026-07-14")

    def test_substitution_no_match_does_not_set_last_planned(self):
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "made lasagna instead",
            {
                "not_cooked": [],
                "substitutions": [{
                    "original_recipe_id": None,
                    "recipe_id": None,
                    "free_text": "lasagna",
                    "cook_date": "2026-07-13",
                }],
            },
        )
        self.assertIsNone(self.store.get_last_planned("lasagna"))

    # -----------------------------------------------------------------------
    # cook_date inference fallback
    # -----------------------------------------------------------------------

    def test_cook_date_inferred_from_original_slot_when_null(self):
        """When Claude returns cook_date=null, falls back to the original slot's date."""
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "made cacciatore instead of tacos",
            {
                "not_cooked": [],
                "substitutions": [{
                    "original_recipe_id": "chorizo-and-potato-tacos",
                    "recipe_id": "chicken-cacciatore",
                    "free_text": "chicken cacciatore",
                    "cook_date": None,
                }],
            },
        )
        lp = self.store.get_last_planned("chicken-cacciatore")
        # Tacos were slot 0 → date = last_monday + 0 days = 2026-07-13
        self.assertEqual(lp, date(2026, 7, 13))

    def test_cook_date_falls_back_to_last_monday_when_no_original(self):
        """When cook_date=null and no original_recipe_id, uses last_monday."""
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "made cacciatore at some point",
            {
                "not_cooked": [],
                "substitutions": [{
                    "original_recipe_id": None,
                    "recipe_id": "chicken-cacciatore",
                    "free_text": "chicken cacciatore",
                    "cook_date": None,
                }],
            },
        )
        lp = self.store.get_last_planned("chicken-cacciatore")
        self.assertEqual(lp, self.last_monday)

    # -----------------------------------------------------------------------
    # early-exit conditions
    # -----------------------------------------------------------------------

    def test_empty_input_skips_api_call(self):
        from main import _review_last_week
        with patch("builtins.input", return_value=""), \
             patch("anthropic.Anthropic") as mock_ant:
            _review_last_week(self.store, self.this_monday, "claude-test", self.recipe_dir)
        mock_ant.assert_not_called()

    def test_no_last_week_plan_returns_early(self):
        from main import _review_last_week
        far_future = self.this_monday + timedelta(weeks=52)
        with patch("anthropic.Anthropic") as mock_ant:
            _review_last_week(self.store, far_future, "claude-test", self.recipe_dir)
        mock_ant.assert_not_called()
