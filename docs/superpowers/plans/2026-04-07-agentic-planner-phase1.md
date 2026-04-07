# Agentic Planner — Phase 1 (CLI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the deterministic `planner.py` with `agentic_planner.py` (Claude + tools), add terminal output formatting, extend the data model with note types + rationale, extend state_store with experiment rating tables, and wire `main.py` to the Phase 1 CLI flow (print to terminal, reply via stdin).

**Architecture:** `agentic_planner.py` wraps the existing deterministic helpers (calendar_reader, stacker, recipe YAML) behind tool functions and runs a Claude API agentic loop. The agent calls tools to gather context and assign meals, then calls `finalize_plan(rationale)` to signal completion. Everything downstream (stacker, grocery_builder, output_formatter) stays deterministic. `main.py plan` prints to terminal instead of sending email; `main.py reply` reads feedback from stdin.

**Tech Stack:** Python 3.11+, `anthropic` SDK (already imported in reply_handler.py), SQLite (via existing StateStore), PyYAML, argparse.

**PRD Reference:** `prd_v1_5.md` — §4 (agent tools + system prompt), §5 (terminal output format), §6 (freeform notes), §7 (experiment ratings), §10 (intent types), §11 (data model), §13 (build sequence Phase 1).

---

## Key interface facts (read before touching code)

- **`WeekConstraints`** (`calendar_reader.py`): fields are `week_start_monday: date` and `days: list[DayConstraints]`. Everything else is a method or property: `.get(d)` returns `Optional[DayConstraints]`, `.open_saturday()` returns `Optional[DayConstraints]`, `.sunday_needs_easy` is a property. There is no `week_start_friday` field, no `week_key` field, no `open_saturday` field, no `cook_event_count`.
- **`DayConstraints`**: fields are `date`, `is_no_cook`, `is_fasting`, `is_open_saturday`, `no_cook_reason`, `qualifying_events`.
- **`GroceryList`**: attribute is `items_by_category: dict[str, list[GroceryItem]]` (not `categories`). `GroceryItem` has `name`, `quantity`, `unit` — no `display` field.
- **`Stacker.analyse()`** returns `list[StackingNote]` (dataclass objects). Convert with `str(note)` or `note.message`.
- **`parse_reply_intents()`** in `reply_handler.py` is a module-level function, not an instance method.
- **`apply_intents()`** in `reply_handler.py` is a module-level function that takes `(plan, intents, recipes, store)`.
- The planning week is **Friday–Thursday**. `week_start_monday` is the Monday *containing* that Friday. Friday = `monday + timedelta(days=4)`.

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `planner.py` | Modify | Add `note_type`, `note_text` to `MealSlot`; add `rationale` to `WeekPlan`; update serialisation |
| `state_store.py` | Modify | Add `experiment_ratings` + `meal_notes` tables; add rating/note/promotion methods |
| `output_formatter.py` | Create | Render `WeekPlan` + grocery + stacking to terminal string (§5 format) |
| `agentic_planner.py` | Create | Claude + tools agent; produces `WeekPlan` identical in shape to `planner.py` output |
| `reply_handler.py` | Modify | Add stdin mode; add new Phase 1 intent types (§10) |
| `main.py` | Modify | `plan` → terminal output; `reply` → stdin; wire `AgenticPlanner` |
| `test_planner.py` | Modify | Add tests for new `MealSlot` / `WeekPlan` fields |
| `test_state_store.py` | Modify | Add tests for new tables and methods |
| `test_output_formatter.py` | Create | Snapshot-style tests for formatted output |
| `test_agentic_planner.py` | Create | Unit tests for each tool function (no Claude API calls) |

---

## Task 1: Extend MealSlot and WeekPlan data model

**Files:**
- Modify: `planner.py:56-130` (MealSlot and WeekPlan dataclasses)
- Modify: `test_planner.py`

### Context

`MealSlot` needs two optional fields (PRD §11.1):
- `note_type: Optional[str]` — `'out'` or `'cook'`, `None` for recipe slots
- `note_text: Optional[str]` — freeform text for note slots

`WeekPlan` needs:
- `rationale: str = ""` — the agent's reasoning narrative

Both need roundtrip serialisation via `to_dict()`.

- [ ] **Step 1: Write failing tests**

Add to `test_planner.py`:

```python
from planner import MealSlot, WeekPlan
from datetime import date, timedelta

def test_mealslot_note_fields_default_none():
    slot = MealSlot(
        date=date(2026, 4, 7),
        slot="dinner",
        recipe_id=None,
        label="Dinner at Sarah's",
        tags=[],
        ingredients=[],
        is_no_cook=True,
    )
    assert slot.note_type is None
    assert slot.note_text is None

def test_mealslot_note_fields_set():
    slot = MealSlot(
        date=date(2026, 4, 7),
        slot="dinner",
        recipe_id=None,
        label="Dinner at Sarah's",
        tags=[],
        ingredients=[],
        note_type="out",
        note_text="Dinner at Sarah's",
    )
    assert slot.note_type == "out"
    assert slot.note_text == "Dinner at Sarah's"

def test_weekplan_rationale_defaults_empty():
    plan = WeekPlan(
        week_start_monday=date(2026, 4, 7),
        week_key="2026-04-07",
    )
    assert plan.rationale == ""

def test_weekplan_to_dict_includes_rationale():
    plan = WeekPlan(
        week_start_monday=date(2026, 4, 7),
        week_key="2026-04-07",
        rationale="Busy week, kept things simple.",
    )
    d = plan.to_dict()
    assert d["rationale"] == "Busy week, kept things simple."

def test_mealslot_to_dict_includes_note_fields():
    monday = date(2026, 4, 7)
    plan = WeekPlan(week_start_monday=monday, week_key="2026-04-07")
    plan.dinners = [
        MealSlot(
            date=monday + timedelta(days=1),
            slot="dinner",
            recipe_id=None,
            label="Dinner at Sarah's",
            tags=[],
            ingredients=[],
            note_type="out",
            note_text="Dinner at Sarah's",
        )
    ]
    d = plan.to_dict()
    dinner = d["dinners"][0]
    assert dinner["note_type"] == "out"
    assert dinner["note_text"] == "Dinner at Sarah's"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/glenmcleod/Desktop/katharinecode/souschef
python -m pytest test_planner.py::test_mealslot_note_fields_default_none test_planner.py::test_weekplan_rationale_defaults_empty -v
```
Expected: `AttributeError` or `TypeError`.

- [ ] **Step 3: Add fields to MealSlot**

In `planner.py`, update the `MealSlot` dataclass — add two fields after `is_fasting`:

```python
note_type:   Optional[str] = None   # 'out' or 'cook'
note_text:   Optional[str] = None   # freeform text for note slots
```

- [ ] **Step 4: Add rationale to WeekPlan**

In `planner.py`, update `WeekPlan` — add after `warnings`:

```python
rationale: str = ""
```

- [ ] **Step 5: Update to_dict() to include new fields**

In `planner.py`, update `to_dict()`:
- In each dinner dict, add: `"note_type": s.note_type, "note_text": s.note_text`
- At the top level of the returned dict, add: `"rationale": self.rationale`

- [ ] **Step 6: Run tests to confirm they pass**

```bash
python -m pytest test_planner.py -v
```
Expected: all existing tests plus the 5 new ones pass.

- [ ] **Step 7: Commit**

```bash
git add planner.py test_planner.py
git commit -m "feat: add note_type/note_text to MealSlot and rationale to WeekPlan"
```

---

## Task 2: Extend StateStore with experiment ratings and meal notes

**Files:**
- Modify: `state_store.py` (SCHEMA + new methods)
- Modify: `test_state_store.py`

### Context

Two new tables from PRD §11.2. The `promote_recipe_to_rotation` helper updates the recipe's YAML on disk (adds `onRotation` tag, sets `promoted_at`).

- [ ] **Step 1: Write failing tests**

