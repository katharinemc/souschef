"""
agentic_planner.py

Agentic meal planner: single Claude API call with pre-assembled context.

Pre-assembles all deterministic data (calendar constraints, recipes, rotation
history, recent meals) in Python, then makes ONE Claude API call.  Claude
returns the full plan via the submit_plan tool.  Constraint validation runs
in Python after parsing the response.

Falls back to deterministic Planner on any API error or parse failure.

Architecture decision: ADR-001 in prd_v1_5.md §4.5.

Interface matches planner.Planner:
    planner = AgenticPlanner(config=cfg, store=store)
    plan = planner.plan_week(constraints)  # returns WeekPlan
"""

import json
import logging
from datetime import date, timedelta
from typing import Optional

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore

from calendar_reader import WeekConstraints, DayConstraints
from planner import (
    WeekPlan, MealSlot,
    _ingredients_from_recipe, _week_key,
    load_recipes, load_lunches,
)
from state_store import StateStore

log = logging.getLogger(__name__)

MODEL = "claude-opus-4-5"

MEATLESS_TAGS = {"vegetarian", "pescatarian", "vegan"}
ALWAYS_MEATLESS_WEEKDAYS = {2, 4}  # Wednesday, Friday


# ---------------------------------------------------------------------------
# submit_plan tool — the only tool Claude calls
# ---------------------------------------------------------------------------

SUBMIT_PLAN_TOOL = {
    "name": "submit_plan",
    "description": (
        "Submit the complete weekly meal plan. Call this exactly once with "
        "all 7 dinners assigned and a rationale."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "rationale": {
                "type": "string",
                "description": (
                    "3-5 sentence narrative covering the 2-3 most interesting "
                    "decisions of the week. Not a slot-by-slot summary."
                ),
            },
            "dinners": {
                "type": "array",
                "description": "Exactly 7 dinner assignments, one per day. Use the exact ISO dates from the CALENDAR CONSTRAINTS — Mon through Sun.",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "ISO date, e.g. '2026-04-10'.",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["recipe", "no_cook", "note"],
                            "description": (
                                "recipe: cook from library. "
                                "no_cook: leftovers / no cooking. "
                                "note: eating out (out) or cooking something not in library (cook)."
                            ),
                        },
                        "recipe_id": {
                            "type": "string",
                            "description": "Required when type=recipe.",
                        },
                        "note_text": {
                            "type": "string",
                            "description": "Required when type=note.",
                        },
                        "note_type": {
                            "type": "string",
                            "enum": ["out", "cook"],
                            "description": "Required when type=note.",
                        },
                    },
                    "required": ["date", "type"],
                },
            },
            "lunch_id": {
                "type": "string",
                "description": "Lunch ID from the available lunches list, or omit.",
            },
        },
        "required": ["rationale", "dinners"],
    },
}


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def _fmt_day(d: date) -> str:
    return d.strftime("%A %b %-d (%Y-%m-%d)")


