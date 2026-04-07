"""
main.py

Entry point for the meal planning agent.

Commands:

  python main.py plan
      Generate and send the weekly meal plan for the next calendar week.
      Reads Google Calendar, runs the planner, stacker, and grocery builder,
      sends the Thursday email, and saves the plan to the database.

  python main.py reply --week 2026-03-23
      Poll Gmail for a reply to the given week's plan and trigger a re-plan
      if feedback is found. Runs hourly until a reply is received or end of
      Thursday. Omit --week to use the current week.

  python main.py ingest --input ./recipes_raw
      Convert Paprika .txt exports to YAML recipe files.

  python main.py status
      Print a summary of the current database state (tracked recipes,
      weeks stored, upcoming week key).

  python main.py preview
      Run the full plan pipeline and print the email to stdout without
      sending anything or writing to the database. Useful for testing
      before first real run.

Global flags:
  --config PATH     Path to config.yaml (default: config.yaml)
  --dry-run         Print output without sending email or writing state
  --verbose         Enable debug logging
"""

import argparse
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet noisy third-party loggers
    for noisy in ("googleapiclient", "google.auth", "urllib3", "httplib2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

log = logging.getLogger("main")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path: str) -> dict:
    path = Path(config_path)
    if not path.exists():
        log.error(
            "Config file not found: %s\n"
            "Copy config.yaml.template to config.yaml and fill in your values.",
            config_path,
        )
        sys.exit(1)
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


def flatten_config(cfg: dict) -> dict:
    """
    Merge nested config sections into a single flat dict for passing
    to module constructors.
    """
    flat = {}
    # Top-level keys first
    for k, v in cfg.items():
        if not isinstance(v, dict):
            flat[k] = v
    # Section keys override (allow email.to_address etc.)
    for section in ("email", "calendar", "planner", "anthropic", "reply_handler"):
        flat.update(cfg.get(section, {}))
    # Ensure anthropic model is accessible as flat["model"]
    if "model" not in flat and "anthropic_model" in flat:
        flat["model"] = flat["anthropic_model"]
    return flat


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_plan(args, cfg: dict, flat: dict):
    """Generate and display the weekly meal plan. Enters interactive reply loop."""
    from calendar_reader import CalendarReader
    from stacker import Stacker
    from grocery_builder import GroceryBuilder
    from state_store import StateStore
    from output_formatter import print_plan
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


def cmd_rate(args, cfg: dict, flat: dict):
    """Record a star rating for an experiment recipe."""
    import sys
    from state_store import StateStore
    from planner import load_recipes

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
        from reply_handler import _promote_recipe_yaml
        _promote_recipe_yaml(recipe["id"], store)
        print(f"Rated '{recipe['name']}' {stars} stars and promoted to onRotation.")
    else:
        print(f"Rated '{recipe['name']}' {stars} stars.")

    store.close()


def cmd_reply(args, cfg: dict, flat: dict):
    """Poll for replies and trigger re-plans."""
    from reply_handler import ReplyHandler
    from calendar_reader import CalendarReader

    # Determine week key
    if args.week:
        week_key = args.week
    else:
        reader = CalendarReader(
            config=flat,
            timezone=flat.get("timezone", "America/New_York")
        )
        monday = reader.next_planning_monday()
        week_key = monday.isoformat()

    log.info("Polling for replies to week: %s", week_key)

    handler = ReplyHandler(config=flat, dry_run=args.dry_run)

    if args.once:
        # Single check — useful for testing
        from email_sender import EmailSender, _fmt_date_range
        monday = date.fromisoformat(week_key)
        week_label = _fmt_date_range(monday)
        sender = EmailSender(config=flat, dry_run=args.dry_run)
        reply = sender.get_latest_reply(week_label)
        if reply:
            log.info("Reply found: %s", reply.get("from", ""))
            handler._handle_reply(reply, week_key, sender)
        else:
            log.info("No reply found for week %s.", week_key)
    else:
        result = handler.poll_and_handle(week_key)
        if result:
            log.info("Reply handled successfully.")
        else:
            log.info("Polling ended without receiving a reply.")


