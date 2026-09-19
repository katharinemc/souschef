"""
test_main.py

Tests for main.py's amend/confirm CLI commands (cmd_amend, cmd_confirm).

These exercise the CLI wiring only: draft-plan lookup, the "no draft plan"
error exit, the acknowledgment shortcut, and persistence. The planning
internals (parse_reply_intents, apply_intents, Stacker, GroceryBuilder,
print_plan) are already covered by test_reply_handler.py / test_stacker.py /
test_grocery_builder.py and are patched out here. No Anthropic API calls are
made.

Run with: python -m pytest test_main.py -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

import main
from state_store import StateStore
from test_reply_handler import make_plan, make_recipes, WEEK_KEY


class CmdAmendConfirmTestCase(unittest.TestCase):
    """Shared setup: a temp DB with one draft plan already recorded."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        store = StateStore(self.tmp.name)
        self.plan = make_plan()
        store.record_plan(WEEK_KEY, self.plan.to_dict(), self.plan.to_state_meals())
        store.close()  # cmd_amend/cmd_confirm open their own connection

        self.flat = {
            "db_path": self.tmp.name,
            "recipe_dir": "recipes_yaml",
            "model": "test-model",
        }

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)

    def _reopen_store(self):
        return StateStore(self.tmp.name)

    def _empty_db_flat(self):
        """A second temp DB with no plan recorded, for the "no draft" case."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
        return {"db_path": tmp.name, "recipe_dir": "recipes_yaml", "model": "test-model"}


class TestCmdAmend(CmdAmendConfirmTestCase):

    def test_no_draft_plan_exits_with_error(self):
        args = SimpleNamespace(message="swap Tuesday for pasta")
        with self.assertRaises(SystemExit) as ctx:
            main.cmd_amend(args, {}, self._empty_db_flat())
        self.assertEqual(ctx.exception.code, 1)

    @patch("output_formatter.print_plan")
    @patch("grocery_builder.GroceryBuilder")
    @patch("stacker.Stacker")
    @patch("planner.load_recipes")
    @patch("reply_handler.apply_intents")
    @patch("reply_handler.parse_reply_intents")
    def test_amend_applies_change_and_persists(
        self,
        mock_parse,
        mock_apply,
        mock_load_recipes,
        mock_stacker_cls,
        mock_grocery_cls,
        mock_print_plan,
    ):
        mock_parse.return_value = [
            {"type": "swap_day", "day": "Tuesday", "constraint": "pasta"}
        ]
        modified = make_plan()
        mock_apply.return_value = (modified, ["Swapped Tuesday for a pasta dish"])
        mock_load_recipes.return_value = make_recipes()
        mock_stacker_cls.return_value.analyse.return_value = []
        mock_grocery = MagicMock()
        mock_grocery.likely_on_hand = []
        mock_grocery_cls.return_value.build.return_value = mock_grocery

        args = SimpleNamespace(message="swap Tuesday for pasta")
        main.cmd_amend(args, {}, self.flat)

        mock_parse.assert_called_once()
        mock_apply.assert_called_once()
        mock_print_plan.assert_called_once()

        store = self._reopen_store()
        try:
            week_key, _ = store.get_draft_plan()
            self.assertEqual(week_key, WEEK_KEY)
            self.assertFalse(store.is_plan_approved(WEEK_KEY))
        finally:
            store.close()

    @patch("reply_handler.apply_intents")
    @patch("reply_handler.parse_reply_intents")
    def test_amend_acknowledgment_confirms_instead_of_applying(
        self, mock_parse, mock_apply
    ):
        mock_parse.return_value = [{"type": "acknowledgment"}]

        args = SimpleNamespace(message="looks good")
        main.cmd_amend(args, {}, self.flat)

        mock_apply.assert_not_called()
        store = self._reopen_store()
        try:
            self.assertTrue(store.is_plan_approved(WEEK_KEY))
        finally:
            store.close()


class TestCmdConfirm(CmdAmendConfirmTestCase):

    def test_no_draft_plan_exits_with_error(self):
        args = SimpleNamespace()
        with self.assertRaises(SystemExit) as ctx:
            main.cmd_confirm(args, {}, self._empty_db_flat())
        self.assertEqual(ctx.exception.code, 1)

    def test_confirm_marks_draft_approved(self):
        args = SimpleNamespace()
        main.cmd_confirm(args, {}, self.flat)

        store = self._reopen_store()
        try:
            self.assertTrue(store.is_plan_approved(WEEK_KEY))
            self.assertIsNone(store.get_draft_plan())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
