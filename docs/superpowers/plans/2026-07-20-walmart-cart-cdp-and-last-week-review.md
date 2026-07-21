# Walmart Cart CDP Fix + Last-Week Review Substitutions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix Walmart cart bot detection by switching to CDP (real Chrome), and extend the pre-planning last-week review to record what was actually cooked when a substitution is mentioned.

**Architecture:** Two independent features. Part 1 swaps Playwright's bundled Chromium for `connect_over_cdp` in `cart_filler.py`; Part 2 extends the `_review_last_week` function in `main.py` to parse `substitutions` out of Claude's correction response and update `state_store` accordingly. No new files, no new state_store methods — `set_last_planned` and `record_meal_note` already exist.

**Tech Stack:** Python 3.11+, `playwright` (async API), `anthropic` SDK, SQLite via `StateStore`, PyYAML.

**Spec:** `docs/superpowers/specs/2026-07-20-walmart-cart-cdp-and-last-week-review-design.md`

---

## File Map

| File | Change |
|------|--------|
| `cart_filler.py` | Replace `profile_dir`/`headless`/`launch_persistent_context` with `cdp_url`/`connect_over_cdp`; simplify `_ensure_session`; remove `playwright_stealth` import |
| `config.yaml` | Swap `profile_dir`/`headless` → `cdp_url` in `walmart:` section |
| `config.yaml.template` | Same swap as config.yaml |
| `main.py` | Update `cmd_cart` connection message; add `recipe_dir` param to `_review_last_week`; extend prompt and action-handling for substitutions |
| `README.md` | Add `chrome-debug` alias setup under Walmart section |
| `test_cart_filler.py` | Update `_make_filler` helpers to use `cdp_url` instead of `profile_dir`/`headless` |
| `test_last_week_review.py` | New file — unit tests for substitution parsing and state mutations |

---

## Part 1: Walmart Cart CDP Fix

---

### Task 1: Update cart_filler.py

**Files:**
- Modify: `cart_filler.py` — `__init__`, `fill`, `_ensure_session`, module docstring

- [ ] **Step 1: Update the module docstring**

Replace the first 17 lines of `cart_filler.py` (through the closing `"""`) with:

```python
"""
cart_filler.py

Fills a Walmart cart from a GroceryList using an iterative Claude agent loop
backed by Playwright browser automation.

Connects to the user's running Chrome instance via CDP (Chrome DevTools Protocol)
so Walmart sees a real browser with a real session — no bot detection.

One-time setup:
  1. Add to ~/.zshrc:
       alias chrome-debug='/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --profile-directory=Default &'
  2. Run: chrome-debug
  3. Log into Walmart in that Chrome window.
  Future runs: just have Chrome open (the alias starts it if not already running).

Requires: pip install playwright && playwright install chromium
"""
```

- [ ] **Step 2: Update `__init__` — remove `profile_dir`/`headless`, add `cdp_url`**

Find this block in `CartFiller.__init__` (lines 211–218):
```python
    def __init__(self, config: dict, grocery: GroceryList):
        self.client      = anthropic.Anthropic(api_key=config.get("api_key") or None)
        self.model       = config.get("model", "claude-opus-4-5")
        self.profile_dir = Path(config.get("profile_dir", "~/.souschef/walmart_profile")).expanduser()
        self.headless    = config.get("headless", False)
        self.grocery     = grocery
        self.max_iterations = 150   # ~3 tool calls per item × 50 items
        self._last_results: list[dict] = []   # most recent search_walmart results
```

Replace with:
```python
    def __init__(self, config: dict, grocery: GroceryList):
        self.client      = anthropic.Anthropic(api_key=config.get("api_key") or None)
        self.model       = config.get("model", "claude-opus-4-5")
        self.cdp_url     = config.get("cdp_url", "http://localhost:9222")
        self.grocery     = grocery
        self.max_iterations = 150   # ~3 tool calls per item × 50 items
        self._last_results: list[dict] = []   # most recent search_walmart results
```

- [ ] **Step 3: Update `fill()` — swap to `connect_over_cdp`**

