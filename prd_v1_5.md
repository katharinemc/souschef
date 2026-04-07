# Meal Planning Agent — PRD v1.5
**Status:** Draft  
**Supersedes:** v1.0 (deterministic pipeline)  
**Target implementation:** Claude Code + Superpowers

---

## 1. Overview

A weekly meal planning agent for a household of four. Every Thursday morning it reads the family Google Calendar, reasons about the upcoming week, assigns meals from a curated recipe library, surfaces ingredient stacking opportunities, and delivers a plan and grocery list for review. The user reviews and responds in plain English; the agent revises immediately.

### What changed from v1.0

v1.0 was a deterministic pipeline with one LLM call (reply parsing). v1.5 replaces the core planner with a reasoning agent — Claude as orchestrator, with tools for calendar access, recipe queries, and meal assignment. Everything downstream (stacking, grocery building, output formatting) remains deterministic. The human approval loop is preserved.

### Build sequence

v1.5 is built CLI-first. Email is added only after the core plan → review → revise loop is working and the agent's reasoning feels right. This keeps the iteration cycle tight — you can run `plan` ten times in two minutes, inspect the rationale each time, and tune the system prompt without OAuth friction or email threading complexity.

```
Phase 1 — CLI only          agent plans, prints to terminal; you respond in terminal
Phase 2 — Add persistence   plan saved to SQLite; reply loads it without re-planning
Phase 3 — Add email         existing CLI output wrapped in email_sender; reply becomes Gmail reply
```

Email is a delivery mechanism, not a logic layer. The plan → review → revise loop does not change when email is added.

---

## 2. Core Principles

**Deterministic where predictability matters.** Calendar reading, ingredient categorisation, grocery aggregation, and output formatting are deterministic Python. These tasks have correct answers; LLM reasoning adds noise, not value.

**Agentic where judgment matters.** Recipe selection, week-level reasoning, conflict resolution, and natural language interaction are handled by the agent. These tasks require weighing tradeoffs the algorithm can't encode.

**Human in the loop, always.** The agent proposes; the user approves. No downstream action (cart fill, recipe import) runs without explicit user confirmation.

**Auditable output.** Every plan includes a rationale explaining the agent's key decisions. The user should always be able to understand why the plan looks the way it does.

---

## 3. System Architecture

### 3.1 Module Map

```
main.py                     Entry point and CLI
│
├── agentic_planner.py      NEW — replaces planner.py
│   ├── Tool definitions    get_calendar_constraints, get_recipes,
│   │                       get_rotation_history, get_recent_meals,
│   │                       assign_meal, assign_no_cook, assign_note,
│   │                       get_stacking_opportunities, finalize_plan
│   └── Agent loop          Claude as orchestrator
│
├── calendar_reader.py      Unchanged — returns WeekConstraints
├── stacker.py              Unchanged — ingredient overlap analysis
├── grocery_builder.py      Unchanged — categorised grocery list
├── output_formatter.py     NEW (Phase 1) — prints plan to terminal
├── email_sender.py         Updated (Phase 3) — wraps formatter output for email
├── reply_handler.py        Updated — handles notes, ratings, ATK; stdin in Phase 1
├── state_store.py          Updated — experiment ratings, notes history
├── recipe_loader.py        Unchanged — .txt ingest fallback
├── paprika_import.py       Unchanged — .paprikarecipes ingest
└── atk_importer.py         NEW — ATK recipe discovery and import
```

### 3.2 Data Flow

#### Phase 1 — CLI

```
python main.py plan
    │
    ▼
calendar_reader → WeekConstraints
    │
    ▼
agentic_planner (Claude + tools) → WeekPlan + rationale
    │
    ▼
stacker → StackingNotes
    │
    ▼
grocery_builder → GroceryList
    │
    ▼
output_formatter → printed to terminal
    │
    ▼
python main.py reply
    │
    ▼
[stdin] user types feedback
    │
    ▼
reply_handler → re-plan → printed to terminal
```

