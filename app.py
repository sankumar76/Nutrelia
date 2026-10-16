"""BiteLedger web app: build a day's food log by listing what you ate."""
from __future__ import annotations

import hmac
import os
import sys
from datetime import date, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from biteledger import db, matching  # noqa: E402
from biteledger.diary_parser import NUTRIENTS  # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("BITELEDGER_SECRET_KEY", "dev-only-change-me")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=60))
PASSWORD = os.environ.get("BITELEDGER_PASSWORD", "")
UNITS = {"calories": "", "carbs": "g", "fat": "g", "protein": "g", "cholesterol": "mg", "sodium": "mg", "sugar": "g", "fiber": "g"}


def get_db():
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def seed_if_empty():
    """A fresh deployment starts from the MFP history committed in data/diary_history.csv."""
    conn = db.connect()
    try:
        if conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 0:
            from scripts.import_diary import load_history
            entries = load_history()
            if entries:
                db.import_entries(conn, entries)
    finally:
        conn.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if PASSWORD and not session.get("ok"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def parse_day(value: str) -> str:
    if value == "today":
        return date.today().isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        abort(404)


def totals(rows, qty_key="quantity") -> dict:
    out = {}
    for n in NUTRIENTS:
        vals = [(r[n] * r[qty_key]) for r in rows if r[n] is not None]
        out[n] = round(sum(vals)) if vals else None
    return out


@app.template_filter("amount")
def amount(value, unit=""):
    if value is None:
        return "–"
    value = round(value, 1)
    return f"{int(value) if float(value).is_integer() else value}{unit}"


@app.context_processor
def inject():
    return {"NUTRIENTS": NUTRIENTS, "UNITS": UNITS, "MEALS": db.MEAL_ORDER, "auth_enabled": bool(PASSWORD)}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if PASSWORD and hmac.compare_digest(request.form.get("password", ""), PASSWORD):
            session.permanent = True
            session["ok"] = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
        flash("Wrong password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return redirect(url_for("day_view", day=date.today().isoformat()))


@app.route("/day/<day>")
@login_required
def day_view(day):
    day = parse_day(day)
    rows = [dict(r) for r in db.day_entries(get_db(), day)]
    for r in rows:
        r["scaled"] = {n: None if r[n] is None else r[n] * r["quantity"] for n in NUTRIENTS}
    by_meal = {m: [r for r in rows if r["meal"] == m] for m in db.MEAL_ORDER}
    d = date.fromisoformat(day)
    return render_template(
        "day.html", day=day, d=d, by_meal=by_meal, day_totals=totals(rows),
        meal_totals={m: totals(v) for m, v in by_meal.items()},
        prev_day=(d - timedelta(days=1)).isoformat(), next_day=(d + timedelta(days=1)).isoformat(),
        default_meal=request.args.get("meal", "Breakfast"),
    )


@app.post("/day/<day>/match")
@login_required
def match(day):
    day = parse_day(day)
    meal = request.form.get("meal", "Breakfast")
    text = request.form.get("foods", "")
    lines = matching.match_text(get_db(), text)
    if not lines:
        flash("Type at least one food.")
        return redirect(url_for("day_view", day=day))
    return render_template("match.html", day=day, meal=meal, lines=lines)


@app.post("/day/<day>/add")
@login_required
def add(day):
    day = parse_day(day)
    meal = request.form.get("meal", "Breakfast")
    conn = get_db()
    added = 0
    for i in range(int(request.form.get("count", 0))):
        choice = request.form.get(f"food_{i}", "")
        if not choice.isdigit():
            continue  # skipped
        try:
            qty = float(request.form.get(f"qty_{i}", "1") or 1)
        except ValueError:
            qty = 1
        db.add_entry(conn, day, meal, int(choice), qty)
        added += 1
    flash(f"Added {added} food{'s' if added != 1 else ''} to {meal}.")
    return redirect(url_for("day_view", day=day))


@app.post("/day/<day>/copy")
@login_required
def copy_day(day):
    day = parse_day(day)
    src = parse_day(request.form.get("from", ""))
    conn = get_db()
    rows = db.day_entries(conn, src)
    for r in rows:
        db.add_entry(conn, day, r["meal"], r["food_id"], r["quantity"])
    flash(f"Copied {len(rows)} foods from {src}.")
    return redirect(url_for("day_view", day=day))


@app.post("/entry/<int:entry_id>/update")
@login_required
def update_entry(entry_id):
    conn = get_db()
    row = conn.execute("SELECT day FROM log_entries WHERE id = ?", (entry_id,)).fetchone()
    if not row:
        abort(404)
    with conn:
        if "delete" in request.form:
            conn.execute("DELETE FROM log_entries WHERE id = ?", (entry_id,))
        else:
            try:
                qty = float(request.form.get("quantity", "1"))
            except ValueError:
                qty = 1
            conn.execute("UPDATE log_entries SET quantity = ? WHERE id = ?", (qty, entry_id))
    return redirect(url_for("day_view", day=row["day"]))


@app.route("/foods")
@login_required
def foods():
    q = request.args.get("q", "").strip()
    conn = get_db()
    if q:
        rows = conn.execute(
            "SELECT * FROM foods WHERE name LIKE ? OR serving LIKE ? ORDER BY name, times_logged DESC",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM foods ORDER BY name COLLATE NOCASE, times_logged DESC").fetchall()
    return render_template("foods.html", rows=rows, q=q)


@app.route("/foods/new", methods=["GET", "POST"])
@login_required
def new_food():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.")
            return render_template("food_form.html", form=request.form)
        nutrients = {}
        for n in NUTRIENTS:
            v = request.form.get(n, "").strip()
            nutrients[n] = float(v) if v else None
        conn = get_db()
        with conn:
            fid = db.upsert_food(conn, name, request.form.get("serving", "").strip(), nutrients, source="manual")
        day, meal = request.form.get("day"), request.form.get("meal")
        if day and meal:
            db.add_entry(conn, parse_day(day), meal, fid, float(request.form.get("quantity") or 1))
            flash(f"Saved {name} and added it to {meal}.")
            return redirect(url_for("day_view", day=day))
        flash(f"Saved {name}.")
        return redirect(url_for("foods", q=name))
    return render_template("food_form.html", form=request.args)


seed_if_empty()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
