"""
output_formatter.py

Formats a WeekPlan + GroceryList + list[StackingNote] to a terminal string.
Output matches PRD section 5 exactly.

format_plan() returns a string.
print_plan() calls print() once with that string.
print_plan is kept separate so email_sender can reuse format_plan in Phase 3.
"""

import textwrap
from datetime import date, timedelta

from grocery_builder import GroceryList, GroceryItem, CATEGORY_ORDER
from planner import WeekPlan, MealSlot
from stacker import StackingNote

DIVIDER = "\u2500" * 50   # ─ repeated 50 times
DAY_COL_WIDTH = 22         # width of the date column (left-justified)
INDENT = "  "              # 2-space indent for meal rows


def format_plan(
    plan: WeekPlan,
    grocery: GroceryList,
    stacking_notes: list[StackingNote],
) -> str:
    """Return the full terminal-formatted plan as a string."""
    lines: list[str] = []

    # ── Header ──────────────────────────────────────────────────────────────
    date_str = _fmt_date_range(plan.week_start_monday)
    lines.append(f"MEAL PLAN: {date_str}  \u00b7  {plan.cook_nights} cook nights")
    lines.append(DIVIDER)

    # ── Rationale (only when non-empty) ─────────────────────────────────────
    if plan.rationale:
        lines.append("")
        lines.append("RATIONALE")
        lines.append("")
        wrapped = textwrap.fill(plan.rationale, width=70)
        lines.append(wrapped)
        lines.append("")
        lines.append(DIVIDER)

    # ── This week ───────────────────────────────────────────────────────────
    lines.append("THIS WEEK")
    lines.append("")

    for slot in sorted(plan.dinners, key=lambda s: s.date):
        day_str  = slot.date.strftime("%A, %b %-d")
        label    = _slot_label(slot)
        lines.append(f"{INDENT}{day_str:<{DAY_COL_WIDTH}}{label}")

    lines.append("")

    # ── Lunch this week ──────────────────────────────────────────────────────
    if plan.lunch:
        lines.append("LUNCH THIS WEEK")
        lines.append("")
        lines.append(f"{INDENT}{plan.lunch.label}")
        for note in plan.lunch.notes:
            lines.append(f"{INDENT}\u2691 {note}")   # ⚑
        lines.append("")

    # ── Stacking notes ───────────────────────────────────────────────────────
    if stacking_notes:
        lines.append("STACKING NOTES")
        lines.append("")
        for note in stacking_notes:
            lines.append(f"{INDENT}\u2022 {note.message}")   # •
        lines.append("")

    # ── Grocery list ─────────────────────────────────────────────────────────
    lines.append("GROCERY LIST")
    lines.append("")

    has_items = False
    for cat in CATEGORY_ORDER:
        items = grocery.category_items(cat)
        if not items:
            continue
        has_items = True
        lines.append(f"{INDENT}{cat.upper()}")
        item_strs = []
        for item in sorted(items, key=lambda i: i.name.lower()):
            qty = _fmt_quantity(item)
            item_strs.append(f"{item.name} \u2014 {qty}" if qty else item.name)
        lines.append(f"{INDENT}  " + "  \u00b7  ".join(item_strs))
        lines.append("")

    if not has_items:
        lines.append(f"{INDENT}(no grocery items this week)")
        lines.append("")

    # ── Likely on hand ───────────────────────────────────────────────────────
    if grocery.likely_on_hand:
        lines.append("LIKELY ON HAND (confirm before buying)")
        for item in sorted(grocery.likely_on_hand, key=lambda i: i.name.lower()):
            lines.append(f"{INDENT}\u2022 {item.name}")
        lines.append("")

    lines.append(DIVIDER)

    return "\n".join(lines)


def print_plan(
    plan: WeekPlan,
    grocery: GroceryList,
    stacking_notes: list[StackingNote],
) -> None:
    """Print the formatted plan to stdout."""
    print(format_plan(plan, grocery, stacking_notes))


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_date_range(monday: date) -> str:
    sunday = monday + timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday.strftime('%b %-d')} \u2013 {sunday.strftime('%-d')}"
    return f"{monday.strftime('%b %-d')} \u2013 {sunday.strftime('%b %-d')}"


def _slot_label(slot: MealSlot) -> str:
    """Build the display string for a single meal slot, with inline flags."""
    # Note slots (out / cook)
    if slot.note_type == "out":
        return f"{slot.label}  [out]"
    if slot.note_type == "cook":
        return f"{slot.label}  [cook \u2014 add ingredients manually]"

    # Build base label
    parts = [slot.label]

    # Add pizza emoji for pizza slots
    if "pizza" in slot.tags and not slot.is_no_cook:
        parts = [f"{slot.label} \U0001f355"]   # 🍕

    # Tag badges
    if "vegetarian" in slot.tags:
        parts.append("  [vegetarian]")
    elif "vegan" in slot.tags:
        parts.append("  [vegan]")
    elif "pescatarian" in slot.tags:
        parts.append("  [pescatarian]")
    if "experiment" in slot.tags:
        parts.append("  [experiment]")

    # No-cook: show reason in parens, then meatless annotation if applicable
    if slot.is_no_cook:
        # Show reason (first note that isn't the meatless annotation)
        reason_notes = [
            n for n in slot.notes
            if n not in ("Would have been meatless", "Fasting day", "Meatless day")
        ]
        if reason_notes:
            parts.append(f"  ({reason_notes[0]})")
        # Would-have-been-meatless flag
        if "Would have been meatless" in slot.notes or (
            slot.is_meatless and slot.date.weekday() in (2, 4)
        ):
            parts.append("  [would have been meatless]")

    return "".join(parts)


def _fmt_quantity(item: GroceryItem) -> str:
    parts = []
    if item.quantity:
        parts.append(item.quantity)
    if item.unit:
        parts.append(item.unit)
    qty = " ".join(parts)
    if item.quantity_note:
        qty = f"{qty}  [{item.quantity_note}]" if qty else f"[{item.quantity_note}]"
    return qty
