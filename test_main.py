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


class TestFlattenConfig(unittest.TestCase):
    """
    ReplyHandler reads self.cfg["anthropic_model"], but config.yaml's
    anthropic section uses the key "model" (flattened to flat["model"]).
    flatten_config must populate both keys from whichever one the user set,
    or ReplyHandler silently ignores config.yaml and falls back to its own
    hardcoded DEFAULT_CONFIG value forever.
    """

    def test_anthropic_model_key_populated_from_model_key(self):
        cfg = {"anthropic": {"model": "claude-sonnet-5", "api_key": "x"}}
        flat = main.flatten_config(cfg)
        self.assertEqual(flat["model"], "claude-sonnet-5")
        self.assertEqual(flat["anthropic_model"], "claude-sonnet-5")

    def test_model_key_populated_from_anthropic_model_key(self):
        cfg = {"anthropic_model": "claude-sonnet-5"}
        flat = main.flatten_config(cfg)
        self.assertEqual(flat["model"], "claude-sonnet-5")
        self.assertEqual(flat["anthropic_model"], "claude-sonnet-5")

    def test_reply_handler_picks_up_configured_model(self):
        from reply_handler import ReplyHandler

        cfg = {"anthropic": {"model": "claude-sonnet-5", "api_key": "x"}}
        flat = main.flatten_config(cfg)
        handler = ReplyHandler(config=flat)
        self.assertEqual(handler.cfg["anthropic_model"], "claude-sonnet-5")


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

    @patch("reply_handler.apply_intents")
    @patch("reply_handler.parse_reply_intents")
    def test_amend_parse_failure_does_not_confirm(self, mock_parse, mock_apply):
        # A parse failure (e.g. the model 404'd) must not be treated as the
        # user approving the plan — that silently discards their message.
        mock_parse.return_value = [{"type": "parse_failed", "error": "model 404"}]

        args = SimpleNamespace(message="swap Tuesday for pasta")
        with self.assertRaises(SystemExit) as ctx:
            main.cmd_amend(args, {}, self.flat)
        self.assertEqual(ctx.exception.code, 1)

        mock_apply.assert_not_called()
        store = self._reopen_store()
        try:
            self.assertFalse(store.is_plan_approved(WEEK_KEY))
            week_key, _ = store.get_draft_plan()
            self.assertEqual(week_key, WEEK_KEY)
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


class TestCmdPlanEmail(unittest.TestCase):
    """
    `plan --email` (Phase 3): same planning pipeline as the terminal path,
    but the plan goes out by email and the reply loop is skipped — the
    user's email reply, processed by `main.py reply`, replaces it.
    Calendar, planner, stacker, grocery builder and Gmail are all patched.
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.addCleanup(lambda: Path(self.tmp.name).unlink(missing_ok=True))
        self.plan = make_plan()
        self.flat = {
            "db_path": self.tmp.name,
            "recipe_dir": "recipes_yaml",
            "model": "test-model",
            "to_address": "me@example.com",
        }

    def _run(self, dry_run=False, send_side_effect=None):
        reader = MagicMock()
        reader.next_planning_monday.return_value = self.plan.week_start_monday
        planner = MagicMock()
        planner.plan_week.return_value = self.plan
        sender = MagicMock()
        sender.send_plan.side_effect = send_side_effect
        self.grocery = MagicMock()
        self.notes = [MagicMock()]

        args = SimpleNamespace(dry_run=dry_run, legacy=False, email=True)
        with patch("calendar_reader.CalendarReader", return_value=reader), \
             patch("agentic_planner.AgenticPlanner", return_value=planner), \
             patch("stacker.Stacker") as stacker_cls, \
             patch("grocery_builder.GroceryBuilder") as gb_cls, \
             patch("output_formatter.print_plan") as self.print_plan, \
             patch("email_sender.EmailSender", return_value=sender) as self.sender_cls, \
             patch.object(main, "_review_last_week") as self.review, \
             patch("builtins.input") as self.input, \
             patch("builtins.print") as self.print_:
            stacker_cls.return_value.analyse.return_value = self.notes
            gb_cls.return_value.build.return_value = self.grocery
            main.cmd_plan(args, {}, self.flat)
        return sender

    def _printed(self):
        return " ".join(" ".join(map(str, c.args)) for c in self.print_.call_args_list)

    def _stored_status(self):
        store = StateStore(self.tmp.name)
        try:
            return store.get_plan(self.plan.week_key), store.is_plan_approved(self.plan.week_key)
        finally:
            store.close()

    def test_sends_plan_saves_draft_and_skips_reply_loop(self):
        sender = self._run()
        sender.send_plan.assert_called_once_with(self.plan, self.grocery, self.notes)
        self.sender_cls.assert_called_once_with(config=self.flat, dry_run=False)
        plan_dict, approved = self._stored_status()
        self.assertIsNotNone(plan_dict)
        self.assertFalse(approved)          # approval comes from the email reply
        self.input.assert_not_called()      # no terminal reply loop
        self.print_plan.assert_not_called() # emailed instead of printed
        self.assertIn("me@example.com", self._printed())
        self.assertIn("reply --once", self._printed())

    def test_still_runs_last_week_review(self):
        self._run()
        self.review.assert_called_once()

    def test_dry_run_prints_email_without_sending_or_saving(self):
        sender = self._run(dry_run=True)
        self.sender_cls.assert_called_once_with(config=self.flat, dry_run=True)
        sender.send_plan.assert_called_once()   # dry-run sender prints instead of sending
        plan_dict, _ = self._stored_status()
        self.assertIsNone(plan_dict)

    def test_send_failure_keeps_draft_and_explains(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(send_side_effect=RuntimeError("403 insufficient scopes"))
        self.assertEqual(cm.exception.code, 1)
        plan_dict, approved = self._stored_status()
        self.assertIsNotNone(plan_dict)     # draft survives so it can be retried/amended
        self.assertFalse(approved)
        out = self._printed()
        self.assertIn("403 insufficient scopes", out)
        self.assertIn("confirm", out)


if __name__ == "__main__":
    unittest.main()
