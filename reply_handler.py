"""
reply_handler.py

Polls Gmail for replies to the Thursday meal plan email and triggers
re-planning when the user sends feedback.

Polling behavior:
  - Runs hourly on Thursdays only
  - Stops after receiving any reply (re-plan request or acknowledgment)
  - "Okay thanks" / acknowledgment patterns → stop polling, no re-plan
  - Any other reply → parse intent via LLM, re-plan, re-send

Reply intent parsing:
  The user's reply is passed to the Claude API to extract structured
  swap/modify intents. The LLM returns a JSON list of actions, which
  the handler applies to the existing WeekPlan before re-running
  the stacker and grocery builder and re-sending.

Supported intent types (v1):
  - swap_day:       "Swap Tuesday for a pasta dish"
  - remove_on_hand: "The cream cheese is already gone"
  - add_note:       "We're out of chickpeas this week"
  - no_cook_day:    "Nothing on Wednesday actually, make it a cook night"

Usage (run directly for manual trigger):
  python reply_handler.py --week 2026-03-23 --config config.yaml
"""

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore

from calendar_reader import CalendarReader
from email_sender import EmailSender
from grocery_builder import GroceryBuilder
from planner import Planner, WeekPlan, load_recipes, load_lunches
from stacker import Stacker
from state_store import StateStore

log = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "credentials_path": "credentials.json",
    "token_path":       "token.json",
    "to_address":       None,
    "recipe_dir":       "recipes_yaml",
    "lunch_file":       "lunches.yaml",
    "db_path":          "meal_planner.db",
    "timezone":         "America/New_York",
    "poll_interval_seconds": 3600,    # 1 hour
    "anthropic_model":  "claude-sonnet-4-20250514",
}


# ---------------------------------------------------------------------------
# Intent parsing via Claude API
# ---------------------------------------------------------------------------

INTENT_SYSTEM_PROMPT = """You are a meal plan assistant. The user has replied to their weekly meal plan with feedback.

Extract all actions from their reply as a JSON array. Each action has a "type" and relevant fields.

Supported action types:

1. swap_day — user wants to change a specific day's meal
   {"type": "swap_day", "day": "Tuesday", "constraint": "pasta"}

2. remove_on_hand — user says an ingredient is not actually on hand
   {"type": "remove_on_hand", "ingredient": "cream cheese"}

3. force_no_cook — user wants a day to be no-cook / leftovers
   {"type": "force_no_cook", "day": "Thursday"}

4. force_cook — user wants a no-cook day to become a cook night
   {"type": "force_cook", "day": "Wednesday"}

5. skip_experiment — user wants to skip the experiment recipe this week
   {"type": "skip_experiment"}

6. assign_note_out — user is eating out / food provided (not a cook night, no grocery items)
   {"type": "assign_note_out", "day": "Tuesday", "note_text": "Dinner at Sarah's"}

7. assign_note_cook — user is cooking something not in the library (counts as cook night)
   {"type": "assign_note_cook", "day": "Sunday", "note_text": "Waffles for dinner"}

8. rate_experiment — user is rating the Saturday experiment
   {"type": "rate_experiment", "day": "Saturday", "stars": 4, "recipe_id": null}
   stars is an integer 1-5. recipe_id is optional (use null if not known).

9. promote_experiment — user wants to add the experiment to onRotation
   {"type": "promote_experiment", "recipe_id": null}
   recipe_id is optional; if null, apply to the most recent Saturday experiment.

10. import_atk — user wants to import an ATK recipe
    {"type": "import_atk", "selection": "miso salmon"}

11. acknowledgment — user is done ("okay thanks", "looks good", "done", etc.)
    {"type": "acknowledgment"}

Rules:
- Return ONLY valid JSON. No preamble, no markdown, no explanation.
- If the reply is ambiguous, return your best interpretation.
- Day names are case-insensitive. Normalise to title case (Monday, Tuesday, etc.).
- If no clear action, return [{"type": "acknowledgment"}].

Examples:
User: "Swap Tuesday for a pasta dish"
Output: [{"type": "swap_day", "day": "Tuesday", "constraint": "pasta"}]

User: "Mark Tuesday as dinner at Sarah's"
Output: [{"type": "assign_note_out", "day": "Tuesday", "note_text": "Dinner at Sarah's"}]

User: "Rate Saturday 4 stars"
Output: [{"type": "rate_experiment", "day": "Saturday", "stars": 4, "recipe_id": null}]

User: "Rate the merguez 5 stars, add to onRotation"
Output: [{"type": "rate_experiment", "day": "Saturday", "stars": 5, "recipe_id": null}, {"type": "promote_experiment", "recipe_id": null}]

User: "Put waffles on Sunday"
Output: [{"type": "assign_note_cook", "day": "Sunday", "note_text": "Waffles for dinner"}]

User: "Okay looks good, thanks"
Output: [{"type": "acknowledgment"}]
"""