Find the full `fill()` method (lines 220–246):
```python
    async def fill(self) -> CartResult:
        """Launch persistent Playwright context and run the cart-filling agent loop."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright is required for cart filling.\n"
                "Install it with: pip install playwright && playwright install chromium"
            )

        self.profile_dir.mkdir(parents=True, exist_ok=True)

        from playwright_stealth import Stealth

        async with Stealth().use_async(async_playwright()) as pw:
            context = await pw.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.headless,
                channel="chrome",
                args=["--no-sandbox"],
            )
            page = await context.new_page()
            try:
                await self._ensure_session(page)
                return await self._run_agent_loop(page)
            finally:
                await context.close()
```

Replace with:
```python
    async def fill(self) -> CartResult:
        """Connect to running Chrome via CDP and run the cart-filling agent loop."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise RuntimeError(
                "Playwright is required for cart filling.\n"
                "Install it with: pip install playwright && playwright install chromium"
            )

        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.connect_over_cdp(self.cdp_url)
            except Exception:
                raise RuntimeError(
                    f"Could not connect to Chrome at {self.cdp_url}.\n"
                    "Start Chrome with remote debugging enabled:\n"
                    "  chrome-debug\n"
                    "(Add the alias to ~/.zshrc — see README for setup.)"
                )
            context = browser.contexts[0]
            page = await context.new_page()
            try:
                await self._ensure_session(page)
                return await self._run_agent_loop(page)
            finally:
                await page.close()
```

- [ ] **Step 4: Simplify `_ensure_session` — remove headed login flow**

Find `_ensure_session` (lines 252–277):
```python
    async def _ensure_session(self, page) -> None:
        """
        Navigate to Walmart and verify the session is active.
        If not logged in: headed mode prompts the user; headless mode raises.
        """
        await page.goto("https://www.walmart.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

        # Detect sign-in state: "Sign In" link present means not logged in
        sign_in = await page.query_selector('a[link-identifier="header-signin"], [data-testid="header-signin-btn"]')
        if sign_in is None:
            log.debug("Walmart session active.")
            return

        if self.headless:
            raise RuntimeError(
                "Not logged into Walmart and running headless.\n"
                "Set walmart.headless: false in config.yaml, run once to log in, "
                "then switch back to headless."
            )

        print(
            "\nNot logged into Walmart. Please log in using the browser window that just opened,\n"
            "then press Enter here to continue..."
        )
        input()
```

Replace with:
```python
    async def _ensure_session(self, page) -> None:
        """Verify Walmart session is active in the connected Chrome instance."""
        await page.goto("https://www.walmart.com", wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

        sign_in = await page.query_selector('a[link-identifier="header-signin"], [data-testid="header-signin-btn"]')
        if sign_in is None:
            log.debug("Walmart session active.")
            return

        raise RuntimeError(
            "Not logged into Walmart in the connected Chrome window.\n"
            "Please log into Walmart in Chrome, then run the cart command again."
        )
```

- [ ] **Step 5: Remove unused `Path` import if `profile_dir` was its only use**

Check the top of `cart_filler.py` for `from pathlib import Path`. If `Path` is no longer referenced anywhere in the file after the changes above, remove that import line. If it's used elsewhere, leave it.

```bash
grep -n "Path\b" cart_filler.py
```

Remove the import only if the grep returns no hits other than the import line itself.

- [ ] **Step 6: Commit**

```bash
git add cart_filler.py
git commit -m "feat: switch CartFiller from Playwright profile to CDP (connect_over_cdp)"
```

---

### Task 2: Update config files, main.py, and README

**Files:**
- Modify: `config.yaml`
- Modify: `config.yaml.template`
- Modify: `main.py` — `cmd_cart`
- Modify: `README.md`

- [ ] **Step 1: Update `config.yaml`**

Find the `walmart:` section:
```yaml
walmart:
  enabled: false
  profile_dir: "~/.souschef/walmart_profile"  # Playwright saves your Walmart session here
  headless: false                              # Set true after first login to run silently
```

