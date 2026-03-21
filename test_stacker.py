"""
test_stacker.py

Run with: python -m pytest test_stacker.py -v
"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from stacker import (
    Stacker, StackingNote,
    normalise_ingredient,
    parse_quantity_ml,
    _natural_list,
    _qty_str,
    IngredientOccurrence,
)
from planner import WeekPlan, MealSlot


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MONDAY    = date(2026, 3, 23)
SATURDAY  = date(2026, 3, 21)
SUNDAY    = date(2026, 3, 22)
MONDAY    = date(2026, 3, 23)
TUESDAY   = date(2026, 3, 24)
WEDNESDAY = date(2026, 3, 25)
THURSDAY  = date(2026, 3, 26)


def make_slot(
    d: date,
    recipe_id: str,
    label: str,
    ingredients: list[dict],
    is_no_cook: bool = False,
) -> MealSlot:
    return MealSlot(
        date=d,
        slot="dinner",
        recipe_id=recipe_id if not is_no_cook else None,
        label=label,
        tags=[],
        ingredients=ingredients,
        is_no_cook=is_no_cook,
    )


def make_plan(slots: list[MealSlot]) -> WeekPlan:
    plan = WeekPlan(
        week_start_monday=MONDAY,
        week_key="2026-03-16",
        dinners=slots,
    )
    return plan


def ing(name: str, quantity: str = None, unit: str = None) -> dict:
    d = {"name": name}
    if quantity:
        d["quantity"] = quantity
    if unit:
        d["unit"] = unit
    return d


# ---------------------------------------------------------------------------
# Normalisation tests
# ---------------------------------------------------------------------------

class TestNormalisation(unittest.TestCase):

    def test_rice_variants(self):
        self.assertEqual(normalise_ingredient("basmati rice"), "rice")
        self.assertEqual(normalise_ingredient("jasmine rice"), "rice")
        self.assertEqual(normalise_ingredient("1 cup rice"), "rice")

    def test_pasta_variants(self):
        self.assertEqual(normalise_ingredient("penne pasta"), "pasta")
        self.assertEqual(normalise_ingredient("spaghetti"), "pasta")
        self.assertEqual(normalise_ingredient("fettuccine"), "pasta")

    def test_chickpea_variants(self):
        self.assertEqual(normalise_ingredient("chickpeas"), "chickpeas")
        self.assertEqual(normalise_ingredient("canned chickpeas"), "chickpeas")
        self.assertEqual(normalise_ingredient("garbanzo beans"), "chickpeas")

    def test_cream_cheese(self):
        self.assertEqual(normalise_ingredient("cream cheese, room temperature"), "cream cheese")
        self.assertEqual(normalise_ingredient("(8-ounce) packages cream cheese"), "cream cheese")

    def test_ignored_ingredients(self):
        self.assertIsNone(normalise_ingredient("salt"))
        self.assertIsNone(normalise_ingredient("kosher salt"))
        self.assertIsNone(normalise_ingredient("olive oil"))
        self.assertIsNone(normalise_ingredient("butter"))
        self.assertIsNone(normalise_ingredient("water"))

    def test_descriptor_stripping(self):
        self.assertEqual(normalise_ingredient("garlic, minced"), "garlic")
        self.assertEqual(normalise_ingredient("onion, diced"), "onion")
        self.assertEqual(normalise_ingredient("spinach, fresh"), "spinach")

    def test_unknown_ingredient_returns_cleaned_name(self):
        result = normalise_ingredient("harissa paste")
        self.assertEqual(result, "harissa paste")

    def test_empty_string_returns_none(self):
        self.assertIsNone(normalise_ingredient(""))


# ---------------------------------------------------------------------------
# Quantity parsing tests
# ---------------------------------------------------------------------------

class TestQuantityParsing(unittest.TestCase):

    def test_tablespoon(self):
        self.assertAlmostEqual(parse_quantity_ml("1", "tablespoon"), 15.0)
        self.assertAlmostEqual(parse_quantity_ml("2", "tbsp"), 30.0)

    def test_cup(self):
        self.assertAlmostEqual(parse_quantity_ml("1", "cup"), 240.0)
        self.assertAlmostEqual(parse_quantity_ml("1/2", "cup"), 120.0)

    def test_fraction(self):
        self.assertAlmostEqual(parse_quantity_ml("1/4", "cup"), 60.0)

    def test_mixed_number(self):
        self.assertAlmostEqual(parse_quantity_ml("1 1/2", "cups"), 360.0)

    def test_unicode_fraction(self):
        self.assertAlmostEqual(parse_quantity_ml("¼", "cup"), 60.0)
        self.assertAlmostEqual(parse_quantity_ml("½", "cup"), 120.0)

    def test_unknown_unit_returns_none(self):
        self.assertIsNone(parse_quantity_ml("1", "clove"))

    def test_missing_quantity_returns_none(self):
        self.assertIsNone(parse_quantity_ml(None, "cup"))

    def test_missing_unit_returns_none(self):
        self.assertIsNone(parse_quantity_ml("1", None))


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

class TestFormatting(unittest.TestCase):

    def test_natural_list_one(self):
        self.assertEqual(_natural_list(["Monday"]), "Monday")

    def test_natural_list_two(self):
        self.assertEqual(_natural_list(["Monday", "Wednesday"]), "Monday and Wednesday")

    def test_natural_list_three(self):
        self.assertEqual(
            _natural_list(["Monday", "Wednesday", "Thursday"]),
            "Monday, Wednesday, and Thursday"
        )

    def test_natural_list_deduplicates(self):
        self.assertEqual(_natural_list(["A", "A", "B"]), "A and B")


# ---------------------------------------------------------------------------
# BATCH note tests
# ---------------------------------------------------------------------------

class TestBatchNotes(unittest.TestCase):

    def test_batch_note_for_rice(self):
        slots = [
            make_slot(TUESDAY, "spiced-rice", "Spiced Rice",
                      [ing("basmati rice", "1", "cup")]),
            make_slot(THURSDAY, "rice-bowl", "Rice Bowl",
                      [ing("jasmine rice", "1", "cup")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        batch = [n for n in notes if n.kind == "BATCH" and n.canonical == "rice"]
        self.assertEqual(len(batch), 1)
        self.assertIn("double batch", batch[0].message)

    def test_batch_note_for_chickpeas(self):
        slots = [
            make_slot(MONDAY, "chick-a", "Chickpea Stew",
                      [ing("chickpeas", "1", "can")]),
            make_slot(WEDNESDAY, "chick-b", "Chickpea Salad",
                      [ing("canned chickpeas", "1", "can")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        batch = [n for n in notes if n.kind == "BATCH" and n.canonical == "chickpeas"]
        self.assertEqual(len(batch), 1)

    def test_no_batch_note_for_single_recipe(self):
        slots = [
            make_slot(TUESDAY, "rice-dish", "Rice Dish",
                      [ing("basmati rice", "1", "cup")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        batch = [n for n in notes if n.kind == "BATCH"]
        self.assertEqual(len(batch), 0)

    def test_no_batch_note_for_spices(self):
        # Cumin appears twice but should NOT generate a batch note
        slots = [
            make_slot(MONDAY, "dish-a", "Dish A", [ing("cumin", "1", "tsp")]),
            make_slot(TUESDAY, "dish-b", "Dish B", [ing("cumin", "1/2", "tsp")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        batch = [n for n in notes if n.kind == "BATCH"]
        self.assertEqual(len(batch), 0)

    def test_batch_note_names_earlier_meal_as_batch_day(self):
        slots = [
            make_slot(THURSDAY, "dish-b", "Thursday Dish",
                      [ing("rice", "1", "cup")]),
            make_slot(MONDAY, "dish-a", "Monday Dish",
                      [ing("basmati rice", "2", "cups")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        batch = [n for n in notes if n.kind == "BATCH" and n.canonical == "rice"]
        self.assertEqual(len(batch), 1)
        self.assertIn("Monday", batch[0].message)   # Monday is earlier

    def test_no_batch_for_ignored_ingredients(self):
        slots = [
            make_slot(MONDAY, "dish-a", "Dish A", [ing("salt", "1", "tsp")]),
            make_slot(TUESDAY, "dish-b", "Dish B", [ing("salt", "1", "tsp")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        batch = [n for n in notes if n.kind == "BATCH"]
        self.assertEqual(len(batch), 0)

    def test_no_cook_slots_excluded(self):
        slots = [
            make_slot(TUESDAY, None, "no cook", [], is_no_cook=True),
            make_slot(THURSDAY, "rice-dish", "Rice Dish", [ing("rice", "1", "cup")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        batch = [n for n in notes if n.kind == "BATCH"]
        self.assertEqual(len(batch), 0)


# ---------------------------------------------------------------------------
# REMNANT note tests
# ---------------------------------------------------------------------------

class TestRemnantNotes(unittest.TestCase):

    def test_remnant_note_for_small_cream_cheese_usage(self):
        # 1 tbsp cream cheese = 15ml; package = 225ml → should flag
        slots = [
            make_slot(MONDAY, "pasta-dish", "Tomato Pasta",
                      [ing("cream cheese", "1", "tbsp")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        remnant = [n for n in notes if n.kind == "REMNANT" and n.canonical == "cream cheese"]
        self.assertEqual(len(remnant), 1)
        self.assertIn("cream cheese", remnant[0].message)

    def test_no_remnant_for_large_cream_cheese_usage(self):
        # 8 oz = 240ml ≈ full package → no remnant note
        slots = [
            make_slot(MONDAY, "cheesecake", "Cheesecake",
                      [ing("cream cheese", "8", "oz")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        remnant = [n for n in notes if n.kind == "REMNANT" and n.canonical == "cream cheese"]
        self.assertEqual(len(remnant), 0)

    def test_remnant_note_for_two_recipes_using_cream_cheese(self):
        slots = [
            make_slot(MONDAY, "pasta", "Pasta",
                      [ing("cream cheese", "4", "oz")]),
            make_slot(WEDNESDAY, "dip", "Dip",
                      [ing("cream cheese", "2", "oz")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        remnant = [n for n in notes if n.kind == "REMNANT" and n.canonical == "cream cheese"]
        self.assertEqual(len(remnant), 1)
        self.assertIn("cream cheese", remnant[0].message)


# ---------------------------------------------------------------------------
# SHARED note tests
# ---------------------------------------------------------------------------

class TestSharedNotes(unittest.TestCase):

    def test_shared_note_for_canned_chickpeas(self):
        slots = [
            make_slot(TUESDAY, "dish-a", "Dish A",
                      [ing("canned chickpeas", "1", "can")]),
            make_slot(THURSDAY, "dish-b", "Dish B",
                      [ing("chickpeas", "1", "can")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        shared = [n for n in notes if n.kind == "SHARED" and n.canonical == "chickpeas"]
        self.assertEqual(len(shared), 1)
        self.assertIn("One purchase may cover both", shared[0].message)

    def test_no_shared_note_for_single_recipe(self):
        slots = [
            make_slot(TUESDAY, "dish-a", "Dish A",
                      [ing("chickpeas", "1", "can")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        shared = [n for n in notes if n.kind == "SHARED"]
        self.assertEqual(len(shared), 0)

    def test_shared_note_for_tomato_paste(self):
        slots = [
            make_slot(MONDAY, "dish-a", "Dish A",
                      [ing("tomato paste", "2", "tbsp")]),
            make_slot(WEDNESDAY, "dish-b", "Dish B",
                      [ing("tomato paste", "1", "tbsp")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        shared = [n for n in notes if n.kind == "SHARED" and n.canonical == "tomato paste"]
        self.assertEqual(len(shared), 1)


# ---------------------------------------------------------------------------
# Integration: real-ish recipe data
# ---------------------------------------------------------------------------

class TestRealRecipeData(unittest.TestCase):

    def test_creamy_tomato_pasta_and_chickpea_rice(self):
        """
        Replicates the actual recipes in the test library.
        Creamy Tomato Pasta uses cream cheese and tomato paste.
        Spiced Rice uses chickpeas and rice.
        If both are in the same week, no cross-recipe stacking between them,
        but stacker should not crash.
        """
        slots = [
            make_slot(TUESDAY, "creamy-tomato-pasta", "Creamy Tomato Pasta", [
                ing("penne pasta",       "1",   "lb"),
                ing("cream cheese",      "4",   "oz"),
                ing("tomato paste",      "4",   "tbsp"),
                ing("diced tomatoes",    "30",  "oz"),
                ing("parmesan",          "1/2", "cup"),
                ing("spinach",           "9",   "oz"),
            ]),
            make_slot(THURSDAY, "spiced-rice", "Spiced Rice", [
                ing("basmati rice",  "1",   "cup"),
                ing("chickpeas",     "1",   "can"),
                ing("olive oil",     "5",   "tablespoons"),
                ing("garam masala",  "1.5", "teaspoons"),
            ]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        # Should complete without error; no batch notes (different base ingredients)
        batch = [n for n in notes if n.kind == "BATCH"]
        self.assertEqual(len(batch), 0)

    def test_two_pasta_dishes_same_week(self):
        slots = [
            make_slot(MONDAY, "pasta-a", "Tomato Pasta",
                      [ing("penne", "1", "lb"), ing("tomato paste", "2", "tbsp")]),
            make_slot(WEDNESDAY, "pasta-b", "Veg Pasta",
                      [ing("spaghetti", "8", "oz"), ing("tomato paste", "1", "tbsp")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        # Both pasta dishes → BATCH note for pasta
        batch_pasta = [n for n in notes if n.kind == "BATCH" and n.canonical == "pasta"]
        self.assertEqual(len(batch_pasta), 1)
        # Shared tomato paste
        shared_tp = [n for n in notes if n.canonical == "tomato paste"]
        self.assertGreater(len(shared_tp), 0)

    def test_empty_plan_produces_no_notes(self):
        plan = make_plan([])
        notes = Stacker().analyse(plan)
        self.assertEqual(notes, [])

    def test_all_no_cook_plan_produces_no_notes(self):
        slots = [
            make_slot(d, None, "no cook", [], is_no_cook=True)
            for d in [MONDAY, TUESDAY, WEDNESDAY, THURSDAY]
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        self.assertEqual(notes, [])

    def test_notes_are_deduplicated(self):
        # Same ingredient three times — should produce only one BATCH note
        slots = [
            make_slot(MONDAY,    "dish-a", "Dish A", [ing("basmati rice", "1", "cup")]),
            make_slot(TUESDAY,   "dish-b", "Dish B", [ing("jasmine rice", "1", "cup")]),
            make_slot(WEDNESDAY, "dish-c", "Dish C", [ing("rice", "2", "cups")]),
        ]
        plan = make_plan(slots)
        notes = Stacker().analyse(plan)
        batch_rice = [n for n in notes if n.kind == "BATCH" and n.canonical == "rice"]
        self.assertEqual(len(batch_rice), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