#### Phase 3 — Email added

```
(same as above through grocery_builder)
    │
    ▼
email_sender → Thursday email delivered
    │
    ▼
[user replies to email]
    │
    ▼
reply_handler (Gmail polling) → re-plan → revised email sent
```

The plan → review → revise logic is identical in both phases. Email wraps it; it does not change it.

### 3.3 CLI Commands

#### Phase 1

```bash
python main.py plan
# Agent reads calendar, reasons about the week, prints plan + rationale

python main.py reply
# Reads stdin, parses intent, re-plans, prints revised plan

python main.py plan --dry-run
# Runs full pipeline without writing to state_store

python main.py status
# Prints database summary and next week key

python main.py ingest --input MyRecipes.paprikarecipes
# Imports Paprika export to recipes_yaml/
```

#### Phase 3 additions

```bash
python main.py plan --email
# Sends plan email instead of printing to terminal

python main.py reply --email
# Polls Gmail for replies instead of reading stdin

python main.py cart --week 2026-03-23
# Fills Walmart cart from approved grocery list (requires Chrome MCP)
```

---

## 4. The Agentic Planner

### 4.1 What It Replaces

`planner.py` sorted recipes by `last_planned` and filled slots via a fixed priority algorithm. It produced valid plans but had no judgment — it couldn't reason about the week as a whole, explain its choices, or handle constraint conflicts gracefully.

`agentic_planner.py` replaces the algorithm with Claude as the reasoning engine. Same inputs, same `WeekPlan` output, plus a `rationale` field.

### 4.2 Agent Tools

Each tool is a deterministic Python function. The agent decides when to call them and what to do with the results.

| Tool | Description |
|---|---|
| `get_calendar_constraints()` | Returns the week's `WeekConstraints` — no-cook days, fasting days, open Saturday, Sunday easy flag |
| `get_recipes(tags, meatless_only, exclude_ids)` | Returns recipes from the library, optionally filtered |
| `get_rotation_history(recipe_ids)` | Returns `last_planned` dates for recipes |
| `get_recent_meals(weeks)` | Returns the last N weeks of planned meals |
| `assign_meal(day, recipe_id, notes)` | Assigns a recipe to a day; validates hard constraints |
| `assign_no_cook(day, reason)` | Marks a day no-cook or leftovers |
| `assign_note(day, note_text, note_type)` | Assigns a freeform note to a day (`out` or `cook`) |
| `assign_lunch(lunch_id)` | Assigns the lunch of the week |
| `get_stacking_opportunities(recipe_ids)` | Returns ingredient overlap analysis for assigned recipes |
| `finalize_plan(rationale)` | Signals completion; takes the rationale string |

**Tool-level constraint enforcement.** `assign_meal()` validates the assignment against hard constraints (no meat on Wednesday/Friday/fasting days, experiment recipes Saturday-only, pizza Friday-only) and returns a structured error if violated. The agent reads the error and recovers. This is belt-and-suspenders — the system prompt states the rules, and the tool enforces them independently.

**Loop safety.** Maximum 30 tool calls per planning run. In practice a full week requires 10–14. If the limit is hit, the partial plan is returned with a warning and the deterministic planner runs as fallback.

### 4.3 System Prompt Structure

**Non-negotiable household rules**
- Wednesday and Friday are always meatless (vegetarian or pescatarian)
- Friday is always pizza — homemade first Friday of the month, takeout all others
- Fasting days marked on the calendar (`fasting` event at 5am) are meatless
- Saturday and Sunday: cook one or the other, never both. Saturday takes priority. Sunday only cooks if Saturday was no-cook, and then must be an `easy` recipe.
- Experiment recipes are scheduled only on open Saturdays (no qualifying KRM/Family events over 2 hours)
- Calendar events prefixed `KRM` or `Family` after 3pm = no-cook day. Events prefixed `SGM` are ignored.