Replace with:
```yaml
walmart:
  enabled: false
  cdp_url: "http://localhost:9222"   # Connect to Chrome with: chrome-debug (see README)
```

- [ ] **Step 2: Update `config.yaml.template`**

Make the same change in `config.yaml.template` — replace the `walmart:` section:
```yaml
walmart:
  enabled: false
  cdp_url: "http://localhost:9222"   # Connect to Chrome with: chrome-debug (see README)
```

- [ ] **Step 3: Update `cmd_cart` connection message in `main.py`**

Find these lines in `cmd_cart` (around line 568–573):
```python
    cart_cfg = {**flat, **walmart_cfg}
    filler   = CartFiller(config=cart_cfg, grocery=grocery)

    profile = cart_cfg.get("profile_dir", "~/.souschef/walmart_profile")
    headless = cart_cfg.get("headless", False)
    print(f"\nLaunching browser (profile: {profile}, headless: {headless}) ...")
```

Replace with:
```python
    cart_cfg = {**flat, **walmart_cfg}
    filler   = CartFiller(config=cart_cfg, grocery=grocery)

    cdp_url = cart_cfg.get("cdp_url", "http://localhost:9222")
    print(f"\nConnecting to Chrome at {cdp_url} ...")
    print("(If this fails, start Chrome with: chrome-debug — see README for setup.)")
```

- [ ] **Step 4: Update README.md — add chrome-debug setup under Walmart**

Find the Walmart section in `README.md`. Add a "First-time setup" subsection. The exact location will be wherever `### Walmart` or `## Walmart` currently appears. Add:

```markdown
### First-time Chrome setup

The cart filler connects to your running Chrome instance so Walmart sees your real browser and session.

**Add the `chrome-debug` alias to `~/.zshrc`:**

```bash
alias chrome-debug='/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --profile-directory=Default &'
```

Then reload your shell:

```bash
source ~/.zshrc
```

**One-time login:**

```bash
chrome-debug        # opens Chrome with remote debugging on port 9222
```

Log into Walmart in that Chrome window. You only need to do this once — your session is preserved.

**Every subsequent run:**

Just have Chrome open (the alias starts it with the debug port if it isn't already running). Then:

```bash
python main.py cart --week YYYY-MM-DD
```
```

- [ ] **Step 5: Commit**

```bash
git add config.yaml config.yaml.template main.py README.md
git commit -m "feat: update config and main.py for CDP cart connection; add README setup"
```

---

### Task 3: Update test_cart_filler.py

**Files:**
- Modify: `test_cart_filler.py` — two `_make_filler` helpers

The tests call `_run_agent_loop` directly with a mock page, so they never touch `fill()` or `connect_over_cdp`. The only change needed is removing the now-nonexistent `profile_dir`/`headless` config keys from the two `_make_filler` helpers.

- [ ] **Step 1: Update `_make_filler` in `TestCartFillerHelpers`**

Find (around line 150):
```python
    def _make_filler(self, *names):
        from cart_filler import CartFiller
        grocery = _make_grocery(*names)
        cfg = {"model": "claude-test", "profile_dir": "/tmp/test_profile", "headless": True}
        return CartFiller(config=cfg, grocery=grocery)
```

Replace with:
```python
    def _make_filler(self, *names):
        from cart_filler import CartFiller
        grocery = _make_grocery(*names)
        cfg = {"model": "claude-test", "cdp_url": "http://localhost:9222"}
        return CartFiller(config=cfg, grocery=grocery)
```

- [ ] **Step 2: Update `_make_filler` in `TestCartFillerAgentLoop`**

Find (around line 198):
```python
    def _make_filler(self, *names):
        from cart_filler import CartFiller
        grocery = _make_grocery(*(names or ("ground beef", "onion")))
        cfg = {"model": "claude-test", "profile_dir": "/tmp/test_profile", "headless": True}
        f = CartFiller(config=cfg, grocery=grocery)
        f.max_iterations = 10
        return f
```

