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
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS shares (
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    viewer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (owner_id, viewer_id)
);
CREATE TABLE IF NOT EXISTS user_foods (  -- each person's own food list
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    food_id INTEGER NOT NULL REFERENCES foods(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, food_id)
);
CREATE TABLE IF NOT EXISTS user_targets (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    nutrient TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (user_id, nutrient)
);
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
    _migrate(conn)
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring databases from older versions up to date."""
    with conn:
        if "user_id" not in _columns(conn, "log_entries"):
            # single-user databases: entries are given to the first account once it exists
            conn.execute("ALTER TABLE log_entries ADD COLUMN user_id INTEGER REFERENCES users(id)")
            conn.execute("CREATE INDEX IF NOT EXISTS log_user_day ON log_entries (user_id, day)")
        if "created_by" not in _columns(conn, "foods"):
            conn.execute("ALTER TABLE foods ADD COLUMN created_by INTEGER REFERENCES users(id)")
        # foods typed in before accounts existed belong to the first admin
        conn.execute("""UPDATE foods SET created_by = (SELECT MIN(id) FROM users WHERE is_admin = 1)
                        WHERE source = 'manual' AND created_by IS NULL""")
        if "is_public" not in _columns(conn, "foods"):
            # the public list: the original MFP foods. Foods people added become theirs alone.
            conn.execute("ALTER TABLE foods ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE foods ADD COLUMN source_ref TEXT")  # id in the source database
            conn.execute("UPDATE foods SET is_public = 1 WHERE created_by IS NULL AND source = 'mfp'")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS foods_source_ref ON foods (source, source_ref) "
                     "WHERE source_ref IS NOT NULL")
        if not conn.execute("SELECT 1 FROM settings WHERE key = 'my_foods_seeded'").fetchone() \
                and conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            # before personal lists existed: everyone starts with what they logged or added
            _seed_my_foods(conn)
            conn.execute("INSERT INTO settings (key, value) VALUES ('my_foods_seeded', '1')")


def _seed_my_foods(conn: sqlite3.Connection) -> None:
    conn.execute("""INSERT OR IGNORE INTO user_foods (user_id, food_id)
                    SELECT DISTINCT user_id, food_id FROM log_entries WHERE user_id IS NOT NULL""")
    conn.execute("""INSERT OR IGNORE INTO user_foods (user_id, food_id)
                    SELECT created_by, id FROM foods WHERE created_by IS NOT NULL""")


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


def find_food(conn: sqlite3.Connection, name: str, serving: str, nutrients: dict,
              created_by: int | None = None) -> tuple[int, float] | None:
    """An existing food this row is a resize of, and the row's amount in that food's unit.
    Only public foods and the person's own are considered, so nobody ends up sharing a private food."""
    s = parse_serving(serving)
    for food in conn.execute(
            """SELECT * FROM foods WHERE name = ? COLLATE NOCASE AND (is_public = 1 OR created_by IS ?)
               ORDER BY is_public DESC, times_logged DESC, id""", (name, created_by)):
        amount = convert(s.amount, s.unit, food["unit"])
        if not amount:
            continue
        if _consistent(food, nutrients, food["base_amount"] / amount):
            return food["id"], amount
    return None


def upsert_food(conn: sqlite3.Connection, name: str, serving: str, nutrients: dict, source: str = "mfp",
                created_by: int | None = None, public: bool | None = None) -> tuple[int, float]:
    """Return (food id, amount in that food's unit), creating the food if no existing one matches.
    New foods are public when nobody in particular added them (MFP imports) unless `public` says otherwise."""
    found = find_food(conn, name, serving, nutrients, created_by)
    if found:
        if created_by is not None:
            add_to_my_foods(conn, created_by, found[0])
        return found
    s = parse_serving(serving)
    cur = conn.execute(
        f"INSERT INTO foods (name, base_amount, unit, {', '.join(NUTRIENTS)}, source, created_by, is_public) "
        f"VALUES (?, ?, ?, {', '.join('?' * len(NUTRIENTS))}, ?, ?, ?)",
        [name, s.amount, s.unit] + [nutrients.get(n) for n in NUTRIENTS]
        + [source, created_by, int(created_by is None if public is None else public)],
    )
    if created_by is not None:
        add_to_my_foods(conn, created_by, cur.lastrowid)
    return cur.lastrowid, s.amount