**Scheduling preferences**
- Target 3 cook nights on busy weeks (2+ no-cook weekdays), 4 on light weeks
- `onRotation` recipes should appear roughly monthly; prioritise most overdue
- Never repeat a recipe from the previous two weeks if alternatives exist
- Prefer variety in protein type and cuisine style across the week
- On high-calendar-density weeks, prefer easier recipes

**Conflict handling**
- Always satisfy hard dietary rules first
- If the meatless pool is exhausted, say so in the rationale — do not silently skip the constraint
- Prefer a shorter honest plan over a padded one
- Flag anything unusual rather than making a quiet judgment call

**Output expectations**
- Assign every day (recipe, no-cook, note, or leftovers — every day gets a label)
- Call `finalize_plan()` with a rationale of 3–5 sentences covering the 2–3 most interesting decisions of the week
- The rationale should read like a note from a thoughtful assistant, not a system log

### 4.4 The Rationale Field

The rationale appears above the meal schedule in both terminal and email output. It explains the agent's reasoning — not a recitation of every slot, but the interesting decisions.

**Good rationale:**
> "Busy week — KRM events Tuesday and Wednesday evening, so I kept those as no-cook and front-loaded cooking on Monday and Thursday. Saturday is open so I scheduled the merguez experiment, which hasn't appeared yet. Wednesday would normally be a cook night but given the calendar I marked it leftovers; a simple pasta would work if you want to add it back."

**Bad rationale:**
> "Monday: Hamburger Steaks. Tuesday: no cook. Wednesday: no cook. Thursday: Chickpea Rice. Friday: Takeout pizza. Saturday: Experiment. Sunday: leftovers."

The rationale should never summarise every slot. It should explain what was interesting or difficult about this particular week.

### 4.5 Architecture Decision — Pre-assembled Context (ADR-001)

**Date:** 2026-04-07  
**Status:** Adopted (replaces the multi-turn tool loop from Phase 1 initial implementation)

#### Context

Phase 1 shipped a standard agentic tool-use loop: Claude called `get_calendar_constraints`, `get_recipes`, `get_rotation_history`, `get_recent_meals`, then `assign_meal` once per dinner slot, then `finalize_plan`. Dogfooding revealed:

- 12+ API round-trips per planning run
- Input tokens compounded on every call (full conversation history re-sent each time)
- ~57k input tokens + ~1.3k output tokens for a typical week
- ~$1.00 per run on Opus at current pricing
- ~30 seconds wall-clock time

#### Decision

Collapse to a **single Claude API call** with pre-assembled context. All deterministic data-gathering (calendar, recipes, rotation history, recent meals) runs in Python before the API call. Claude receives the full context in one prompt and returns a structured plan via a single `submit_plan` tool call.

#### Measured results (first dogfood run, 2026-04-07)

- **Tokens:** 2,491 input / 488 output (2,979 total)
- **Cost:** ~$0.074 per run ($0.037 input + $0.037 output at Opus pricing)
- **vs. multi-turn loop:** ~57k input / ~1.3k output / ~$1.00 per run
- **Savings:** ~13× cheaper per run

#### Consequences

- Cost: ~$0.07–0.10 per run — roughly 13× cheaper than the tool loop
- Latency: one network round-trip instead of 12+
- Tradeoff: Claude cannot ask follow-up questions mid-plan or react to tool errors. Constraint validation moves entirely to post-processing in Python (same rules, different location).
- The `assign_meal` / `assign_no_cook` / `assign_note` tools are removed; `submit_plan` is the only tool.

#### What stays the same

- Same `WeekPlan` output shape
- Same hard constraint rules (enforced in Python after parsing the response)
- Same rationale field
- Same fallback to deterministic `Planner` on any API or parse error

---

## 5. Terminal Output Format (Phase 1)

The terminal output mirrors the email format closely so Phase 3 is a thin wrapper, not a rewrite.