def _build_context_message(
    constraints: WeekConstraints,
    recipes: dict,
    lunches: list,
    store: Optional[StateStore],
    today: date,
) -> str:
    """Assemble all planning context into a single user message string."""
    monday = constraints.week_start_monday
    sunday  = monday + timedelta(days=6)

    lines = [
        f"Plan the 7-day window: {monday.strftime('%b %-d')} (Mon) – {sunday.strftime('%b %-d, %Y')} (Sun).",
        f"Today is {today.strftime('%A, %B %-d, %Y')}.",
        "Use the exact ISO dates shown in CALENDAR CONSTRAINTS below — do not invent or shift dates.",
        "",
    ]

    # --- Calendar constraints ---
    lines.append("## CALENDAR CONSTRAINTS")
    lines.append("")
    for dc in constraints.days:
        day_str = _fmt_day(dc.date)
        flags = []
        if dc.is_no_cook:
            reason = dc.no_cook_reason or "no-cook"
            if dc.qualifying_events:
                reason = dc.qualifying_events[0]
            flags.append(f"NO-COOK ({reason})")
        if dc.is_fasting:
            flags.append("FASTING (meatless)")
        if dc.is_open_saturday:
            flags.append("open Saturday (experiment eligible)")
        flag_str = "  [" + ", ".join(flags) + "]" if flags else "  [open]"
        lines.append(f"  {day_str}{flag_str}")
    lines.append(f"  sunday_needs_easy: {constraints.sunday_needs_easy}")
    lines.append("")

    # --- Rotation history ---
    all_last: dict = store.get_all_last_planned() if store else {}
    recent_ids: set = set()
    recent_rows = store.get_recent_meals(weeks=2) if store else []
    for row in recent_rows:
        if row.get("recipe_id"):
            recent_ids.add(row["recipe_id"])

    # --- Recipes ---
    lines.append("## AVAILABLE RECIPES")
    lines.append("id | name | tags | last_planned | cook_time")
    lines.append("")
    for recipe in sorted(recipes.values(), key=lambda r: r["name"]):
        rid = recipe["id"]
        tags = ", ".join(sorted(recipe.get("tags") or []))
        last = all_last.get(rid)
        last_str = last.isoformat() if last else "never"
        cook_time = recipe.get("cook_time") or "?"
        recent_flag = "  ← recent" if rid in recent_ids else ""
        lines.append(f"  {rid} | {recipe['name']} | {tags} | {last_str} | {cook_time}min{recent_flag}")
    lines.append("")

    # --- Recent meals ---
    if recent_rows:
        lines.append("## RECENT MEALS (last 2 weeks — avoid repeating)")
        for row in recent_rows:
            lines.append(f"  {row.get('week_key','?')} {row.get('slot','?')}: {row.get('recipe_id','?')}")
        lines.append("")

    # --- Lunches ---
    if lunches:
        lines.append("## AVAILABLE LUNCHES")
        for entry in lunches:
            lines.append(f"  {entry.get('id','?')} | {entry.get('label','?')}")
        lines.append("")

    lines.append("Submit the complete plan using submit_plan().")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a meal planning agent for a household of four.

You will receive a full planning context (calendar, recipes, history) in the user message.
Study it, then call submit_plan() exactly once with all 7 dinners assigned.

## NON-NEGOTIABLE HOUSEHOLD RULES

- Wednesday and Friday are always meatless (vegetarian or pescatarian).
- Friday is always pizza — homemade on the first Friday of the month, takeout all other Fridays.
- Fasting days marked on the calendar are meatless.
- Saturday and Sunday: cook one or the other, never both. Saturday takes priority.
  Sunday only cooks if Saturday was no-cook, and then must be an easy recipe.
- Experiment recipes are scheduled only on open Saturdays.
- Calendar events after 3pm that are prefixed KRM or Family = no-cook day.

## SCHEDULING PREFERENCES

- Target 3 cook nights on busy weeks (2+ no-cook weekdays), 4 on light weeks.
- onRotation recipes should appear roughly monthly; prioritise most overdue.
- Never repeat a recipe marked "← recent" if alternatives exist.
- Prefer variety in protein type and cuisine style across the week.
- On high-calendar-density weeks, prefer easier recipes (shorter cook_time).

## CONFLICT HANDLING

- Always satisfy hard dietary rules first.
- If the meatless pool is exhausted, note it in the rationale.
- Flag anything unusual rather than making a quiet judgment call.

## OUTPUT EXPECTATIONS

