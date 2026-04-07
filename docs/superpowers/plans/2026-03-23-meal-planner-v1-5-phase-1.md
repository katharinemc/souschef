# Meal Planner v1.5 Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the deterministic planner core with a Claude-as-orchestrator agent (agentic_planner.py), add terminal output formatting, new meal note types, experiment ratings, and a Phase 1 stdin reply loop — all CLI-driven, no email changes.

**Architecture:** The agentic planner wraps Claude with 10 tool-use functions (calendar, recipe queries, meal assignment, finalization); all downstream logic (stacker, grocery builder, output formatting) stays deterministic. The existing deterministic `planner.py` is preserved as a fallback and its dataclasses are shared by both planners.

**Tech Stack:** Python 3.11+, Anthropic Python SDK (`anthropic`), SQLite via `state_store.py`, PyYAML, `unittest` + `unittest.mock` for tests, `pytest` as test runner.

---

## Spec References

- `prd_v1_5.md` — full feature spec
- Section 4.3 — system prompt rules (household rules, preferences, conflict handling, output expectations)
- Section 5 — exact terminal output format
- Section 10 — reply intent types
- Section 11 — data model changes

---

## File Structure

### Modified
| File | Change |
|---|---|
| `planner.py` | Add `rationale: str = ""` to `WeekPlan`; add `note_type: Optional[str] = None` and `note_text: Optional[str] = None` to `MealSlot`; update `to_dict()` to include new fields |
| `state_store.py` | Add `experiment_ratings` and `meal_notes` tables; add 4 new methods |
| `reply_handler.py` | Add `read_stdin_reply()`; add 5 new intent types to system prompt and handler |
| `main.py` | Replace `cmd_plan` with Phase 1 version (stdin loop); add `cmd_rate`; add `--legacy` flag to plan subcommand; stub `--email` flag |

### Created
| File | Responsibility |
|---|---|
| `agentic_planner.py` | Agent class, 10 tool functions, system prompt, agent loop, fallback to deterministic planner |
| `output_formatter.py` | `format_plan()` and `print_plan()` — terminal output matching PRD section 5 exactly |
| `atk_importer.py` | Stub only — raises `NotImplementedError` |
| `test_agentic_planner.py` | Tests for tools in isolation + mocked agent loop |
| `test_output_formatter.py` | Tests for formatter output correctness |

### Unchanged
`calendar_reader.py`, `stacker.py`, `grocery_builder.py`, `email_sender.py`, `recipe_loader.py`, `paprika_import.py`

---

## Build Order

Tasks are sequenced to maintain a working codebase at every step — each task can be run and tested before moving to the next.

1. Data model changes (`planner.py`) — existing tests must keep passing
2. StateStore additions — existing tests must keep passing
3. `output_formatter.py` — no agent dependency
4. `agentic_planner.py` tools in isolation
5. `agentic_planner.py` agent loop (mocked API)
6. `reply_handler.py` updates
7. `main.py` Phase 1 wiring
8. `atk_importer.py` stub

---

## Task 1: Data Model Changes

**Files:**
- Modify: `planner.py` (MealSlot and WeekPlan dataclasses, to_dict)
- Verify: `test_planner.py` (existing tests — no new tests needed)

The `WeekPlan.rationale` field and `MealSlot.note_type`/`note_text` fields use defaults so all existing construction sites continue to work without change.

- [ ] **Step 1: Add fields to `MealSlot`**

In `planner.py`, find the `MealSlot` dataclass (line ~57). Add two new optional fields after `is_fasting`:

```python
@dataclass
class MealSlot:
    """A single assigned meal slot."""
    date:        date
    slot:        str
    recipe_id:   Optional[str]
    label:       str
    tags:        list[str]
    ingredients: list[dict]
    notes:       list[str] = field(default_factory=list)
    is_no_cook:  bool = False
    is_meatless: bool = False
    is_fasting:  bool = False
    note_type:   Optional[str] = None   # 'out' or 'cook' — None for recipe/no-cook slots
    note_text:   Optional[str] = None   # freeform label text for note slots
```

- [ ] **Step 2: Add `rationale` field to `WeekPlan`**

In `planner.py`, find the `WeekPlan` dataclass (line ~76). Add `rationale` after `warnings`:

```python
@dataclass
class WeekPlan:
    week_start_monday: date
    week_key:          str
    dinners:           list[MealSlot] = field(default_factory=list)
    lunch:             Optional[MealSlot] = None
    cook_nights:       int = 0
    warnings:          list[str] = field(default_factory=list)
    rationale:         str = ""
```

- [ ] **Step 3: Update `WeekPlan.to_dict()` to include new fields**

In `to_dict()`, add `rationale` to the top-level dict and `note_type`/`note_text` to each dinner entry:

```python
def to_dict(self) -> dict:
    return {
        "week_start_monday": self.week_start_monday.isoformat(),
        "week_key":          self.week_key,
        "cook_nights":       self.cook_nights,
        "warnings":          self.warnings,
        "rationale":         self.rationale,           # NEW
        "dinners": [
            {
                "date":        s.date.isoformat(),
                "weekday":     s.weekday_name,
                "slot":        s.slot,
                "recipe_id":   s.recipe_id,
                "label":       s.label,
                "tags":        s.tags,
                "notes":       s.notes,
                "is_no_cook":  s.is_no_cook,
                "is_meatless": s.is_meatless,
                "is_fasting":  s.is_fasting,
                "note_type":   s.note_type,            # NEW
                "note_text":   s.note_text,            # NEW
                "ingredients": s.ingredients,
            }
            for s in self.dinners
        ],
        "lunch": {
            "lunch_id":    self.lunch.recipe_id,
            "label":       self.lunch.label,
            "notes":       self.lunch.notes,
            "ingredients": self.lunch.ingredients,
        } if self.lunch else None,
    }
```

- [ ] **Step 4: Update `_reconstruct_plan` in `reply_handler.py` to read new fields**

In `reply_handler.py`, find `_reconstruct_plan` (~line 517). When building MealSlot objects from stored dict, add the new fields:

```python
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
    note_type=d.get("note_type"),     # NEW
    note_text=d.get("note_text"),     # NEW
)
```

Also update the WeekPlan construction to include rationale:
```python
plan = WeekPlan(
    week_start_monday=monday,
    week_key=plan_dict["week_key"],
    cook_nights=plan_dict.get("cook_nights", 0),
    warnings=list(plan_dict.get("warnings", [])),
    rationale=plan_dict.get("rationale", ""),    # NEW
)
```

- [ ] **Step 5: Run existing tests — all must pass**

```bash
cd /Users/glenmcleod/Desktop/katharinecode/souschef
python -m pytest test_planner.py test_reply_handler.py -v
```

Expected: all tests pass. If any fail, the new fields are breaking backward compat — check that defaults are set.