# ---------------------------------------------------------------------------
# Phase 1: stdin reply reader
# ---------------------------------------------------------------------------

ACK_PATTERNS = [
    "okay thanks", "ok thanks", "looks good", "perfect",
    "great thanks", "all good", "sounds good", "done",
    "got it", "thanks", "thank you", "good", "approved",
]


def read_stdin_reply() -> Optional[str]:
    """
    Read a reply from stdin.

    Returns None if the user types a terminal acknowledgment ('done', 'okay thanks', etc.).
    Returns the reply text string otherwise.
    """
    try:
        text = input().strip()
    except EOFError:
        return None

    if not text:
        return ""

    lower = text.lower()
    if any(p in lower for p in ACK_PATTERNS):
        return None

    return text


def parse_reply_intents(reply_body: str, model: str, api_key: Optional[str] = None) -> list[dict]:
    """
    Send the reply body to Claude and extract structured intents.

    Returns a list of action dicts, e.g.:
      [{"type": "swap_day", "day": "Tuesday", "constraint": "pasta"}]

    Falls back to [{"type": "acknowledgment"}] on any error.
    """
    if anthropic is None:
        raise RuntimeError("anthropic package not installed. Run: pip install anthropic")

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    try:
        response = client.messages.create(
            model=model,
            max_tokens=512,
            system=INTENT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": reply_body}],
        )
        raw = response.content[0].text.strip()

        # Strip any accidental markdown fences
        raw = raw.replace("```json", "").replace("```", "").strip()

        intents = json.loads(raw)
        if not isinstance(intents, list):
            intents = [intents]
        log.info("Parsed %d intent(s) from reply", len(intents))
        return intents

    except (json.JSONDecodeError, Exception) as e:
        log.error("Intent parsing failed: %s — falling back to acknowledgment", e)
        return [{"type": "acknowledgment"}]


# ---------------------------------------------------------------------------
# Plan mutation
# ---------------------------------------------------------------------------