```
MEAL PLAN: Mar 23 – 29  ·  4 cook nights
──────────────────────────────────────────────────

RATIONALE

Busy week — KRM events Tuesday and Wednesday evening, so I kept
those as no-cook and front-loaded cooking on Monday and Thursday.
Saturday is open so I scheduled the merguez experiment. Wednesday
would normally be a cook night; a simple pasta would work if you
want to add it back.

──────────────────────────────────────────────────
THIS WEEK

  Monday, Mar 23        Hamburger Steaks with Onion Gravy
  Tuesday, Mar 24       no cook  (KRM event after 3pm)
  Wednesday, Mar 25     no cook  (KRM event after 3pm)  [would have been meatless]
  Thursday, Mar 26      Spiced Rice with Crispy Chickpeas  [vegetarian]
  Friday, Mar 27        Takeout pizza 🍕
  Saturday, Mar 28      Homemade Merguez  [experiment]
  Sunday, Mar 29        leftovers

LUNCH THIS WEEK

  Quinoa Mediterranean grain bowl
  ⚑ Weekend prep: Cook quinoa Sunday

STACKING NOTES

  • Thursday and Saturday both use rice — consider a double batch Thursday.

GROCERY LIST

  PRODUCE
    shallots — 2  ·  garlic — 6 cloves  ·  onion — 1

  PROTEIN
    ground beef — 1 1/2 lb  ·  ground lamb — 1 lb

  DAIRY
    plain yogurt — 1/2 cup

  PANTRY
    basmati rice — 2 cups  ·  beef broth — 1 1/2 cups

LIKELY ON HAND (confirm before buying)
  • garam masala
  • beef broth

──────────────────────────────────────────────────
Respond to make changes, or type 'done' to approve:
```

The final prompt — `Respond to make changes, or type 'done' to approve` — is the Phase 1 equivalent of the reply email. Functionally identical; different transport.

---

## 6. Freeform Meal Notes

### 6.1 Two Note Types

**`out` notes** — eating out, dinner at someone's house, event with food provided. No ingredients, does not count as a cook night, does not affect grocery list.

Examples: `"Dinner at Sarah's"`, `"Date night"`, `"Kids' school event"`

**`cook` notes** — cooking something not in the recipe library. Counts as a cook night. No ingredients generated automatically; user adds ingredients manually if needed.

Examples: `"Waffles for dinner"`, `"Cleaning out the fridge"`, `"Dad's birthday cake"`

### 6.2 How Notes Enter the Plan

Notes are added via the same reply mechanism as swapping a recipe — terminal input in Phase 1, email reply in Phase 3:

- `"Mark Tuesday as dinner at Sarah's"` → `out` note
- `"Put waffles for dinner on Sunday"` → `cook` note (agent infers type from context; asks if ambiguous)

### 6.3 Display

```
Tuesday, Mar 24       Dinner at Sarah's  [out]
Sunday, Mar 29        Waffles for dinner  [cook — add ingredients manually]
```

`out` notes are excluded from the grocery list entirely. `cook` notes appear in the plan with a prompt to add ingredients manually.

---

## 7. Experiment Rating and Promotion

### 7.1 Rating Flow

After cooking a Saturday experiment, the user replies via the same channel used for plan feedback:

- `"Rate Saturday's experiment 4 stars"` — logs rating, no further action
- `"Rate the merguez 5 stars, add to onRotation"` — logs rating and promotes recipe
- `"Rate Saturday 2 stars"` — logs rating; agent deprioritises for future experiments

The reply handler parses ratings via the same LLM intent parser used for swap requests.

### 7.2 Rating Storage

Ratings are stored in `state_store` against the recipe ID with a timestamp. A recipe can be rated multiple times if cooked more than once as an experiment.

### 7.3 Promotion to onRotation

Promotion is triggered by explicit user intent — phrases like `"add to onRotation"`, `"add it to the rotation"`, `"we'll make that again"`. The agent does not auto-promote based on a star threshold; the user decides.

On promotion the agent:
1. Updates the recipe's YAML to add `onRotation` to the tags
2. Logs the promotion in `state_store`
3. Confirms: `"Done — [recipe name] added to rotation."`

