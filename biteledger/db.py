"""SQLite storage: a deduplicated food library plus the day-by-day log."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .diary_parser import NUTRIENTS, DiaryEntry

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS foods (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    serving TEXT NOT NULL DEFAULT '',
    {", ".join(f"{n} REAL" for n in NUTRIENTS)},
    times_logged INTEGER NOT NULL DEFAULT 0,
    last_logged TEXT,
    source TEXT NOT NULL DEFAULT 'mfp'
);
CREATE UNIQUE INDEX IF NOT EXISTS foods_identity ON foods (
    name, serving, {", ".join(f"IFNULL({n}, -1)" for n in NUTRIENTS)}
);
CREATE TABLE IF NOT EXISTS log_entries (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    meal TEXT NOT NULL,
    food_id INTEGER NOT NULL REFERENCES foods(id),
    quantity REAL NOT NULL DEFAULT 1,
    position INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS log_day ON log_entries (day);
"""

MEAL_ORDER = ["Breakfast", "Lunch", "Dinner", "Snacks"]


def db_path() -> str:
    return os.environ.get("BITELEDGER_DB", str(Path(__file__).resolve().parent.parent / "data" / "biteledger.db"))


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def upsert_food(conn: sqlite3.Connection, name: str, serving: str, nutrients: dict, source: str = "mfp") -> int:
    """Return the id of the food with exactly this name, serving and nutrition, creating it if new."""
    where = " AND ".join(["name = ?", "serving = ?"] + [f"IFNULL({n}, -1) = IFNULL(?, -1)" for n in NUTRIENTS])
    params = [name, serving] + [nutrients.get(n) for n in NUTRIENTS]
    row = conn.execute(f"SELECT id FROM foods WHERE {where}", params).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        f"INSERT INTO foods (name, serving, {', '.join(NUTRIENTS)}, source) VALUES (?, ?, {', '.join('?' * len(NUTRIENTS))}, ?)",
        params + [source],
    )
    return cur.lastrowid


def import_entries(conn: sqlite3.Connection, entries: list[DiaryEntry]) -> dict:
    """Load parsed diary rows. Re-importing the same days replaces them, so it is safe to run twice."""
    days = sorted({e.date for e in entries})
    before = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    with conn:
        conn.executemany("DELETE FROM log_entries WHERE day = ?", [(d,) for d in days])
        for pos, e in enumerate(entries):
            fid = upsert_food(conn, e.name, e.serving, e.nutrients())
            conn.execute(
                "INSERT INTO log_entries (day, meal, food_id, quantity, position) VALUES (?, ?, ?, 1, ?)",
                (e.date, e.meal, fid, pos),
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
        """SELECT l.id, l.meal, l.quantity, f.* , l.id AS entry_id, f.id AS food_id
           FROM log_entries l JOIN foods f ON f.id = l.food_id
           WHERE l.day = ? ORDER BY l.position, l.id""",
        (day,),
    ).fetchall()


def add_entry(conn: sqlite3.Connection, day: str, meal: str, food_id: int, quantity: float = 1) -> None:
    pos = conn.execute("SELECT IFNULL(MAX(position), 0) + 1 FROM log_entries").fetchone()[0]
    with conn:
        conn.execute(
            "INSERT INTO log_entries (day, meal, food_id, quantity, position) VALUES (?, ?, ?, ?, ?)",
            (day, meal, food_id, quantity, pos),
        )
        conn.execute(
            "UPDATE foods SET times_logged = times_logged + 1, last_logged = MAX(IFNULL(last_logged, ''), ?) WHERE id = ?",
            (day, food_id),
        )