- [ ] **Step 6: Commit**

```bash
git add planner.py reply_handler.py
git commit -m "feat: add rationale to WeekPlan and note_type/note_text to MealSlot"
```

---

## Task 2: StateStore Additions

**Files:**
- Modify: `state_store.py`
- Modify: `test_state_store.py` (add new test class)

- [ ] **Step 1: Write the failing tests**

Add a new test class to `test_state_store.py`:

```python
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest test_state_store.py::TestExperimentRatings test_state_store.py::TestMealNotes -v
```

Expected: `AttributeError: 'StateStore' object has no attribute 'record_experiment_rating'`

- [ ] **Step 3: Add schema additions to `state_store.py`**

In `state_store.py`, append to the `SCHEMA` string (after the existing `CREATE INDEX` statements):

```python
SCHEMA = """
... (existing schema) ...

CREATE TABLE IF NOT EXISTS experiment_ratings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id       TEXT NOT NULL,
    rated_at        TEXT NOT NULL,
    stars           INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 5),
    promoted        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meal_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    week_key        TEXT NOT NULL,
    meal_date       TEXT NOT NULL,
    note_type       TEXT NOT NULL CHECK (note_type IN ('out', 'cook')),
    note_text       TEXT NOT NULL
);
"""
```

- [ ] **Step 4: Add new methods to `StateStore`**

Add after the existing `purge_now` method:

```python
# -----------------------------------------------------------------------
# Experiment ratings
# -----------------------------------------------------------------------

def record_experiment_rating(
    self,
    recipe_id: str,
    stars: int,
    promoted: bool = False,
):
    """Record a star rating for an experiment recipe."""
    from datetime import datetime
    rated_at = datetime.now().isoformat(timespec="seconds")
    with self._transaction():
        self._conn.execute("""
            INSERT INTO experiment_ratings (recipe_id, rated_at, stars, promoted)
            VALUES (?, ?, ?, ?)
        """, (recipe_id, rated_at, stars, int(promoted)))
    log.info("Recorded %d-star rating for %s", stars, recipe_id)

def get_experiment_ratings(self, recipe_id: str) -> list[dict]:
    """Return all ratings for a recipe, oldest first."""
    rows = self._conn.execute(
        "SELECT recipe_id, rated_at, stars, promoted FROM experiment_ratings "
        "WHERE recipe_id = ? ORDER BY rated_at ASC",
        (recipe_id,)
    ).fetchall()
    return [dict(r) for r in rows]

def promote_experiment(self, recipe_id: str):
    """Set promoted=1 on the latest rating for this recipe."""
    with self._transaction():
        self._conn.execute("""
            UPDATE experiment_ratings
            SET promoted = 1
            WHERE recipe_id = ?
              AND id = (
                  SELECT MAX(id) FROM experiment_ratings WHERE recipe_id = ?
              )
        """, (recipe_id, recipe_id))
    log.info("Promoted experiment recipe: %s", recipe_id)

# -----------------------------------------------------------------------
# Meal notes
# -----------------------------------------------------------------------

def record_meal_note(
    self,
    week_key: str,
    meal_date: str,
    note_type: str,
    note_text: str,
):
    """Persist a freeform meal note (out or cook type)."""
    with self._transaction():
        self._conn.execute("""
            INSERT INTO meal_notes (week_key, meal_date, note_type, note_text)
            VALUES (?, ?, ?, ?)
        """, (week_key, meal_date, note_type, note_text))
    log.info("Recorded %s note for %s: %s", note_type, meal_date, note_text)
```

- [ ] **Step 5: Run the new tests — all must pass**

```bash
python -m pytest test_state_store.py -v
```

Expected: all tests pass including existing tests.

- [ ] **Step 6: Commit**

```bash
git add state_store.py test_state_store.py
git commit -m "feat: add experiment_ratings and meal_notes tables to StateStore"
```

---

## Task 3: output_formatter.py

**Files:**
- Create: `output_formatter.py`
- Create: `test_output_formatter.py`

The formatter matches PRD section 5 exactly. Key layout decisions:
- Divider: 50 `─` characters
- Day column: 22 chars wide (date string left-padded), 2-space leading indent
- Items within a grocery category: joined on one line with `  ·  `
- LIKELY ON HAND: bullet list with `  •` prefix
- Rationale section only appears when `plan.rationale` is non-empty
- The final prompt line is NOT printed here — it belongs in `main.py`

- [ ] **Step 1: Write the failing tests**

Create `test_output_formatter.py`:

```python
"""
test_output_formatter.py

Run with: python -m pytest test_output_formatter.py -v
"""

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from planner import WeekPlan, MealSlot
from grocery_builder import GroceryList, GroceryItem
from stacker import StackingNote


MONDAY = date(2026, 3, 23)


def make_slot(d, rid=None, label="Test Meal", tags=None,
              is_no_cook=False, is_meatless=False, is_fasting=False,
              notes=None, note_type=None, note_text=None):
    return MealSlot(
        date=d, slot="dinner",
        recipe_id=rid, label=label,
        tags=tags or [],
        ingredients=[],
        notes=notes or [],
        is_no_cook=is_no_cook,
        is_meatless=is_meatless,
        is_fasting=is_fasting,
        note_type=note_type,
        note_text=note_text,
    )


def make_plan(rationale="") -> WeekPlan:
    plan = WeekPlan(
        week_start_monday=MONDAY,
        week_key="2026-03-23",
        cook_nights=3,
        rationale=rationale,
    )
    for i in range(7):
        d = MONDAY + timedelta(days=i)
        plan.dinners.append(make_slot(d, rid=f"recipe-{i}", label=f"Meal {i}"))
    plan.lunch = make_slot(MONDAY, rid="lunch-1", label="Quinoa bowl")
    plan.lunch.slot = "lunch"
    return plan


def make_grocery() -> GroceryList:
    g = GroceryList(week_start_monday=MONDAY)
    g.items_by_category = {
        "produce": [GroceryItem("onion", "onion", "produce", "1", None)],
        "protein": [],
        "dairy": [],
        "pantry": [],
        "frozen": [],
        "other": [],
    }
    g.likely_on_hand = []
    return g


class TestFormatPlan(unittest.TestCase):
    def setUp(self):
        from output_formatter import format_plan
        self.format_plan = format_plan

    def test_returns_string(self):
        plan = make_plan()
        result = self.format_plan(plan, make_grocery(), [])
        self.assertIsInstance(result, str)

    def test_contains_all_seven_day_names(self):
        plan = make_plan()
        result = self.format_plan(plan, make_grocery(), [])
        for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]:
            self.assertIn(day, result)

    def test_rationale_section_appears_when_nonempty(self):
        plan = make_plan(rationale="Busy week — scheduled accordingly.")
        result = self.format_plan(plan, make_grocery(), [])
        self.assertIn("RATIONALE", result)
        self.assertIn("Busy week", result)

    def test_rationale_section_absent_when_empty(self):
        plan = make_plan(rationale="")
        result = self.format_plan(plan, make_grocery(), [])
        self.assertNotIn("RATIONALE", result)

    def test_out_note_displays_out_flag(self):
        plan = make_plan()
        plan.dinners[1] = make_slot(
            MONDAY + timedelta(days=1),
            label="Dinner at Sarah's",
            is_no_cook=True,
            note_type="out",
            note_text="Dinner at Sarah's",
        )
        result = self.format_plan(plan, make_grocery(), [])
        self.assertIn("[out]", result)
        self.assertIn("Dinner at Sarah's", result)

    def test_cook_note_displays_cook_flag(self):
        plan = make_plan()
        plan.dinners[6] = make_slot(
            MONDAY + timedelta(days=6),
            label="Waffles for dinner",
            note_type="cook",
            note_text="Waffles for dinner",
        )
        result = self.format_plan(plan, make_grocery(), [])
        self.assertIn("[cook", result)
        self.assertIn("Waffles for dinner", result)

    def test_would_have_been_meatless_on_no_cook_wednesday(self):
        plan = make_plan()
        wednesday = MONDAY + timedelta(days=2)  # Wednesday
        plan.dinners[2] = make_slot(
            wednesday,
            label="no cook",
            is_no_cook=True,
            is_meatless=True,
            notes=["Would have been meatless"],
        )
        result = self.format_plan(plan, make_grocery(), [])
        self.assertIn("[would have been meatless]", result)

    def test_grocery_list_produce_section(self):
        plan = make_plan()
        result = self.format_plan(plan, make_grocery(), [])
        self.assertIn("PRODUCE", result)
        self.assertIn("onion", result)

    def test_likely_on_hand_appears_only_when_nonempty(self):
        plan = make_plan()
        grocery = make_grocery()

        result_no_hand = self.format_plan(plan, grocery, [])
        self.assertNotIn("LIKELY ON HAND", result_no_hand)

        grocery.likely_on_hand = [
            GroceryItem("garam masala", "garam masala", "pantry", None, None)
        ]
        result_with_hand = self.format_plan(plan, grocery, [])
        self.assertIn("LIKELY ON HAND", result_with_hand)
        self.assertIn("garam masala", result_with_hand)

    def test_stacking_notes_appear(self):
        plan = make_plan()
        notes = [StackingNote(kind="BATCH", canonical="rice",
                              meals=["Meal 1", "Meal 3"],
                              message="Thursday and Saturday both use rice.")]
        result = self.format_plan(plan, make_grocery(), notes)
        self.assertIn("STACKING NOTES", result)
        self.assertIn("Thursday and Saturday both use rice.", result)

    def test_stacking_notes_absent_when_empty(self):
        plan = make_plan()
        result = self.format_plan(plan, make_grocery(), [])
        self.assertNotIn("STACKING NOTES", result)


class TestPrintPlan(unittest.TestCase):
    def test_print_plan_calls_print(self):
        from output_formatter import print_plan, format_plan
        plan = make_plan()
        with patch("builtins.print") as mock_print:
            print_plan(plan, make_grocery(), [])
        mock_print.assert_called_once()
        args = mock_print.call_args[0][0]
        self.assertIsInstance(args, str)
        self.assertIn("MEAL PLAN", args)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest test_output_formatter.py -v
```

Expected: `ModuleNotFoundError: No module named 'output_formatter'`

- [ ] **Step 3: Implement `output_formatter.py`**

Create `output_formatter.py`:

```python
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
from typing import Optional

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
```

- [ ] **Step 4: Run the tests — all must pass**

```bash
python -m pytest test_output_formatter.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Verify existing test suite still passes**

```bash
python -m pytest test_planner.py test_state_store.py test_reply_handler.py test_stacker.py test_grocery_builder.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add output_formatter.py test_output_formatter.py
git commit -m "feat: add output_formatter for terminal plan display"
```

---

## Task 4: agentic_planner.py — Tools and System Prompt

**Files:**
- Create: `agentic_planner.py`
- Create: `test_agentic_planner.py` (tool-level tests)

Build the 10 tool functions and system prompt. Do NOT wire the agent loop yet — just make sure tools are testable in isolation.

- [ ] **Step 1: Write failing tests for tool constraint validation**

Create `test_agentic_planner.py`:

```python
"""
test_agentic_planner.py

Run with: python -m pytest test_agentic_planner.py -v
"""

import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

from calendar_reader import WeekConstraints, DayConstraints
from state_store import StateStore


MONDAY = date(2026, 3, 23)


def make_constraints(monday=MONDAY, open_saturday=True) -> WeekConstraints:
    wc = WeekConstraints(week_start_monday=monday)
    for i in range(7):
        d = monday + timedelta(days=i)
        dc = DayConstraints(date=d)
        if d.weekday() == 5:   # Saturday
            dc.is_open_saturday = open_saturday
        wc.days.append(dc)
    return wc


VEGETARIAN_RECIPE = {
    "id": "spiced-rice",
    "name": "Spiced Rice with Crispy Chickpeas",
    "tags": ["vegetarian", "onRotation"],
    "servings": "4",
    "ingredients": [{"name": "rice", "quantity": "1", "unit": "cup"}],
    "cook_time": "30",
    "prep_time": "10",
}

MEAT_RECIPE = {
    "id": "hamburger-steaks",
    "name": "Hamburger Steaks",
    "tags": ["onRotation"],
    "servings": "4",
    "ingredients": [{"name": "ground beef", "quantity": "1", "unit": "lb"}],
    "cook_time": "30",
    "prep_time": "10",
}

EXPERIMENT_RECIPE = {
    "id": "merguez",
    "name": "Homemade Merguez",
    "tags": ["experiment"],
    "servings": "4",
    "ingredients": [{"name": "ground lamb", "quantity": "1", "unit": "lb"}],
    "cook_time": "45",
    "prep_time": "20",
}

PIZZA_RECIPE = {
    "id": "homemade-pizza",
    "name": "Homemade pizza",
    "tags": ["pizza"],
    "servings": "4",
    "ingredients": [],
    "cook_time": "60",
    "prep_time": "30",
}