Low-rated experiments (1–2 stars) are not deleted but are deprioritised — the agent avoids scheduling them again as experiments unless the pool is exhausted.

---

## 8. ATK Recipe Import

### 8.1 Trigger Condition

The ATK importer runs when:
1. There is an open experiment Saturday in the upcoming planning window, AND
2. The experiment recipe queue has fewer than 2 unplanned candidates

This is demand-driven — it runs when needed, not on a fixed schedule.

### 8.2 Candidate Selection

The agent browses ATK using the Chrome MCP and surfaces 3 experiment candidates. Candidate selection is guided by the existing recipe library — the agent reasons about what would expand the household's repertoire without being alienating, based on:

- Cuisines and flavor profiles already in rotation
- Dietary patterns (e.g. pescatarian options underrepresented)
- Complexity level relative to existing experiments
- Ingredients likely already familiar to the household

The agent does not import automatically. It surfaces candidates and waits for user selection.

### 8.3 Candidate Presentation

In Phase 1, candidates are printed to the terminal. In Phase 3, they are appended to the Thursday output or sent as a follow-up.

```
EXPERIMENT CANDIDATES FOR NEXT OPEN SATURDAY

1. Spiced Lamb Flatbreads with Yogurt Sauce
   ATK | 45 min | Serves 4
   "Expands on your existing lamb/merguez direction. Moderate complexity."

2. Miso-Glazed Salmon with Cucumber Salad
   ATK | 30 min | Serves 4
   "Fills a gap in your pescatarian options. Simple technique."

3. Shakshuka with Feta
   ATK | 35 min | Serves 4
   "Vegetarian, Mediterranean-adjacent, lower complexity than most experiments."

Type "import 2" or "import miso salmon" to add to your library.
```

### 8.4 Import Mechanics

On user selection, `atk_importer.py` uses the Chrome MCP to:
1. Navigate to the selected recipe on ATK
2. Extract recipe data (name, ingredients, instructions, times, servings)
3. Write to `recipes_yaml/` with `tags: [experiment]`
4. Confirm: `"Miso-Glazed Salmon imported and tagged as experiment."`

The imported recipe enters the normal experiment rotation immediately.

---

## 9. Walmart Cart Integration

### 9.1 v1.5 — Manual Command

```bash
python main.py cart --week 2026-03-23
```

Reads the approved grocery list for the given week from `state_store` and launches the cart-filling flow via Chrome MCP.

Requires:
- Chrome open with the Claude in Chrome extension active
- User logged into Walmart in Chrome
- Plan marked approved in `state_store`

### 9.2 Cart Filling Logic

The agent receives the grocery list and works through it item by item:

1. Search Walmart for each item
2. Apply item matching heuristics:
   - Prefer items from the user's Walmart purchase history
   - Match quantity as closely as possible to the grocery list amount
   - Prefer store brand where no history exists
   - Skip items already in the cart
3. Add to cart
4. Flag items it couldn't confidently match for user review

The agent does not check out. It fills the cart and surfaces a summary: `"Added 18 items. 2 items need your review: [beef broth — 3 options], [garam masala — couldn't find a close match]."`

### 9.3 v2 — Automatic Trigger

In v2, cart fill runs automatically after the user approves the plan. The approval (`"Looks good"` / `"done"`) triggers both plan finalisation and cart fill in sequence.

---

## 10. Reply Intent Types

The reply handler's LLM intent parser supports these types in v1.5:

| Intent | Example | Action |
|---|---|---|
| `swap_day` | "Swap Tuesday for a pasta dish" | Re-plan the slot |
| `assign_note_out` | "Mark Wednesday as dinner at Sarah's" | `assign_note(out)` |
| `assign_note_cook` | "Put waffles on Sunday" | `assign_note(cook)` |
| `remove_on_hand` | "The cream cheese is already gone" | Remove from on-hand list |
| `force_no_cook` | "Make Thursday leftovers" | `assign_no_cook()` |
| `force_cook` | "Actually cook on Wednesday" | Re-plan the slot |
| `skip_experiment` | "Skip the experiment this week" | Replace with easy/onRotation |
| `rate_experiment` | "Rate Saturday 4 stars" | Log rating |
| `promote_experiment` | "Add it to onRotation" | Promote recipe |
| `import_atk` | "Import the miso salmon" | Trigger ATK import |
| `acknowledgment` | "done" / "okay thanks" | Approve plan, end session |