def cmd_ingest(args, cfg: dict, flat: dict):
    """Convert Paprika exports to YAML recipe files.

    Accepts either:
      - A .paprikarecipes bulk export file (recommended)
      - A directory of .txt single-recipe exports
    """
    input_path = Path(args.input)
    output_dir = Path(flat.get("recipe_dir", "recipes_yaml"))
    output_dir.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        log.error("Input path does not exist: %s", input_path)
        sys.exit(1)

    # Route based on file type
    if input_path.is_file() and input_path.suffix.lower() == ".paprikarecipes":
        # Bulk export path — cleaner, recommended
        from paprika_import import iter_recipes, write_recipe
        if args.list:
            print(f"\nRecipes in {input_path.name}:\n")
            count = 0
            for _, recipe in iter_recipes(input_path):
                tags_str = ", ".join(recipe["tags"]) if recipe["tags"] else "(no tags mapped)"
                print(f"  {recipe['name']:<45}  [{tags_str}]")
                count += 1
            print(f"\n{count} recipe(s) total.\n")
            return
        written = skipped = failed = 0
        for _, recipe in iter_recipes(input_path):
            try:
                result = write_recipe(recipe, output_dir, force=args.force)
                if result: written += 1
                else: skipped += 1
            except Exception as e:
                log.error("Failed to write %s: %s", recipe.get("id", "?"), e)
                failed += 1
        log.info("Done. %d written, %d skipped, %d failed.", written, skipped, failed)
        if failed:
            sys.exit(1)

    else:
        # .txt single-file path
        from recipe_loader import process_file
        if input_path.is_file():
            files = [input_path]
        elif input_path.is_dir():
            files = sorted(input_path.glob("*.txt"))
            if not files:
                log.error("No .txt files found in %s", input_path)
                sys.exit(1)
        else:
            log.error("Unrecognised input: %s", input_path)
            sys.exit(1)
        log.info("Ingesting %d recipe file(s) → %s", len(files), output_dir)
        results = [process_file(f, output_dir, force=args.force) for f in files]
        failed = results.count(False)
        log.info("Done. %d/%d recipes converted.", len(results) - failed, len(results))
        if failed:
            sys.exit(1)


def cmd_status(args, cfg: dict, flat: dict):
    """Print a summary of the current database state."""
    from state_store import StateStore
    from calendar_reader import CalendarReader

    store = StateStore(flat.get("db_path", "meal_planner.db"))
    summary = store.summary()
    store.close()

    reader = CalendarReader(
        config=flat,
        timezone=flat.get("timezone", "America/New_York")
    )
    next_monday = reader.next_planning_monday()

    print("\nMeal Planner Status")
    print("─" * 40)
    print(f"  Tracked recipes:   {summary['tracked_recipes']}")
    print(f"  Weeks in history:  {summary['weeks_stored']}")
    print(f"  Meal rows stored:  {summary['meal_rows']}")
    if summary['oldest_meal']:
        print(f"  History range:     {summary['oldest_meal']} → {summary['newest_meal']}")
    print(f"  Next plan week:    {next_monday} – {next_monday + timedelta(days=6)}")
    print(f"  Database:          {flat.get('db_path', 'meal_planner.db')}")
    print(f"  Recipe dir:        {flat.get('recipe_dir', 'recipes_yaml')}")
    print()


def cmd_preview(args, cfg: dict, flat: dict):
    """Run the full pipeline and print the email without sending."""
    args.dry_run = True
    cmd_plan(args, cfg, flat)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meal-planner",
        description="Weekly meal planning agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config.yaml (default: config.yaml)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print output without sending email or writing state"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging"
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # plan
    p_plan = sub.add_parser("plan", help="Generate and send the weekly meal plan")
    p_plan.add_argument(
        "--legacy", action="store_true",
        help="Use the deterministic planner instead of the agentic planner"
    )
    p_plan.add_argument(
        "--email", action="store_true",
        help="[Phase 3] Send plan by email instead of printing to terminal"
    )
    p_plan.set_defaults(func=cmd_plan)

    # reply
    p_reply = sub.add_parser("reply", help="Poll for replies and trigger re-plans")
    p_reply.add_argument(
        "--week", default=None,
        help="Week key as ISO Monday date, e.g. 2026-03-23 (default: next calendar week)"
    )
    p_reply.add_argument(
        "--once", action="store_true",
        help="Check for a reply once and exit instead of polling"
    )
    p_reply.set_defaults(func=cmd_reply)

    # ingest
    p_ingest = sub.add_parser("ingest", help="Convert Paprika .txt exports to YAML")
    p_ingest.add_argument(
        "--input", "-i", required=True,
        help="Path to a .txt file or directory of .txt files"
    )
    p_ingest.add_argument(
        "--force", "-f", action="store_true",
        help="Overwrite existing YAML files"
    )
    p_ingest.add_argument(
        "--list", action="store_true",
        help="Preview recipe names and mapped tags without writing files (.paprikarecipes only)"
    )
    p_ingest.set_defaults(func=cmd_ingest)

    # status
    p_status = sub.add_parser("status", help="Print database and config summary")
    p_status.set_defaults(func=cmd_status)

    # preview
    p_preview = sub.add_parser(
        "preview",
        help="Run the full pipeline and print the email without sending"
    )
    p_preview.set_defaults(func=cmd_preview)

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

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    setup_logging(verbose=args.verbose)

    cfg = load_config(args.config)
    flat = flatten_config(cfg)

    # Allow ANTHROPIC_API_KEY env var to override config
    if "ANTHROPIC_API_KEY" not in os.environ:
        api_key = flat.get("api_key") or cfg.get("anthropic", {}).get("api_key")
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key

    try:
        args.func(args, cfg, flat)
    except KeyboardInterrupt:
        log.info("Interrupted.")
        sys.exit(0)
    except Exception as e:
        log.error("Unexpected error: %s", e, exc_info=args.verbose)
        sys.exit(1)


if __name__ == "__main__":
    main()