Replace with:
```python
    def _make_filler(self, *names):
        from cart_filler import CartFiller
        grocery = _make_grocery(*(names or ("ground beef", "onion")))
        cfg = {"model": "claude-test", "cdp_url": "http://localhost:9222"}
        f = CartFiller(config=cfg, grocery=grocery)
        f.max_iterations = 10
        return f
```

- [ ] **Step 3: Run the full cart test suite**

```bash
python -m pytest test_cart_filler.py -v
```

Expected: all tests pass. There should be no references to `profile_dir` or `headless` in any failure.

- [ ] **Step 4: Commit**

```bash
git add test_cart_filler.py
git commit -m "test: update cart_filler tests for CDP config (remove profile_dir/headless)"
```

---

## Part 2: Last-Week Review Substitutions

---

### Task 4: Write tests for substitution handling

**Files:**
- Create: `test_last_week_review.py`

These tests mock the Claude API response and verify that `_review_last_week` calls the right `StateStore` methods when substitutions are present.

- [ ] **Step 1: Create `test_last_week_review.py`**

```python
"""Tests for _review_last_week substitution handling in main.py."""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from state_store import StateStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_recipe(recipe_dir: str, rid: str, name: str) -> None:
    p = Path(recipe_dir) / f"{rid}.yaml"
    p.write_text(yaml.dump({
        "id": rid,
        "name": name,
        "tags": ["onRotation"],
        "ingredients": [],
    }))


def _save_plan(store: StateStore, monday: date, dinners: list[dict]) -> None:
    """Store a minimal plan dict for the given week."""
    week_key = monday.isoformat()
    plan_dict = {
        "week_start_monday": week_key,
        "week_key": week_key,
        "dinners": [
            {
                "date": (monday + timedelta(days=i)).isoformat(),
                "weekday": (monday + timedelta(days=i)).strftime("%A"),
                "recipe_id": d["recipe_id"],
                "label": d["label"],
                "tags": [],
                "ingredients": [],
                "is_no_cook": False,
                "note_type": None,
                "note_text": None,
            }
            for i, d in enumerate(dinners)
        ],
        "lunch": None,
        "cook_nights": len(dinners),
        "rationale": "",
        "warnings": [],
    }
    store.record_plan(week_key, plan_dict, [])


def _mock_claude(payload: dict) -> MagicMock:
    client = MagicMock()
    content = MagicMock()
    content.text = json.dumps(payload)
    client.messages.create.return_value.content = [content]
    return client


def _call_review(store, this_monday, model, recipe_dir, user_input, claude_response):
    from main import _review_last_week
    mock_client = _mock_claude(claude_response)
    with patch("builtins.input", return_value=user_input), \
         patch("anthropic.Anthropic", return_value=mock_client):
        _review_last_week(store, this_monday, model, recipe_dir)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class TestReviewLastWeekSubstitutions(unittest.TestCase):

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = StateStore(self.db_path)

        self.recipe_dir = tempfile.mkdtemp()
        _write_recipe(self.recipe_dir, "chorizo-and-potato-tacos", "Chorizo Tacos")
        _write_recipe(self.recipe_dir, "hamburger-steaks", "Hamburger Steaks")
        _write_recipe(self.recipe_dir, "chicken-cacciatore", "Chicken Cacciatore")

        self.last_monday = date(2026, 7, 13)
        _save_plan(self.store, self.last_monday, [
            {"recipe_id": "chorizo-and-potato-tacos", "label": "Chorizo Tacos"},
            {"recipe_id": "hamburger-steaks",         "label": "Hamburger Steaks"},
        ])
        self.this_monday = self.last_monday + timedelta(weeks=1)

    def tearDown(self):
        self.store.close()
        os.unlink(self.db_path)

    # -----------------------------------------------------------------------
    # not_cooked behaviour (existing, regression check)
    # -----------------------------------------------------------------------

    def test_not_cooked_clears_recipe_history(self):
        self.store.set_last_planned("chorizo-and-potato-tacos", self.last_monday)
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "we didn't make the tacos",
            {"not_cooked": ["chorizo-and-potato-tacos"], "substitutions": []},
        )
        self.assertIsNone(self.store.get_last_planned("chorizo-and-potato-tacos"))

    # -----------------------------------------------------------------------
    # substitution — library match
    # -----------------------------------------------------------------------

    def test_substitution_library_match_records_last_planned(self):
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "we didn't make tacos, we made chicken cacciatore instead",
            {
                "not_cooked": ["chorizo-and-potato-tacos"],
                "substitutions": [{
                    "original_recipe_id": "chorizo-and-potato-tacos",
                    "recipe_id": "chicken-cacciatore",
                    "free_text": "chicken cacciatore",
                    "cook_date": "2026-07-13",
                }],
            },
        )
        lp = self.store.get_last_planned("chicken-cacciatore")
        self.assertEqual(lp, date(2026, 7, 13))

    def test_substitution_library_match_does_not_record_meal_note(self):
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "made cacciatore instead of tacos",
            {
                "not_cooked": [],
                "substitutions": [{
                    "original_recipe_id": None,
                    "recipe_id": "chicken-cacciatore",
                    "free_text": "chicken cacciatore",
                    "cook_date": "2026-07-13",
                }],
            },
        )
        notes = self.store.get_meal_notes(self.last_monday.isoformat())
        self.assertEqual(notes, [])

    # -----------------------------------------------------------------------
    # substitution — free-text fallback (no library match)
    # -----------------------------------------------------------------------

    def test_substitution_no_match_records_meal_note(self):
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "we made lasagna instead of steaks",
            {
                "not_cooked": ["hamburger-steaks"],
                "substitutions": [{
                    "original_recipe_id": "hamburger-steaks",
                    "recipe_id": None,
                    "free_text": "lasagna",
                    "cook_date": "2026-07-14",
                }],
            },
        )
        notes = self.store.get_meal_notes(self.last_monday.isoformat())
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["note_text"], "lasagna")
        self.assertEqual(notes[0]["note_type"], "cook")
        self.assertEqual(notes[0]["meal_date"], "2026-07-14")

    def test_substitution_no_match_does_not_set_last_planned(self):
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "made lasagna instead",
            {
                "not_cooked": [],
                "substitutions": [{
                    "original_recipe_id": None,
                    "recipe_id": None,
                    "free_text": "lasagna",
                    "cook_date": "2026-07-13",
                }],
            },
        )
        self.assertIsNone(self.store.get_last_planned("lasagna"))

    # -----------------------------------------------------------------------
    # cook_date inference fallback
    # -----------------------------------------------------------------------

    def test_cook_date_inferred_from_original_slot_when_null(self):
        """When Claude returns cook_date=null, falls back to the original slot's date."""
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "made cacciatore instead of tacos",
            {
                "not_cooked": [],
                "substitutions": [{
                    "original_recipe_id": "chorizo-and-potato-tacos",
                    "recipe_id": "chicken-cacciatore",
                    "free_text": "chicken cacciatore",
                    "cook_date": None,
                }],
            },
        )
        lp = self.store.get_last_planned("chicken-cacciatore")
        # Tacos were slot 0 → date = last_monday + 0 days = 2026-07-13
        self.assertEqual(lp, date(2026, 7, 13))

    def test_cook_date_falls_back_to_last_monday_when_no_original(self):
        """When cook_date=null and no original_recipe_id, uses last_monday."""
        _call_review(
            self.store, self.this_monday, "claude-test", self.recipe_dir,
            "made cacciatore at some point",
            {
                "not_cooked": [],
                "substitutions": [{
                    "original_recipe_id": None,
                    "recipe_id": "chicken-cacciatore",
                    "free_text": "chicken cacciatore",
                    "cook_date": None,
                }],
            },
        )
        lp = self.store.get_last_planned("chicken-cacciatore")
        self.assertEqual(lp, self.last_monday)

    # -----------------------------------------------------------------------
    # early-exit conditions
    # -----------------------------------------------------------------------

    def test_empty_input_skips_api_call(self):
        from main import _review_last_week
        with patch("builtins.input", return_value=""), \
             patch("anthropic.Anthropic") as mock_ant:
            _review_last_week(self.store, self.this_monday, "claude-test", self.recipe_dir)
        mock_ant.assert_not_called()

    def test_no_last_week_plan_returns_early(self):
        from main import _review_last_week
        far_future = self.this_monday + timedelta(weeks=52)
        with patch("anthropic.Anthropic") as mock_ant:
            _review_last_week(self.store, far_future, "claude-test", self.recipe_dir)
        mock_ant.assert_not_called()
```