def import_entries(conn: sqlite3.Connection, entries: list[DiaryEntry], user_id: int | None = None) -> dict:
    """Load parsed diary rows into a user's log. Re-importing the same days replaces them."""
    days = sorted({e.date for e in entries})
    before = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    # The most-logged size of each food becomes its base serving, so add those first.
    freq = Counter((e.name, e.serving, tuple(e.nutrients().values())) for e in entries)
    with conn:
        conn.executemany("DELETE FROM log_entries WHERE day = ? AND user_id IS ?", [(d, user_id) for d in days])
        resolved = {}
        for (name, serving, values), _n in freq.most_common():
            resolved[(name, serving, values)] = upsert_food(conn, name, serving, dict(zip(NUTRIENTS, values)))
        for pos, e in enumerate(entries):
            fid, amount = resolved[(e.name, e.serving, tuple(e.nutrients().values()))]
            conn.execute(
                "INSERT INTO log_entries (user_id, day, meal, food_id, amount, position) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, e.date, e.meal, fid, amount, pos),
            )
        if user_id is not None:
            for fid, _amount in resolved.values():
                add_to_my_foods(conn, user_id, fid)
        refresh_usage(conn)
    after = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    return {"entries": len(entries), "days": len(days), "new_foods": after - before, "total_foods": after}


def refresh_usage(conn: sqlite3.Connection) -> None:
    conn.execute(
        """UPDATE foods SET
             times_logged = (SELECT COUNT(*) FROM log_entries l WHERE l.food_id = foods.id),
             last_logged = (SELECT MAX(day) FROM log_entries l WHERE l.food_id = foods.id)"""
    )


