# Sous Chef — Decision Log

Track architectural decisions, pivots, and scope changes as the project evolves.

---

## How to use this file

Add an entry whenever a meaningful decision is made — something you'd want to explain to a future collaborator, or something that would look surprising without context. Each entry should state what was decided, why, and what was given up. Lightweight is fine; not every entry needs full ADR treatment.

---

## Project evolution

### Initial concept (v1.0)

A deterministic weekly meal planner. Python script reads Google Calendar, applies hard rules (no-cook nights, meatless Wednesday/Friday, pizza Friday, Saturday experiment), selects recipes from a YAML library, and builds a grocery list. One LLM call: parsing free-text replies from the user. Everything else is rule-based.

- Recipe selection: deterministic (tag + recency filter)
- Conflict resolution: hard-coded rule priority
- Reply handling: Claude parses the user's approval/change message, returns a structured intent
- No persistent state beyond the database

**Why:** Simple to reason about. Correct output for the common case. Easy to test.

**What it couldn't do well:** It couldn't weigh tradeoffs or explain its own reasoning. "Why did you pick that recipe?" had no answer. The week felt mechanical.

---

### v1.5 — Agentic planner (current)

Replace the recipe-selection algorithm with Claude as the orchestrator. Claude receives the full week context (calendar constraints, recipe library, rotation history, recent meals) and assigns dinners with a rationale. Hard rules are still enforced in Python post-processing — Claude proposes, Python validates.

**What changed:**
- `planner.py` → `agentic_planner.py`: single Claude API call with pre-assembled context
- `output_formatter.py` added: terminal output of plan + rationale
- `reply_handler.py`: Claude now interprets and acts on free-text replies
- Deterministic planner kept as `--legacy` fallback

**What stayed the same:** Grocery building, stacking analysis, calendar reading, output structure.

**Build sequence:** CLI-first (Phase 1) → SQLite persistence (Phase 2) → email delivery (Phase 3).  
Phase 3 wraps the CLI output in email; it does not change the plan/review/revise logic.

---

## Architecture decisions

---

### ADR-001 — Pre-assembled context (single API call)

**Date:** 2026-04-07  
**Status:** Adopted

#### Context

The initial v1.5 implementation used a standard agentic tool-use loop: Claude called `get_calendar_constraints`, `get_recipes`, `get_rotation_history`, `get_recent_meals`, then `assign_meal` once per dinner slot, then `finalize_plan`. Dogfooding immediately revealed:

- 12+ API round-trips per planning run
- ~57k input tokens + ~1.3k output tokens per run (history resent on each call)
- ~$1.00/run at Opus pricing
- ~30 seconds wall-clock time

#### Decision

Collapse to a **single Claude API call**. All data gathering (calendar, recipes, rotation history, recent meals) runs in Python before the API call. Claude receives everything in one context message and returns a structured plan via a single `submit_plan` tool call.

The `assign_meal`, `assign_no_cook`, `assign_note`, and `get_*` tools are removed. `submit_plan` is the only tool.

#### Measured results (first dogfood run, 2026-04-07)

| Metric | Multi-turn loop | Single call |
|--------|-----------------|-------------|
| Input tokens | ~57,000 | 2,491 |
| Output tokens | ~1,300 | 488 |
| Cost (Opus) | ~$1.00 | ~$0.074 |
| Savings | — | ~13× cheaper |

#### Tradeoffs

**Gained:** 13× cost reduction, ~1 network round-trip instead of 12+, simpler code path.

**Lost:** Claude cannot ask clarifying questions mid-plan or react to tool call errors. Any constraint validation that would have surfaced mid-loop must now happen in Python post-processing. This is the same rules in a different location — no functional regression, but errors surface later.

---

---

### 2026-05-30 — Experiment recipes must not be vegetarian/vegan

**Status:** Adopted

Experiment slots (open Saturdays) are the household's opportunity to try genuinely new meat-based cooking. Vegetarian/vegan experiments were appearing as candidates because no rule excluded them. Pescatarian experiments remain allowed.

**Change:** Added to system prompt and post-processing constraint check in `agentic_planner.py`. A warning is logged if the constraint is violated (no hard override — if the experiment pool is ever exhausted, you still get a meal).

---

### 2026-05-30 — Recipe seasons

**Status:** Adopted

Some recipes are only appropriate in certain seasons (e.g. a heavy stew in summer, a cold salad in winter). Rather than instructing Claude to reason about seasons with all recipes visible, out-of-season recipes are simply excluded from the AVAILABLE RECIPES context message.

**Change:** Optional `season` field added to recipe YAML (`spring` | `summer` | `fall` | `winter`). Recipes with no season are always available. Filtering happens in `_build_context_message` in `agentic_planner.py`.

---

### 2026-05-30 — Last-week review before planning

**Status:** Adopted

The planner uses `last_planned` dates from `recipe_history` to avoid repeating recipes too soon. If the user didn't actually cook something that was planned, that record is stale and will incorrectly suppress the recipe. A brief review step before each new plan gives the user a chance to correct the record.

**Change:** `cmd_plan` in `main.py` now shows the previous week's dinners and asks for corrections before planning begins. Corrections are parsed with a small Claude call. `state_store.unplan_recipe()` reverts `last_planned` for any recipe the user confirms wasn't cooked. Skipped in `--dry-run` mode.

*Add new decisions below this line.*
