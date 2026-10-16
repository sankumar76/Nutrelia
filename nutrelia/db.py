"""SQLite storage: one entry per food (nutrition for a base serving) plus the day-by-day log.

MFP logs the same food at many sizes ("Flax seeds, 1 tbsp" and "Flax seeds, 2 tbsp").
Those are one food here: rows whose nutrition scales with the amount are merged, and the
log keeps how much was eaten. Rows with the same name whose nutrition does not scale
(different brands or recipes) stay separate foods, and the app asks which one you mean.
"""
from __future__ import annotations

import os
import sqlite3
from collections import Counter
from pathlib import Path

from .diary_parser import NUTRIENTS, DiaryEntry
from .units import convert, fmt_amount, parse_serving

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS foods (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    base_amount REAL NOT NULL DEFAULT 1,
    unit TEXT NOT NULL DEFAULT 'serving',
    {", ".join(f"{n} REAL" for n in NUTRIENTS)},
    times_logged INTEGER NOT NULL DEFAULT 0,
    last_logged TEXT,
    source TEXT NOT NULL DEFAULT 'mfp'
);
CREATE INDEX IF NOT EXISTS foods_name ON foods (name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS log_entries (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    meal TEXT NOT NULL,
    food_id INTEGER NOT NULL REFERENCES foods(id),
    amount REAL NOT NULL,  -- in the food's unit
    position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS log_day ON log_entries (day);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

MEAL_ORDER = ["Breakfast", "Lunch", "Snacks", "Dinner"]
TARGET_NUTRIENTS = ["calories", "carbs", "fat", "protein", "fiber"]


def db_path() -> str:
    return os.environ.get("NUTRELIA_DB", str(Path(__file__).resolve().parent.parent / "data" / "nutrelia.db"))


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def scale(food, amount: float) -> dict:
    """Nutrition for `amount` of a food (amount in the food's unit)."""
    k = amount / food["base_amount"] if food["base_amount"] else 0
    return {n: None if food[n] is None else food[n] * k for n in NUTRIENTS}


def serving_label(food) -> str:
    return f"{fmt_amount(food['base_amount'])} {food['unit']}"


def _consistent(food, nutrients: dict, k: float) -> bool:
    """Do these nutrients, multiplied by k, match the food's? Allows for MFP's whole-number rounding."""
    if nutrients.get("calories") is None or food["calories"] is None:
        return False
    for n in NUTRIENTS:
        a, b = food[n], nutrients.get(n)
        if a is None or b is None:
            continue
        b *= k
        if abs(a - b) > 0.5 + 0.5 * k + 0.03 * max(abs(a), abs(b)):
            return False
    return True


def find_food(conn: sqlite3.Connection, name: str, serving: str, nutrients: dict) -> tuple[int, float] | None:
    """An existing food this row is a resize of, and the row's amount in that food's unit."""
    s = parse_serving(serving)
    for food in conn.execute("SELECT * FROM foods WHERE name = ? COLLATE NOCASE ORDER BY times_logged DESC, id", (name,)):
        amount = convert(s.amount, s.unit, food["unit"])
        if not amount:
            continue
        if _consistent(food, nutrients, food["base_amount"] / amount):
            return food["id"], amount
    return None


def upsert_food(conn: sqlite3.Connection, name: str, serving: str, nutrients: dict, source: str = "mfp") -> tuple[int, float]:
    """Return (food id, amount in that food's unit), creating the food if no existing one matches."""
    found = find_food(conn, name, serving, nutrients)
    if found:
        return found
    s = parse_serving(serving)
    cur = conn.execute(
        f"INSERT INTO foods (name, base_amount, unit, {', '.join(NUTRIENTS)}, source) "
        f"VALUES (?, ?, ?, {', '.join('?' * len(NUTRIENTS))}, ?)",
        [name, s.amount, s.unit] + [nutrients.get(n) for n in NUTRIENTS] + [source],
    )
    return cur.lastrowid, s.amount


def import_entries(conn: sqlite3.Connection, entries: list[DiaryEntry]) -> dict:
    """Load parsed diary rows. Re-importing the same days replaces them, so it is safe to run twice."""
    days = sorted({e.date for e in entries})
    before = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    # The most-logged size of each food becomes its base serving, so add those first.
    freq = Counter((e.name, e.serving, tuple(e.nutrients().values())) for e in entries)
    with conn:
        conn.executemany("DELETE FROM log_entries WHERE day = ?", [(d,) for d in days])
        resolved = {}
        for (name, serving, values), _n in freq.most_common():
            resolved[(name, serving, values)] = upsert_food(conn, name, serving, dict(zip(NUTRIENTS, values)))
        for pos, e in enumerate(entries):
            fid, amount = resolved[(e.name, e.serving, tuple(e.nutrients().values()))]
            conn.execute(
                "INSERT INTO log_entries (day, meal, food_id, amount, position) VALUES (?, ?, ?, ?, ?)",
                (e.date, e.meal, fid, amount, pos),
            )
        refresh_usage(conn)
    after = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    return {"entries": len(entries), "days": len(days), "new_foods": after - before, "total_foods": after}


def refresh_usage(conn: sqlite3.Connection) -> None:
    conn.execute(
        """UPDATE foods SET
             times_logged = (SELECT COUNT(*) FROM log_entries l WHERE l.food_id = foods.id),
             last_logged = (SELECT MAX(day) FROM log_entries l WHERE l.food_id = foods.id)"""
    )


def day_entries(conn: sqlite3.Connection, day: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT f.*, l.meal, l.amount, l.id AS entry_id, f.id AS food_id
           FROM log_entries l JOIN foods f ON f.id = l.food_id
           WHERE l.day = ? ORDER BY l.position, l.id""",
        (day,),
    ).fetchall()


def add_entry(conn: sqlite3.Connection, day: str, meal: str, food_id: int, amount: float) -> None:
    pos = conn.execute("SELECT IFNULL(MAX(position), 0) + 1 FROM log_entries").fetchone()[0]
    with conn:
        conn.execute(
            "INSERT INTO log_entries (day, meal, food_id, amount, position) VALUES (?, ?, ?, ?, ?)",
            (day, meal, food_id, amount, pos),
        )
        conn.execute(
            "UPDATE foods SET times_logged = times_logged + 1, last_logged = MAX(IFNULL(last_logged, ''), ?) WHERE id = ?",
            (day, food_id),
        )


def get_targets(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'target_%'").fetchall()
    found = {r["key"][len("target_"):]: float(r["value"]) for r in rows if r["value"]}
    return {n: found.get(n) for n in TARGET_NUTRIENTS}


def set_targets(conn: sqlite3.Connection, targets: dict) -> None:
    with conn:
        for n in TARGET_NUTRIENTS:
            value = targets.get(n)
            if value is None:
                conn.execute("DELETE FROM settings WHERE key = ?", (f"target_{n}",))
            else:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (f"target_{n}", str(value)))


def update_food(conn: sqlite3.Connection, food_id: int, name: str, base_amount: float, unit: str, nutrients: dict) -> None:
    """Edit a food. Past log entries keep their amounts, so their totals follow the new nutrition."""
    with conn:
        conn.execute(
            f"UPDATE foods SET name = ?, base_amount = ?, unit = ?, {', '.join(f'{n} = ?' for n in NUTRIENTS)} WHERE id = ?",
            [name, base_amount, unit] + [nutrients.get(n) for n in NUTRIENTS] + [food_id],
        )


def delete_food(conn: sqlite3.Connection, food_id: int) -> bool:
    """Delete a food that has never been logged. Returns False if it is in the log."""
    if conn.execute("SELECT 1 FROM log_entries WHERE food_id = ? LIMIT 1", (food_id,)).fetchone():
        return False
    with conn:
        conn.execute("DELETE FROM foods WHERE id = ?", (food_id,))
    return True


def add_foods_only(conn: sqlite3.Connection, entries: list[DiaryEntry]) -> dict:
    """Add the foods from diary rows to the library without touching the daily log."""
    before = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    freq = Counter((e.name, e.serving, tuple(e.nutrients().values())) for e in entries)
    with conn:
        for (name, serving, values), _n in freq.most_common():
            upsert_food(conn, name, serving, dict(zip(NUTRIENTS, values)))
    after = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    return {"rows": len(entries), "new_foods": after - before, "total_foods": after}
