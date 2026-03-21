"""
state_store.py

SQLite-backed state store for the meal planner.

Responsibilities:
  - Track last_planned date per recipe and lunch entry
  - Store weekly plans (full JSON blob + per-slot rows)
  - Provide ingredient history for leftover detection
  - Purge plan history older than 4 months on each write

Week key convention: ISO date of the Monday that starts the calendar week
containing the plan's Friday. E.g. a plan covering Fri 2026-03-20 through
Thu 2026-03-26 is keyed to Monday 2026-03-16.

Usage:
    from state_store import StateStore
    db = StateStore("meal_planner.db")
    db.record_plan(week_key, plan_dict)
    db.get_recent_ingredients(weeks=2)
"""

import json
import sqlite3
import logging
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

HISTORY_MONTHS = 4


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipe_history (
    recipe_id       TEXT PRIMARY KEY,
    last_planned    TEXT             -- ISO date, e.g. 2026-03-20
);

CREATE TABLE IF NOT EXISTS lunch_history (
    lunch_id        TEXT PRIMARY KEY,
    last_planned    TEXT             -- ISO date
);

CREATE TABLE IF NOT EXISTS weekly_plans (
    week_key        TEXT PRIMARY KEY, -- ISO date of Monday (e.g. 2026-03-16)
    created_at      TEXT NOT NULL,    -- ISO datetime when plan was generated
    plan_json       TEXT NOT NULL,    -- full plan as JSON
    approved        INTEGER DEFAULT 0 -- 0=pending, 1=approved
);

CREATE TABLE IF NOT EXISTS planned_meals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    week_key        TEXT NOT NULL,
    meal_date       TEXT NOT NULL,    -- ISO date of this specific meal
    slot            TEXT NOT NULL,    -- 'dinner' or 'lunch'
    recipe_id       TEXT,             -- null for no-cook days
    label           TEXT NOT NULL,    -- display name or 'no cook'
    tags            TEXT,             -- JSON array of tags
    ingredients_json TEXT,            -- JSON array of {name, quantity, unit}
    FOREIGN KEY (week_key) REFERENCES weekly_plans(week_key)
);

CREATE INDEX IF NOT EXISTS idx_planned_meals_week
    ON planned_meals(week_key);

CREATE INDEX IF NOT EXISTS idx_planned_meals_date
    ON planned_meals(meal_date);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _week_key_for_date(d: date) -> str:
    """Return the ISO date string of the Monday of the week containing d."""
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat()


def _cutoff_date(months: int = HISTORY_MONTHS) -> date:
    """Return the date before which records should be purged."""
    today = date.today()
    # Approximate: 4 months = 4 * 30 days
    return today - timedelta(days=months * 30)


# ---------------------------------------------------------------------------
# StateStore
# ---------------------------------------------------------------------------

class StateStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        log.debug("StateStore opened: %s", self.db_path)

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def _transaction(self):
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _init_schema(self):
        with self._transaction():
            self._conn.executescript(SCHEMA)

    # -----------------------------------------------------------------------
    # Recipe rotation tracking
    # -----------------------------------------------------------------------

    def get_last_planned(self, recipe_id: str) -> Optional[date]:
        """Return the last date this recipe was planned, or None."""
        row = self._conn.execute(
            "SELECT last_planned FROM recipe_history WHERE recipe_id = ?",
            (recipe_id,)
        ).fetchone()
        if row and row["last_planned"]:
            return date.fromisoformat(row["last_planned"])
        return None

    def set_last_planned(self, recipe_id: str, planned_date: date):
        """Upsert the last_planned date for a recipe."""
        with self._transaction():
            self._conn.execute("""
                INSERT INTO recipe_history (recipe_id, last_planned)
                VALUES (?, ?)
                ON CONFLICT(recipe_id) DO UPDATE SET last_planned = excluded.last_planned
            """, (recipe_id, planned_date.isoformat()))

    def get_all_last_planned(self) -> dict[str, date]:
        """Return {recipe_id: last_planned_date} for all tracked recipes."""
        rows = self._conn.execute(
            "SELECT recipe_id, last_planned FROM recipe_history"
        ).fetchall()
        return {
            r["recipe_id"]: date.fromisoformat(r["last_planned"])
            for r in rows if r["last_planned"]
        }

    # -----------------------------------------------------------------------
    # Lunch rotation tracking
    # -----------------------------------------------------------------------

    def get_lunch_last_planned(self, lunch_id: str) -> Optional[date]:
        row = self._conn.execute(
            "SELECT last_planned FROM lunch_history WHERE lunch_id = ?",
            (lunch_id,)
        ).fetchone()
        if row and row["last_planned"]:
            return date.fromisoformat(row["last_planned"])
        return None

    def set_lunch_last_planned(self, lunch_id: str, planned_date: date):
        with self._transaction():
            self._conn.execute("""
                INSERT INTO lunch_history (lunch_id, last_planned)
                VALUES (?, ?)
                ON CONFLICT(lunch_id) DO UPDATE SET last_planned = excluded.last_planned
            """, (lunch_id, planned_date.isoformat()))

    def get_all_lunch_last_planned(self) -> dict[str, date]:
        rows = self._conn.execute(
            "SELECT lunch_id, last_planned FROM lunch_history"
        ).fetchall()
        return {
            r["lunch_id"]: date.fromisoformat(r["last_planned"])
            for r in rows if r["last_planned"]
        }

    # -----------------------------------------------------------------------
    # Weekly plan storage
    # -----------------------------------------------------------------------

    def record_plan(
        self,
        week_key: str,
        plan: dict,
        meals: list[dict],
        created_at: Optional[str] = None,
    ):
        """
        Persist a full weekly plan.

        Args:
            week_key:   ISO date of the Monday (e.g. '2026-03-16')
            plan:       Full plan dict (will be stored as JSON blob)
            meals:      List of meal dicts, each with keys:
                          meal_date, slot, recipe_id, label, tags, ingredients
            created_at: ISO datetime string; defaults to now
        """
        from datetime import datetime
        if created_at is None:
            created_at = datetime.now().isoformat(timespec="seconds")

        with self._transaction():
            # Upsert the plan blob
            self._conn.execute("""
                INSERT INTO weekly_plans (week_key, created_at, plan_json, approved)
                VALUES (?, ?, ?, 0)
                ON CONFLICT(week_key) DO UPDATE SET
                    created_at = excluded.created_at,
                    plan_json  = excluded.plan_json,
                    approved   = 0
            """, (week_key, created_at, json.dumps(plan)))

            # Replace meal rows for this week
            self._conn.execute(
                "DELETE FROM planned_meals WHERE week_key = ?", (week_key,)
            )
            for meal in meals:
                self._conn.execute("""
                    INSERT INTO planned_meals
                        (week_key, meal_date, slot, recipe_id, label, tags, ingredients_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    week_key,
                    meal["meal_date"],
                    meal["slot"],
                    meal.get("recipe_id"),
                    meal["label"],
                    json.dumps(meal.get("tags", [])),
                    json.dumps(meal.get("ingredients", [])),
                ))

            # Update last_planned for each recipe in this plan
            for meal in meals:
                if meal.get("recipe_id"):
                    self._conn.execute("""
                        INSERT INTO recipe_history (recipe_id, last_planned)
                        VALUES (?, ?)
                        ON CONFLICT(recipe_id) DO UPDATE SET
                            last_planned = excluded.last_planned
                        WHERE excluded.last_planned > COALESCE(last_planned, '')
                    """, (meal["recipe_id"], meal["meal_date"]))

        self._purge_old_history()

    def mark_plan_approved(self, week_key: str):
        """Mark a plan as approved by the user."""
        with self._transaction():
            self._conn.execute(
                "UPDATE weekly_plans SET approved = 1 WHERE week_key = ?",
                (week_key,)
            )

    def get_plan(self, week_key: str) -> Optional[dict]:
        """Retrieve a stored plan by week key."""
        row = self._conn.execute(
            "SELECT plan_json FROM weekly_plans WHERE week_key = ?",
            (week_key,)
        ).fetchone()
        return json.loads(row["plan_json"]) if row else None

    def get_current_week_key(self) -> str:
        """Return the week key for the current calendar week."""
        return _week_key_for_date(date.today())

    def get_next_week_key(self) -> str:
        """Return the week key for next calendar week."""
        return _week_key_for_date(date.today() + timedelta(weeks=1))

    # -----------------------------------------------------------------------
    # Leftover / ingredient history
    # -----------------------------------------------------------------------

    def get_recent_ingredients(self, weeks: int = 2) -> list[dict]:
        """
        Return all ingredient rows from the last N weeks of planned meals.

        Returns a flat list of dicts:
            {week_key, meal_date, slot, recipe_id, label, name, quantity, unit}
        """
        cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
        rows = self._conn.execute("""
            SELECT pm.week_key, pm.meal_date, pm.slot, pm.recipe_id,
                   pm.label, pm.ingredients_json
            FROM planned_meals pm
            JOIN weekly_plans wp ON pm.week_key = wp.week_key
            WHERE pm.meal_date >= ?
            ORDER BY pm.meal_date
        """, (cutoff,)).fetchall()

        result = []
        for row in rows:
            ingredients = json.loads(row["ingredients_json"] or "[]")
            for ing in ingredients:
                result.append({
                    "week_key":  row["week_key"],
                    "meal_date": row["meal_date"],
                    "slot":      row["slot"],
                    "recipe_id": row["recipe_id"],
                    "label":     row["label"],
                    "name":      ing.get("name", ""),
                    "quantity":  ing.get("quantity"),
                    "unit":      ing.get("unit"),
                })
        return result

    def get_recent_meals(self, weeks: int = 2) -> list[dict]:
        """
        Return all planned meal rows from the last N weeks.
        Used by the planner to avoid repeating recipes too soon.
        """
        cutoff = (date.today() - timedelta(weeks=weeks)).isoformat()
        rows = self._conn.execute("""
            SELECT pm.meal_date, pm.slot, pm.recipe_id, pm.label, pm.tags
            FROM planned_meals pm
            WHERE pm.meal_date >= ?
            ORDER BY pm.meal_date
        """, (cutoff,)).fetchall()
        return [dict(r) for r in rows]

    # -----------------------------------------------------------------------
    # Purge
    # -----------------------------------------------------------------------

    def _purge_old_history(self):
        """Remove plan data older than HISTORY_MONTHS months."""
        cutoff = _cutoff_date(HISTORY_MONTHS).isoformat()
        with self._transaction():
            # planned_meals rows older than cutoff
            self._conn.execute(
                "DELETE FROM planned_meals WHERE meal_date < ?", (cutoff,)
            )
            # weekly_plans with no remaining meals (or older than cutoff by key)
            self._conn.execute("""
                DELETE FROM weekly_plans
                WHERE week_key < ?
                  AND week_key NOT IN (SELECT DISTINCT week_key FROM planned_meals)
            """, (cutoff,))
        log.debug("Purged history older than %s", cutoff)

    def purge_now(self):
        """Manually trigger a history purge. Useful in tests."""
        self._purge_old_history()

    # -----------------------------------------------------------------------
    # Debug / inspection
    # -----------------------------------------------------------------------

    def summary(self) -> dict:
        """Return a summary dict for logging/debugging."""
        recipe_count = self._conn.execute(
            "SELECT COUNT(*) FROM recipe_history"
        ).fetchone()[0]
        plan_count = self._conn.execute(
            "SELECT COUNT(*) FROM weekly_plans"
        ).fetchone()[0]
        meal_count = self._conn.execute(
            "SELECT COUNT(*) FROM planned_meals"
        ).fetchone()[0]
        oldest = self._conn.execute(
            "SELECT MIN(meal_date) FROM planned_meals"
        ).fetchone()[0]
        newest = self._conn.execute(
            "SELECT MAX(meal_date) FROM planned_meals"
        ).fetchone()[0]
        return {
            "tracked_recipes": recipe_count,
            "weeks_stored":    plan_count,
            "meal_rows":       meal_count,
            "oldest_meal":     oldest,
            "newest_meal":     newest,
        }