Add to `test_state_store.py`:

```python
import tempfile, os
from state_store import StateStore

def _tmp_store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return StateStore(path), path

def test_record_and_get_experiment_rating():
    store, path = _tmp_store()
    store.record_experiment_rating("merguez-01", stars=4)
    ratings = store.get_experiment_ratings("merguez-01")
    assert len(ratings) == 1
    assert ratings[0]["stars"] == 4
    assert ratings[0]["promoted"] == 0
    store.close(); os.unlink(path)

def test_record_experiment_rating_promoted():
    store, path = _tmp_store()
    store.record_experiment_rating("merguez-01", stars=5, promoted=True)
    ratings = store.get_experiment_ratings("merguez-01")
    assert ratings[0]["promoted"] == 1
    store.close(); os.unlink(path)

def test_record_meal_note():
    store, path = _tmp_store()
    store.record_meal_note("2026-04-07", "2026-04-08", "out", "Dinner at Sarah's")
    notes = store.get_meal_notes("2026-04-07")
    assert len(notes) == 1
    assert notes[0]["note_type"] == "out"
    assert notes[0]["note_text"] == "Dinner at Sarah's"
    store.close(); os.unlink(path)

def test_multiple_ratings_same_recipe():
    store, path = _tmp_store()
    store.record_experiment_rating("merguez-01", stars=3)
    store.record_experiment_rating("merguez-01", stars=5)
    ratings = store.get_experiment_ratings("merguez-01")
    assert len(ratings) == 2
    store.close(); os.unlink(path)
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python -m pytest test_state_store.py::test_record_and_get_experiment_rating -v
```
Expected: `AttributeError: 'StateStore' object has no attribute 'record_experiment_rating'`

- [ ] **Step 3: Add tables to SCHEMA in state_store.py**

Append to the `SCHEMA` string before the closing `"""`:

```sql
CREATE TABLE IF NOT EXISTS experiment_ratings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id       TEXT NOT NULL,
    rated_at        TEXT NOT NULL,
    stars           INTEGER NOT NULL,
    promoted        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meal_notes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    week_key        TEXT NOT NULL,
    meal_date       TEXT NOT NULL,
    note_type       TEXT NOT NULL,
    note_text       TEXT NOT NULL
);
```

- [ ] **Step 4: Add methods to StateStore**

Add after `get_recent_meals()`:

```python
# -----------------------------------------------------------------------
# Experiment ratings
# -----------------------------------------------------------------------

def record_experiment_rating(
    self, recipe_id: str, stars: int, promoted: bool = False
):
    """Record a star rating for an experiment recipe."""
    from datetime import datetime
    rated_at = datetime.now().isoformat(timespec="seconds")
    with self._transaction():
        self._conn.execute("""
            INSERT INTO experiment_ratings (recipe_id, rated_at, stars, promoted)
            VALUES (?, ?, ?, ?)
        """, (recipe_id, rated_at, stars, 1 if promoted else 0))

def get_experiment_ratings(self, recipe_id: str) -> list[dict]:
    """Return all ratings for a recipe, newest first."""
    rows = self._conn.execute("""
        SELECT recipe_id, rated_at, stars, promoted
        FROM experiment_ratings
        WHERE recipe_id = ?
        ORDER BY rated_at DESC
    """, (recipe_id,)).fetchall()
    return [dict(r) for r in rows]

# -----------------------------------------------------------------------
# Meal notes
# -----------------------------------------------------------------------

def record_meal_note(
    self, week_key: str, meal_date: str, note_type: str, note_text: str
):
    """Persist a freeform meal note (out or cook)."""
    with self._transaction():
        self._conn.execute("""
            INSERT INTO meal_notes (week_key, meal_date, note_type, note_text)
            VALUES (?, ?, ?, ?)
        """, (week_key, meal_date, note_type, note_text))

def get_meal_notes(self, week_key: str) -> list[dict]:
    """Return all meal notes for a given week."""
    rows = self._conn.execute("""
        SELECT week_key, meal_date, note_type, note_text
        FROM meal_notes WHERE week_key = ?
        ORDER BY meal_date
    """, (week_key,)).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python -m pytest test_state_store.py -v
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add state_store.py test_state_store.py
git commit -m "feat: add experiment_ratings and meal_notes tables to StateStore"
```

---

## Task 3: Build output_formatter.py

**Files:**
- Create: `output_formatter.py`
- Create: `test_output_formatter.py`

### Context

Renders the plan to the terminal string format from PRD §5. Takes `WeekPlan`, `GroceryList`, and `stacking_notes: list[str]`. Returns a string — the caller prints it.

`GroceryList.items_by_category` is a `dict[str, list[GroceryItem]]`. Each `GroceryItem` has `name`, `quantity` (Optional[str]), `unit` (Optional[str]). Build the display string as:
```python
qty = f" — {item.quantity} {item.unit}".rstrip() if item.quantity else ""
display = f"{item.name}{qty}"
```

`GroceryList.likely_on_hand` is a `list[GroceryItem]`.

Stacking notes passed in are already `list[str]` (the caller converts `StackingNote` objects before passing).

- [ ] **Step 1: Write failing tests**

Create `test_output_formatter.py`:

```python
from datetime import date, timedelta
from planner import MealSlot, WeekPlan
from output_formatter import format_plan

# Minimal fake grocery structures matching the real GroceryItem/GroceryList interface

class _FakeItem:
    def __init__(self, name, quantity=None, unit=None):
        self.name = name
        self.quantity = quantity
        self.unit = unit

class _FakeGrocery:
    def __init__(self):
        item = _FakeItem("onion", quantity="1")
        self.items_by_category = {"PRODUCE": [item]}
        on_hand = _FakeItem("garam masala")
        self.likely_on_hand = [on_hand]


def _make_plan(rationale="Straightforward week."):
    monday = date(2026, 4, 6)
    friday = monday + timedelta(days=4)
    plan = WeekPlan(
        week_start_monday=monday,
        week_key="2026-04-06",
        rationale=rationale,
        cook_nights=2,
    )
    plan.dinners = [
        MealSlot(
            date=friday + timedelta(days=i),
            slot="dinner",
            recipe_id="chickpea-curry" if i == 0 else None,
            label="Chickpea Curry" if i == 0 else "no cook",
            tags=["vegetarian"] if i == 0 else [],
            ingredients=[],
            is_no_cook=(i != 0),
        )
        for i in range(7)
    ]
    return plan


def test_format_plan_contains_header():
    out = format_plan(_make_plan(), _FakeGrocery(), [])
    assert "MEAL PLAN" in out
    assert "Apr" in out

def test_format_plan_contains_rationale():
    out = format_plan(_make_plan("Busy week, kept simple."), _FakeGrocery(), [])
    assert "RATIONALE" in out
    assert "Busy week, kept simple." in out

def test_format_plan_no_rationale_section_when_empty():
    plan = _make_plan("")
    out = format_plan(plan, _FakeGrocery(), [])
    assert "RATIONALE" not in out

def test_format_plan_contains_days():
    out = format_plan(_make_plan(), _FakeGrocery(), [])
    assert "Friday" in out
    assert "Chickpea Curry" in out

def test_format_plan_contains_grocery():
    out = format_plan(_make_plan(), _FakeGrocery(), [])
    assert "GROCERY LIST" in out
    assert "PRODUCE" in out
    assert "onion" in out

def test_format_plan_contains_on_hand():
    out = format_plan(_make_plan(), _FakeGrocery(), [])
    assert "LIKELY ON HAND" in out
    assert "garam masala" in out

def test_format_plan_stacking_notes_shown():
    out = format_plan(_make_plan(), _FakeGrocery(), ["Both dishes use rice — double batch Thursday."])
    assert "STACKING NOTES" in out
    assert "double batch" in out

def test_format_plan_no_stacking_section_when_empty():
    out = format_plan(_make_plan(), _FakeGrocery(), [])
    assert "STACKING NOTES" not in out

def test_format_plan_ends_with_prompt():
    out = format_plan(_make_plan(), _FakeGrocery(), [])
    assert "done" in out.lower()

def test_note_slot_out_displayed():
    plan = _make_plan()
    plan.dinners[1].note_type = "out"
    plan.dinners[1].note_text = "Dinner at Sarah's"
    plan.dinners[1].label = "Dinner at Sarah's"
    out = format_plan(plan, _FakeGrocery(), [])
    assert "Dinner at Sarah's" in out
    assert "[out]" in out

def test_note_slot_cook_displayed():
    plan = _make_plan()
    plan.dinners[2].note_type = "cook"
    plan.dinners[2].note_text = "Waffles for dinner"
    plan.dinners[2].label = "Waffles for dinner"
    out = format_plan(plan, _FakeGrocery(), [])
    assert "Waffles for dinner" in out
    assert "[cook" in out
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python -m pytest test_output_formatter.py -v
```
Expected: `ModuleNotFoundError: No module named 'output_formatter'`