def apply_intents(
    plan: WeekPlan,
    intents: list[dict],
    recipes: dict,
    store: Optional[StateStore],
) -> tuple[WeekPlan, list[str]]:
    """
    Apply parsed intents to an existing WeekPlan.

    Returns (modified_plan, list_of_change_notes).
    Changes are applied in-place on a copy of the plan.
    """
    import copy
    from planner import (
        _sort_by_last_planned, _recipe_is_meatless,
        _is_meatless_required, _make_recipe_slot_standalone,
        _ingredients_from_recipe, _recipe_has_tag,
    )

    plan = copy.deepcopy(plan)
    notes = []
    last_planned = store.get_all_last_planned() if store else {}
    already_assigned = {s.recipe_id for s in plan.dinners if s.recipe_id}

    for intent in intents:
        kind = intent.get("type")

        if kind == "acknowledgment":
            notes.append("No changes — plan confirmed.")

        elif kind == "swap_day":
            day_name = intent.get("day", "").strip().title()
            constraint = (intent.get("constraint") or "").strip().lower()

            target = next(
                (s for s in plan.dinners if s.weekday_name == day_name), None
            )
            if not target:
                notes.append(f"Could not find {day_name} in the plan.")
                continue

            meatless = _is_meatless_required(target.date, _dummy_dc(target.date))
            pool = [
                r for r in recipes.values()
                if r["id"] not in already_assigned
                and not _recipe_has_tag(r, "experiment")
                and not _recipe_has_tag(r, "pizza")
            ]
            if meatless:
                pool = [r for r in pool if _recipe_is_meatless(r)]
            if constraint:
                # Filter by tag or name match
                tag_pool = [r for r in pool if constraint in (r.get("tags") or [])]
                name_pool = [r for r in pool if constraint in r.get("name", "").lower()]
                constrained = tag_pool or name_pool
                if constrained:
                    pool = constrained

            if not pool:
                # Relax: allow re-use
                pool = [
                    r for r in recipes.values()
                    if not _recipe_has_tag(r, "experiment")
                    and not _recipe_has_tag(r, "pizza")
                ]
                if meatless:
                    pool = [r for r in pool if _recipe_is_meatless(r)]

            if not pool:
                notes.append(f"Could not find a suitable recipe for {day_name}.")
                continue

            sorted_pool = _sort_by_last_planned(pool, last_planned)
            recipe = sorted_pool[0]

            # Replace the slot
            new_slot = _make_recipe_slot_standalone(target.date, target, recipe)
            idx = plan.dinners.index(target)
            plan.dinners[idx] = new_slot
            already_assigned.discard(target.recipe_id)
            already_assigned.add(recipe["id"])
            notes.append(
                f"{day_name} changed to {recipe['name']}"
                + (f" [{constraint}]" if constraint else "")
            )

        elif kind == "remove_on_hand":
            ingredient = intent.get("ingredient", "").strip()
            notes.append(
                f"Noted: '{ingredient}' removed from likely-on-hand. "
                f"Add it to your grocery list."
            )
            # Actual removal happens in grocery_builder on re-run via
            # a flag we attach to the plan
            if not hasattr(plan, "_removed_on_hand"):
                plan._removed_on_hand = []
            plan._removed_on_hand.append(ingredient.lower())

        elif kind == "force_no_cook":
            day_name = intent.get("day", "").strip().title()
            target = next(
                (s for s in plan.dinners if s.weekday_name == day_name), None
            )
            if target:
                from planner import MealSlot, _is_meatless_required
                dc = _dummy_dc(target.date)
                idx = plan.dinners.index(target)
                plan.dinners[idx] = MealSlot(
                    date=target.date, slot="dinner",
                    recipe_id=None, label="leftovers",
                    tags=[], ingredients=[],
                    notes=["Changed to leftovers per your request"],
                    is_meatless=_is_meatless_required(target.date, dc),
                )
                already_assigned.discard(target.recipe_id)
                notes.append(f"{day_name} changed to leftovers.")

        elif kind == "force_cook":
            day_name = intent.get("day", "").strip().title()
            target = next(
                (s for s in plan.dinners if s.weekday_name == day_name), None
            )
            if not target:
                continue
            # Pick a recipe for the day
            meatless = _is_meatless_required(target.date, _dummy_dc(target.date))
            pool = [
                r for r in recipes.values()
                if r["id"] not in already_assigned
                and not _recipe_has_tag(r, "experiment")
                and not _recipe_has_tag(r, "pizza")
            ]
            if meatless:
                pool = [r for r in pool if _recipe_is_meatless(r)]
            if pool:
                recipe = _sort_by_last_planned(pool, last_planned)[0]
                new_slot = _make_recipe_slot_standalone(target.date, target, recipe)
                idx = plan.dinners.index(target)
                plan.dinners[idx] = new_slot
                already_assigned.add(recipe["id"])
                notes.append(f"{day_name} changed to {recipe['name']}.")
            else:
                notes.append(f"Could not find a recipe for {day_name}.")

        elif kind == "skip_experiment":
            for i, slot in enumerate(plan.dinners):
                if slot.date.weekday() == 5 and _recipe_has_tag(
                    recipes.get(slot.recipe_id, {}), "experiment"
                ):
                    # Replace with easy/onRotation
                    pool = [
                        r for r in recipes.values()
                        if not _recipe_has_tag(r, "experiment")
                        and not _recipe_has_tag(r, "pizza")
                        and r["id"] not in already_assigned
                    ]
                    if pool:
                        recipe = _sort_by_last_planned(pool, last_planned)[0]
                        new_slot = _make_recipe_slot_standalone(slot.date, slot, recipe)
                        plan.dinners[i] = new_slot
                        already_assigned.discard(slot.recipe_id)
                        already_assigned.add(recipe["id"])
                        notes.append(
                            f"Saturday experiment replaced with {recipe['name']}."
                        )
                    break

        elif kind == "assign_note_out":
            day_name  = intent.get("day", "").strip().title()
            note_text = intent.get("note_text", day_name)
            target = next((s for s in plan.dinners if s.weekday_name == day_name), None)
            if target:
                from planner import MealSlot
                idx = plan.dinners.index(target)
                plan.dinners[idx] = MealSlot(
                    date=target.date, slot="dinner",
                    recipe_id=None, label=note_text,
                    tags=[], ingredients=[],
                    is_no_cook=True,
                    note_type="out",
                    note_text=note_text,
                )
                already_assigned.discard(target.recipe_id)
                notes.append(f"{day_name} marked as '{note_text}' [out].")

        elif kind == "assign_note_cook":
            day_name  = intent.get("day", "").strip().title()
            note_text = intent.get("note_text", day_name)
            target = next((s for s in plan.dinners if s.weekday_name == day_name), None)
            if target:
                from planner import MealSlot
                idx = plan.dinners.index(target)
                plan.dinners[idx] = MealSlot(
                    date=target.date, slot="dinner",
                    recipe_id=None, label=note_text,
                    tags=[], ingredients=[],
                    is_no_cook=False,
                    note_type="cook",
                    note_text=note_text,
                )
                already_assigned.discard(target.recipe_id)
                notes.append(f"{day_name} marked as '{note_text}' [cook].")

        elif kind == "rate_experiment":
            stars     = int(intent.get("stars", 0))
            recipe_id = intent.get("recipe_id")
            if not recipe_id:
                # Find Saturday's recipe in the plan
                sat = next(
                    (s for s in plan.dinners if s.date.weekday() == 5 and s.recipe_id),
                    None,
                )
                recipe_id = sat.recipe_id if sat else None
            if recipe_id and store and 1 <= stars <= 5:
                store.record_experiment_rating(recipe_id, stars=stars)
                notes.append(f"Rated {recipe_id} {stars} stars.")
            elif not (1 <= stars <= 5):
                notes.append("Rating must be 1-5 stars.")
            else:
                notes.append("Could not find experiment recipe to rate.")

        elif kind == "promote_experiment":
            recipe_id = intent.get("recipe_id")
            if not recipe_id:
                sat = next(
                    (s for s in plan.dinners if s.date.weekday() == 5 and s.recipe_id),
                    None,
                )
                recipe_id = sat.recipe_id if sat else None
            if recipe_id and store:
                store.promote_experiment(recipe_id)
                # Update the recipe YAML to add onRotation tag
                _promote_recipe_yaml(recipe_id, store)
                notes.append(f"Promoted {recipe_id} to onRotation.")
            else:
                notes.append("Could not find experiment recipe to promote.")

        elif kind == "import_atk":
            log.info("ATK import requested for '%s' — not implemented in Phase 1", intent.get("selection"))
            notes.append("ATK import not yet implemented in Phase 1.")

    return plan, notes


