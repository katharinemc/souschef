"""
test_grocery_builder.py

Run with: python -m pytest test_grocery_builder.py -v
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from grocery_builder import (
    GroceryBuilder, GroceryList, GroceryItem,
    categorise, is_perishable, _format_quantity,
    CATEGORY_ORDER,
)
from planner import WeekPlan, MealSlot
from state_store import StateStore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MONDAY    = date(2026, 3, 23)
TUESDAY   = date(2026, 3, 24)
WEDNESDAY = date(2026, 3, 25)
THURSDAY  = date(2026, 3, 26)
FRIDAY    = date(2026, 3, 27)


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
        recipe_id=None if is_no_cook else recipe_id,
        label=label,
        tags=[],
        ingredients=ingredients,
        is_no_cook=is_no_cook,
    )


def make_plan(slots: list[MealSlot]) -> WeekPlan:
    return WeekPlan(
        week_start_monday=MONDAY,
        week_key="2026-03-23",
        dinners=slots,
    )


def ing(name: str, quantity: str = None, unit: str = None) -> dict:
    d = {"name": name}
    if quantity:
        d["quantity"] = quantity
    if unit:
        d["unit"] = unit
    return d


def build(slots, store=None):
    return GroceryBuilder(store=store).build(make_plan(slots))


# ---------------------------------------------------------------------------
# Categorisation tests
# ---------------------------------------------------------------------------

class TestCategorisation(unittest.TestCase):

    def test_onion_is_produce(self):
        self.assertEqual(categorise("onion"), "produce")

    def test_ground_beef_is_protein(self):
        self.assertEqual(categorise("ground beef"), "protein")

    def test_cream_cheese_is_dairy(self):
        self.assertEqual(categorise("cream cheese"), "dairy")

    def test_rice_is_pantry(self):
        self.assertEqual(categorise("basmati rice"), "pantry")

    def test_chickpeas_is_pantry(self):
        self.assertEqual(categorise("chickpeas"), "pantry")

    def test_tomato_paste_is_pantry(self):
        self.assertEqual(categorise("tomato paste"), "pantry")

    def test_frozen_item(self):
        self.assertEqual(categorise("frozen peas"), "frozen")

    def test_harissa_is_pantry(self):
        self.assertEqual(categorise("harissa paste"), "pantry")

    def test_fresh_herb_is_produce(self):
        self.assertEqual(categorise("fresh cilantro"), "produce")

    def test_parmesan_is_dairy(self):
        self.assertEqual(categorise("parmesan cheese"), "dairy")


# ---------------------------------------------------------------------------
# Perishability tests
# ---------------------------------------------------------------------------

class TestPerishability(unittest.TestCase):

    def test_ground_beef_is_perishable(self):
        self.assertTrue(is_perishable("ground beef"))

    def test_fresh_spinach_is_perishable(self):
        self.assertTrue(is_perishable("fresh spinach"))

    def test_chicken_is_perishable(self):
        self.assertTrue(is_perishable("chicken breast"))

    def test_milk_is_perishable(self):
        self.assertTrue(is_perishable("whole milk"))

    def test_rice_is_not_perishable(self):
        self.assertFalse(is_perishable("basmati rice"))

    def test_canned_chickpeas_not_perishable(self):
        self.assertFalse(is_perishable("canned chickpeas"))

    def test_tomato_paste_not_perishable(self):
        self.assertFalse(is_perishable("tomato paste"))

    def test_cream_cheese_not_perishable(self):
        self.assertFalse(is_perishable("cream cheese"))


# ---------------------------------------------------------------------------
# Quantity formatting
# ---------------------------------------------------------------------------

class TestFormatQuantity(unittest.TestCase):

    def test_whole_number(self):
        self.assertEqual(_format_quantity(2.0), "2")

    def test_decimal(self):
        self.assertEqual(_format_quantity(1.5), "1.5")

    def test_trailing_zero_stripped(self):
        self.assertEqual(_format_quantity(1.50), "1.5")

    def test_small_fraction(self):
        self.assertEqual(_format_quantity(0.25), "0.25")


# ---------------------------------------------------------------------------
# Basic list building
# ---------------------------------------------------------------------------

class TestGroceryListBuilding(unittest.TestCase):

    def test_single_slot_ingredients_appear(self):
        slots = [make_slot(MONDAY, "pasta", "Tomato Pasta", [
            ing("penne pasta",  "1",   "lb"),
            ing("cream cheese", "4",   "oz"),
            ing("onion",        "2"),
        ])]
        gl = build(slots)
        names = [i.name.lower() for i in gl.all_items]
        self.assertTrue(any("penne" in n or "pasta" in n for n in names))
        self.assertTrue(any("cream cheese" in n for n in names))
        self.assertTrue(any("onion" in n for n in names))

    def test_no_cook_slots_excluded(self):
        slots = [
            make_slot(MONDAY, None, "no cook", [
                ing("steak", "1", "lb"),
            ], is_no_cook=True),
            make_slot(TUESDAY, "pasta", "Pasta", [
                ing("penne", "8", "oz"),
            ]),
        ]
        gl = build(slots)
        names = [i.name.lower() for i in gl.all_items]
        self.assertFalse(any("steak" in n for n in names))
        self.assertTrue(any("penne" in n for n in names))

    def test_items_grouped_by_category(self):
        slots = [make_slot(MONDAY, "dish", "Dish", [
            ing("spinach",      "1", "bag"),
            ing("ground beef",  "1", "lb"),
            ing("cream cheese", "4", "oz"),
            ing("basmati rice", "1", "cup"),
        ])]
        gl = build(slots)
        self.assertTrue(len(gl.category_items("produce")) > 0)
        self.assertTrue(len(gl.category_items("protein")) > 0)
        self.assertTrue(len(gl.category_items("dairy")) > 0)
        self.assertTrue(len(gl.category_items("pantry")) > 0)

    def test_all_categories_present_in_order(self):
        gl = build([])
        # items_by_category keys should follow CATEGORY_ORDER
        for cat in gl.items_by_category:
            self.assertIn(cat, CATEGORY_ORDER)

    def test_empty_plan_produces_empty_list(self):
        gl = build([])
        self.assertEqual(gl.all_items, [])

    def test_summary_returns_string(self):
        slots = [make_slot(MONDAY, "dish", "Dish", [ing("onion", "1")])]
        gl = build(slots)
        s = gl.summary()
        self.assertIsInstance(s, str)
        self.assertIn("2026-03-23", s)


# ---------------------------------------------------------------------------
# Deduplication and quantity combining
# ---------------------------------------------------------------------------

class TestAggregation(unittest.TestCase):

    def test_same_ingredient_two_recipes_combined(self):
        slots = [
            make_slot(MONDAY, "dish-a", "Dish A", [ing("basmati rice", "1", "cup")]),
            make_slot(TUESDAY, "dish-b", "Dish B", [ing("jasmine rice", "1", "cup")]),
        ]
        gl = build(slots)
        rice_items = [i for i in gl.all_items if "rice" in i.name.lower()]
        # Should be ONE combined rice item
        self.assertEqual(len(rice_items), 1)

    def test_combined_quantity_summed(self):
        slots = [
            make_slot(MONDAY,  "a", "A", [ing("onion", "1", "cup")]),
            make_slot(TUESDAY, "b", "B", [ing("onion", "2", "cups")]),
        ]
        gl = build(slots)
        onion = next((i for i in gl.all_items if "onion" in i.name.lower()), None)
        self.assertIsNotNone(onion)
        self.assertEqual(onion.quantity, "3")
        self.assertIn("cup", onion.unit.lower())

    def test_mixed_units_flagged(self):
        slots = [
            make_slot(MONDAY,  "a", "A", [ing("garlic", "2", "cloves")]),
            make_slot(TUESDAY, "b", "B", [ing("garlic", "1", "tbsp")]),
        ]
        gl = build(slots)
        garlic = next((i for i in gl.all_items if "garlic" in i.name.lower()), None)
        self.assertIsNotNone(garlic)
        self.assertIsNotNone(garlic.quantity_note)
        self.assertIn("mixed", garlic.quantity_note.lower())

    def test_no_unit_flagged_with_note(self):
        slots = [
            make_slot(MONDAY,  "a", "A", [ing("shallot", "2")]),
            make_slot(TUESDAY, "b", "B", [ing("shallot", "1")]),
        ]
        gl = build(slots)
        shallot = next((i for i in gl.all_items if "shallot" in i.name.lower()), None)
        self.assertIsNotNone(shallot)
        # No unit — should get a note rather than crashing
        self.assertIsNotNone(shallot.quantity_note)

    def test_best_name_is_most_descriptive(self):
        # "ground beef, 85% lean" is more descriptive than "ground beef"
        slots = [
            make_slot(MONDAY,  "a", "A", [ing("ground beef")]),
            make_slot(TUESDAY, "b", "B", [ing("ground beef, 85 percent lean")]),
        ]
        gl = build(slots)
        beef = next((i for i in gl.all_items if "beef" in i.name.lower()), None)
        self.assertIsNotNone(beef)
        self.assertGreater(len(beef.name), len("ground beef"))

    def test_recipes_list_populated(self):
        slots = [
            make_slot(MONDAY,  "dish-a", "Tomato Pasta",  [ing("onion", "1")]),
            make_slot(TUESDAY, "dish-b", "Chickpea Stew", [ing("onion", "2")]),
        ]
        gl = build(slots)
        onion = next((i for i in gl.all_items if "onion" in i.name.lower()), None)
        self.assertIsNotNone(onion)
        self.assertIn("Tomato Pasta",  onion.recipes)
        self.assertIn("Chickpea Stew", onion.recipes)


# ---------------------------------------------------------------------------
# Likely on hand
# ---------------------------------------------------------------------------

class TestLikelyOnHand(unittest.TestCase):

    def _store_with_prior_ingredients(self, ingredient_names: list[str]) -> StateStore:
        """Create a store with prior-week meals containing the given ingredients."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        store = StateStore(db_path)
        last_week = (date.today() - timedelta(days=5)).isoformat()
        prior_week_key = (date.today() - timedelta(days=7)).isoformat()
        meals = [{
            "meal_date":   last_week,
            "slot":        "dinner",
            "recipe_id":   "prior-recipe",
            "label":       "Prior Recipe",
            "tags":        [],
            "ingredients": [{"name": n} for n in ingredient_names],
        }]
        store.record_plan(prior_week_key, {}, meals)
        return store

    def test_pantry_item_from_prior_week_flagged(self):
        store = self._store_with_prior_ingredients(["basmati rice", "tomato paste"])
        slots = [make_slot(MONDAY, "dish", "Dish", [
            ing("basmati rice", "1", "cup"),
        ])]
        gl = build(slots, store=store)
        on_hand_names = [i.name.lower() for i in gl.likely_on_hand]
        self.assertTrue(any("rice" in n for n in on_hand_names))

    def test_perishable_from_prior_week_not_flagged(self):
        store = self._store_with_prior_ingredients(["ground beef"])
        slots = [make_slot(MONDAY, "dish", "Dish", [
            ing("ground beef", "1", "lb"),
        ])]
        gl = build(slots, store=store)
        on_hand_names = [i.name.lower() for i in gl.likely_on_hand]
        self.assertFalse(any("beef" in n for n in on_hand_names))

    def test_fresh_herbs_not_flagged_on_hand(self):
        store = self._store_with_prior_ingredients(["fresh cilantro"])
        slots = [make_slot(MONDAY, "dish", "Dish", [
            ing("fresh cilantro", "1", "bunch"),
        ])]
        gl = build(slots, store=store)
        on_hand_names = [i.name.lower() for i in gl.likely_on_hand]
        self.assertFalse(any("cilantro" in n for n in on_hand_names))

    def test_no_store_no_on_hand_items(self):
        slots = [make_slot(MONDAY, "dish", "Dish", [
            ing("basmati rice", "1", "cup"),
        ])]
        gl = build(slots, store=None)
        self.assertEqual(gl.likely_on_hand, [])

    def test_on_hand_items_not_in_main_list(self):
        store = self._store_with_prior_ingredients(["basmati rice"])
        slots = [make_slot(MONDAY, "dish", "Dish", [
            ing("basmati rice", "1", "cup"),
        ])]
        gl = build(slots, store=store)
        main_names = [i.name.lower() for i in gl.all_items]
        # Rice should be in on_hand, NOT in the main list
        self.assertFalse(any("rice" in n for n in main_names))
        on_hand_names = [i.name.lower() for i in gl.likely_on_hand]
        self.assertTrue(any("rice" in n for n in on_hand_names))