- [ ] **Step 3: Implement output_formatter.py**

Create `output_formatter.py`:

```python
"""
output_formatter.py

Renders a WeekPlan + GroceryList + stacking notes to a terminal string.
Mirrors the email format from PRD §5 so Phase 3 is a thin wrapper.
"""

import textwrap
from datetime import timedelta
from planner import WeekPlan

_SEP = "─" * 50


def format_plan(plan: WeekPlan, grocery, stacking_notes: list[str]) -> str:
    """
    Return a formatted string ready to print to the terminal.

    Args:
        plan:           WeekPlan from agentic_planner or planner
        grocery:        GroceryList from grocery_builder
        stacking_notes: list of strings from stacker (StackingNote.__str__)
    """
    lines = []

    # Header — planning week is Fri–Thu
    friday = plan.dinners[0].date if plan.dinners else plan.week_start_monday + timedelta(days=4)
    thursday = friday + timedelta(days=6)
    date_range = f"{friday.strftime('%b %-d')} – {thursday.strftime('%-d')}"
    cook_str = f"{plan.cook_nights} cook night{'s' if plan.cook_nights != 1 else ''}"
    lines.append(f"\nMEAL PLAN: {date_range}  ·  {cook_str}")
    lines.append(_SEP)

    # Rationale
    if plan.rationale:
        lines.append("")
        lines.append("RATIONALE")
        lines.append("")
        for paragraph in plan.rationale.split("\n"):
            lines.append(textwrap.fill(paragraph, width=80))
        lines.append("")
        lines.append(_SEP)

    # This week
    lines.append("THIS WEEK")
    lines.append("")
    for slot in plan.dinners:
        day_label = slot.date.strftime("%A, %b %-d").ljust(22)
        meal_label = slot.label

        tags = []
        if slot.note_type == "out":
            tags.append("[out]")
        elif slot.note_type == "cook":
            tags.append("[cook — add ingredients manually]")
        elif not slot.is_no_cook:
            if "experiment" in slot.tags:
                tags.append("[experiment]")
            if slot.is_meatless:
                tags.append("[vegetarian]" if "vegetarian" in slot.tags else "[meatless]")

        tag_str = "  " + "  ".join(tags) if tags else ""
        lines.append(f"  {day_label}  {meal_label}{tag_str}")

    # Lunch
    if plan.lunch:
        lines.append("")
        lines.append("LUNCH THIS WEEK")
        lines.append("")
        lines.append(f"  {plan.lunch.label}")
        for note in plan.lunch.notes:
            lines.append(f"  ⚑ {note}")

    # Stacking notes
    if stacking_notes:
        lines.append("")
        lines.append("STACKING NOTES")
        lines.append("")
        for note in stacking_notes:
            lines.append(f"  • {note}")

    # Grocery list
    lines.append("")
    lines.append("GROCERY LIST")
    lines.append("")
    for category, items in grocery.items_by_category.items():
        if items:
            lines.append(f"  {category}")
            parts = []
            for item in items:
                qty = ""
                if item.quantity:
                    qty = f" — {item.quantity}"
                    if item.unit:
                        qty += f" {item.unit}"
                parts.append(f"{item.name}{qty}")
            lines.append("    " + "  ·  ".join(parts))
            lines.append("")

    # Likely on hand
    if grocery.likely_on_hand:
        lines.append("LIKELY ON HAND (confirm before buying)")
        for item in grocery.likely_on_hand:
            lines.append(f"  • {item.name}")
        lines.append("")

    lines.append(_SEP)
    lines.append("Respond to make changes, or type 'done' to approve:")
    lines.append("")

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest test_output_formatter.py -v
```
Fix any failures. All 11 tests should pass.

- [ ] **Step 5: Commit**

```bash
git add output_formatter.py test_output_formatter.py
git commit -m "feat: add output_formatter for Phase 1 terminal output"
```

---

## Task 4: Build agentic_planner.py — tool functions

**Files:**
- Create: `agentic_planner.py`
- Create: `test_agentic_planner.py`

### Context

The tool functions are deterministic Python. The agent loop (Task 5) calls them via `_dispatch_tool()`. Test the tool functions in isolation here — no Claude API calls needed.

`WeekConstraints.week_start_monday` is the Monday of the week. The planning week (Fri–Thu) starts on `monday + timedelta(days=4)`.

For the meatless constraint check, use `d.weekday()` — Python's `datetime.weekday()` returns 0=Mon…4=Fri…5=Sat…6=Sun. Wednesday=2, Friday=4 are always meatless.

For fasting check, use `self.constraints.get(d)` (the `.get()` method on WeekConstraints, which iterates the list and matches by date).

For open Saturday check, use `self.constraints.open_saturday()` (a method returning `Optional[DayConstraints]`).

- [ ] **Step 1: Write failing tests for tool functions**

Create `test_agentic_planner.py`:

```python
"""Unit tests for agentic_planner tool functions — no Claude API calls."""
import tempfile, os, yaml
from datetime import date, timedelta
from pathlib import Path

import pytest
from agentic_planner import AgenticPlanner, AgenticPlannerState, ToolError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def recipe_dir(tmp_path):
    recipes = [
        {
            "id": "chickpea-curry",
            "name": "Chickpea Curry",
            "tags": ["vegetarian", "onRotation"],
            "ingredients": [{"name": "chickpeas", "quantity": "2", "unit": "cans"}],
            "last_planned": None,
        },
        {
            "id": "hamburger-steaks",
            "name": "Hamburger Steaks",
            "tags": ["onRotation"],
            "ingredients": [{"name": "ground beef", "quantity": "1.5", "unit": "lb"}],
            "last_planned": None,
        },
        {
            "id": "merguez-experiment",
            "name": "Homemade Merguez",
            "tags": ["experiment"],
            "ingredients": [{"name": "ground lamb", "quantity": "1", "unit": "lb"}],
            "last_planned": None,
        },
        {
            "id": "easy-pasta",
            "name": "Easy Pasta",
            "tags": ["easy", "vegetarian"],
            "ingredients": [{"name": "pasta", "quantity": "1", "unit": "lb"}],
            "last_planned": None,
        },
    ]
    for r in recipes:
        p = tmp_path / f"{r['id']}.yaml"
        p.write_text(yaml.dump(r))
    return str(tmp_path)


@pytest.fixture
def lunch_file(tmp_path):
    lunches = [{"id": "grain-bowl", "name": "Grain Bowl", "ingredients": []}]
    p = tmp_path / "lunches.yaml"
    p.write_text(yaml.dump(lunches))
    return str(p)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def planner(recipe_dir, lunch_file, db_path):
    from calendar_reader import WeekConstraints, DayConstraints

    monday = date(2026, 4, 6)   # Mon Apr 6, 2026; Friday = Apr 10

    constraints = WeekConstraints(week_start_monday=monday)
    for i in range(7):
        d = monday + timedelta(days=i)
        dc = DayConstraints(date=d)
        if d.weekday() == 5:  # Saturday — mark open for experiments
            dc.is_open_saturday = True
        constraints.days.append(dc)

    cfg = {"recipe_dir": recipe_dir, "lunch_file": lunch_file, "db_path": db_path}
    return AgenticPlanner(config=cfg, constraints=constraints)


# ---------------------------------------------------------------------------
# Tool function tests
# ---------------------------------------------------------------------------

def test_get_calendar_constraints_returns_dict(planner):
    result = planner._tool_get_calendar_constraints()
    assert "week_start_monday" in result
    assert "days" in result
    assert "open_saturday" in result

def test_get_recipes_returns_all(planner):
    result = planner._tool_get_recipes()
    assert len(result) == 4

def test_get_recipes_filter_meatless(planner):
    result = planner._tool_get_recipes(meatless_only=True)
    for r in result:
        assert any(t in r["tags"] for t in ("vegetarian", "pescatarian", "meatless"))

def test_get_recipes_filter_tags(planner):
    result = planner._tool_get_recipes(tags=["easy"])
    assert all("easy" in r["tags"] for r in result)

def test_get_recipes_exclude_ids(planner):
    result = planner._tool_get_recipes(exclude_ids=["chickpea-curry"])
    ids = [r["id"] for r in result]
    assert "chickpea-curry" not in ids

def test_assign_meal_valid(planner):
    friday = planner.constraints.week_start_monday + timedelta(days=4)
    result = planner._tool_assign_meal(friday.isoformat(), "hamburger-steaks")
    assert result["ok"] is True
    assert planner.state.assignments[friday].recipe_id == "hamburger-steaks"

def test_assign_meal_meatless_violation(planner):
    """Assigning a meat recipe on Wednesday should return an error."""
    wednesday = planner.constraints.week_start_monday + timedelta(days=2)
    result = planner._tool_assign_meal(wednesday.isoformat(), "hamburger-steaks")
    assert result["ok"] is False
    assert "meatless" in result["error"].lower()

def test_assign_meal_vegetarian_on_wednesday_ok(planner):
    wednesday = planner.constraints.week_start_monday + timedelta(days=2)
    result = planner._tool_assign_meal(wednesday.isoformat(), "chickpea-curry")
    assert result["ok"] is True

def test_assign_no_cook(planner):
    friday = planner.constraints.week_start_monday + timedelta(days=4)
    result = planner._tool_assign_no_cook(friday.isoformat(), reason="KRM event")
    assert result["ok"] is True
    assert planner.state.assignments[friday].is_no_cook is True

def test_assign_note_out(planner):
    friday = planner.constraints.week_start_monday + timedelta(days=4)
    result = planner._tool_assign_note(friday.isoformat(), "Dinner at Sarah's", "out")
    assert result["ok"] is True
    slot = planner.state.assignments[friday]
    assert slot.note_type == "out"

def test_assign_note_invalid_type(planner):
    friday = planner.constraints.week_start_monday + timedelta(days=4)
    result = planner._tool_assign_note(friday.isoformat(), "some note", "invalid")
    assert result["ok"] is False

def test_assign_experiment_on_open_saturday_ok(planner):
    saturday = planner.constraints.week_start_monday + timedelta(days=5)
    result = planner._tool_assign_meal(saturday.isoformat(), "merguez-experiment")
    assert result["ok"] is True

def test_assign_experiment_on_non_saturday_fails(planner):
    monday = planner.constraints.week_start_monday
    result = planner._tool_assign_meal(monday.isoformat(), "merguez-experiment")
    assert result["ok"] is False

def test_finalize_plan_builds_weekplan(planner):
    friday = planner.constraints.week_start_monday + timedelta(days=4)
    planner._tool_assign_meal(friday.isoformat(), "hamburger-steaks")
    plan = planner._tool_finalize_plan("Light week, simple choices.")
    assert plan.rationale == "Light week, simple choices."
    assert len(plan.dinners) == 7  # one slot per day, unassigned filled as no-cook

def test_get_rotation_history(planner):
    result = planner._tool_get_rotation_history(["chickpea-curry", "hamburger-steaks"])
    assert "chickpea-curry" in result
    assert result["chickpea-curry"] is None  # never planned in fresh db

def test_get_recent_meals_empty_db(planner):
    result = planner._tool_get_recent_meals(weeks=2)
    assert isinstance(result, list)
    assert result == []
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python -m pytest test_agentic_planner.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError: No module named 'agentic_planner'`

- [ ] **Step 3: Implement agentic_planner.py tool functions (no agent loop yet)**

Create `agentic_planner.py`:

```python
"""
agentic_planner.py

Agentic meal planner: Claude as orchestrator with deterministic tool functions.

The AgenticPlanner.plan_week() method runs an agent loop using the Claude API.
Each tool function is deterministic Python; the agent decides when to call them.

Tool functions are prefixed _tool_ and are called only by the agent loop or tests.

Max tool calls per run: 30 (loop safety). Falls back to deterministic Planner.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import yaml

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore

from calendar_reader import WeekConstraints
from planner import MealSlot, WeekPlan, load_recipes, load_lunches
from stacker import Stacker
from state_store import StateStore

log = logging.getLogger(__name__)

MAX_TOOL_CALLS = 30

# Days always meatless regardless of calendar (Python weekday: 0=Mon, 4=Fri)
ALWAYS_MEATLESS_WEEKDAYS = {2, 4}  # Wednesday, Friday

# Tags considered meatless
MEATLESS_TAGS = {"vegetarian", "pescatarian", "meatless", "vegan"}


# ---------------------------------------------------------------------------
# Planning state accumulator
# ---------------------------------------------------------------------------

@dataclass
class AgenticPlannerState:
    """Mutable state built up by tool calls during a planning run."""
    assignments: dict[date, MealSlot] = field(default_factory=dict)
    lunch_id: Optional[str] = None
    lunch_label: Optional[str] = None
    tool_call_count: int = 0


# ---------------------------------------------------------------------------
# AgenticPlanner
# ---------------------------------------------------------------------------

class AgenticPlanner:
    def __init__(self, config: dict, constraints: WeekConstraints):
        self.config = config
        self.constraints = constraints
        self.recipes = load_recipes(config.get("recipe_dir", "recipes_yaml"))
        self.lunches = load_lunches(config.get("lunch_file", "lunches.yaml"))
        self.store = StateStore(config.get("db_path", "meal_planner.db"))
        self.state = AgenticPlannerState()

    # -----------------------------------------------------------------------
    # Tool: get_calendar_constraints
    # -----------------------------------------------------------------------

    def _tool_get_calendar_constraints(self) -> dict:
        """Return the week's constraints as a serialisable dict."""
        c = self.constraints
        days = {}
        for dc in c.days:
            days[dc.date.isoformat()] = {
                "is_no_cook":      dc.is_no_cook,
                "is_fasting":      dc.is_fasting,
                "is_open_saturday": dc.is_open_saturday,
                "no_cook_reason":  dc.no_cook_reason,
                "weekday":         dc.date.strftime("%A"),
            }
        open_sat = c.open_saturday()
        return {
            "week_start_monday":  c.week_start_monday.isoformat(),
            "week_start_friday":  (c.week_start_monday + timedelta(days=4)).isoformat(),
            "open_saturday":      open_sat.date.isoformat() if open_sat else None,
            "sunday_needs_easy":  c.sunday_needs_easy,
            "no_cook_count":      len(c.no_cook_days()),
            "days":               days,
        }

    # -----------------------------------------------------------------------
    # Tool: get_recipes
    # -----------------------------------------------------------------------

    def _tool_get_recipes(
        self,
        tags: Optional[list[str]] = None,
        meatless_only: bool = False,
        exclude_ids: Optional[list[str]] = None,
    ) -> list[dict]:
        """Return recipes from the library, optionally filtered."""
        exclude_ids = set(exclude_ids or [])
        result = []
        for rid, r in self.recipes.items():
            if rid in exclude_ids:
                continue
            recipe_tags = set(r.get("tags", []))
            if meatless_only and not (recipe_tags & MEATLESS_TAGS):
                continue
            if tags and not any(t in recipe_tags for t in tags):
                continue
            result.append({
                "id":           r["id"],
                "name":         r.get("name", r["id"]),
                "tags":         r.get("tags", []),
                "last_planned": r.get("last_planned"),
            })
        return result

    # -----------------------------------------------------------------------
    # Tool: get_rotation_history
    # -----------------------------------------------------------------------

    def _tool_get_rotation_history(self, recipe_ids: list[str]) -> dict[str, Optional[str]]:
        """Return {recipe_id: last_planned ISO date or None}."""
        all_history = self.store.get_all_last_planned()
        return {
            rid: (all_history[rid].isoformat() if rid in all_history else None)
            for rid in recipe_ids
        }

    # -----------------------------------------------------------------------
    # Tool: get_recent_meals
    # -----------------------------------------------------------------------

    def _tool_get_recent_meals(self, weeks: int = 2) -> list[dict]:
        return self.store.get_recent_meals(weeks=weeks)

    # -----------------------------------------------------------------------
    # Tool: assign_meal
    # -----------------------------------------------------------------------

    def _tool_assign_meal(
        self, day: str, recipe_id: str, notes: Optional[str] = None
    ) -> dict:
        try:
            d = date.fromisoformat(day)
        except ValueError:
            return {"ok": False, "error": f"Invalid date: {day}"}

        recipe = self.recipes.get(recipe_id)
        if not recipe:
            return {"ok": False, "error": f"Recipe not found: {recipe_id}"}

        recipe_tags = set(recipe.get("tags", []))

        # Meatless constraint
        dc = self.constraints.get(d)
        is_fasting = dc.is_fasting if dc else False
        if d.weekday() in ALWAYS_MEATLESS_WEEKDAYS or is_fasting:
            if not (recipe_tags & MEATLESS_TAGS):
                return {
                    "ok": False,
                    "error": (
                        f"{d.strftime('%A')} must be meatless. "
                        f"Recipe '{recipe.get('name')}' is not tagged vegetarian/pescatarian/meatless."
                    ),
                }

        # Experiment recipes: open Saturday only
        if "experiment" in recipe_tags:
            open_sat = self.constraints.open_saturday()
            if d.weekday() != 5 or open_sat is None or open_sat.date != d:
                return {
                    "ok": False,
                    "error": "Experiment recipes can only be scheduled on open Saturdays.",
                }

        # Pizza: Friday only
        if "pizza" in recipe_tags and d.weekday() != 4:
            return {"ok": False, "error": "Pizza recipes are only for Fridays."}

        slot = MealSlot(
            date=d,
            slot="dinner",
            recipe_id=recipe_id,
            label=recipe.get("name", recipe_id),
            tags=recipe.get("tags", []),
            ingredients=recipe.get("ingredients", []),
            notes=[notes] if notes else [],
            is_meatless=bool(recipe_tags & MEATLESS_TAGS),
        )
        self.state.assignments[d] = slot
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Tool: assign_no_cook
    # -----------------------------------------------------------------------

    def _tool_assign_no_cook(self, day: str, reason: Optional[str] = None) -> dict:
        try:
            d = date.fromisoformat(day)
        except ValueError:
            return {"ok": False, "error": f"Invalid date: {day}"}
        notes = [f"({reason})"] if reason else []
        slot = MealSlot(
            date=d, slot="dinner", recipe_id=None,
            label="no cook", tags=[], ingredients=[],
            notes=notes, is_no_cook=True,
        )
        self.state.assignments[d] = slot
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Tool: assign_note
    # -----------------------------------------------------------------------

    def _tool_assign_note(self, day: str, note_text: str, note_type: str) -> dict:
        if note_type not in ("out", "cook"):
            return {"ok": False, "error": "note_type must be 'out' or 'cook'"}
        try:
            d = date.fromisoformat(day)
        except ValueError:
            return {"ok": False, "error": f"Invalid date: {day}"}
        slot = MealSlot(
            date=d, slot="dinner", recipe_id=None,
            label=note_text, tags=[], ingredients=[],
            note_type=note_type, note_text=note_text,
            is_no_cook=(note_type == "out"),
        )
        self.state.assignments[d] = slot
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Tool: assign_lunch
    # -----------------------------------------------------------------------

    def _tool_assign_lunch(self, lunch_id: str) -> dict:
        lunch = next((l for l in self.lunches if l["id"] == lunch_id), None)
        if not lunch:
            return {"ok": False, "error": f"Lunch not found: {lunch_id}"}
        self.state.lunch_id = lunch_id
        self.state.lunch_label = lunch.get("name", lunch_id)
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Tool: get_stacking_opportunities
    # -----------------------------------------------------------------------

    def _tool_get_stacking_opportunities(self, recipe_ids: list[str]) -> list[str]:
        """Return ingredient stacking notes as strings for a set of recipe IDs."""
        recipes = [self.recipes[rid] for rid in recipe_ids if rid in self.recipes]
        if not recipes:
            return []
        monday = self.constraints.week_start_monday
        plan = WeekPlan(week_start_monday=monday, week_key=monday.isoformat())
        plan.dinners = [
            MealSlot(
                date=monday,
                slot="dinner",
                recipe_id=r["id"],
                label=r.get("name", r["id"]),
                tags=r.get("tags", []),
                ingredients=r.get("ingredients", []),
            )
            for r in recipes
        ]
        return [str(note) for note in Stacker().analyse(plan)]

    # -----------------------------------------------------------------------
    # Tool: finalize_plan
    # -----------------------------------------------------------------------

    def _tool_finalize_plan(self, rationale: str) -> WeekPlan:
        """Build the final WeekPlan from accumulated assignments."""
        monday = self.constraints.week_start_monday
        friday = monday + timedelta(days=4)
        week_key = monday.isoformat()

        # Ensure all 7 days (Fri–Thu) have a slot
        all_days = [friday + timedelta(days=i) for i in range(7)]
        for d in all_days:
            if d not in self.state.assignments:
                self.state.assignments[d] = MealSlot(
                    date=d, slot="dinner", recipe_id=None,
                    label="no cook", tags=[], ingredients=[],
                    is_no_cook=True,
                )

        dinners = [self.state.assignments[d] for d in sorted(self.state.assignments)
                   if friday <= d <= friday + timedelta(days=6)]
        cook_nights = sum(
            1 for s in dinners
            if not s.is_no_cook and s.note_type != "out"
        )

        lunch_slot = None
        if self.state.lunch_id:
            lunch_data = next(
                (l for l in self.lunches if l["id"] == self.state.lunch_id), None
            )
            if lunch_data:
                lunch_slot = MealSlot(
                    date=monday,
                    slot="lunch",
                    recipe_id=self.state.lunch_id,
                    label=self.state.lunch_label or lunch_data.get("name", ""),
                    tags=[],
                    ingredients=lunch_data.get("ingredients", []),
                )

        return WeekPlan(
            week_start_monday=monday,
            week_key=week_key,
            dinners=dinners,
            lunch=lunch_slot,
            cook_nights=cook_nights,
            rationale=rationale,
        )

    # -----------------------------------------------------------------------
    # Tool dispatch (called by agent loop)
    # -----------------------------------------------------------------------

    def _dispatch_tool(self, name: str, input_data: dict):
        dispatch = {
            "get_calendar_constraints":   lambda i: self._tool_get_calendar_constraints(),
            "get_recipes":                lambda i: self._tool_get_recipes(**i),
            "get_rotation_history":       lambda i: self._tool_get_rotation_history(i["recipe_ids"]),
            "get_recent_meals":           lambda i: self._tool_get_recent_meals(**i),
            "assign_meal":                lambda i: self._tool_assign_meal(**i),
            "assign_no_cook":             lambda i: self._tool_assign_no_cook(**i),
            "assign_note":                lambda i: self._tool_assign_note(**i),
            "assign_lunch":               lambda i: self._tool_assign_lunch(**i),
            "get_stacking_opportunities": lambda i: self._tool_get_stacking_opportunities(i["recipe_ids"]),
            "finalize_plan":              lambda i: self._tool_finalize_plan(i["rationale"]),
        }
        fn = dispatch.get(name)
        if fn is None:
            return {"error": f"Unknown tool: {name}"}
        return fn(input_data)

    # -----------------------------------------------------------------------
    # Public API — agent loop implemented in Task 5
    # -----------------------------------------------------------------------

    def plan_week(self) -> WeekPlan:
        raise NotImplementedError("Agent loop not yet implemented — see Task 5")
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest test_agentic_planner.py -v
```
All tool-function tests should pass (plan_week tests will be added in Task 5).