def day_entries(conn: sqlite3.Connection, user_id: int, day: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT f.*, l.meal, l.amount, l.id AS entry_id, f.id AS food_id
           FROM log_entries l JOIN foods f ON f.id = l.food_id
           WHERE l.user_id = ? AND l.day = ? ORDER BY l.position, l.id""",
        (user_id, day),
    ).fetchall()


def add_entry(conn: sqlite3.Connection, user_id: int, day: str, meal: str, food_id: int, amount: float) -> None:
    pos = conn.execute("SELECT IFNULL(MAX(position), 0) + 1 FROM log_entries").fetchone()[0]
    with conn:
        conn.execute(
            "INSERT INTO log_entries (user_id, day, meal, food_id, amount, position) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, day, meal, food_id, amount, pos),
        )
        conn.execute(
            "UPDATE foods SET times_logged = times_logged + 1, last_logged = MAX(IFNULL(last_logged, ''), ?) WHERE id = ?",
            (day, food_id),
        )
        add_to_my_foods(conn, user_id, food_id)


# --- each person's own food list ---------------------------------------------

def add_to_my_foods(conn: sqlite3.Connection, user_id: int, food_id: int) -> None:
    conn.execute("INSERT OR IGNORE INTO user_foods (user_id, food_id) VALUES (?, ?)", (user_id, food_id))


def remove_from_my_foods(conn: sqlite3.Connection, user_id: int, food_id: int) -> None:
    """Take a food off someone's list. Their log keeps it; it just stops coming up first."""
    with conn:
        conn.execute("DELETE FROM user_foods WHERE user_id = ? AND food_id = ?", (user_id, food_id))


def my_food_ids(conn: sqlite3.Connection, user_id: int) -> set[int]:
    return {r[0] for r in conn.execute("SELECT food_id FROM user_foods WHERE user_id = ?", (user_id,))}


def get_targets(conn: sqlite3.Connection, user_id: int) -> dict:
    rows = conn.execute("SELECT nutrient, value FROM user_targets WHERE user_id = ?", (user_id,)).fetchall()
    found = {r["nutrient"]: r["value"] for r in rows}
    return {n: found.get(n) for n in TARGET_NUTRIENTS}


def set_targets(conn: sqlite3.Connection, user_id: int, targets: dict) -> None:
    with conn:
        for n in TARGET_NUTRIENTS:
            value = targets.get(n)
            if value is None:
                conn.execute("DELETE FROM user_targets WHERE user_id = ? AND nutrient = ?", (user_id, n))
            else:
                conn.execute("INSERT OR REPLACE INTO user_targets (user_id, nutrient, value) VALUES (?, ?, ?)",
                             (user_id, n, value))


# --- users -----------------------------------------------------------------

def create_user(conn: sqlite3.Connection, username: str, name: str, password: str, is_admin: bool = False) -> int:
    from werkzeug.security import generate_password_hash
    with conn:
        cur = conn.execute(
            "INSERT INTO users (username, name, password_hash, is_admin) VALUES (?, ?, ?, ?)",
            (username.strip(), name.strip() or username.strip(), generate_password_hash(password), int(is_admin)),
        )
        if is_admin and conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1:
            _adopt_single_user_data(conn, cur.lastrowid)
    return cur.lastrowid


def _adopt_single_user_data(conn: sqlite3.Connection, user_id: int) -> None:
    """The first account takes over the log and targets kept before Nutrelia had accounts."""
    conn.execute("UPDATE log_entries SET user_id = ? WHERE user_id IS NULL", (user_id,))
    conn.execute("INSERT OR IGNORE INTO user_foods (user_id, food_id) SELECT DISTINCT ?, food_id FROM log_entries "
                 "WHERE user_id = ?", (user_id, user_id))
    for r in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'target_%'").fetchall():
        if r["value"]:
            conn.execute("INSERT OR REPLACE INTO user_targets (user_id, nutrient, value) VALUES (?, ?, ?)",
                         (user_id, r["key"][len("target_"):], float(r["value"])))
    conn.execute("DELETE FROM settings WHERE key LIKE 'target_%'")


def check_login(conn: sqlite3.Connection, username: str, password: str):
    from werkzeug.security import check_password_hash
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    from werkzeug.security import generate_password_hash
    with conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user_id))


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    """Remove an account with its log and targets. Foods they added stay in the shared library."""
    with conn:
        conn.execute("DELETE FROM log_entries WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_targets WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM user_foods WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM shares WHERE owner_id = ? OR viewer_id = ?", (user_id, user_id))
        conn.execute("UPDATE foods SET created_by = NULL WHERE created_by = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        refresh_usage(conn)


def set_public(conn: sqlite3.Connection, food_id: int, public: bool) -> None:
    with conn:
        conn.execute("UPDATE foods SET is_public = ? WHERE id = ?", (int(public), food_id))


def save_public_food(conn: sqlite3.Connection, source: str, ref: str, name: str, base_amount: float, unit: str,
                     nutrients: dict) -> bool:
    """Add or refresh a food from a public database (INDB, USDA, Open Food Facts). True if it was new."""
    values = [name, base_amount, unit] + [nutrients.get(n) for n in NUTRIENTS]
    cols = ["name", "base_amount", "unit"] + list(NUTRIENTS)
    cur = conn.execute(f"UPDATE foods SET {', '.join(c + ' = ?' for c in cols)} WHERE source = ? AND source_ref = ?",
                       values + [source, ref])
    if cur.rowcount:
        return False
    conn.execute(f"INSERT INTO foods ({', '.join(cols)}, source, source_ref, is_public) "
                 f"VALUES ({', '.join('?' * len(cols))}, ?, ?, 1)", values + [source, ref])
    return True


def visible_foods_sql(alias: str = "f") -> str:
    """Foods a person (bound as :uid) can see: the public list, foods they added, and their own list."""
    return (f"({alias}.is_public = 1 OR {alias}.created_by = :uid OR "
            f"EXISTS (SELECT 1 FROM user_foods mf WHERE mf.user_id = :uid AND mf.food_id = {alias}.id))")


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
        conn.execute("DELETE FROM user_foods WHERE food_id = ?", (food_id,))
        conn.execute("DELETE FROM foods WHERE id = ?", (food_id,))
    return True


def add_foods_only(conn: sqlite3.Connection, entries: list[DiaryEntry], user_id: int | None = None,
                   public: bool = False) -> dict:
    """Add the foods from diary rows to the library without touching the daily log."""
    before = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    freq = Counter((e.name, e.serving, tuple(e.nutrients().values())) for e in entries)
    with conn:
        for (name, serving, values), _n in freq.most_common():
            upsert_food(conn, name, serving, dict(zip(NUTRIENTS, values)), source="upload", created_by=user_id,
                        public=public)
    after = conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    return {"rows": len(entries), "new_foods": after - before, "total_foods": after}


# --- sharing ---------------------------------------------------------------

def shared_with(conn: sqlite3.Connection, owner_id: int) -> set[int]:
    """Who may see this person's diary."""
    return {r[0] for r in conn.execute("SELECT viewer_id FROM shares WHERE owner_id = ?", (owner_id,))}


def set_shared_with(conn: sqlite3.Connection, owner_id: int, viewer_ids: set[int]) -> None:
    with conn:
        conn.execute("DELETE FROM shares WHERE owner_id = ?", (owner_id,))
        conn.executemany("INSERT INTO shares (owner_id, viewer_id) VALUES (?, ?)",
                         [(owner_id, v) for v in viewer_ids if v != owner_id])


def can_view(conn: sqlite3.Connection, viewer_id: int, owner_id: int) -> bool:
    return viewer_id == owner_id or conn.execute(
        "SELECT 1 FROM shares WHERE owner_id = ? AND viewer_id = ?", (owner_id, viewer_id)).fetchone() is not None


def friends_sharing_with(conn: sqlite3.Connection, viewer_id: int) -> list[sqlite3.Row]:
    """People whose diaries this person can see, with when they last logged."""
    return conn.execute(
        """SELECT u.id, u.name, u.username,
                  (SELECT MAX(day) FROM log_entries l WHERE l.user_id = u.id) AS last_day
           FROM shares s JOIN users u ON u.id = s.owner_id
           WHERE s.viewer_id = ? ORDER BY u.name COLLATE NOCASE""",
        (viewer_id,),
    ).fetchall()
