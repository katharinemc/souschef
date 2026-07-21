"""Tests for cart_filler.py and WeekPlan.from_dict / StateStore.is_plan_approved."""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from grocery_builder import GroceryItem, GroceryList
from planner import MealSlot, WeekPlan
from state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_grocery(*names) -> GroceryList:
    items = [
        GroceryItem(name=n, canonical=n.lower(), category="produce", quantity="1", unit="lb")
        for n in names
    ]
    gl = GroceryList(week_start_monday=date(2026, 6, 2))
    gl.items_by_category = {"produce": items}
    return gl


def _make_plan(week_key="2026-06-02") -> WeekPlan:
    monday = date.fromisoformat(week_key)
    plan = WeekPlan(week_start_monday=monday, week_key=week_key)
    plan.dinners.append(MealSlot(
        date=monday, slot="dinner",
        recipe_id="hamburger-steaks", label="Hamburger Steaks",
        tags=["onRotation"],
        ingredients=[{"name": "ground beef", "quantity": "1.5", "unit": "lb"}],
    ))
    return plan


# ---------------------------------------------------------------------------
# WeekPlan.from_dict roundtrip
# ---------------------------------------------------------------------------

class TestWeekPlanFromDict(unittest.TestCase):

    def test_roundtrip_preserves_dinners(self):
        plan = _make_plan()
        restored = WeekPlan.from_dict(plan.to_dict())
        self.assertEqual(restored.week_key, plan.week_key)
        self.assertEqual(len(restored.dinners), 1)
        slot = restored.dinners[0]
        self.assertEqual(slot.recipe_id, "hamburger-steaks")
        self.assertEqual(slot.tags, ["onRotation"])
        self.assertEqual(slot.ingredients, [{"name": "ground beef", "quantity": "1.5", "unit": "lb"}])

    def test_roundtrip_no_cook_slot(self):
        plan = _make_plan()
        plan.dinners.append(MealSlot(
            date=date(2026, 6, 3), slot="dinner",
            recipe_id=None, label="no cook",
            tags=[], ingredients=[], is_no_cook=True,
        ))
        restored = WeekPlan.from_dict(plan.to_dict())
        self.assertTrue(restored.dinners[1].is_no_cook)
        self.assertIsNone(restored.dinners[1].recipe_id)

    def test_roundtrip_with_lunch(self):
        plan = _make_plan()
        plan.lunch = MealSlot(
            date=plan.week_start_monday, slot="lunch",
            recipe_id="quinoa-bowl", label="Quinoa Bowl",
            tags=[], ingredients=[],
        )
        restored = WeekPlan.from_dict(plan.to_dict())
        self.assertIsNotNone(restored.lunch)
        self.assertEqual(restored.lunch.recipe_id, "quinoa-bowl")

    def test_roundtrip_empty_plan(self):
        plan = WeekPlan(week_start_monday=date(2026, 6, 2), week_key="2026-06-02")
        restored = WeekPlan.from_dict(plan.to_dict())
        self.assertEqual(restored.dinners, [])
        self.assertIsNone(restored.lunch)


# ---------------------------------------------------------------------------
# StateStore.is_plan_approved
# ---------------------------------------------------------------------------