class TestAgenticPlannerTools(unittest.TestCase):
    """Test each tool function in isolation without running the agent loop."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = StateStore(self.tmp.name)
        self.cfg = {
            "recipe_dir": "recipes_yaml",
            "lunch_file":  "lunches.yaml",
            "db_path":     self.tmp.name,
        }
        self.constraints = make_constraints()

    def tearDown(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _make_planner_with_recipes(self, recipes):
        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)
        planner._constraints = self.constraints
        planner._assigned = {}
        planner._lunch = None
        planner._rationale = ""
        planner._finalized = False
        planner.recipes = {r["id"]: r for r in recipes}
        planner.lunches = []
        return planner

    # --- assign_meal constraint validation ---

    def test_assign_meat_to_wednesday_returns_error(self):
        planner = self._make_planner_with_recipes([MEAT_RECIPE])
        result = planner._tool_assign_meal("Wednesday", "hamburger-steaks")
        self.assertFalse(result["ok"])
        self.assertIn("meatless", result["error"].lower())

    def test_assign_meat_to_friday_returns_error(self):
        planner = self._make_planner_with_recipes([MEAT_RECIPE])
        result = planner._tool_assign_meal("Friday", "hamburger-steaks")
        self.assertFalse(result["ok"])
        self.assertIn("meatless", result["error"].lower())

    def test_assign_vegetarian_to_wednesday_succeeds(self):
        planner = self._make_planner_with_recipes([VEGETARIAN_RECIPE])
        result = planner._tool_assign_meal("Wednesday", "spiced-rice")
        self.assertTrue(result["ok"])
        self.assertEqual(result["day"], "Wednesday")

    def test_assign_experiment_to_non_saturday_returns_error(self):
        planner = self._make_planner_with_recipes([EXPERIMENT_RECIPE])
        result = planner._tool_assign_meal("Monday", "merguez")
        self.assertFalse(result["ok"])
        self.assertIn("experiment", result["error"].lower())

    def test_assign_experiment_to_saturday_succeeds(self):
        planner = self._make_planner_with_recipes([EXPERIMENT_RECIPE])
        result = planner._tool_assign_meal("Saturday", "merguez")
        self.assertTrue(result["ok"])

    def test_assign_pizza_to_non_friday_returns_error(self):
        planner = self._make_planner_with_recipes([PIZZA_RECIPE])
        result = planner._tool_assign_meal("Monday", "homemade-pizza")
        self.assertFalse(result["ok"])
        self.assertIn("friday", result["error"].lower())

    def test_assign_same_day_twice_returns_error(self):
        planner = self._make_planner_with_recipes([MEAT_RECIPE, VEGETARIAN_RECIPE])
        planner._tool_assign_meal("Monday", "hamburger-steaks")
        result = planner._tool_assign_meal("Monday", "spiced-rice")
        self.assertFalse(result["ok"])
        self.assertIn("already assigned", result["error"].lower())

    def test_assign_unknown_recipe_returns_error(self):
        planner = self._make_planner_with_recipes([])
        result = planner._tool_assign_meal("Monday", "no-such-recipe")
        self.assertFalse(result["ok"])

    # --- assign_no_cook ---

    def test_assign_no_cook_succeeds(self):
        planner = self._make_planner_with_recipes([])
        result = planner._tool_assign_no_cook("Tuesday", "KRM event after 3pm")
        self.assertTrue(result["ok"])
        self.assertEqual(result["label"], "no cook")
        self.assertIn("Tuesday", planner._assigned)

    # --- assign_note ---

    def test_assign_note_out(self):
        planner = self._make_planner_with_recipes([])
        result = planner._tool_assign_note("Tuesday", "Dinner at Sarah's", "out")
        self.assertTrue(result["ok"])
        self.assertEqual(result["note_type"], "out")
        slot = planner._assigned["Tuesday"]
        self.assertEqual(slot.note_type, "out")
        self.assertEqual(slot.label, "Dinner at Sarah's")

    def test_assign_note_cook(self):
        planner = self._make_planner_with_recipes([])
        result = planner._tool_assign_note("Sunday", "Waffles for dinner", "cook")
        self.assertTrue(result["ok"])
        slot = planner._assigned["Sunday"]
        self.assertEqual(slot.note_type, "cook")

    # --- finalize_plan ---

    def test_finalize_plan_sets_flag_and_rationale(self):
        planner = self._make_planner_with_recipes([])
        self.assertFalse(planner._finalized)
        result = planner._tool_finalize_plan("Busy week — kept it simple.")
        self.assertTrue(result["ok"])
        self.assertTrue(planner._finalized)
        self.assertEqual(planner._rationale, "Busy week — kept it simple.")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest test_agentic_planner.py::TestAgenticPlannerTools -v
```

Expected: `ModuleNotFoundError: No module named 'agentic_planner'`

- [ ] **Step 3: Implement `agentic_planner.py` — tools only (no agent loop yet)**

Create `agentic_planner.py` with the tool functions, system prompt builder, and `TOOLS` list (no agent loop yet — `plan_week` can raise `NotImplementedError` for now):

```python
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
    load_recipes, load_lunches,
    _ingredients_from_recipe, _recipe_is_meatless, _recipe_has_tag, _week_key,
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
        # Populated at plan_week() time
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
            log.error("anthropic package not installed — check ANTHROPIC_API_KEY and run: pip install anthropic")
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

                # Collect tool_use blocks from the response
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

                # Append assistant turn and tool results
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})

            if tool_call_count >= MAX_TOOL_CALLS and not self._finalized:
                log.warning(
                    "Agent hit %d-tool-call limit without finalizing — falling back to deterministic planner",
                    MAX_TOOL_CALLS,
                )
                return self._fallback(constraints, f"Tool call limit ({MAX_TOOL_CALLS}) reached")

        except Exception as e:
            log.error(
                "Agentic planner failed (%s) — falling back to deterministic planner. "
                "Check ANTHROPIC_API_KEY is set correctly.",
                e,
            )
            return self._fallback(constraints, str(e))

        return self._build_week_plan(constraints)

    # -----------------------------------------------------------------------
    # Fallback
    # -----------------------------------------------------------------------

    def _fallback(self, constraints: WeekConstraints, reason: str) -> WeekPlan:
        """Fall back to the deterministic planner and annotate the plan."""
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
            "get_calendar_constraints":  self._tool_get_calendar_constraints,
            "get_recipes":               self._tool_get_recipes,
            "get_rotation_history":      self._tool_get_rotation_history,
            "get_recent_meals":          self._tool_get_recent_meals,
            "assign_meal":               lambda i: self._tool_assign_meal(
                                            i.get("day", ""), i.get("recipe_id", ""), i.get("notes", "")
                                         ),
            "assign_no_cook":            lambda i: self._tool_assign_no_cook(
                                            i.get("day", ""), i.get("reason", "")
                                         ),
            "assign_note":               lambda i: self._tool_assign_note(
                                            i.get("day", ""), i.get("note_text", ""), i.get("note_type", "out")
                                         ),
            "assign_lunch":              lambda i: self._tool_assign_lunch(i.get("lunch_id", "")),
            "get_stacking_opportunities":lambda i: self._tool_get_stacking_opportunities(
                                            i.get("recipe_ids", [])
                                         ),
            "finalize_plan":             lambda i: self._tool_finalize_plan(i.get("rationale", "")),
        }
        fn = dispatch.get(name)
        if fn is None:
            return {"error": f"Unknown tool: {name}"}
        try:
            if name in ("assign_meal", "assign_no_cook", "assign_note", "assign_lunch",
                        "get_stacking_opportunities", "finalize_plan",
                        "get_recipes", "get_rotation_history", "get_recent_meals"):
                return fn(input_dict)
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
                "date":            dc.date.isoformat(),
                "weekday":         dc.date.strftime("%A"),
                "is_no_cook":      dc.is_no_cook,
                "is_fasting":      dc.is_fasting,
                "is_open_saturday":dc.is_open_saturday,
                "no_cook_reason":  dc.no_cook_reason,
            })
        return {
            "week_start_monday":  wc.week_start_monday.isoformat(),
            "sunday_needs_easy":  wc.sunday_needs_easy,
            "days":               days,
            "no_cook_count":      len(wc.no_cook_days()),
            "fasting_days":       [dc.date.isoformat() for dc in wc.fasting_days()],
            "open_saturday":      bool(wc.open_saturday()),
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
                "last_planned": None,  # rotation history comes from get_rotation_history
                "cook_time":    recipe.get("cook_time"),
                "prep_time":    recipe.get("prep_time"),
            })
        return {"recipes": results, "count": len(results)}

    def _tool_get_rotation_history(self, input_dict: dict) -> dict:
        recipe_ids = input_dict.get("recipe_ids")
        if self.store:
            all_last = self.store.get_all_last_planned()
        else:
            all_last = {}

        if recipe_ids:
            result = {rid: (all_last.get(rid).isoformat() if all_last.get(rid) else None)
                      for rid in recipe_ids}
        else:
            result = {rid: d.isoformat() for rid, d in all_last.items()}
        return result

    def _tool_get_recent_meals(self, input_dict: dict) -> dict:
        weeks = input_dict.get("weeks", 2)
        if self.store:
            rows = self.store.get_recent_meals(weeks=weeks)
        else:
            rows = []
        return {"meals": rows, "count": len(rows)}

    def _tool_assign_meal(self, day: str, recipe_id: str, notes: str = "") -> dict:
        day = day.strip().title()

        if day in self._assigned:
            return {
                "ok": False,
                "error": f"{day} is already assigned to '{self._assigned[day].label}'. "
                         f"If you want to change it, note that re-assigning is not supported — "
                         f"plan around it.",
            }

        recipe = self.recipes.get(recipe_id)
        if recipe is None:
            return {"ok": False, "error": f"Recipe '{recipe_id}' not found in the library."}

        dc = self._get_dc_for_day(day)
        if dc is None:
            return {"ok": False, "error": f"Unknown day: '{day}'."}

        tags    = set(recipe.get("tags") or [])
        weekday = dc.date.weekday()

        # Pizza only on Friday
        if "pizza" in tags and weekday != 4:
            return {"ok": False, "error": "Pizza recipes can only be assigned to Friday."}

        # Experiment only on Saturday
        if "experiment" in tags and weekday != 5:
            return {"ok": False, "error": "Experiment recipes can only be assigned to Saturday."}

        # Meatless constraint
        needs_meatless = weekday in ALWAYS_MEATLESS_WEEKDAYS or dc.is_fasting
        is_meatless    = bool(tags & MEATLESS_TAGS)
        if needs_meatless and not is_meatless:
            reason = (
                "Wednesday" if weekday == 2 else
                "Friday" if weekday == 4 else
                "fasting day"
            )
            return {
                "ok": False,
                "error": f"{day} requires a meatless recipe ({reason} rule). "
                         f"This recipe appears to contain meat (no vegetarian/pescatarian/vegan tag).",
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
        log.debug("Assigned %s → %s", day, recipe["name"])
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
        recipe_ids = input_dict if isinstance(input_dict, list) else input_dict.get("recipe_ids", [])
        from stacker import Stacker
        from planner import WeekPlan as WP

        # Build synthetic plan with just the requested recipes
        monday = self._constraints.week_start_monday
        synthetic = WP(week_start_monday=monday, week_key=_week_key(monday))
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
            d      = monday + timedelta(days=i)
            day_name = d.strftime("%A")
            slot = self._assigned.get(day_name)
            if slot is None:
                # Agent left this day unassigned — use leftovers
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
            if not s.is_no_cook and s.recipe_id and s.note_type != "out"
        )
        if plan.cook_nights == 0 and any(s.note_type == "cook" for s in plan.dinners):
            plan.cook_nights += sum(1 for s in plan.dinners if s.note_type == "cook")

        return plan
```

- [ ] **Step 4: Run tool-level tests — all must pass**

```bash
python -m pytest test_agentic_planner.py::TestAgenticPlannerTools -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add agentic_planner.py test_agentic_planner.py
git commit -m "feat: add agentic_planner tool functions and system prompt"
```

---

## Task 5: agentic_planner.py — Agent Loop Tests

**Files:**
- Modify: `test_agentic_planner.py` (add agent loop tests with mocked API)

The agent loop is already implemented in Task 4's `plan_week()` — now we test it by mocking the Anthropic client.

- [ ] **Step 1: Add agent loop tests to `test_agentic_planner.py`**

Add a new test class at the bottom of `test_agentic_planner.py`:

```python
class TestAgenticPlannerLoop(unittest.TestCase):
    """Test the agent loop using a mocked Anthropic client."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = StateStore(self.tmp.name)
        self.cfg = {
            "recipe_dir": "recipes_yaml",
            "lunch_file":  "lunches.yaml",
            "db_path":     self.tmp.name,
            "model":       "claude-opus-4-5",
        }
        self.constraints = make_constraints()

    def tearDown(self):
        self.store.close()
        Path(self.tmp.name).unlink(missing_ok=True)

    def _make_tool_use_response(self, tool_name, tool_id, tool_input):
        """Helper: build a mock API response with a single tool_use block."""
        block = MagicMock()
        block.type = "tool_use"
        block.name = tool_name
        block.id = tool_id
        block.input = tool_input

        response = MagicMock()
        response.stop_reason = "tool_use"
        response.content = [block]
        return response

    def _make_end_turn_response(self):
        response = MagicMock()
        response.stop_reason = "end_turn"
        response.content = []
        return response

    @patch('agentic_planner.anthropic')
    @patch('agentic_planner.load_recipes', return_value={})
    @patch('agentic_planner.load_lunches', return_value=[])
    def test_finalize_plan_stops_loop(self, mock_lunches, mock_recipes, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        call_count = [0]
        def side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return self._make_tool_use_response(
                    "finalize_plan", "call_1",
                    {"rationale": "Simple week — no complex decisions."}
                )
            return self._make_end_turn_response()

        mock_client.messages.create.side_effect = side_effect

        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)
        plan = planner.plan_week(self.constraints)

        self.assertEqual(plan.rationale, "Simple week — no complex decisions.")
        # Only 1 API call needed (first call returns finalize_plan)
        self.assertEqual(mock_client.messages.create.call_count, 1)

    @patch('agentic_planner.anthropic')
    @patch('agentic_planner.load_recipes', return_value={})
    @patch('agentic_planner.load_lunches', return_value=[])
    def test_fallback_on_api_error(self, mock_lunches, mock_recipes, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.side_effect = RuntimeError("API connection refused")

        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)

        with patch('agentic_planner.Planner') as mock_planner_class:
            mock_fallback = MagicMock()
            mock_fallback.plan_week.return_value = WeekPlan(
                week_start_monday=MONDAY,
                week_key="2026-03-23",
            )
            mock_planner_class.return_value = mock_fallback

            plan = planner.plan_week(self.constraints)

        mock_fallback.plan_week.assert_called_once()
        self.assertTrue(any("fallback" in w.lower() for w in plan.warnings))

    @patch('agentic_planner.anthropic')
    @patch('agentic_planner.load_recipes', return_value={})
    @patch('agentic_planner.load_lunches', return_value=[])
    def test_30_call_limit_triggers_fallback(self, mock_lunches, mock_recipes, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        # Always return a tool_use response (never finalize_plan → hits limit)
        def side_effect(**kwargs):
            return self._make_tool_use_response(
                "get_calendar_constraints", "call_x", {}
            )
        mock_client.messages.create.side_effect = side_effect

        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)

        with patch('agentic_planner.Planner') as mock_planner_class:
            mock_fallback = MagicMock()
            mock_fallback.plan_week.return_value = WeekPlan(
                week_start_monday=MONDAY,
                week_key="2026-03-23",
            )
            mock_planner_class.return_value = mock_fallback
            plan = planner.plan_week(self.constraints)

        mock_fallback.plan_week.assert_called_once()

    @patch('agentic_planner.anthropic')
    @patch('agentic_planner.load_recipes')
    @patch('agentic_planner.load_lunches', return_value=[])
    def test_full_plan_has_seven_days(self, mock_lunches, mock_recipes, mock_anthropic):
        """Agent assigns all 7 days then finalizes."""
        RECIPES = {
            "hamburger-steaks": MEAT_RECIPE,
            "spiced-rice":      VEGETARIAN_RECIPE,
        }
        mock_recipes.return_value = RECIPES
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        # Simulate agent: assign Mon, Tue, Thu as recipes, rest no-cook, finalize
        call_seq = [0]
        def make_seq(**kwargs):
            call_seq[0] += 1
            n = call_seq[0]
            if n == 1:
                return self._make_tool_use_response("assign_meal", f"c{n}",
                    {"day": "Monday", "recipe_id": "hamburger-steaks"})
            elif n == 2:
                return self._make_tool_use_response("assign_no_cook", f"c{n}",
                    {"day": "Tuesday"})
            elif n == 3:
                return self._make_tool_use_response("assign_meal", f"c{n}",
                    {"day": "Wednesday", "recipe_id": "spiced-rice"})
            elif n == 4:
                return self._make_tool_use_response("assign_meal", f"c{n}",
                    {"day": "Thursday", "recipe_id": "hamburger-steaks"})
            elif n == 5:
                return self._make_tool_use_response("assign_no_cook", f"c{n}",
                    {"day": "Friday"})
            elif n == 6:
                return self._make_tool_use_response("assign_no_cook", f"c{n}",
                    {"day": "Saturday"})
            elif n == 7:
                return self._make_tool_use_response("assign_no_cook", f"c{n}",
                    {"day": "Sunday"})
            elif n == 8:
                return self._make_tool_use_response("finalize_plan", f"c{n}",
                    {"rationale": "Test rationale."})
            return self._make_end_turn_response()

        mock_client.messages.create.side_effect = make_seq

        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=self.cfg, store=self.store)
        plan = planner.plan_week(self.constraints)

        # All 7 days should be present (unassigned filled with leftovers)
        self.assertEqual(len(plan.dinners), 7)
        day_names = {s.weekday_name for s in plan.dinners}
        expected = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}
        self.assertEqual(day_names, expected)
        self.assertEqual(plan.rationale, "Test rationale.")
