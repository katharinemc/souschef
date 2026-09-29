# Next Steps

_Last updated: 2026-09-28_

---

## Status

- **Walmart cart filling works end to end** (first full live run 2026-09-28).
  Setup and the "Robot or human?" prompt are in the README's "Walmart cart
  filling" section. The fixes and the one unverified piece are in
  `docs/RESUME.md`.
- **Last-week review substitutions** (shipped 2026-07-20) have **not** been
  manually verified yet. The steps below are out of date: the seed and check
  scripts use `souschef.db` and a `tracked_recipes` table, but the real
  database is `meal_planner.db` and the table is `recipe_history`. Fix the
  scripts before running them.

---

## Manual verification

### Last-week review substitutions

This runs automatically at the start of `python main.py plan` when a plan for the previous week exists.

**Setup: make sure last week has a plan**

If the state store already has a plan for the week of Mon 2026-07-13, you're ready. Otherwise, seed one:

```bash
python - <<'EOF'
import sqlite3, json
from datetime import date

db = sqlite3.connect("souschef.db")
week_key = "2026-07-13"
plan = {
    "week_key": week_key,
    "dinners": [
        {"weekday": "monday",    "date": "2026-07-13", "recipe_id": "chorizo-and-potato-tacos",  "label": "Chorizo & Potato Tacos",   "is_no_cook": False},
        {"weekday": "tuesday",   "date": "2026-07-14", "recipe_id": "chicken-tikka-masala",       "label": "Chicken Tikka Masala",     "is_no_cook": False},
        {"weekday": "wednesday", "date": "2026-07-15", "recipe_id": None,                          "label": "no cook",                  "is_no_cook": True},
        {"weekday": "thursday",  "date": "2026-07-16", "recipe_id": "pasta-e-fagioli",             "label": "Pasta e Fagioli",          "is_no_cook": False},
        {"weekday": "friday",    "date": "2026-07-17", "recipe_id": "sheet-pan-salmon",            "label": "Sheet Pan Salmon",         "is_no_cook": False},
    ],
}
db.execute(
    "INSERT OR REPLACE INTO weekly_plans (week_key, plan_json) VALUES (?, ?)",
    (week_key, json.dumps(plan))
)
db.commit()
print("Seeded plan for", week_key)
EOF
```

**Test case A: skipped recipe only**

Run `python main.py plan`. At the "Any corrections?" prompt, type:

```
we didn't make the tacos Monday
```

Expected output:
```
  Got it — Chorizo & Potato Tacos cleared from last week's records.
```

Then check the state store — `last_planned` for `chorizo-and-potato-tacos` should now be unset or earlier than 2026-07-13:
```bash
python - <<'EOF'
import sqlite3
db = sqlite3.connect("souschef.db")
row = db.execute("SELECT * FROM tracked_recipes WHERE recipe_id = 'chorizo-and-potato-tacos'").fetchone()
print(row)
EOF
```

**Test case B: substitution matched to library**

Reset and reseed (run the seed script above again), then run `python main.py plan` and enter:

```
we didn't make the tacos Monday, we made chicken cacciatore instead
```