class TestIsPlanApproved(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = StateStore(self.tmp.name)

    def tearDown(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _save(self, week_key="2026-06-02"):
        plan = _make_plan(week_key)
        self.store.record_plan(week_key, plan.to_dict(), plan.to_state_meals())

    def test_false_when_no_plan(self):
        self.assertFalse(self.store.is_plan_approved("2026-06-02"))

    def test_false_when_saved_but_not_approved(self):
        self._save()
        self.assertFalse(self.store.is_plan_approved("2026-06-02"))

    def test_true_after_approval(self):
        self._save()
        self.store.mark_plan_approved("2026-06-02")
        self.assertTrue(self.store.is_plan_approved("2026-06-02"))


# ---------------------------------------------------------------------------
# Pantry staple filtering
# ---------------------------------------------------------------------------

class TestPantryStaple(unittest.TestCase):

    def test_salt_variants(self):
        from cart_filler import _is_pantry_staple
        for name in ["salt", "kosher salt", "table salt", "sea salt", "seasoned salt"]:
            with self.subTest(name=name):
                self.assertTrue(_is_pantry_staple(name))

    def test_spices(self):
        from cart_filler import _is_pantry_staple
        for name in ["garam masala", "cumin", "paprika", "turmeric", "cinnamon", "black pepper"]:
            with self.subTest(name=name):
                self.assertTrue(_is_pantry_staple(name))

    def test_non_staples_pass_through(self):
        from cart_filler import _is_pantry_staple
        for name in ["ground beef", "onion", "basmati rice", "plain yogurt", "chicken breast"]:
            with self.subTest(name=name):
                self.assertFalse(_is_pantry_staple(name))


# ---------------------------------------------------------------------------
# CartFiller helpers
# ---------------------------------------------------------------------------

class TestCartFillerHelpers(unittest.TestCase):

    def _make_filler(self, *names):
        from cart_filler import CartFiller
        grocery = _make_grocery(*names)
        cfg = {"model": "claude-test", "cdp_url": "http://localhost:9222"}
        return CartFiller(config=cfg, grocery=grocery)

    def test_split_items_separates_staples(self):
        filler = self._make_filler("ground beef", "onion", "salt", "cumin")
        buy, staples = filler._split_items()
        self.assertEqual([i.name for i in buy], ["ground beef", "onion"])
        self.assertIn("salt", staples)
        self.assertIn("cumin", staples)

    def test_split_items_all_staples_returns_empty_buy(self):
        filler = self._make_filler("salt", "pepper", "garam masala")
        buy, staples = filler._split_items()
        self.assertEqual(buy, [])
        self.assertEqual(len(staples), 3)

    def test_build_user_prompt_excludes_staples(self):
        filler = self._make_filler("ground beef", "onion")
        buy, _ = filler._split_items()
        prompt = filler._build_user_prompt(buy)
        self.assertIn("ground beef", prompt)
        self.assertIn("onion", prompt)


# ---------------------------------------------------------------------------
# CartFiller agent loop (mocked Anthropic + mocked Playwright page)
# ---------------------------------------------------------------------------

def _tool_use(name: str, inp: dict, tid="tu_1"):
    b = MagicMock()
    b.type = "tool_use"
    b.name = name
    b.input = inp
    b.id = tid
    return b


def _finish_input(added=None, needs_review=None, skipped=None, not_found=None):
    return {
        "added":        added or [],
        "needs_review": needs_review or [],
        "skipped":      skipped or [],
        "not_found":    not_found or [],
    }


class TestCartFillerAgentLoop(unittest.IsolatedAsyncioTestCase):

    def _make_filler(self, *names):
        from cart_filler import CartFiller
        grocery = _make_grocery(*(names or ("ground beef", "onion")))
        cfg = {"model": "claude-test", "cdp_url": "http://localhost:9222"}
        f = CartFiller(config=cfg, grocery=grocery)
        f.max_iterations = 10
        return f

    def _mock_page(self):
        page = AsyncMock()
        page.goto = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        page.query_selector = AsyncMock(return_value=None)
        page.query_selector_all = AsyncMock(return_value=[])
        page.evaluate = AsyncMock(return_value=[
            {"index": 0, "name": "Great Value Ground Beef 1lb", "price": "$5.98", "size": "1 lb", "badge": ""},
            {"index": 1, "name": "80/20 Ground Beef 2lb", "price": "$9.47", "size": "2 lb", "badge": ""},
        ])
        page.inner_text = AsyncMock(return_value="")
        return page

    def _api_response(self, *blocks):
        r = MagicMock()
        r.content = list(blocks)
        return r

    async def test_finish_cart_on_first_turn(self):
        from cart_filler import CartFiller
        filler = self._make_filler()
        page = self._mock_page()

        finish = _tool_use("finish_cart", _finish_input(
            added=[{"name": "ground beef", "walmart_name": "Great Value Ground Beef 1lb", "status": "added"}],
        ))
        with patch.object(filler.client.messages, "create", return_value=self._api_response(finish)):
            result = await filler._run_agent_loop(page)

        self.assertEqual(len(result.added), 1)
        self.assertEqual(result.added[0].walmart_name, "Great Value Ground Beef 1lb")

    async def test_search_then_add_then_finish(self):
        from cart_filler import CartFiller
        filler = self._make_filler()
        page = self._mock_page()

        search_call  = _tool_use("search_walmart", {"query": "ground beef", "quantity": "1.5 lb"}, "tu_1")
        add_call     = _tool_use("add_to_cart", {"result_index": 0}, "tu_2")
        finish_call  = _tool_use("finish_cart", _finish_input(
            added=[{"name": "ground beef", "walmart_name": "Great Value Ground Beef 1lb", "status": "added"}],
        ), "tu_3")

        responses = iter([
            self._api_response(search_call),
            self._api_response(add_call),
            self._api_response(finish_call),
        ])

        with patch.object(filler.client.messages, "create", side_effect=lambda **kw: next(responses)):
            result = await filler._run_agent_loop(page)

        self.assertEqual(len(result.added), 1)
        self.assertEqual(result.total_processed, 1)

    async def test_max_iterations_returns_empty(self):
        from cart_filler import CartFiller
        filler = self._make_filler()
        filler.max_iterations = 3
        page = self._mock_page()

        loop_call = _tool_use("search_walmart", {"query": "ground beef"})
        with patch.object(filler.client.messages, "create", return_value=self._api_response(loop_call)):
            result = await filler._run_agent_loop(page)

        self.assertEqual(result.total_processed, 0)

    async def test_all_staples_skips_api(self):
        from cart_filler import CartFiller
        filler = self._make_filler("salt", "pepper", "garam masala")
        page = self._mock_page()

        with patch.object(filler.client.messages, "create") as mock_api:
            result = await filler._run_agent_loop(page)

        mock_api.assert_not_called()
        self.assertEqual(len(result.pantry_staples), 3)
        self.assertEqual(result.total_processed, 0)

    async def test_search_uses_last_results_for_add(self):
        """_add_to_cart uses self._last_results set by _search_walmart."""
        from cart_filler import CartFiller
        filler = self._make_filler()
        page = self._mock_page()

        # Simulate a search that populates _last_results
        await filler._search_walmart(page, "ground beef")
        self.assertEqual(len(filler._last_results), 2)

        # Add index 0 — page has no tiles so we expect the "page may have changed" error
        result = await filler._add_to_cart(page, 0)
        self.assertIn("Could not locate tile", result)

    async def test_add_to_cart_out_of_range(self):
        from cart_filler import CartFiller
        filler = self._make_filler()
        page = self._mock_page()
        result = await filler._add_to_cart(page, 99)
        self.assertIn("no result at index", result)


# ---------------------------------------------------------------------------
# cmd_cart integration tests (mock CartFiller.fill)
# ---------------------------------------------------------------------------

class TestCmdCart(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = StateStore(self.tmp.name)
        self.week_key = "2026-06-02"

    def tearDown(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _save_and_approve(self):
        plan = _make_plan(self.week_key)
        self.store.record_plan(self.week_key, plan.to_dict(), plan.to_state_meals())
        self.store.mark_plan_approved(self.week_key)

    def _run(self, dry_run=True, walmart_enabled=True):
        import argparse
        from main import cmd_cart
        args = argparse.Namespace(week=self.week_key, dry_run=dry_run, verbose=False)
        cfg = {
            "walmart": {
                "enabled": walmart_enabled,
                "cdp_url": "http://localhost:9222",
            },
        }
        flat = {"db_path": self.tmp.name, "recipe_dir": "recipes_yaml", "model": "claude-test"}
        return cmd_cart(args, cfg, flat)

    def test_disabled_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(walmart_enabled=False)
        self.assertEqual(ctx.exception.code, 1)

    def test_missing_plan_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run()
        self.assertEqual(ctx.exception.code, 1)

    def test_unapproved_plan_exits(self):
        plan = _make_plan(self.week_key)
        self.store.record_plan(self.week_key, plan.to_dict(), plan.to_state_meals())
        with self.assertRaises(SystemExit) as ctx:
            self._run()
        self.assertEqual(ctx.exception.code, 1)

    def test_dry_run_no_filler(self):
        self._save_and_approve()
        with patch("cart_filler.CartFiller") as mock_cls:
            import io
            from contextlib import redirect_stdout
            with redirect_stdout(io.StringIO()):
                self._run(dry_run=True)
            mock_cls.assert_not_called()


if __name__ == "__main__":
    unittest.main()
