# Design: Walmart Cart CDP Fix + Last-Week Review Substitutions

**Date:** 2026-07-20  
**Status:** Approved  
**PRD reference:** `prd_v1_5.md` §9 (Walmart cart), `docs/walmart-cart-next-steps.md`

---

## Overview

Two independent features:

1. **Walmart cart CDP fix** — swap Playwright's bundled Chromium for a CDP connection to the user's real running Chrome instance, eliminating bot detection.
2. **Last-week review substitutions** — extend the pre-planning correction flow so users can say "we didn't make the tacos, we made chicken cacciatore instead," clearing the original recipe from recency history and recording the actual meal cooked.

---

## Feature 1: Walmart Cart CDP Fix

### Problem

`CartFiller.fill()` currently calls `pw.chromium.launch_persistent_context(...)`, which launches Playwright's bundled Chromium. Walmart's bot detection flags this browser and presents a CAPTCHA. `playwright-stealth` did not resolve it.

### Solution

Connect to the user's actual running Chrome instance via Chrome DevTools Protocol (CDP). Walmart sees a real browser with real cookies, real fingerprint, and an existing authenticated session.

### File changes

**`cart_filler.py`**
- `__init__`: remove `profile_dir` and `headless` params; add `cdp_url = config.get("walmart", {}).get("cdp_url", "http://localhost:9222")`.
- `fill()`: replace `pw.chromium.launch_persistent_context(...)` with `pw.chromium.connect_over_cdp(self.cdp_url)`. Use `browser.contexts[0]` (already-logged-in context) instead of a new context. `_ensure_session` remains as a fast sanity check but login handling is no longer needed.
- Error message on failed connection points user to `chrome-debug` alias.

**`config.yaml` / `config.yaml.template`**
```yaml
walmart:
  enabled: true
  cdp_url: "http://localhost:9222"
  # removed: profile_dir, headless
```

**`main.py` `cmd_cart`**
- Replace "Launching browser" message with "Connecting to Chrome at {cdp_url}..."
- Add: "(Start Chrome with debug port if this fails: chrome-debug)"

**`README.md`**
- Add one-time setup under Walmart section:
  - Add `chrome-debug` alias to `~/.zshrc`
  - Log into Walmart in that Chrome window once
  - Future runs: just have Chrome open (alias starts it with debug port if not already running)

**`test_cart_filler.py`**
- Mock `connect_over_cdp` instead of `launch_persistent_context`; no structural test changes needed.

### One-time user setup

```bash
# Add to ~/.zshrc
alias chrome-debug='/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --profile-directory=Default &'

# Then run once before cart filling:
chrome-debug
# Log into Walmart in that Chrome window
```

---

## Feature 2: Last-Week Review Substitutions

### Problem

The existing `_review_last_week` flow only handles removal: "we didn't cook the tacos" clears that recipe's `last_planned`. It cannot record what was actually cooked instead. This leaves the planner's recency data stale in both directions — the skipped recipe appears overdue sooner than it should, and the actual meal cooked is invisible to the planner.

### Solution

Extend the Claude prompt in `_review_last_week` to extract both what wasn't cooked *and* what was substituted. Match substitutions against the recipe library (Claude gets the library as context). For matched recipes, update `last_planned`. For unmatched free-text meals, record a `meal_notes` entry.

### Data flow

```
User: "we didn't make the tacos Monday, we made chicken cacciatore instead"
    │
    ▼
Claude (with recipe library as context)
    │
    ▼
{
  "not_cooked": ["chorizo-and-potato-tacos"],
  "substitutions": [
    {
      "original_recipe_id": "chorizo-and-potato-tacos",
      "recipe_id": "chicken-cacciatore",        ← matched from library, or null
      "free_text": "chicken cacciatore",
      "cook_date": "2026-07-14"                 ← inferred from day mention, or null
    }
  ]
}
    │
    ├─ not_cooked → store.unplan_recipe(recipe_id, last_week_key)   [existing]
    │
    ├─ substitution.recipe_id not null → store.mark_recipe_planned(recipe_id, cook_date)  [new]
    │
    └─ substitution.recipe_id null → store.record_meal_note(last_week_key, date_str, "cook", free_text)  [existing]
```

### `state_store.py` — new method

```python
def mark_recipe_planned(self, recipe_id: str, cook_date: date) -> None:
    """Record that a recipe was cooked on cook_date, even if not in the original plan."""
    with self._transaction():
        self._conn.execute("""
            INSERT INTO tracked_recipes (recipe_id, last_planned)
            VALUES (?, ?)
            ON CONFLICT(recipe_id) DO UPDATE SET last_planned = excluded.last_planned
        """, (recipe_id, cook_date.isoformat()))
```

### Claude prompt changes (`_review_last_week`)

The prompt gains two additions:
1. The recipe library appended as a list of `(id, name)` pairs so Claude can do fuzzy matching.
2. The return schema extended to include `substitutions`:

```
Return a JSON object with this exact shape:
{
  "not_cooked": ["recipe-id-1"],
  "substitutions": [
    {
      "original_recipe_id": "recipe-id-replaced-or-null",
      "recipe_id": "matched-library-id-or-null",
      "free_text": "what they said they cooked",
      "cook_date": "YYYY-MM-DD or null"
    }
  ]
}
substitutions may be empty []. recipe_id must be an exact id from the library or null.
cook_date: infer from day name if mentioned (e.g. "Monday" → date of last Monday), else null.
original_recipe_id: the planned recipe being replaced, or null if not specified.
```

### `cook_date` inference

`_review_last_week` already knows `last_week_key` (the Monday of last week). If Claude returns `cook_date: null`, fall back to the date of the original slot for that recipe (look it up from the saved plan dict). If that's also unavailable, use the Monday of last week.

### Confirmation output

```
Got it — chorizo tacos cleared from last week.
Got it — chicken cacciatore recorded as cooked Monday Jul 14.
```

Or for a free-text substitution with no library match:
```
Got it — chorizo tacos cleared from last week.
Got it — "chicken cacciatore" noted (not in recipe library — won't affect rotation timing).
```

### Files changed

| File | Change |
|------|--------|
| `state_store.py` | Add `mark_recipe_planned(recipe_id, cook_date)` |
| `main.py` | Extend `_review_last_week` prompt + action handling |
| `test_state_store.py` | Add tests for `mark_recipe_planned` |

---

## What is NOT in scope

- Adding the actual substituted recipe to the recipe library (that's ATK import territory)
- Handling substitutions in the email reply flow (Phase 3, not yet started)
- Auto-promoting a substituted recipe to `onRotation`