- [ ] **Step 2: Run the tests to confirm they fail**

```bash
python -m pytest test_last_week_review.py -v 2>&1 | head -30
```

Expected: `TypeError: _review_last_week() takes 3 positional arguments but 4 were given` (the `recipe_dir` param doesn't exist yet).

---

### Task 5: Extend `_review_last_week` in main.py

**Files:**
- Modify: `main.py` — `_review_last_week` function (lines 104–184) and its call site in `cmd_plan`

- [ ] **Step 1: Replace `_review_last_week` with the extended version**

Find and replace the entire `_review_last_week` function (lines 104–184). The new version adds `recipe_dir` parameter, extends the Claude prompt, and handles `substitutions` alongside `not_cooked`.

```python
def _review_last_week(store, monday: date, model: str, recipe_dir: str = "recipes_yaml") -> None:
    """
    Show the previous week's plan and let the user correct what actually happened
    before the new plan is generated. Handles both skipped recipes (not_cooked)
    and substitutions ("we made X instead of Y"), updating recipe history accordingly.
    """
    import anthropic
    import json as _json
    from planner import load_recipes

    last_monday = monday - timedelta(weeks=1)
    last_week_key = last_monday.isoformat()
    plan = store.get_plan(last_week_key)
    if not plan:
        return

    all_dinners = plan.get("dinners", [])
    dinners = [d for d in all_dinners if not d.get("is_no_cook") and d.get("recipe_id")]
    if not dinners:
        return

    # Display last week
    week_end = last_monday + timedelta(days=6)
    print(f"\n━━━ LAST WEEK ({last_monday.strftime('%b %-d')} – {week_end.strftime('%b %-d')}) ━━━\n")
    for d in all_dinners:
        weekday = d.get("weekday", "")[:3]
        label   = d.get("label", "no cook")
        tags    = d.get("tags") or []
        tag_str = f"  [{', '.join(tags)}]" if tags else ""
        print(f"  {weekday:<4}  {label}{tag_str}")

    print("\nAny corrections before planning this week? (press Enter to skip): ", end="", flush=True)
    try:
        correction = input().strip()
    except (KeyboardInterrupt, EOFError):
        print()
        return

    if not correction:
        return

    # Load full recipe library for fuzzy matching
    all_recipes = load_recipes(recipe_dir)

    # Compact list of planned cook nights (for prompt context)
    recipe_list = "\n".join(
        f"  {d['recipe_id']} — {d['label']}"
        for d in dinners
    )

    # Full library list (for Claude to fuzzy-match substitutions against)
    library_lines = "\n".join(
        f"  {rid} — {r.get('name', rid)}"
        for rid, r in all_recipes.items()
    )

    # Day-name → ISO date mapping so Claude can resolve "Monday" → "2026-07-13"
    date_by_weekday: dict[str, str] = {}
    for d in all_dinners:
        d_date = d.get("date")
        d_weekday = d.get("weekday", "").lower()
        if d_date and d_weekday:
            date_by_weekday[d_weekday] = d_date
    day_map_lines = "\n".join(f"  {day}: {dt}" for day, dt in date_by_weekday.items())

    prompt = (
        f"Last week's planned cook nights:\n{recipe_list}\n\n"
        f"Full recipe library (for matching substitutions):\n{library_lines}\n\n"
        f"Day-to-date mapping for last week:\n{day_map_lines}\n\n"
        f"User says: \"{correction}\"\n\n"
        "Return a JSON object with this exact shape:\n"
        "{\n"
        "  \"not_cooked\": [\"recipe-id\"],\n"
        "  \"substitutions\": [\n"
        "    {\n"
        "      \"original_recipe_id\": \"planned-recipe-id-or-null\",\n"
        "      \"recipe_id\": \"matched-library-id-or-null\",\n"
        "      \"free_text\": \"what they said they cooked\",\n"
        "      \"cook_date\": \"YYYY-MM-DD-or-null\"\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "recipe_id must be an exact id from the full recipe library, or null.\n"
        "cook_date: use the day-to-date mapping if a day name is mentioned, else null.\n"
        "original_recipe_id: the planned recipe being replaced, or null if not specified.\n"
        "substitutions may be an empty list.\n"
        "Return only the JSON object, nothing else."
    )

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1].lstrip("json").strip()
        parsed = _json.loads(raw)
        not_cooked    = parsed.get("not_cooked", [])
        substitutions = parsed.get("substitutions", [])
    except Exception as exc:
        log.warning("Could not parse last-week correction: %s", exc)
        return

    # Apply not_cooked: remove recipe from history
    for recipe_id in not_cooked:
        if any(d.get("recipe_id") == recipe_id for d in dinners):
            store.unplan_recipe(recipe_id, last_week_key)
            label = next(d["label"] for d in dinners if d.get("recipe_id") == recipe_id)
            print(f"  Got it — {label} cleared from last week's records.")
        else:
            log.warning("Correction referenced unknown recipe_id '%s' — skipped.", recipe_id)

    # Apply substitutions: record what was actually cooked
    for sub in substitutions:
        rid        = sub.get("recipe_id")
        free_text  = sub.get("free_text") or ""
        cook_date  = sub.get("cook_date")
        orig_rid   = sub.get("original_recipe_id")

        # Infer cook_date from the original slot when Claude couldn't determine it
        if not cook_date and orig_rid:
            orig_slot = next(
                (d for d in all_dinners if d.get("recipe_id") == orig_rid), None
            )
            if orig_slot:
                cook_date = orig_slot.get("date")

        # Final fallback: Monday of last week
        if not cook_date:
            cook_date = last_week_key

        if rid and rid in all_recipes:
            recipe_name = all_recipes[rid].get("name", rid)
            cook_date_fmt = date.fromisoformat(cook_date).strftime("%a %b %-d")
            store.set_last_planned(rid, date.fromisoformat(cook_date))
            print(f"  Got it — {recipe_name} recorded as cooked {cook_date_fmt}.")
        else:
            store.record_meal_note(last_week_key, cook_date, "cook", free_text)
            print(
                f"  Got it — \"{free_text}\" noted "
                f"(not in recipe library — won't affect rotation timing)."
            )

    print()
```

- [ ] **Step 2: Update the call site in `cmd_plan`**

Find the call to `_review_last_week` in `cmd_plan` (around line 215):
```python
        _review_last_week(store, monday, flat.get("model", "claude-sonnet-4-20250514"))
```

Replace with:
```python
        _review_last_week(
            store,
            monday,
            flat.get("model", "claude-sonnet-4-20250514"),
            flat.get("recipe_dir", "recipes_yaml"),
        )
```

- [ ] **Step 3: Run the tests**

```bash
python -m pytest test_last_week_review.py -v
```

Expected: all tests pass.

- [ ] **Step 4: Run the full test suite to check for regressions**

```bash
python -m pytest -v --tb=short
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add main.py test_last_week_review.py
git commit -m "feat: extend last-week review to record substitutions (clear skipped, mark actual)"
```

---

## Final smoke check

- [ ] **Verify CDP cart config**

```bash
python -c "
import yaml
with open('config.yaml') as f:
    cfg = yaml.safe_load(f)
w = cfg.get('walmart', {})
assert 'cdp_url' in w, 'cdp_url missing from walmart config'
assert 'profile_dir' not in w, 'profile_dir should be removed'
assert 'headless' not in w, 'headless should be removed'
print('Config OK:', w)
"
```

- [ ] **Verify all tests pass**

```bash
python -m pytest -v --tb=short
```

Expected: all tests pass, no errors.