- Assign every day in the planning window (Mon–Sun, 7 days total).
- Use the exact ISO dates from CALENDAR CONSTRAINTS — do not shift or invent dates.
- Use type=note with note_type=out for eating out.
- Use type=note with note_type=cook for cooking something not in the library.
- Call submit_plan() with a rationale of 3–5 sentences covering the 2–3 most
  interesting decisions of the week. Not a slot-by-slot recitation."""


# ---------------------------------------------------------------------------
# AgenticPlanner
# ---------------------------------------------------------------------------

class AgenticPlanner:
    """
    Meal planner driven by a single Claude API call with pre-assembled context.

    Args:
        config:  Dict of config values (recipe_dir, lunch_file, db_path, model, ...).
        store:   StateStore for rotation history and recent meals.
    """

    def __init__(self, config: dict, store: StateStore):
        self.cfg   = config
        self.store = store
        self.recipes: dict = {}
        self.lunches: list = []

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    def plan_week(self, constraints: WeekConstraints) -> WeekPlan:
        """Run the agentic planner and return a WeekPlan. Falls back to deterministic on error."""
        self.recipes = load_recipes(self.cfg.get("recipe_dir", "recipes_yaml"))
        self.lunches = load_lunches(self.cfg.get("lunch_file", "lunches.yaml"))

        if anthropic is None:
            log.error("anthropic package not installed — falling back to deterministic planner")
            return self._fallback(constraints, "anthropic package not installed")

        client = anthropic.Anthropic(
            api_key=self.cfg.get("api_key") or None  # falls back to ANTHROPIC_API_KEY env var
        )

        context_message = _build_context_message(
            constraints, self.recipes, self.lunches, self.store, date.today()
        )

        try:
            response = client.messages.create(
                model=self.cfg.get("model", MODEL),
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=[SUBMIT_PLAN_TOOL],
                tool_choice={"type": "tool", "name": "submit_plan"},
                messages=[{"role": "user", "content": context_message}],
            )
        except Exception as e:
            log.error("Agentic planner API call failed (%s) — falling back.", e)
            return self._fallback(constraints, str(e))

        log.info(
            "API call: %d in / %d out tokens",
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        # Extract submit_plan tool call
        tool_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_blocks:
            log.error("Claude did not call submit_plan — falling back.")
            return self._fallback(constraints, "no tool call in response")

        plan_input = tool_blocks[0].input
        try:
            return self._build_week_plan(constraints, plan_input)
        except Exception as e:
            log.error("Failed to parse submit_plan response (%s) — falling back.", e)
            return self._fallback(constraints, str(e))

    # -----------------------------------------------------------------------
    # Plan builder
    # -----------------------------------------------------------------------

    def _build_week_plan(self, constraints: WeekConstraints, plan_input: dict) -> WeekPlan:
        """Parse and validate Claude's submit_plan response into a WeekPlan."""
        monday = constraints.week_start_monday
        plan = WeekPlan(
            week_start_monday=monday,
            week_key=_week_key(monday),
            rationale=plan_input.get("rationale", ""),
        )

        # Index DayConstraints by date string for quick lookup
        dc_by_date: dict[str, DayConstraints] = {
            dc.date.isoformat(): dc for dc in constraints.days
        }

        assigned_dates: set[str] = set()

        for dinner in plan_input.get("dinners", []):
            date_str = dinner.get("date", "")
            dtype    = dinner.get("type", "no_cook")
            dc       = dc_by_date.get(date_str)

            if not dc:
                log.warning("submit_plan included unknown date %s — skipping.", date_str)
                continue
            if date_str in assigned_dates:
                log.warning("submit_plan assigned %s twice — keeping first.", date_str)
                continue
            assigned_dates.add(date_str)

            slot = self._parse_dinner_slot(dc, dtype, dinner)
            plan.dinners.append(slot)

        # Fill any unassigned days with leftovers
        for dc in constraints.days:
            if dc.date.isoformat() not in assigned_dates:
                is_meatless = dc.date.weekday() in ALWAYS_MEATLESS_WEEKDAYS or dc.is_fasting
                plan.dinners.append(MealSlot(
                    date=dc.date, slot="dinner",
                    recipe_id=None, label="leftovers",
                    tags=[], ingredients=[],
                    is_no_cook=True, is_meatless=is_meatless,
                    is_fasting=dc.is_fasting,
                ))
                log.warning("Day %s not in submit_plan response — marked leftovers.", dc.date)

        plan.dinners.sort(key=lambda s: s.date)

        # Lunch
        lunch_id = plan_input.get("lunch_id")
        if lunch_id:
            plan.lunch = self._build_lunch_slot(lunch_id)

        # Cook night count
        plan.cook_nights = sum(
            1 for s in plan.dinners
            if s.note_type == "cook"
            or (not s.is_no_cook and s.recipe_id is not None and s.note_type != "out")
        )

        return plan

    def _parse_dinner_slot(self, dc: DayConstraints, dtype: str, dinner: dict) -> MealSlot:
        """Convert a single dinner dict from Claude into a MealSlot."""
        weekday = dc.date.weekday()
        is_meatless_day = weekday in ALWAYS_MEATLESS_WEEKDAYS or dc.is_fasting

        if dtype == "recipe":
            recipe_id = dinner.get("recipe_id", "")
            recipe    = self.recipes.get(recipe_id)

            if recipe is None:
                log.warning("submit_plan references unknown recipe '%s' — marking no-cook.", recipe_id)
                return MealSlot(
                    date=dc.date, slot="dinner",
                    recipe_id=None, label="no cook",
                    tags=[], ingredients=[],
                    is_no_cook=True, is_meatless=is_meatless_day,
                    is_fasting=dc.is_fasting,
                )

            recipe_tags = set(recipe.get("tags") or [])
            is_meatless = bool(recipe_tags & MEATLESS_TAGS)

            # Constraint check: meatless days
            if is_meatless_day and not is_meatless:
                log.warning(
                    "submit_plan assigned non-meatless recipe on meatless day %s — overriding to no-cook.",
                    dc.date,
                )
                return MealSlot(
                    date=dc.date, slot="dinner",
                    recipe_id=None, label="no cook (meatless constraint violated — check plan)",
                    tags=[], ingredients=[],
                    is_no_cook=True, is_meatless=True,
                    is_fasting=dc.is_fasting,
                )

            return MealSlot(
                date=dc.date, slot="dinner",
                recipe_id=recipe_id,
                label=recipe["name"],
                tags=list(recipe_tags),
                ingredients=_ingredients_from_recipe(recipe),
                is_meatless=is_meatless or is_meatless_day,
                is_fasting=dc.is_fasting,
            )

        elif dtype == "note":
            note_text = dinner.get("note_text", "")
            note_type = dinner.get("note_type", "out")
            return MealSlot(
                date=dc.date, slot="dinner",
                recipe_id=None, label=note_text,
                tags=[], ingredients=[],
                note_type=note_type, note_text=note_text,
                is_meatless=is_meatless_day,
                is_fasting=dc.is_fasting,
            )

        else:  # no_cook
            notes = []
            if dc.qualifying_events:
                notes.append(dc.qualifying_events[0])
            if is_meatless_day:
                notes.append("Would have been meatless")
            return MealSlot(
                date=dc.date, slot="dinner",
                recipe_id=None, label="no cook",
                tags=[], notes=notes, ingredients=[],
                is_no_cook=True, is_meatless=is_meatless_day,
                is_fasting=dc.is_fasting,
            )

    def _build_lunch_slot(self, lunch_id: str) -> Optional[MealSlot]:
        for entry in self.lunches:
            if entry.get("id") == lunch_id:
                notes = []
                prep = entry.get("prep_notes")
                if prep:
                    notes.append(f"Weekend prep: {prep}")
                return MealSlot(
                    date=date.today(), slot="lunch",
                    recipe_id=lunch_id,
                    label=entry.get("label", lunch_id),
                    tags=list(entry.get("tags") or []),
                    ingredients=[],
                    notes=notes,
                )
        log.warning("Lunch ID '%s' not found in lunches list.", lunch_id)
        return None

    # -----------------------------------------------------------------------
    # Fallback
    # -----------------------------------------------------------------------

    def _fallback(self, constraints: WeekConstraints, reason: str) -> WeekPlan:
        from planner import Planner
        log.warning("FALLBACK: using deterministic planner. Reason: %s", reason)
        fallback_planner = Planner(config=self.cfg, store=self.store)
        plan = fallback_planner.plan_week(constraints)
        plan.warnings.append(f"Agentic planner failed — used fallback. Reason: {reason}")
        return plan