Expected output (exact recipe name depends on what's in your library):
```
  Got it — Chorizo & Potato Tacos cleared from last week's records.
  Got it — Chicken Cacciatore recorded as cooked Mon Jul 13.
```

If "chicken cacciatore" isn't in your recipe library, it falls through to test case C behavior.

**Test case C: substitution not in library (free-text fallback)**

Run `python main.py plan` and enter something definitely not in the library:

```
we skipped tikka masala Tuesday and made homemade pizza instead
```

Expected output:
```
  Got it — Chicken Tikka Masala cleared from last week's records.
  Got it — "homemade pizza" noted for Tue Jul 14 (not in recipe library — won't affect rotation timing).
```

**Test case D: press Enter to skip corrections**

Run `python main.py plan` and press Enter at the corrections prompt. The planner should proceed immediately with no changes to history.

---

## Remaining known issues

As of 2026-09-28 the full suite passes (366 passed).

- **Walmart: possible double-adds on rerun.** Search tiles don't always
  show that an item is already in the cart, so running the cart fill twice
  may add duplicates. See the open item in `docs/RESUME.md` for how to check on the next run.

### Fixed 2026-09-18: TestAmendConfirmFlow failures

The 7 `test_reply_handler.py::TestAmendConfirmFlow` failures seen after the
2026-08-05 commit were a stale test fixture, not a product bug: `MONDAY` was
hardcoded to `date(2026, 3, 23)`, and once real time passed that date by more
than 4 months, `state_store.record_plan()`'s history purge deleted the
just-inserted plan before the test could read it back. Fixed by deriving
`MONDAY`/`WEEK_KEY` from `date.today()` instead.

### Fixed 2026-09-18: lunch rotation never actually rotated

The backlog below used to list "Lunch rotation" as unbuilt. It's more
precise to say it was built but broken: both planners already select a
lunch each week (`planner.py::_assign_lunch`, `agentic_planner.py`'s
AVAILABLE LUNCHES prompt section), but nothing ever wrote the chosen
lunch's date to `lunch_history` — `state_store.set_lunch_last_planned()`
existed but was dead code. `record_plan()`'s "update last_planned" loop
routed *every* meal, including the lunch row, into `recipe_history`
(the dinner-rotation table), keyed on the lunch's id.

Effect: `get_all_lunch_last_planned()` always returned `{}`, so the legacy
planner's overdue-first sort was a no-op tie (stable sort → always the
first entry in `lunches.yaml`), and the agentic planner's prompt never
mentioned lunch history at all, giving Claude no rotation signal either.
Every week would pick the same lunch. (Checked the live `meal_planner.db`
— no pollution had accumulated in `recipe_history` yet, so no data
migration was needed.)

Fixed: `record_plan()` now routes `slot == "lunch"` rows to
`lunch_history` instead of `recipe_history`; `agentic_planner.py`'s
AVAILABLE LUNCHES section now lists each lunch's `last_planned` date and
the system prompt instructs Claude to prefer the most overdue one.

---

## Backlog (from project docs)

Priority order based on `decisions.md` and prior dogfooding notes:

1. **Email reply flow (Phase 3)** — handle substitution corrections via email reply, not just the CLI prompt. Note: `main.py amend`/`confirm` (shipped 2026-08-05) already cover this for CLI-driven corrections; this item is specifically about doing it via email.
2. **ATK recipe import** — import recipes from America's Test Kitchen into the YAML library.
3. **Walmart cart: quantity-aware search** — the agent picks one package per list line and doesn't reason about amounts (e.g., "1.5 lb ground beef" vs. package sizes). Real prices now reach the agent (fixed 2026-09-28), which should help here. Two related findings from the 2026-09-28 run: it reasonably bought one gallon of milk to cover two milk lines, and the grocery list has near-duplicate lines (two chicken-breast entries from different recipes) that the grocery builder could merge before the cart step.
4. **Substitution → rotation promotion** — if you substitute a recipe three times, prompt to add it to the official rotation.
5. **Multi-dish ("combo") meals** — the plan model is one recipe per day (`MealSlot.recipe_id`), so a meal made of a main + sides (e.g. Chicken Fried Steak + Biscuits with Sawmill Gravy + Spicy Southern Cabbage) has nowhere real to go. Worked around 2026-09-18 by baking the sides into the day's label string, which (a) doesn't pull the sides' ingredients into the grocery list and (b) isn't something `swap_day`/`amend` can produce on its own — it took a direct DB edit. If combo nights are a recurring pattern rather than a one-off, this needs a real field (e.g. `MealSlot.sides: list[str]` merged into grocery building), not another label hack.

_(Removed: "Seasons support" — already implemented, see `decisions.md` 2026-05-30. "Lunch rotation" — fixed 2026-09-18, see above.)_