def _promote_recipe_yaml(recipe_id: str, store=None) -> None:
    """Add 'onRotation' tag to the recipe's YAML file."""
    import yaml
    from pathlib import Path

    recipe_dir = Path("recipes_yaml")
    yaml_path  = recipe_dir / f"{recipe_id}.yaml"
    if not yaml_path.exists():
        log.warning("Cannot promote %s — YAML file not found at %s", recipe_id, yaml_path)
        return

    with open(yaml_path, encoding="utf-8") as f:
        recipe = yaml.safe_load(f)

    tags = recipe.get("tags") or []
    if "onRotation" not in tags:
        tags.append("onRotation")
        recipe["tags"] = tags
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(recipe, f, allow_unicode=True, default_flow_style=False)
        log.info("Added onRotation tag to %s", yaml_path)


def _dummy_dc(d: date):
    """Return a minimal DayConstraints for a date (no calendar lookup needed)."""
    from calendar_reader import DayConstraints
    return DayConstraints(date=d)


# ---------------------------------------------------------------------------
# Helper: expose planner internals needed by apply_intents
# ---------------------------------------------------------------------------

def _patch_planner_exports():
    """
    Ensure planner.py exposes the helper functions apply_intents needs.
    Called once at import time.
    """
    import planner as _p

    if not hasattr(_p, "_make_recipe_slot_standalone"):
        def _make_recipe_slot_standalone(d, existing_slot, recipe):
            """Build a MealSlot from a recipe, preserving the date/slot."""
            from planner import MealSlot, _ingredients_from_recipe, _recipe_has_tag
            from calendar_reader import DayConstraints
            from planner import _is_meatless_required, _recipe_is_meatless
            dc = DayConstraints(date=d)
            notes = []
            if _recipe_has_tag(recipe, "freezer"):
                notes.append("Freezer-friendly — consider doubling")
            return MealSlot(
                date=d,
                slot="dinner",
                recipe_id=recipe["id"],
                label=recipe["name"],
                tags=list(recipe.get("tags") or []),
                notes=notes,
                ingredients=_ingredients_from_recipe(recipe),
                is_meatless=_recipe_is_meatless(recipe) or _is_meatless_required(d, dc),
                is_fasting=existing_slot.is_fasting if existing_slot else False,
            )
        _p._make_recipe_slot_standalone = _make_recipe_slot_standalone