# ---------------------------------------------------------------------------
# Integration: real recipe data
# ---------------------------------------------------------------------------

class TestRealRecipeData(unittest.TestCase):

    def test_hamburger_steaks_ingredients(self):
        slots = [make_slot(MONDAY, "hamburger-steaks", "Hamburger Steaks with Onion Gravy", [
            ing("85 percent lean ground beef",    "1 1/2", "pounds"),
            ing("panko bread crumb",              "1/2",   "cup"),
            ing("Lipton Onion Soup and Dip Mix",  "2",     "tablespoons"),
            ing("pepper",                         "1/2",   "teaspoon"),
            ing("unsalted butter divided",        "3",     "tablespoons"),
            ing("onion halved and sliced thin",   "1"),
            ing("all-purpose flour",              "1 1/2", "tablespoons"),
            ing("beef broth",                     "1 1/2", "cups"),
            ing("minced fresh chives",            "1",     "tablespoon"),
        ])]
        gl = build(slots)
        names = [i.name.lower() for i in gl.all_items]
        # Ground beef should appear in protein
        protein_names = [i.name.lower() for i in gl.category_items("protein")]
        self.assertTrue(any("beef" in n for n in protein_names))

    def test_two_pasta_dishes_combine_pasta(self):
        slots = [
            make_slot(MONDAY, "pasta-a", "Tomato Pasta", [
                ing("penne pasta", "1", "lb"),
                ing("cream cheese", "4", "oz"),
                ing("onion", "2"),
            ]),
            make_slot(WEDNESDAY, "pasta-b", "IP Pasta", [
                ing("campanelle pasta", "8", "oz"),
                ing("onion", "1", "small"),
            ]),
        ]
        gl = build(slots)
        # Both pasta items → combined under pasta canonical
        pantry = gl.category_items("pantry")
        pasta_items = [i for i in pantry if "pasta" in i.name.lower()]
        self.assertEqual(len(pasta_items), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