- [ ] **Step 5: Commit**

```bash
git add agentic_planner.py test_agentic_planner.py
git commit -m "feat: add AgenticPlanner tool functions (no agent loop yet)"
```

---

## Task 5: Implement the agent loop in agentic_planner.py

**Files:**
- Modify: `agentic_planner.py` (add `TOOL_DEFINITIONS`, `SYSTEM_PROMPT`, implement `plan_week()`)
- Modify: `test_agentic_planner.py` (add agent loop tests with mocked Anthropic client)

### Context

`plan_week()` calls the Claude API using the `anthropic` SDK's tool-use API. It loops until `finalize_plan` is called or `MAX_TOOL_CALLS` individual tool invocations are exhausted. The tool call counter increments once per tool call (per block in a turn), not per API call.

- [ ] **Step 1: Write failing test for agent loop**

Add to `test_agentic_planner.py`:

```python
from unittest.mock import MagicMock, patch

def test_plan_week_calls_finalize_and_returns_weekplan(planner):
    """Mock the Anthropic client so the agent calls finalize_plan immediately."""
    friday = planner.constraints.week_start_monday + timedelta(days=4)

    fake_assign = MagicMock()
    fake_assign.type = "tool_use"
    fake_assign.id = "call_1"
    fake_assign.name = "assign_meal"
    fake_assign.input = {"day": friday.isoformat(), "recipe_id": "hamburger-steaks"}

    fake_finalize = MagicMock()
    fake_finalize.type = "tool_use"
    fake_finalize.id = "call_2"
    fake_finalize.name = "finalize_plan"
    fake_finalize.input = {"rationale": "Simple week, one cook night."}

    response1 = MagicMock()
    response1.stop_reason = "tool_use"
    response1.content = [fake_assign]

    response2 = MagicMock()
    response2.stop_reason = "tool_use"
    response2.content = [fake_finalize]

    response3 = MagicMock()
    response3.stop_reason = "end_turn"
    response3.content = [MagicMock(type="text", text="Done.")]

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [response1, response2, response3]

    with patch("agentic_planner.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = mock_client
        plan = planner.plan_week()

    assert plan is not None
    assert plan.rationale == "Simple week, one cook night."
    assert len(plan.dinners) == 7

def test_plan_week_fallback_on_tool_limit(planner):
    """If MAX_TOOL_CALLS is exceeded without finalize_plan, falls back."""
    fake_tool_use = MagicMock()
    fake_tool_use.type = "tool_use"
    fake_tool_use.id = "call_x"
    fake_tool_use.name = "get_recipes"
    fake_tool_use.input = {}

    response = MagicMock()
    response.stop_reason = "tool_use"
    response.content = [fake_tool_use]

    mock_client = MagicMock()
    mock_client.messages.create.return_value = response

    with patch("agentic_planner.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = mock_client
        plan = planner.plan_week()

    assert plan is not None  # fallback produces a valid plan
    assert any("tool call limit" in w.lower() for w in plan.warnings)
```

- [ ] **Step 2: Run to confirm they fail**

```bash
python -m pytest test_agentic_planner.py::test_plan_week_calls_finalize_and_returns_weekplan -v
```
Expected: `NotImplementedError: Agent loop not yet implemented`

- [ ] **Step 3: Add TOOL_DEFINITIONS to agentic_planner.py**

Add before the `AgenticPlannerState` class:

```python
TOOL_DEFINITIONS = [
    {
        "name": "get_calendar_constraints",
        "description": "Returns the week's calendar constraints: no-cook days, fasting days, open Saturday date, sunday_needs_easy flag.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_recipes",
        "description": "Returns recipes from the library. Filter by tags, meatless_only, or exclude specific IDs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tags":          {"type": "array", "items": {"type": "string"}, "description": "Only return recipes with at least one of these tags."},
                "meatless_only": {"type": "boolean", "description": "If true, only vegetarian/pescatarian/meatless recipes."},
                "exclude_ids":   {"type": "array", "items": {"type": "string"}, "description": "Recipe IDs to exclude."},
            },
            "required": [],
        },
    },
    {
        "name": "get_rotation_history",
        "description": "Returns last_planned date per recipe ID. Use to find the most overdue onRotation recipes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipe_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["recipe_ids"],
        },
    },
    {
        "name": "get_recent_meals",
        "description": "Returns meals planned in the last N weeks. Use to avoid repeating recent recipes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "weeks": {"type": "integer", "description": "How many weeks back to look (default 2)."},
            },
            "required": [],
        },
    },
    {
        "name": "assign_meal",
        "description": "Assign a recipe to a specific day. Returns ok=True or an error explaining any constraint violation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "day":       {"type": "string", "description": "ISO date (YYYY-MM-DD)."},
                "recipe_id": {"type": "string"},
                "notes":     {"type": "string", "description": "Optional annotation shown in the plan."},
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
                "day":    {"type": "string", "description": "ISO date."},
                "reason": {"type": "string", "description": "Optional reason (e.g. 'KRM event after 3pm')."},
            },
            "required": ["day"],
        },
    },
    {
        "name": "assign_note",
        "description": "Assign a freeform note to a day. Use 'out' for eating out (excluded from grocery list), 'cook' for cooking something not in the library.",
        "input_schema": {
            "type": "object",
            "properties": {
                "day":       {"type": "string"},
                "note_text": {"type": "string", "description": "Display text."},
                "note_type": {"type": "string", "enum": ["out", "cook"]},
            },
            "required": ["day", "note_text", "note_type"],
        },
    },
    {
        "name": "assign_lunch",
        "description": "Assign the lunch of the week.",
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
        "description": "Analyse ingredient overlap for a set of recipes. Returns advisory notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "recipe_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["recipe_ids"],
        },
    },
    {
        "name": "finalize_plan",
        "description": "Signal planning is complete. Call when every day (Fri–Thu) has been assigned. Provide a rationale of 3–5 sentences on the week's key decisions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rationale": {"type": "string", "description": "3-5 sentence narrative — not a slot-by-slot summary."},
            },
            "required": ["rationale"],
        },
    },
]
```

- [ ] **Step 4: Add SYSTEM_PROMPT constant**

Add after `TOOL_DEFINITIONS`:

```python
SYSTEM_PROMPT = """You are a meal planning assistant for a household of four. Plan dinners for the upcoming week (Friday through Thursday) and select the lunch of the week.

## Non-negotiable rules

- Wednesday and Friday are ALWAYS meatless (vegetarian or pescatarian). No exceptions.
- Friday is ALWAYS pizza — use a pizza recipe. Homemade on the first Friday of the month; for all other Fridays use assign_no_cook with reason "Takeout pizza".
- Fasting days (is_fasting=true on the calendar) are meatless.
- Saturday AND Sunday: cook on ONE of them, never both. Saturday takes priority. Only cook Sunday if Saturday was no-cook, and then use an 'easy' recipe.
- Experiment recipes (tagged 'experiment') go on open Saturdays ONLY (open_saturday is not null in calendar constraints). Never on other days.
- Calendar no-cook days (is_no_cook=true) must be marked no-cook.

## Scheduling preferences

- Target 3 cook nights on busy weeks (many no-cook weekdays), 4 on light weeks.
- 'onRotation' recipes should appear roughly monthly. Prioritise the most overdue ones using get_rotation_history().
- Never repeat a recipe from the previous two weeks if alternatives exist. Check with get_recent_meals().
- Prefer variety in protein type and cuisine style across the week.
- On high-calendar-density weeks, prefer easier recipes.

## Conflict handling

- Always satisfy hard dietary rules first.
- If the meatless pool is exhausted, say so in the rationale — do not silently skip the constraint.
- Flag anything unusual rather than making a quiet judgment call.

## Your workflow

1. Call get_calendar_constraints() to understand the week.
2. Call get_recent_meals() to see what was cooked recently.
3. Call get_recipes() to browse available recipes (filter as needed).
4. Call get_rotation_history() on candidate recipes to find overdue ones.
5. Assign every day: assign_meal(), assign_no_cook(), or assign_note().
6. Call assign_lunch() to pick the week's lunch.
7. Optionally call get_stacking_opportunities() if you see useful ingredient overlap.
8. Call finalize_plan() with a 3–5 sentence rationale on the key decisions.

## Rationale expectations

Good: "Busy week — KRM events Tuesday and Wednesday, so I front-loaded cooking on Monday and Thursday. Saturday is open so I scheduled the merguez experiment. Wednesday would normally cook but given the calendar I kept it as leftovers."

Bad: "Monday: Hamburger Steaks. Tuesday: no cook. Wednesday: no cook..." — never do this.
"""
```

- [ ] **Step 5: Implement plan_week() agent loop**

Replace the `plan_week` stub:

```python
def plan_week(self) -> WeekPlan:
    """
    Run the agentic planning loop. Returns a WeekPlan.
    Falls back to deterministic Planner if anthropic is unavailable or
    the tool call limit is hit without finalize_plan being called.
    """
    if anthropic is None:
        log.warning("anthropic package not installed — falling back to deterministic planner")
        return self._fallback_plan()

    client = anthropic.Anthropic()
    model = self.config.get("model", "claude-sonnet-4-20250514")

    messages = []
    finalized_plan = None

    while self.state.tool_call_count < MAX_TOOL_CALLS:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason != "tool_use":
            log.warning("Unexpected stop_reason: %s", response.stop_reason)
            break

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue

            self.state.tool_call_count += 1
            result = self._dispatch_tool(block.name, block.input)

            if block.name == "finalize_plan" and isinstance(result, WeekPlan):
                finalized_plan = result
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "Plan finalised successfully.",
                })
            else:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })

        messages.append({"role": "user", "content": tool_results})

        if finalized_plan is not None:
            break

    if self.state.tool_call_count >= MAX_TOOL_CALLS and finalized_plan is None:
        log.warning("Agent hit tool call limit (%d) — falling back to deterministic planner", MAX_TOOL_CALLS)
        plan = self._fallback_plan()
        plan.warnings.append(f"tool call limit reached ({MAX_TOOL_CALLS}) — plan generated by deterministic fallback")
        return plan

    if finalized_plan is None:
        log.warning("Agent did not call finalize_plan — falling back")
        return self._fallback_plan()

    return finalized_plan

def _fallback_plan(self) -> WeekPlan:
    """Run the deterministic Planner as a fallback."""
    from planner import Planner
    planner = Planner(config=self.config, store=self.store)
    return planner.plan_week(self.constraints)
```

- [ ] **Step 6: Run tests**

```bash
python -m pytest test_agentic_planner.py -v
```
All tests should pass.

- [ ] **Step 7: Commit**

```bash
git add agentic_planner.py test_agentic_planner.py
git commit -m "feat: implement AgenticPlanner agent loop with Claude tool-use"
```

---

## Task 6: Wire main.py for Phase 1 (terminal output, stdin reply)

**Files:**
- Modify: `main.py`

### Context

Phase 1 changes:
- `python main.py plan` uses `AgenticPlanner` and prints via `output_formatter` instead of emailing.
- `python main.py reply` reads feedback from stdin and produces a revised plan.

Note: `output_formatter.format_plan()` expects `stacking_notes: list[str]`, so convert `Stacker().analyse(plan)` results with `[str(n) for n in stacker_result]` before passing.

- [ ] **Step 1: Verify existing tests still pass before touching main.py**

```bash
python -m pytest test_planner.py test_state_store.py test_output_formatter.py test_agentic_planner.py -v
```

- [ ] **Step 2: Update cmd_plan in main.py**

Replace the body of `cmd_plan`:

```python
def cmd_plan(args, cfg: dict, flat: dict):
    """Generate and print the weekly meal plan to terminal (Phase 1)."""
    from calendar_reader import CalendarReader
    from agentic_planner import AgenticPlanner
    from stacker import Stacker
    from grocery_builder import GroceryBuilder
    from output_formatter import format_plan
    from state_store import StateStore

    dry_run = args.dry_run

    reader = CalendarReader(config=flat, timezone=flat.get("timezone", "America/New_York"))
    monday = reader.next_planning_monday()
    log.info("Planning week: %s – %s", monday, monday + timedelta(days=6))

    log.info("Reading Google Calendar...")
    constraints = reader.get_week_constraints(monday)
    log.info(constraints.summary())

    log.info("Running agentic planner...")
    store = StateStore(flat.get("db_path", "meal_planner.db"))
    planner = AgenticPlanner(config=flat, constraints=constraints)
    plan = planner.plan_week()
    log.info("Plan complete: %d cook nights, %d warnings", plan.cook_nights, len(plan.warnings))
    for w in plan.warnings:
        log.warning("Planner warning: %s", w)

    log.info("Analysing stacking opportunities...")
    stacking_notes = [str(n) for n in Stacker().analyse(plan)]

    log.info("Building grocery list...")
    grocery = GroceryBuilder(store=store).build(plan)

    output = format_plan(plan, grocery, stacking_notes)
    print(output)

    if not dry_run:
        store.record_plan(plan.week_key, plan.to_dict(), plan.to_state_meals())
        log.info("Plan saved (week key: %s)", plan.week_key)
    else:
        log.info("Dry run — plan not saved.")

    store.close()
```

- [ ] **Step 3: Update cmd_reply for stdin mode**

Replace the body of `cmd_reply`:

```python
def cmd_reply(args, cfg: dict, flat: dict):
    """Read feedback from stdin and produce a revised plan (Phase 1)."""
    from reply_handler import ReplyHandler, parse_reply_intents, apply_intents
    from calendar_reader import CalendarReader
    from state_store import StateStore
    from stacker import Stacker
    from grocery_builder import GroceryBuilder
    from output_formatter import format_plan
    from planner import load_recipes

    if args.week:
        week_key = args.week
    else:
        reader = CalendarReader(config=flat, timezone=flat.get("timezone", "America/New_York"))
        monday = reader.next_planning_monday()
        week_key = monday.isoformat()

    store = StateStore(flat.get("db_path", "meal_planner.db"))
    plan_dict = store.get_plan(week_key)
    if not plan_dict:
        log.error("No saved plan found for week %s. Run 'plan' first.", week_key)
        store.close()
        sys.exit(1)

    print("\nEnter your feedback (or 'done' to approve):")
    try:
        feedback = input("> ").strip()
    except EOFError:
        feedback = "done"

    model = flat.get("model", flat.get("anthropic_model", "claude-sonnet-4-20250514"))
    intents = parse_reply_intents(feedback, model=model)

    if any(i["type"] == "acknowledgment" for i in intents):
        log.info("Plan approved.")
        if not args.dry_run:
            store.mark_plan_approved(week_key)
        store.close()
        return

    # Reconstruct plan from stored dict and apply intents
    handler = ReplyHandler(config=flat, dry_run=args.dry_run)
    plan = handler._reconstruct_plan(plan_dict)
    recipes = load_recipes(flat.get("recipe_dir", "recipes_yaml"))
    revised_plan, change_notes = apply_intents(plan, intents, recipes, store)

    for note in change_notes:
        log.info("Change: %s", note)

    stacking_notes = [str(n) for n in Stacker().analyse(revised_plan)]
    grocery = GroceryBuilder(store=store).build(revised_plan)
    output = format_plan(revised_plan, grocery, stacking_notes)
    print(output)

    if not args.dry_run:
        store.record_plan(revised_plan.week_key, revised_plan.to_dict(), revised_plan.to_state_meals())

    store.close()
```

