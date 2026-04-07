"""
test_state_store.py

Run with: python -m pytest test_state_store.py -v
or:        python test_state_store.py
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from state_store import StateStore, _week_key_for_date, _cutoff_date, HISTORY_MONTHS


def make_meal(meal_date: str, recipe_id: str, label: str, ingredients=None):
    return {
        "meal_date":   meal_date,
        "slot":        "dinner",
        "recipe_id":   recipe_id,
        "label":       label,
        "tags":        ["onRotation"],
        "ingredients": ingredients or [{"name": "onion", "quantity": "1", "unit": "cup"}],
    }


class TestWeekKey(unittest.TestCase):
    def test_monday_returns_itself(self):
        self.assertEqual(_week_key_for_date(date(2026, 3, 16)), "2026-03-16")

    def test_friday_returns_prior_monday(self):
        self.assertEqual(_week_key_for_date(date(2026, 3, 20)), "2026-03-16")

    def test_sunday_returns_prior_monday(self):
        self.assertEqual(_week_key_for_date(date(2026, 3, 22)), "2026-03-16")

    def test_thursday_returns_prior_monday(self):
        self.assertEqual(_week_key_for_date(date(2026, 3, 19)), "2026-03-16")


class TestStateStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db = StateStore(self.tmp.name)

    def tearDown(self):
        self.db.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    # --- recipe rotation ---

    def test_last_planned_none_for_unknown(self):
        self.assertIsNone(self.db.get_last_planned("unknown-recipe"))

    def test_set_and_get_last_planned(self):
        d = date(2026, 3, 15)
        self.db.set_last_planned("burger-steaks", d)
        self.assertEqual(self.db.get_last_planned("burger-steaks"), d)

    def test_get_all_last_planned(self):
        self.db.set_last_planned("recipe-a", date(2026, 1, 1))
        self.db.set_last_planned("recipe-b", date(2026, 2, 1))
        result = self.db.get_all_last_planned()
        self.assertEqual(len(result), 2)
        self.assertIn("recipe-a", result)

    # --- lunch rotation ---

    def test_lunch_last_planned_roundtrip(self):
        d = date(2026, 3, 10)
        self.db.set_lunch_last_planned("grain-bowl", d)
        self.assertEqual(self.db.get_lunch_last_planned("grain-bowl"), d)

    # --- plan storage ---

    def test_record_and_retrieve_plan(self):
        week_key = "2026-03-16"
        plan = {"week": week_key, "dinners": ["burger-steaks"]}
        meals = [make_meal("2026-03-20", "burger-steaks", "Hamburger Steaks")]
        self.db.record_plan(week_key, plan, meals)
        retrieved = self.db.get_plan(week_key)
        self.assertEqual(retrieved["week"], week_key)

    def test_record_plan_updates_last_planned(self):
        week_key = "2026-03-16"
        meals = [make_meal("2026-03-20", "burger-steaks", "Hamburger Steaks")]
        self.db.record_plan(week_key, {}, meals)
        lp = self.db.get_last_planned("burger-steaks")
        self.assertEqual(lp, date(2026, 3, 20))

    def test_record_plan_replaces_existing_meals(self):
        week_key = "2026-03-16"
        meals_v1 = [make_meal("2026-03-20", "recipe-a", "Recipe A")]
        meals_v2 = [make_meal("2026-03-20", "recipe-b", "Recipe B")]
        self.db.record_plan(week_key, {}, meals_v1)
        self.db.record_plan(week_key, {}, meals_v2)
        recent = self.db.get_recent_meals(weeks=4)
        ids = [m["recipe_id"] for m in recent]
        self.assertNotIn("recipe-a", ids)
        self.assertIn("recipe-b", ids)

    def test_mark_plan_approved(self):
        week_key = "2026-03-16"
        self.db.record_plan(week_key, {}, [])
        self.db.mark_plan_approved(week_key)
        row = self.db._conn.execute(
            "SELECT approved FROM weekly_plans WHERE week_key = ?", (week_key,)
        ).fetchone()
        self.assertEqual(row["approved"], 1)

    # --- no-cook days ---

    def test_no_cook_day_stored(self):
        week_key = "2026-03-16"
        no_cook = {
            "meal_date":   "2026-03-18",
            "slot":        "dinner",
            "recipe_id":   None,
            "label":       "no cook",
            "tags":        [],
            "ingredients": [],
        }
        self.db.record_plan(week_key, {}, [no_cook])
        recent = self.db.get_recent_meals(weeks=4)
        self.assertEqual(recent[0]["label"], "no cook")
        self.assertIsNone(recent[0]["recipe_id"])

    # --- ingredient history ---

    def test_get_recent_ingredients(self):
        week_key = "2026-03-16"
        meals = [make_meal(
            "2026-03-20", "spiced-rice", "Spiced Rice",
            ingredients=[
                {"name": "basmati rice", "quantity": "1", "unit": "cup"},
                {"name": "chickpeas",    "quantity": "1", "unit": "can"},
            ]
        )]
        self.db.record_plan(week_key, {}, meals)
        ingredients = self.db.get_recent_ingredients(weeks=4)
        names = [i["name"] for i in ingredients]
        self.assertIn("basmati rice", names)
        self.assertIn("chickpeas", names)

    def test_recent_ingredients_excludes_old(self):
        old_date = (date.today() - timedelta(weeks=5)).isoformat()
        old_week = _week_key_for_date(date.today() - timedelta(weeks=5))
        meals = [make_meal(old_date, "old-recipe", "Old Recipe")]
        self.db.record_plan(old_week, {}, meals)
        ingredients = self.db.get_recent_ingredients(weeks=2)
        recipe_ids = [i["recipe_id"] for i in ingredients]
        self.assertNotIn("old-recipe", recipe_ids)

    # --- purge ---

    def test_purge_removes_old_plans(self):
        # Insert a plan from 5 months ago
        old_date = date.today() - timedelta(days=HISTORY_MONTHS * 30 + 10)
        old_week = _week_key_for_date(old_date)
        old_meal_date = old_date.isoformat()
        self.db.record_plan(old_week, {"old": True}, [
            make_meal(old_meal_date, "old-recipe", "Old Recipe")
        ])
        # Insert a recent plan
        recent_week = _week_key_for_date(date.today())
        recent_meal_date = date.today().isoformat()
        self.db.record_plan(recent_week, {"recent": True}, [
            make_meal(recent_meal_date, "new-recipe", "New Recipe")
        ])
        # Purge
        self.db.purge_now()
        self.assertIsNone(self.db.get_plan(old_week))
        self.assertIsNotNone(self.db.get_plan(recent_week))

    # --- summary ---

    def test_summary_structure(self):
        s = self.db.summary()
        self.assertIn("tracked_recipes", s)
        self.assertIn("weeks_stored", s)
        self.assertIn("meal_rows", s)


class TestExperimentRatings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db = StateStore(self.tmp.name)

    def tearDown(self):
        self.db.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_record_and_get_rating(self):
        self.db.record_experiment_rating("merguez", stars=4)
        ratings = self.db.get_experiment_ratings("merguez")
        self.assertEqual(len(ratings), 1)
        self.assertEqual(ratings[0]["stars"], 4)
        self.assertEqual(ratings[0]["promoted"], 0)

    def test_multiple_ratings_same_recipe(self):
        self.db.record_experiment_rating("merguez", stars=3)
        self.db.record_experiment_rating("merguez", stars=5)
        ratings = self.db.get_experiment_ratings("merguez")
        self.assertEqual(len(ratings), 2)

    def test_promote_experiment(self):
        self.db.record_experiment_rating("merguez", stars=5)
        self.db.promote_experiment("merguez")
        ratings = self.db.get_experiment_ratings("merguez")
        self.assertEqual(ratings[-1]["promoted"], 1)

    def test_get_ratings_unknown_recipe(self):
        self.assertEqual(self.db.get_experiment_ratings("unknown"), [])

    def test_new_tables_do_not_break_existing_db(self):
        # Simulate an existing db by opening, closing, reopening
        # The new tables are created with IF NOT EXISTS
        self.db.close()
        db2 = StateStore(self.tmp.name)
        summary = db2.summary()
        self.assertIn("tracked_recipes", summary)
        db2.close()


class TestMealNotes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db = StateStore(self.tmp.name)

    def tearDown(self):
        self.db.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_record_and_retrieve_out_note(self):
        self.db.record_meal_note(
            week_key="2026-03-23",
            meal_date="2026-03-24",
            note_type="out",
            note_text="Dinner at Sarah's",
        )
        # No retrieval method required in Phase 1 — just confirm no error

    def test_record_cook_note(self):
        self.db.record_meal_note(
            week_key="2026-03-23",
            meal_date="2026-03-29",
            note_type="cook",
            note_text="Waffles for dinner",
        )

    def test_invalid_note_type_raises(self):
        with self.assertRaises(Exception):
            self.db.record_meal_note(
                week_key="2026-03-23",
                meal_date="2026-03-24",
                note_type="invalid",
                note_text="Something",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
