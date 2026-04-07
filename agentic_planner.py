"""
agentic_planner.py

Agentic meal planner: Claude as orchestrator with tool use.
Replaces planner.py's deterministic algorithm with Claude reasoning.
Falls back to deterministic Planner on any API error or tool-call limit.

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
MAX_TOOL_CALLS = 30

MEATLESS_TAGS = {"vegetarian", "pescatarian", "vegan"}
ALWAYS_MEATLESS_WEEKDAYS = {2, 4}  # Wednesday, Friday



# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

def _build_system_prompt(today: date, monday: date) -> str:
    sunday = monday + timedelta(days=6)

    def fmt(d: date) -> str:
        return d.strftime("%A %b %-d (%Y-%m-%d)")

    days_list = "\n".join(
        f"  - {fmt(monday + timedelta(days=i))}"
        for i in range(7)
    )

    return f"""You are a meal planning agent for a household of four.
Today is {today.strftime("%A, %B %-d, %Y")}.
You are planning the week of {monday.strftime("%B %-d")} through {sunday.strftime("%B %-d, %Y")}.

Planning week days:
{days_list}

Your FIRST action must be get_calendar_constraints() before doing anything else.
You MUST call finalize_plan() at the end — do not stop without calling it.

## NON-NEGOTIABLE HOUSEHOLD RULES

- Wednesday and Friday are always meatless (vegetarian or pescatarian).
- Friday is always pizza — homemade on the first Friday of the month, takeout all other Fridays.
- Fasting days marked on the calendar (fasting event at 5am) are meatless.
- Saturday and Sunday: cook one or the other, never both. Saturday takes priority. Sunday only cooks if Saturday was no-cook, and then must be an easy recipe.
- Experiment recipes are scheduled only on open Saturdays (no qualifying KRM/Family events over 2 hours).
- Calendar events prefixed KRM or Family after 3pm = no-cook day. Events prefixed SGM are ignored.

## SCHEDULING PREFERENCES

- Target 3 cook nights on busy weeks (2+ no-cook weekdays), 4 on light weeks.
- onRotation recipes should appear roughly monthly; prioritise most overdue.
- Never repeat a recipe from the previous two weeks if alternatives exist.
- Prefer variety in protein type and cuisine style across the week.
- On high-calendar-density weeks, prefer easier recipes.

## CONFLICT HANDLING

- Always satisfy hard dietary rules first.
- If the meatless pool is exhausted, say so in the rationale — do not silently skip the constraint.
- Prefer a shorter honest plan over a padded one.
- Flag anything unusual rather than making a quiet judgment call.

## OUTPUT EXPECTATIONS

- Assign every day (recipe, no-cook, note, or leftovers — every day gets a label).
- Use assign_note() for freeform events (dining out, cook nights not in the library).
- Call finalize_plan() with a rationale of 3-5 sentences covering the 2-3 most interesting decisions of the week.
- The rationale should read like a note from a thoughtful assistant, not a system log.
  Do not recite every slot — explain what was interesting or difficult about this particular week.