```

- [ ] **Step 2: Run agent loop tests**

```bash
python -m pytest test_agentic_planner.py -v
```

Expected: all tests pass.

- [ ] **Step 3: Run the full test suite**

```bash
python -m pytest -v
```

Expected: all existing tests pass plus new tests.

- [ ] **Step 4: Commit**

```bash
git add test_agentic_planner.py
git commit -m "test: add agent loop tests with mocked Anthropic client"
```

---

## Task 6: reply_handler.py Updates

**Files:**
- Modify: `reply_handler.py`
- Modify: `test_reply_handler.py` (add tests for new intents + stdin reader)

Add 5 new intent types to the system prompt and handler, plus the `read_stdin_reply()` function.

- [ ] **Step 1: Write failing tests for new intents**

Add to `test_reply_handler.py`:

```python
class TestNewIntentParsing(unittest.TestCase):
    """Test that the intent system prompt handles new v1.5 intent types."""

    @patch('reply_handler.anthropic')
    def test_rate_experiment_intent_parsed(self, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value.content = [
            MagicMock(text='[{"type": "rate_experiment", "day": "Saturday", "stars": 4}]')
        ]
        intents = parse_reply_intents("Rate Saturday's experiment 4 stars", model="test-model")
        self.assertEqual(intents[0]["type"], "rate_experiment")
        self.assertEqual(intents[0]["stars"], 4)

    @patch('reply_handler.anthropic')
    def test_promote_experiment_intent_parsed(self, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value.content = [
            MagicMock(text='[{"type": "promote_experiment", "recipe_id": "merguez"}]')
        ]
        intents = parse_reply_intents("Add the merguez to onRotation", model="test-model")
        self.assertEqual(intents[0]["type"], "promote_experiment")

    @patch('reply_handler.anthropic')
    def test_assign_note_out_intent_parsed(self, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value.content = [
            MagicMock(text='[{"type": "assign_note_out", "day": "Tuesday", "note_text": "Dinner at Sarah\'s"}]')
        ]
        intents = parse_reply_intents("Mark Tuesday as dinner at Sarah's", model="test-model")
        self.assertEqual(intents[0]["type"], "assign_note_out")
        self.assertEqual(intents[0]["day"], "Tuesday")

    @patch('reply_handler.anthropic')
    def test_assign_note_cook_intent_parsed(self, mock_anthropic):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.return_value.content = [
            MagicMock(text='[{"type": "assign_note_cook", "day": "Sunday", "note_text": "Waffles for dinner"}]')
        ]
        intents = parse_reply_intents("Put waffles on Sunday", model="test-model")
        self.assertEqual(intents[0]["type"], "assign_note_cook")

    def test_read_stdin_reply_done_returns_none(self):
        from reply_handler import read_stdin_reply
        with patch('builtins.input', return_value="done"):
            result = read_stdin_reply()
        self.assertIsNone(result)

    def test_read_stdin_reply_okay_thanks_returns_none(self):
        from reply_handler import read_stdin_reply
        with patch('builtins.input', return_value="okay thanks"):
            result = read_stdin_reply()
        self.assertIsNone(result)

    def test_read_stdin_reply_feedback_returns_string(self):
        from reply_handler import read_stdin_reply
        with patch('builtins.input', return_value="Swap Tuesday for pasta"):
            result = read_stdin_reply()
        self.assertEqual(result, "Swap Tuesday for pasta")
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest test_reply_handler.py::TestNewIntentParsing -v
```

Expected: `ImportError` for `read_stdin_reply` and intent parsing tests will run but may pass/fail depending on the mock structure.

- [ ] **Step 3: Update `INTENT_SYSTEM_PROMPT` in `reply_handler.py`**

In `reply_handler.py`, update `INTENT_SYSTEM_PROMPT` to add the 5 new intent types to the numbered list and examples:

```python
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
```

- [ ] **Step 4: Add `read_stdin_reply()` to `reply_handler.py`**

Add after the `_patch_planner_exports()` call at module level:

```python
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
```

- [ ] **Step 5: Add handlers for new intents in `apply_intents()`**

In `apply_intents()`, add `elif` branches after the existing `skip_experiment` handler:

```python
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
```

Add the helper function after `apply_intents`:

```python
def _promote_recipe_yaml(recipe_id: str, store: Optional[StateStore]) -> None:
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
```

- [ ] **Step 6: Run the full reply_handler test suite**

```bash
python -m pytest test_reply_handler.py -v
```

Expected: all tests pass including new ones.

- [ ] **Step 7: Commit**

```bash
git add reply_handler.py test_reply_handler.py
git commit -m "feat: add stdin reply reader and new intent types to reply_handler"
```

---

## Task 7: main.py Phase 1 Wiring

**Files:**
- Modify: `main.py`

Replace `cmd_plan` with Phase 1 version, add `cmd_rate`, add `--legacy` and `--email` stub flags.

- [ ] **Step 1: Update `cmd_plan` in `main.py`**

Replace the existing `cmd_plan` function entirely:

```python
def cmd_plan(args, cfg: dict, flat: dict):
    """Generate and display the weekly meal plan. Enters interactive reply loop."""
    from calendar_reader import CalendarReader
    from stacker import Stacker
    from grocery_builder import GroceryBuilder
    from state_store import StateStore
    from output_formatter import print_plan, format_plan
    from planner import load_recipes

    dry_run = args.dry_run
    use_legacy = getattr(args, "legacy", False)

    # Email stub
    if getattr(args, "email", False):
        print("Email delivery not yet enabled in Phase 1.")
        return

    # Determine planning week
    reader = CalendarReader(config=flat, timezone=flat.get("timezone", "America/New_York"))
    monday = reader.next_planning_monday()
    log.info("Planning week: %s – %s", monday, monday + timedelta(days=6))

    # Fetch calendar constraints
    log.info("Reading Google Calendar...")
    constraints = reader.get_week_constraints(monday)
    log.info(constraints.summary())

    # Run planner (agentic or legacy)
    store = StateStore(flat.get("db_path", "meal_planner.db"))

    if use_legacy:
        log.info("Using legacy deterministic planner (--legacy flag set)...")
        from planner import Planner
        planner = Planner(config=flat, store=store)
    else:
        log.info("Running agentic planner...")
        from agentic_planner import AgenticPlanner
        planner = AgenticPlanner(config=flat, store=store)

    plan = planner.plan_week(constraints)
    log.info("Plan complete: %d cook nights, %d warnings", plan.cook_nights, len(plan.warnings))

    # Run stacker
    stacking_notes = Stacker().analyse(plan)

    # Build grocery list
    grocery = GroceryBuilder(store=store).build(plan)

    # Print plan to terminal
    print_plan(plan, grocery, stacking_notes)

    if dry_run:
        log.info("Dry run — plan not saved to database.")
        store.close()
        return

    # Save plan
    store.record_plan(plan.week_key, plan.to_dict(), plan.to_state_meals())
    log.info("Plan saved to database (week key: %s)", plan.week_key)

    # Load recipes for apply_intents
    recipes = load_recipes(flat.get("recipe_dir", "recipes_yaml"))

    # --- Phase 1 interactive reply loop ---
    from email_sender import EmailSender
    from reply_handler import parse_reply_intents, apply_intents
    from output_formatter import print_plan

    sender = EmailSender(config=flat)

    while True:
        print("\nRespond to make changes, or type 'done' to approve: ", end="", flush=True)
        try:
            user_input = input().strip()
        except (KeyboardInterrupt, EOFError):
            log.info("Interrupted — plan not explicitly approved.")
            break

        if not user_input:
            continue

        if sender.is_acknowledgment(user_input):
            store.mark_plan_approved(plan.week_key)
            print("Plan approved.")
            break

        # Parse intents
        intents = parse_reply_intents(user_input, model=flat.get("model", "claude-sonnet-4-20250514"))

        if all(i.get("type") == "acknowledgment" for i in intents):
            store.mark_plan_approved(plan.week_key)
            print("Plan approved.")
            break

        # Apply intents and re-plan
        modified_plan, change_notes = apply_intents(plan, intents, recipes, store)

        # Handle removed on-hand items
        removed = getattr(modified_plan, "_removed_on_hand", [])

        # Re-run stacker and grocery builder
        stacking_notes = Stacker().analyse(modified_plan)
        grocery = GroceryBuilder(store=store).build(modified_plan)

        if removed:
            grocery.likely_on_hand = [
                item for item in grocery.likely_on_hand
                if not any(r in item.name.lower() for r in removed)
            ]

        # Print revised plan
        print_plan(modified_plan, grocery, stacking_notes)
        if change_notes:
            print("\nChanges made:")
            for note in change_notes:
                print(f"  • {note}")

        # Save revised plan
        store.record_plan(
            modified_plan.week_key,
            modified_plan.to_dict(),
            modified_plan.to_state_meals(),
        )
        plan = modified_plan

    store.close()
```

- [ ] **Step 2: Add `cmd_rate` to `main.py`**

Add after `cmd_plan`:

```python
def cmd_rate(args, cfg: dict, flat: dict):
    """Record a star rating for an experiment recipe."""
    from state_store import StateStore
    from planner import load_recipes
    import yaml
    from pathlib import Path

    store = StateStore(flat.get("db_path", "meal_planner.db"))
    recipes = load_recipes(flat.get("recipe_dir", "recipes_yaml"))

    # Find recipe by name or ID
    query = args.recipe.strip().lower()
    recipe = recipes.get(query)
    if recipe is None:
        # Try name match
        for r in recipes.values():
            if query in r["name"].lower():
                recipe = r
                break

    if recipe is None:
        log.error(
            "Recipe '%s' not found. Use 'python main.py status' to see available recipes, "
            "or check recipes_yaml/ for the exact ID or name.",
            args.recipe,
        )
        store.close()
        sys.exit(1)

    stars = args.stars
    if not (1 <= stars <= 5):
        log.error("Stars must be between 1 and 5.")
        store.close()
        sys.exit(1)

    promote = getattr(args, "promote", False)
    store.record_experiment_rating(recipe["id"], stars=stars, promoted=promote)

    if promote:
        store.promote_experiment(recipe["id"])
        # Update YAML to add onRotation tag
        from reply_handler import _promote_recipe_yaml
        _promote_recipe_yaml(recipe["id"], store)
        print(f"Rated '{recipe['name']}' {stars} stars and promoted to onRotation.")
    else:
        print(f"Rated '{recipe['name']}' {stars} stars.")

    store.close()
```

- [ ] **Step 3: Update `build_parser()` in `main.py`**

Add `--legacy` and `--email` flags to the `plan` subcommand, and add the `rate` subcommand. In `build_parser()`:

```python
    # plan — add flags
    p_plan = sub.add_parser("plan", help="Generate and display the weekly meal plan")
    p_plan.add_argument(
        "--legacy", action="store_true",
        help="Use the deterministic planner instead of the agentic planner"
    )
    p_plan.add_argument(
        "--email", action="store_true",
        help="[Phase 3] Send plan by email instead of printing to terminal"
    )
    p_plan.set_defaults(func=cmd_plan)

    # ... (existing reply, ingest, status, preview subcommands unchanged) ...

    # rate — new in Phase 1
    p_rate = sub.add_parser("rate", help="Record a star rating for an experiment recipe")
    p_rate.add_argument(
        "--recipe", required=True,
        help="Recipe name or ID to rate"
    )
    p_rate.add_argument(
        "--stars", type=int, required=True,
        help="Star rating (1-5)"
    )
    p_rate.add_argument(
        "--promote", action="store_true",
        help="Also promote the recipe to onRotation"
    )
    p_rate.set_defaults(func=cmd_rate)
```

- [ ] **Step 4: Also update `flatten_config()` to include the `model` key**

In `flatten_config()`, the section loop already handles `anthropic:` section. Make sure `model` propagates:

```python
    for section in ("email", "calendar", "planner", "anthropic", "reply_handler"):
        flat.update(cfg.get(section, {}))
    # Ensure anthropic model is accessible as flat["model"]
    if "model" not in flat and "anthropic_model" in flat:
        flat["model"] = flat["anthropic_model"]
```

- [ ] **Step 5: Smoke test the CLI (requires calendar credentials)**

If calendar credentials are not available, use `--legacy` to skip the agentic planner:

```bash
cd /Users/glenmcleod/Desktop/katharinecode/souschef
python main.py plan --dry-run --legacy --config config.yaml
```

Expected: plan printed to terminal, no database writes, exits cleanly.

- [ ] **Step 6: Run the full test suite**

```bash
python -m pytest -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add main.py
git commit -m "feat: wire Phase 1 CLI loop — agentic plan, stdin reply, cmd_rate"
```

---

## Task 8: atk_importer.py Stub

**Files:**
- Create: `atk_importer.py`

- [ ] **Step 1: Create the stub file**

Create `atk_importer.py`:

```python
"""
atk_importer.py

ATK recipe import via Chrome MCP.
NOT IMPLEMENTED IN PHASE 1 — stub only.

Full specification in prd_v1_5.md section 8.

Phase 1 usage: this module is imported by reply_handler when an import_atk
intent is parsed. The handler catches NotImplementedError and logs a message.
"""

import logging

log = logging.getLogger(__name__)


def find_experiment_candidates(recipe_library: dict, count: int = 3) -> list[dict]:
    """
    Browse ATK using Chrome MCP and return experiment candidates.

    Args:
        recipe_library: Existing recipes dict {id: recipe_dict}.
        count:          Number of candidates to surface.

    Returns:
        List of candidate dicts with keys: name, atk_url, description, cook_time, servings.

    NOT IMPLEMENTED IN PHASE 1 — requires Chrome MCP integration.
    """
    raise NotImplementedError(
        "ATK import requires Chrome MCP — Phase 1 stub only. "
        "See prd_v1_5.md section 8 for the full specification."
    )


def import_recipe(atk_url: str, output_dir: str) -> dict:
    """
    Import a single ATK recipe by URL.

    Args:
        atk_url:    Full URL to the ATK recipe page.
        output_dir: Directory to write the YAML file to (e.g. 'recipes_yaml/').

    Returns:
        The imported recipe dict (also written to output_dir as a YAML file).

    NOT IMPLEMENTED IN PHASE 1 — requires Chrome MCP integration.
    """
    raise NotImplementedError(
        "ATK import requires Chrome MCP — Phase 1 stub only. "
        "See prd_v1_5.md section 8 for the full specification."
    )
```

- [ ] **Step 2: Run the full test suite one final time**

```bash
python -m pytest -v 2>&1 | tail -20
```

Expected: all tests pass. Zero failures.

- [ ] **Step 3: Commit**

```bash
git add atk_importer.py
git commit -m "feat: add atk_importer stub (Phase 1 — not implemented)"
```

---

## Definition of Done Checklist

Run these verification commands after all tasks are complete:

```bash
# All tests pass
python -m pytest -v

# Dry run with legacy planner (no calendar credentials needed)
python main.py plan --dry-run --legacy

# Dry run with agentic planner (requires ANTHROPIC_API_KEY)
python main.py plan --dry-run

# Rate command
python main.py rate --recipe "some-recipe-id" --stars 4

# Status command still works
python main.py status
```

Expected outcomes:
- [ ] `python main.py plan` runs, produces plan with rationale, enters reply loop
- [ ] Replying with a swap request re-plans the affected slot and reprints
- [ ] Replying with `"Mark Tuesday as dinner at Sarah's"` assigns `[out]` note correctly
- [ ] Replying `done` approves the plan and exits cleanly
- [ ] `python main.py plan --dry-run` runs without writing to database
- [ ] `python main.py rate --recipe "X" --stars 4` records a rating without error
- [ ] `python main.py rate --recipe "X" --stars 5 --promote` promotes to onRotation
- [ ] All tests pass (existing + new)
- [ ] `planner.py` deterministic planner still works via `--legacy` flag
- [ ] `email_sender.py` is untouched (diff shows zero changes)
