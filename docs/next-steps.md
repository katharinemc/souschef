# Next Steps

_Last updated: 2026-07-20_

---

## What just shipped (branch `main`, commits `0e0464b`–`494fcee`)

Two features:

1. **Walmart cart CDP fix** — the cart filler now connects to your real running Chrome instead of launching Playwright's bundled Chromium. Walmart's bot detection was flagging the Playwright browser; your real Chrome with its existing session is undetectable.

2. **Last-week review substitutions** — during pre-planning you can now say what you _actually_ cooked when the plan changed. The planner clears the skipped recipe from recency history and records the substitution so rotation timing stays accurate.

---

## Manual verification

### Feature 1: Walmart cart filling via CDP

**Prerequisites**

- `walmart.enabled: true` in `config.yaml` (already set)
- The `chrome-debug` alias is in `~/.zshrc` (see README §"Walmart cart filling")
- You are logged into Walmart in that Chrome session

**Steps**

1. Quit any running Chrome instance completely (`⌘Q`).
2. Open a new terminal and run:
   ```bash
   chrome-debug
   ```
   Chrome opens. Navigate to walmart.com and confirm you're logged in.

3. In the souschef directory, run a plan to generate a grocery list (or skip to step 4 if one already exists):
   ```bash
   python main.py plan
   ```

4. Run the cart filler:
   ```bash
   python main.py cart
   ```

**Expected output**
```
Connecting to Chrome at http://localhost:9222 ...
(If this fails, start Chrome with: chrome-debug — see README for setup.)
```
Followed by the agent working through the grocery list and a summary of added/skipped/not-found items.

**Failure modes to check**
- If you see "Could not connect to Chrome at http://localhost:9222" → Chrome wasn't started with `chrome-debug`. Quit Chrome and rerun `chrome-debug`.
- If you see "Not logged into Walmart" → log into Walmart in the Chrome window that opened, then rerun `python main.py cart`.
- No CAPTCHA or "robot or human?" page should appear.

---

### Feature 2: Last-week review substitutions

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

- `test_state_store.py` has 5 pre-existing test failures unrelated to this work. These were failing before this branch and are not regressions.

---

## Backlog (from project docs)

Priority order based on `decisions.md` and prior dogfooding notes:

1. **Seasons support** — recipes tagged `summer`/`winter` should be filtered by current season. Scaffold exists in YAML; planner doesn't filter yet.
2. **Email reply flow (Phase 3)** — handle substitution corrections via email reply, not just the CLI prompt.
3. **ATK recipe import** — import recipes from America's Test Kitchen into the YAML library.
4. **Lunch rotation** — `lunches.yaml` exists; nothing generates a lunch plan yet.
5. **Walmart cart: quantity-aware search** — the agent currently searches by name; matching requested quantities (e.g., "1.5 lb ground beef") to package sizes is unreliable.
6. **Substitution → rotation promotion** — if you substitute a recipe three times, prompt to add it to the official rotation.