- finalize_plan() MUST always be called to complete the plan."""


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic tool dicts with JSON schema)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "get_calendar_constraints",
        "description": (
            "Returns the calendar constraints for the planning week: "
            "no-cook days (with reasons), fasting days, open Saturday flag, "
            "sunday_needs_easy flag, and the date of each day Mon-Sun. "
            "Call this first before doing anything else."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_recipes",
        "description": (
            "Return recipes from the library. Does not include ingredients or instructions. "
            "Filter with optional tags, meatless_only, or exclude_ids."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Only return recipes that have ALL of these tags.",
                },
                "meatless_only": {
                    "type": "boolean",
                    "description": "If true, only return vegetarian/pescatarian/vegan recipes.",
                },
                "exclude_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Recipe IDs to exclude from results.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_rotation_history",
        "description": "Return last_planned ISO date for each recipe ID. Omit recipe_ids to get all.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipe_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_recent_meals",
        "description": "Return planned meals from the last N weeks (default 2).",
        "input_schema": {
            "type": "object",
            "properties": {
                "weeks": {
                    "type": "integer",
                    "description": "Number of weeks of history to return (default 2).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "assign_meal",
        "description": (
            "Assign a recipe to a day. Validates hard constraints (meatless rules, "
            "experiment/Saturday-only, pizza/Friday-only, no double-assignment). "
            "Returns {ok: true} on success or {ok: false, error: '...'} on violation — "
            "do NOT treat an error as fatal; read the message and recover."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "day": {
                    "type": "string",
                    "description": "Day name, e.g. 'Monday'. Title-case.",
                },
                "recipe_id": {
                    "type": "string",
                    "description": "The recipe id from get_recipes.",
                },
                "notes": {
                    "type": "string",
                    "description": "Optional planner annotation (not shown to user).",
                },
            },
            "required": ["day", "recipe_id"],
        },
    },
    {
        "name": "assign_no_cook",
        "description": "Mark a day as no-cook or leftovers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "day":    {"type": "string"},
                "reason": {"type": "string", "description": "Optional reason string."},
            },
            "required": ["day"],
        },
    },
    {
        "name": "assign_note",
        "description": (
            "Assign a freeform note to a day. Use note_type='out' for eating out "
            "(not counted as cook night, no grocery items). "
            "Use note_type='cook' for cooking something not in the library "
            "(counts as a cook night, user adds ingredients manually)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "day":       {"type": "string"},
                "note_text": {"type": "string"},
                "note_type": {
                    "type": "string",
                    "enum": ["out", "cook"],
                },
            },
            "required": ["day", "note_text", "note_type"],
        },
    },
    {
        "name": "assign_lunch",
        "description": "Assign the lunch of the week by lunch ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lunch_id": {"type": "string"},
            },
            "required": ["lunch_id"],
        },
    },
    {
        "name": "get_stacking_opportunities",
        "description": (
            "Analyse ingredient overlap for a set of recipe IDs. "
            "Returns list of advisory strings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipe_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["recipe_ids"],
        },
    },
    {
        "name": "finalize_plan",
        "description": (
            "Signal that planning is complete. "
            "MUST be called exactly once at the end. "
            "Provide a 3-5 sentence rationale explaining the 2-3 most interesting "
            "decisions of the week — not a slot-by-slot summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rationale": {"type": "string"},
            },
            "required": ["rationale"],
        },
    },
]


# ---------------------------------------------------------------------------
# AgenticPlanner
# ---------------------------------------------------------------------------

class AgenticPlanner:
    """
    Meal planner driven by Claude tool use.

    Args:
        config:  Dict of config values (recipe_dir, lunch_file, db_path, model, ...).
        store:   StateStore for rotation history.
    """

    def __init__(self, config: dict, store: StateStore):
        self.cfg   = config
        self.store = store
        self.recipes: dict = {}
        self.lunches: list = []
        self._constraints: Optional[WeekConstraints] = None
        self._assigned: dict[str, MealSlot] = {}
        self._lunch: Optional[MealSlot] = None
        self._rationale: str = ""
        self._finalized: bool = False

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    def plan_week(self, constraints: WeekConstraints) -> WeekPlan:
        """Run the agentic planner and return a WeekPlan. Falls back to deterministic on error."""
        self.recipes = load_recipes(self.cfg.get("recipe_dir", "recipes_yaml"))
        self.lunches = load_lunches(self.cfg.get("lunch_file", "lunches.yaml"))
        self._constraints = constraints
        self._assigned = {}
        self._lunch = None
        self._rationale = ""
        self._finalized = False

        if anthropic is None:
            log.error("anthropic package not installed — falling back to deterministic planner")
            return self._fallback(constraints, "anthropic package not installed")

        monday = constraints.week_start_monday
        system_prompt = _build_system_prompt(date.today(), monday)
        client = anthropic.Anthropic()

        messages = [
            {
                "role": "user",
                "content": "Please plan the meal schedule for the upcoming week.",
            }
        ]

        tool_call_count = 0

        try:
            while tool_call_count < MAX_TOOL_CALLS and not self._finalized:
                response = client.messages.create(
                    model=self.cfg.get("model", MODEL),
                    max_tokens=4096,
                    system=system_prompt,
                    tools=TOOLS,
                    messages=messages,
                )

                if response.stop_reason == "end_turn":
                    break

                if response.stop_reason != "tool_use":
                    log.warning("Unexpected stop_reason: %s", response.stop_reason)
                    break

                tool_use_blocks = [
                    b for b in response.content if b.type == "tool_use"
                ]

                tool_results = []
                for block in tool_use_blocks:
                    tool_call_count += 1
                    log.debug("Tool call %d: %s(%s)", tool_call_count, block.name, block.input)
                    result = self._dispatch_tool(block.name, block.input)
                    tool_results.append({
                        "type":        "tool_result",
                        "tool_use_id": block.id,
                        "content":     json.dumps(result),
                    })

                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

            if tool_call_count >= MAX_TOOL_CALLS and not self._finalized:
                log.warning(
                    "Agent hit %d-tool-call limit without finalizing — falling back",
                    MAX_TOOL_CALLS,
                )
                return self._fallback(constraints, f"Tool call limit ({MAX_TOOL_CALLS}) reached")

        except Exception as e:
            log.error("Agentic planner failed (%s) — falling back to deterministic planner.", e)
            return self._fallback(constraints, str(e))

        return self._build_week_plan(constraints)

    # -----------------------------------------------------------------------
    # Fallback
    # -----------------------------------------------------------------------

    def _fallback(self, constraints: WeekConstraints, reason: str) -> WeekPlan:
        from planner import Planner
        log.warning("FALLBACK: using deterministic planner. Reason: %s", reason)
        fallback_planner = Planner(config=self.cfg, store=self.store)
        plan = fallback_planner.plan_week(constraints)
        plan.warnings.insert(0, f"Agentic planner unavailable — deterministic fallback used. ({reason})")
        return plan

    # -----------------------------------------------------------------------
    # Tool dispatch
    # -----------------------------------------------------------------------

    def _dispatch_tool(self, name: str, input_dict: dict) -> dict:
        dispatch = {
            "get_calendar_constraints":   lambda i: self._tool_get_calendar_constraints(i),
            "get_recipes":                lambda i: self._tool_get_recipes(i),
            "get_rotation_history":       lambda i: self._tool_get_rotation_history(i),
            "get_recent_meals":           lambda i: self._tool_get_recent_meals(i),
            "assign_meal":                lambda i: self._tool_assign_meal(
                                              i.get("day", ""), i.get("recipe_id", ""), i.get("notes", "")
                                          ),
            "assign_no_cook":             lambda i: self._tool_assign_no_cook(
                                              i.get("day", ""), i.get("reason", "")
                                          ),
            "assign_note":                lambda i: self._tool_assign_note(
                                              i.get("day", ""), i.get("note_text", ""), i.get("note_type", "out")
                                          ),
            "assign_lunch":               lambda i: self._tool_assign_lunch(i.get("lunch_id", "")),
            "get_stacking_opportunities": lambda i: self._tool_get_stacking_opportunities(i),
            "finalize_plan":              lambda i: self._tool_finalize_plan(i.get("rationale", "")),
        }
        fn = dispatch.get(name)
        if fn is None:
            return {"error": f"Unknown tool: {name}"}
        try:
            return fn(input_dict)
        except Exception as e:
            log.error("Tool %s raised exception: %s", name, e)
            return {"error": f"Tool error: {e}"}

    # -----------------------------------------------------------------------
    # Tool implementations
    # -----------------------------------------------------------------------

    def _tool_get_calendar_constraints(self, _: dict) -> dict:
        wc = self._constraints
        days = []
        for dc in wc.days:
            days.append({
                "date":             dc.date.isoformat(),
                "weekday":          dc.date.strftime("%A"),
                "is_no_cook":       dc.is_no_cook,
                "is_fasting":       dc.is_fasting,
                "is_open_saturday": dc.is_open_saturday,
                "no_cook_reason":   dc.no_cook_reason,
            })
        return {
            "week_start_monday": wc.week_start_monday.isoformat(),
            "sunday_needs_easy": wc.sunday_needs_easy,
            "days":              days,
            "no_cook_count":     len(wc.no_cook_days()),
            "fasting_days":      [dc.date.isoformat() for dc in wc.fasting_days()],
            "open_saturday":     bool(wc.open_saturday()),
        }

    def _tool_get_recipes(self, input_dict: dict) -> dict:
        tags        = input_dict.get("tags") or []
        meatless    = input_dict.get("meatless_only", False)
        exclude_ids = set(input_dict.get("exclude_ids") or [])

        results = []
        for recipe in self.recipes.values():
            if recipe["id"] in exclude_ids:
                continue
            recipe_tags = set(recipe.get("tags") or [])
            if tags and not all(t in recipe_tags for t in tags):
                continue
            if meatless and not bool(recipe_tags & MEATLESS_TAGS):
                continue
            results.append({
                "id":           recipe["id"],
                "name":         recipe["name"],
                "tags":         list(recipe_tags),
                "servings":     recipe.get("servings"),
                "last_planned": None,
                "cook_time":    recipe.get("cook_time"),
                "prep_time":    recipe.get("prep_time"),
            })
        return {"recipes": results, "count": len(results)}

    def _tool_get_rotation_history(self, input_dict: dict) -> dict:
        recipe_ids = input_dict.get("recipe_ids")
        all_last = self.store.get_all_last_planned() if self.store else {}

        if recipe_ids:
            return {rid: (all_last.get(rid).isoformat() if all_last.get(rid) else None)
                    for rid in recipe_ids}
        return {rid: d.isoformat() for rid, d in all_last.items()}

    def _tool_get_recent_meals(self, input_dict: dict) -> dict:
        weeks = input_dict.get("weeks", 2)
        rows = self.store.get_recent_meals(weeks=weeks) if self.store else []
        return {"meals": rows, "count": len(rows)}

    def _tool_assign_meal(self, day: str, recipe_id: str, notes: str = "") -> dict:
        day = day.strip().title()

        if day in self._assigned:
            return {
                "ok": False,
                "error": f"{day} is already assigned to '{self._assigned[day].label}'. "
                         "Re-assigning is not supported — plan around it.",
            }

        recipe = self.recipes.get(recipe_id)
        if recipe is None:
            return {"ok": False, "error": f"Recipe '{recipe_id}' not found in the library."}

        dc = self._get_dc_for_day(day)
        if dc is None:
            return {"ok": False, "error": f"Unknown day: '{day}'."}

        tags    = set(recipe.get("tags") or [])
        weekday = dc.date.weekday()

        if "pizza" in tags and weekday != 4:
            return {"ok": False, "error": "Pizza recipes can only be assigned to Friday."}

        if "experiment" in tags and weekday != 5:
            return {"ok": False, "error": "Experiment recipes can only be assigned to Saturday."}

        needs_meatless = weekday in ALWAYS_MEATLESS_WEEKDAYS or dc.is_fasting
        is_meatless    = bool(tags & MEATLESS_TAGS)
        if needs_meatless and not is_meatless:
            reason = (
                "Wednesday" if weekday == 2 else
                "Friday"    if weekday == 4 else
                "fasting day"
            )
            return {
                "ok": False,
                "error": f"{day} requires a meatless recipe ({reason} rule). "
                         "This recipe appears to contain meat (no vegetarian/pescatarian/vegan tag).",
            }

        slot = MealSlot(
            date=dc.date,
            slot="dinner",
            recipe_id=recipe_id,
            label=recipe["name"],
            tags=list(tags),
            notes=[notes] if notes else [],
            ingredients=_ingredients_from_recipe(recipe),
            is_meatless=is_meatless or needs_meatless,
            is_fasting=dc.is_fasting,
        )
        self._assigned[day] = slot
        log.debug("Assigned %s -> %s", day, recipe["name"])
        return {"ok": True, "day": day, "label": recipe["name"]}

    def _tool_assign_no_cook(self, day: str, reason: str = "") -> dict:
        day = day.strip().title()
        dc = self._get_dc_for_day(day)
        if dc is None:
            return {"ok": False, "error": f"Unknown day: '{day}'."}

        notes = []
        if reason:
            notes.append(reason)
        weekday = dc.date.weekday()
        needs_meatless = weekday in ALWAYS_MEATLESS_WEEKDAYS or dc.is_fasting
        if needs_meatless:
            notes.append("Would have been meatless")

        slot = MealSlot(
            date=dc.date, slot="dinner",
            recipe_id=None, label="no cook",
            tags=[], notes=notes,
            ingredients=[],
            is_no_cook=True,
            is_meatless=needs_meatless,
            is_fasting=dc.is_fasting,
        )
        self._assigned[day] = slot
        return {"ok": True, "day": day, "label": "no cook"}

    def _tool_assign_note(self, day: str, note_text: str, note_type: str) -> dict:
        day = day.strip().title()
        if note_type not in ("out", "cook"):
            return {"ok": False, "error": "note_type must be 'out' or 'cook'."}
        dc = self._get_dc_for_day(day)
        if dc is None:
            return {"ok": False, "error": f"Unknown day: '{day}'."}

        slot = MealSlot(
            date=dc.date, slot="dinner",
            recipe_id=None, label=note_text,
            tags=[], notes=[],
            ingredients=[],
            is_no_cook=(note_type == "out"),
            note_type=note_type,
            note_text=note_text,
        )
        self._assigned[day] = slot
        return {"ok": True, "day": day, "label": note_text, "note_type": note_type}

    def _tool_assign_lunch(self, lunch_id: str) -> dict:
        entry = next((l for l in self.lunches if l.get("id") == lunch_id), None)
        if entry is None:
            return {"ok": False, "error": f"Lunch '{lunch_id}' not found."}

        notes = []
        prep = entry.get("prep_notes")
        if prep:
            notes.append(f"Weekend prep: {prep}")

        self._lunch = MealSlot(
            date=self._constraints.week_start_monday,
            slot="lunch",
            recipe_id=lunch_id,
            label=entry.get("label", ""),
            tags=[],
            ingredients=[],
            notes=notes,
        )
        return {"ok": True, "label": entry.get("label", ""), "prep_notes": prep or ""}

    def _tool_get_stacking_opportunities(self, input_dict: dict) -> dict:
        recipe_ids = input_dict.get("recipe_ids", [])
        from stacker import Stacker

        monday = self._constraints.week_start_monday
        synthetic = WeekPlan(week_start_monday=monday, week_key=_week_key(monday))
        for day_name, slot in self._assigned.items():
            if slot.recipe_id and slot.recipe_id in recipe_ids:
                synthetic.dinners.append(slot)

        notes = Stacker().analyse(synthetic)
        return {"stacking_notes": [n.message for n in notes]}

    def _tool_finalize_plan(self, rationale: str) -> dict:
        self._rationale = rationale
        self._finalized = True
        log.info("Plan finalized. Rationale: %s...", rationale[:80])
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _get_dc_for_day(self, day_name: str) -> Optional[DayConstraints]:
        for dc in self._constraints.days:
            if dc.date.strftime("%A") == day_name:
                return dc
        return None

    def _build_week_plan(self, constraints: WeekConstraints) -> WeekPlan:
        monday = constraints.week_start_monday
        plan = WeekPlan(
            week_start_monday=monday,
            week_key=_week_key(monday),
            rationale=self._rationale,
        )

        for i in range(7):
            d        = monday + timedelta(days=i)
            day_name = d.strftime("%A")
            slot     = self._assigned.get(day_name)
            if slot is None:
                dc = self._get_dc_for_day(day_name)
                is_meatless = (d.weekday() in ALWAYS_MEATLESS_WEEKDAYS or
                               (dc.is_fasting if dc else False))
                slot = MealSlot(
                    date=d, slot="dinner",
                    recipe_id=None, label="leftovers",
                    tags=[], ingredients=[],
                    is_no_cook=True,
                    is_meatless=is_meatless,
                    is_fasting=dc.is_fasting if dc else False,
                )
                log.warning("Day %s was not assigned by agent — marked as leftovers.", day_name)

            plan.dinners.append(slot)

        plan.dinners.sort(key=lambda s: s.date)
        plan.lunch = self._lunch
        plan.cook_nights = sum(
            1 for s in plan.dinners
            if s.note_type == "cook"
            or (not s.is_no_cook and s.recipe_id is not None and s.note_type != "out")
        )

        return plan