_patch_planner_exports()


# ---------------------------------------------------------------------------
# Reply handler
# ---------------------------------------------------------------------------

class ReplyHandler:
    """
    Polls Gmail for replies to the meal plan email and triggers re-plans.

    Args:
        config:   Full config dict (merged from config.yaml).
        dry_run:  If True, prints actions without sending email or writing state.
    """

    def __init__(self, config: Optional[dict] = None, dry_run: bool = False):
        self.cfg     = {**DEFAULT_CONFIG, **(config or {})}
        self.dry_run = dry_run

    def _make_week_label(self, monday: date) -> str:
        """Build the date-range string used in email subjects."""
        from email_sender import _fmt_date_range
        return _fmt_date_range(monday)

    def _is_thursday(self) -> bool:
        return date.today().weekday() == 3

    # -----------------------------------------------------------------------
    # Main polling loop
    # -----------------------------------------------------------------------

    def poll_and_handle(self, week_key: str) -> bool:
        """
        Poll Gmail for a reply to the plan for the given week_key (ISO Monday date).
        Block until a reply is received or end-of-Thursday is reached.

        Returns True if polling completed (reply received or acknowledged),
        False if polling timed out without a reply.
        """
        monday = date.fromisoformat(week_key)
        week_label = self._make_week_label(monday)
        interval = self.cfg["poll_interval_seconds"]

        sender = EmailSender(config=self.cfg, dry_run=self.dry_run)

        log.info("Starting reply poll for week of %s (label: %s)", monday, week_label)

        while True:
            if not self._is_thursday():
                log.info("Not Thursday — stopping poll.")
                return False

            reply = sender.get_latest_reply(week_label)

            if reply:
                log.info("Reply received from: %s", reply.get("from", "unknown"))
                handled = self._handle_reply(reply, week_key, sender)
                return True

            log.info("No reply yet. Next check in %d seconds.", interval)
            time.sleep(interval)

    # -----------------------------------------------------------------------
    # Reply handling
    # -----------------------------------------------------------------------

    def _handle_reply(
        self,
        reply: dict,
        week_key: str,
        sender: EmailSender,
    ) -> bool:
        """
        Process a single reply. Returns True if handled successfully.
        """
        body = reply.get("body", "")

        # Fast path: acknowledgment
        if sender.is_acknowledgment(body):
            log.info("Acknowledgment received — no re-plan needed.")
            if not self.dry_run:
                store = StateStore(self.cfg["db_path"])
                store.mark_plan_approved(week_key)
                store.close()
            return True

        # Parse intents
        log.info("Parsing reply intents...")
        intents = parse_reply_intents(
            body,
            model=self.cfg["anthropic_model"],
        )

        # Check if all intents are acknowledgments
        if all(i.get("type") == "acknowledgment" for i in intents):
            log.info("All intents are acknowledgments — no re-plan needed.")
            return True

        log.info("Intents: %s", json.dumps(intents))

        # Load current plan
        store = StateStore(self.cfg["db_path"])
        plan_dict = store.get_plan(week_key)
        if not plan_dict:
            log.error("No stored plan found for week %s", week_key)
            store.close()
            return False

        # Reconstruct WeekPlan from stored dict
        plan = self._reconstruct_plan(plan_dict)

        # Load recipes
        recipes = load_recipes(self.cfg["recipe_dir"])

        # Apply intents
        modified_plan, change_notes = apply_intents(plan, intents, recipes, store)
        modified_plan.warnings.extend([f"Revision: {n}" for n in change_notes])

        # Re-run stacker and grocery builder
        stacking_notes = Stacker().analyse(modified_plan)
        grocery = GroceryBuilder(store=store).build(modified_plan)

        # Remove any on-hand items the user flagged as gone
        removed = getattr(modified_plan, "_removed_on_hand", [])
        if removed:
            grocery.likely_on_hand = [
                item for item in grocery.likely_on_hand
                if not any(r in item.name.lower() for r in removed)
            ]

        # Re-send revised plan
        log.info("Re-sending revised plan with %d change(s).", len(change_notes))
        sender.send_plan(modified_plan, grocery, stacking_notes, is_revision=True)

        # Persist revised plan
        if not self.dry_run:
            store.record_plan(
                week_key,
                modified_plan.to_dict(),
                modified_plan.to_state_meals(),
            )

        store.close()
        return True

    # -----------------------------------------------------------------------
    # Plan reconstruction
    # -----------------------------------------------------------------------

    def _reconstruct_plan(self, plan_dict: dict) -> WeekPlan:
        """
        Rebuild a WeekPlan dataclass from the JSON dict stored in SQLite.
        Only the fields needed for mutation are reconstructed.
        """
        from planner import WeekPlan, MealSlot

        monday = date.fromisoformat(plan_dict["week_start_monday"])
        plan = WeekPlan(
            week_start_monday=monday,
            week_key=plan_dict["week_key"],
            cook_nights=plan_dict.get("cook_nights", 0),
            warnings=list(plan_dict.get("warnings", [])),
            rationale=plan_dict.get("rationale", ""),
        )

        for d in plan_dict.get("dinners", []):
            slot = MealSlot(
                date=date.fromisoformat(d["date"]),
                slot=d["slot"],
                recipe_id=d.get("recipe_id"),
                label=d["label"],
                tags=list(d.get("tags") or []),
                ingredients=list(d.get("ingredients") or []),
                notes=list(d.get("notes") or []),
                is_no_cook=d.get("is_no_cook", False),
                is_meatless=d.get("is_meatless", False),
                is_fasting=d.get("is_fasting", False),
                note_type=d.get("note_type"),
                note_text=d.get("note_text"),
            )
            plan.dinners.append(slot)

        lunch_dict = plan_dict.get("lunch")
        if lunch_dict:
            plan.lunch = MealSlot(
                date=monday,
                slot="lunch",
                recipe_id=lunch_dict.get("lunch_id"),
                label=lunch_dict.get("label", ""),
                tags=[],
                ingredients=list(lunch_dict.get("ingredients") or []),
                notes=list(lunch_dict.get("notes") or []),
                note_type=lunch_dict.get("note_type"),
                note_text=lunch_dict.get("note_text"),
            )

        return plan


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description="Poll for replies to the meal plan email and trigger re-plans."
    )
    parser.add_argument(
        "--week", required=True,
        help="Week key as ISO Monday date, e.g. 2026-03-23"
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config.yaml"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print actions without sending email or writing state"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Check for replies once and exit (skip polling loop)"
    )
    args = parser.parse_args()

    config = {}
    try:
        with open(args.config) as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("No config.yaml found, using defaults.")

    # Flatten nested config sections
    flat = {**DEFAULT_CONFIG}
    for section in ("email", "calendar", "planner"):
        flat.update(config.get(section, {}))
    flat.update(config.get("reply_handler", {}))

    handler = ReplyHandler(config=flat, dry_run=args.dry_run)

    if args.once:
        monday = date.fromisoformat(args.week)
        week_label = handler._make_week_label(monday)
        sender = EmailSender(config=flat, dry_run=args.dry_run)
        reply = sender.get_latest_reply(week_label)
        if reply:
            handler._handle_reply(reply, args.week, sender)
        else:
            print("No reply found.")
    else:
        handler.poll_and_handle(args.week)