- [ ] **Step 4: Run full test suite**

```bash
python -m pytest -v
```

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat: wire main.py plan/reply commands to Phase 1 CLI flow"
```

---

## Task 7: Update reply_handler.py for Phase 1 intent types

**Files:**
- Modify: `reply_handler.py` (update INTENT_SYSTEM_PROMPT + add promotion helper)
- Modify: `test_reply_handler.py`

### Context

The existing `parse_reply_intents()` module-level function handles intent parsing. The existing `apply_intents()` module-level function handles plan mutation. For Phase 1, we need the intent parser to recognise the new intent types from PRD §10.

New intents to add to `INTENT_SYSTEM_PROMPT`:
- `assign_note_out`, `assign_note_cook`, `force_no_cook`, `force_cook`, `skip_experiment`, `rate_experiment`, `promote_experiment`, `acknowledgment`

The `import_atk` intent (PRD §10) is deferred to the ATK importer task — do not add it here yet.

Also add `_promote_recipe_to_rotation()` as a helper (called by apply_intents when it processes `promote_experiment`).

- [ ] **Step 1: Write failing tests**

Look at `test_reply_handler.py` for existing fixture patterns, then add:

```python
# These tests require the INTENT_SYSTEM_PROMPT to include the new intent types.
# They call parse_reply_intents() which makes a real Claude API call.
# Mark them with pytest.mark.integration and skip in unit test runs.

import pytest

@pytest.mark.integration
def test_parse_intent_assign_note_out():
    from reply_handler import parse_reply_intents
    intents = parse_reply_intents("Mark Tuesday as dinner at Sarah's", model="claude-haiku-4-5-20251001")
    types = [i["type"] for i in intents]
    assert "assign_note_out" in types

@pytest.mark.integration
def test_parse_intent_rate_experiment():
    from reply_handler import parse_reply_intents
    intents = parse_reply_intents("Rate Saturday's experiment 4 stars", model="claude-haiku-4-5-20251001")
    types = [i["type"] for i in intents]
    assert "rate_experiment" in types
    rating = next(i for i in intents if i["type"] == "rate_experiment")
    assert rating.get("stars") == 4

@pytest.mark.integration
def test_parse_intent_acknowledgment():
    from reply_handler import parse_reply_intents
    intents = parse_reply_intents("done", model="claude-haiku-4-5-20251001")
    types = [i["type"] for i in intents]
    assert "acknowledgment" in types
```

Unit tests for promotion helper (no API call):

```python
def test_promote_recipe_updates_yaml(tmp_path):
    import yaml
    from reply_handler import _promote_recipe_to_rotation

    recipe = {
        "id": "merguez-01",
        "name": "Merguez",
        "tags": ["experiment"],
    }
    p = tmp_path / "merguez-01.yaml"
    p.write_text(yaml.dump(recipe))

    _promote_recipe_to_rotation("merguez-01", str(tmp_path))

    updated = yaml.safe_load(p.read_text())
    assert "onRotation" in updated["tags"]
    assert "promoted_at" in updated
```

- [ ] **Step 2: Run to confirm unit test fails**

```bash
python -m pytest test_reply_handler.py::test_promote_recipe_updates_yaml -v
```

- [ ] **Step 3: Extend INTENT_SYSTEM_PROMPT in reply_handler.py**

Append the new intent type examples to the existing prompt string:

```
5. assign_note_out — user says they are eating out or food is being provided
   {"type": "assign_note_out", "day": "Tuesday", "note_text": "Dinner at Sarah's"}

6. assign_note_cook — user wants to cook something not in the library
   {"type": "assign_note_cook", "day": "Sunday", "note_text": "Waffles for dinner"}

7. force_no_cook — user wants to make a day no-cook/leftovers
   {"type": "force_no_cook", "day": "Thursday"}

8. force_cook — user wants to add a cook night where none was planned
   {"type": "force_cook", "day": "Wednesday", "constraint": null}

9. skip_experiment — user wants to skip the experiment this week
   {"type": "skip_experiment"}

10. rate_experiment — user is rating a cooked experiment recipe
    {"type": "rate_experiment", "day": "Saturday", "stars": 4, "recipe_id": null}
    stars is an integer 1-5. recipe_id is null if unspecified.

11. promote_experiment — user wants to add an experiment recipe to regular rotation
    {"type": "promote_experiment", "recipe_id": null}
    recipe_id is null if the user doesn't specify a name (infer from context).

12. acknowledgment — user is approving the plan or signing off
    {"type": "acknowledgment"}
    Examples: "done", "looks good", "okay thanks", "perfect"
```

- [ ] **Step 4: Add `_promote_recipe_to_rotation()` module-level helper**

Add after `apply_intents()`:

```python
def _promote_recipe_to_rotation(recipe_id: str, recipe_dir: str) -> bool:
    """
    Add 'onRotation' tag to a recipe YAML file and set promoted_at date.
    Returns True on success, False if the file was not found.
    """
    import yaml as _yaml
    from datetime import date as _date
    path = Path(recipe_dir) / f"{recipe_id}.yaml"
    if not path.exists():
        log.warning("Cannot promote — recipe file not found: %s", path)
        return False
    with open(path) as f:
        recipe = _yaml.safe_load(f)
    tags = recipe.get("tags", [])
    if "onRotation" not in tags:
        tags.append("onRotation")
        recipe["tags"] = tags
    recipe["promoted_at"] = _date.today().isoformat()
    with open(path, "w") as f:
        _yaml.dump(recipe, f, allow_unicode=True, sort_keys=False)
    log.info("Promoted %s to onRotation.", recipe_id)
    return True
```

- [ ] **Step 5: Run unit tests**

```bash
python -m pytest test_reply_handler.py -v -m "not integration"
```
Expected: all unit tests pass.

- [ ] **Step 6: Commit**

```bash
git add reply_handler.py test_reply_handler.py
git commit -m "feat: add Phase 1 intent types and promote helper to reply_handler"
```

---

## Task 8: End-to-end smoke test

**Files:** No code changes — manual verification.

- [ ] **Step 1: Verify API key is set**

```bash
python -c "import os; print('API key set:', bool(os.environ.get('ANTHROPIC_API_KEY') or open('config.yaml').read().find('api_key')))"
```

- [ ] **Step 2: Run full test suite**

```bash
python -m pytest -v -m "not integration"
```
Expected: all tests pass.

- [ ] **Step 3: Run a dry-run plan**

```bash
python main.py plan --dry-run --verbose
```
Expected: Agent runs tool loop, plan prints to terminal in §5 format, no database write.

- [ ] **Step 4: Verify output format**

Check the terminal output includes:
- `MEAL PLAN:` header with date range and cook night count
- `RATIONALE` section (3–5 sentence narrative, not a slot-by-slot list)
- `THIS WEEK` with all 7 days labelled Fri–Thu
- `GROCERY LIST` with at least one category
- Closing prompt: `Respond to make changes, or type 'done' to approve:`

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "chore: Phase 1 complete — agentic planner CLI-first"
```