---

## 11. Data Model Changes

### 11.1 MealSlot additions

```python
note_type: Optional[str]   # 'out' or 'cook' — None for recipe slots
note_text: Optional[str]   # freeform text for note slots
```

### 11.2 State store additions

```sql
-- Experiment ratings
CREATE TABLE experiment_ratings (
    recipe_id       TEXT NOT NULL,
    rated_at        TEXT NOT NULL,    -- ISO datetime
    stars           INTEGER NOT NULL, -- 1-5
    promoted        INTEGER DEFAULT 0 -- 1 if added to onRotation
);

-- Freeform note history (for leftover memory awareness)
CREATE TABLE meal_notes (
    week_key        TEXT NOT NULL,
    meal_date       TEXT NOT NULL,
    note_type       TEXT NOT NULL,    -- 'out' or 'cook'
    note_text       TEXT NOT NULL
);
```

### 11.3 Recipe YAML additions

```yaml
rating: 4                    # set when an experiment is rated
promoted_at: 2026-03-28      # set when promoted to onRotation
atk_url: https://...         # set for ATK-imported recipes
```

---

## 12. Config Changes

```yaml
# ATK integration
atk:
  enabled: true
  experiment_queue_min: 2    # replenish when below this threshold

# Walmart integration
walmart:
  enabled: false             # v1.5: false by default, enable manually
  auto_fill: false           # v2: set true to fill on plan approval

# Email — Phase 3 only; not required for Phase 1 or 2
email:
  enabled: false             # set true in Phase 3
  to_address: your@email.com
  from_address: null         # optional; defaults to authenticated account
```

---

## 13. Versioning Roadmap

| Version | Scope |
|---|---|
| **v1.0** | Deterministic pipeline. Calendar integration, email delivery, reply-triggered re-plan. Shipped. |
| **v1.5 — Phase 1** | Agentic planner (Claude + tools). Freeform meal notes. Experiment rating and promotion. ATK import. Walmart cart. All CLI-driven. |
| **v1.5 — Phase 2** | Persistence complete. `reply` loads saved plan from SQLite. No re-planning from scratch on each reply. |
| **v1.5 — Phase 3** | Email delivery added. `--email` flag on `plan` and `reply`. Existing CLI flags still work. |
| **v2.0** | Automatic Walmart cart fill on plan approval. Dedicated sending account. Full re-plan on feedback. |
| **v2.5** | Pantry state management. Preference learning from rating history. Hosted / automatic Thursday trigger. |

---

## 14. Open Questions

1. **ATK authentication.** The Chrome MCP will need to operate within an active ATK session. Is your ATK subscription logged in persistently in Chrome, or will the agent need to handle login?

2. **Walmart item memory.** For the cart filler to prefer previously-purchased items, it needs access to Walmart order history. Does the Chrome MCP have enough access to read purchase history, or does the agent rely on Walmart's own "buy again" suggestions?

3. **Note ingredients.** For `cook` notes ("waffles for dinner"), the current design adds no ingredients to the grocery list. Is there a future state where you'd want to add ingredients inline, or is manual addition always sufficient?

4. **Experiment rating timing.** If you cook the Saturday experiment and want to rate it Sunday or later, which session do you reply to — the current week's plan, or should ratings be a standalone command (`python main.py rate`)?

5. **Rationale tone calibration.** The agent's rationale will vary week to week. Is inline feedback sufficient ("the rationale was confusing"), or do you want a separate mechanism to shape what good rationale looks like over time?
