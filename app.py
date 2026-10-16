"""Nutrelia web app: build a day's food log by listing what you ate."""
from __future__ import annotations

import hmac
import os
import sys
import tempfile
from datetime import date, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from nutrelia import db, matching  # noqa: E402
from nutrelia.diary_parser import NUTRIENTS, parse_pdf  # noqa: E402
from nutrelia.units import fmt_amount, parse_serving  # noqa: E402

app = Flask(__name__)
app.secret_key = os.environ.get("NUTRELIA_SECRET_KEY", "dev-only-change-me")
app.config.update(MAX_CONTENT_LENGTH=40 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=60))
PASSWORD = os.environ.get("NUTRELIA_PASSWORD", "")
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


def totals(rows) -> dict:
    """Sum the already-scaled nutrition of log rows."""
    out = {}
    for n in NUTRIENTS:
        vals = [r["scaled"][n] for r in rows if r["scaled"][n] is not None]
        out[n] = round(sum(vals)) if vals else None
    return out


def form_amount(name: str, default: float) -> float:
    try:
        value = float(request.form.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


@app.template_filter("amount")
def amount(value, unit=""):
    if value is None:
        return "–"
    value = round(value, 1)
    return f"{int(value) if float(value).is_integer() else value}{unit}"


@app.template_filter("qty")
def qty(value):
    return fmt_amount(value) if value is not None else ""


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
    # The server runs on UTC, so let the browser pick "today" in your own time zone.
    return render_template("today.html")


@app.route("/day/<day>")
@login_required
def day_view(day):
    day = parse_day(day)
    rows = [dict(r) for r in db.day_entries(get_db(), day)]
    for r in rows:
        r["scaled"] = db.scale(r, r["amount"])
    by_meal = {m: [r for r in rows if r["meal"] == m] for m in db.MEAL_ORDER}
    d = date.fromisoformat(day)
    return render_template(
        "day.html", day=day, d=d, by_meal=by_meal, day_totals=totals(rows), targets=db.get_targets(get_db()),
        meal_totals={m: totals(v) for m, v in by_meal.items()},
        prev_day=(d - timedelta(days=1)).isoformat(), next_day=(d + timedelta(days=1)).isoformat(),
        default_meal=request.args.get("meal", ""),
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
        food = conn.execute("SELECT * FROM foods WHERE id = ?", (int(choice),)).fetchone()
        if not food:
            continue
        db.add_entry(conn, day, meal, food["id"], form_amount(f"amount_{i}_{choice}", food["base_amount"]))
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
        db.add_entry(conn, day, r["meal"], r["food_id"], r["amount"])
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
            amount = form_amount("amount", 0)
            if amount:
                conn.execute("UPDATE log_entries SET amount = ? WHERE id = ?", (amount, entry_id))
    return redirect(url_for("day_view", day=row["day"]))


@app.route("/foods")
@login_required
def foods():
    q = request.args.get("q", "").strip()
    conn = get_db()
    if q:
        rows = conn.execute(
            "SELECT * FROM foods WHERE name LIKE ? ORDER BY name COLLATE NOCASE, times_logged DESC", (f"%{q}%",)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM foods ORDER BY name COLLATE NOCASE, times_logged DESC").fetchall()
    return render_template("foods.html", rows=rows, q=q)


def form_nutrients() -> dict:
    out = {}
    for n in NUTRIENTS:
        try:
            out[n] = float(request.form.get(n, "").strip())
        except ValueError:
            out[n] = None
    return out


@app.route("/targets", methods=["GET", "POST"])
@login_required
def targets():
    conn = get_db()
    if request.method == "POST":
        values = {}
        for n in db.TARGET_NUTRIENTS:
            try:
                values[n] = float(request.form.get(n, "").strip())
            except ValueError:
                values[n] = None  # blank: no target
        db.set_targets(conn, values)
        flash("Targets saved.")
        return redirect(request.form.get("back") or url_for("index"))
    return render_template("targets.html", targets=db.get_targets(conn), back=request.args.get("back", ""))


@app.route("/foods/<int:food_id>", methods=["GET", "POST"])
@login_required
def edit_food(food_id):
    conn = get_db()
    food = conn.execute("SELECT * FROM foods WHERE id = ?", (food_id,)).fetchone()
    if not food:
        abort(404)
    if request.method == "POST":
        if "delete" in request.form:
            if db.delete_food(conn, food_id):
                flash(f"Deleted {food['name']}.")
                return redirect(url_for("foods"))
            flash("This food is in your log, so it can't be deleted. Edit it instead.")
            return redirect(url_for("edit_food", food_id=food_id))
        name = request.form.get("name", "").strip()
        serving = parse_serving(request.form.get("serving", "").strip() or "1 serving")
        if not name:
            flash("Name is required.")
            return redirect(url_for("edit_food", food_id=food_id))
        db.update_food(conn, food_id, name, serving.amount, serving.unit, form_nutrients())
        flash(f"Saved {name}.")
        return redirect(url_for("foods", q=name))
    return render_template("food_edit.html", food=food, serving=db.serving_label(food))


@app.route("/foods/import", methods=["GET", "POST"])
@login_required
def import_foods():
    if request.method == "POST":
        files = [f for f in request.files.getlist("pdf") if f and f.filename]
        if not files:
            flash("Choose at least one PDF.")
            return redirect(url_for("import_foods"))
        entries = []
        with tempfile.TemporaryDirectory() as tmp:
            for i, f in enumerate(files):
                path = Path(tmp) / f"diary{i}.pdf"
                f.save(path)
                try:
                    entries += parse_pdf(path)
                except Exception:  # noqa: BLE001 - report any unreadable file to the user
                    flash(f"Couldn't read {f.filename}. Is it a MyFitnessPal Printable Diary PDF?")
        if entries:
            stats = db.add_foods_only(get_db(), entries)
            flash(f"Read {stats['rows']} food rows: {stats['new_foods']} new foods added "
                  f"({stats['total_foods']} foods in total).")
        elif files:
            flash("No foods found in that file.")
        return redirect(url_for("foods"))
    return render_template("import.html")


@app.route("/foods/new", methods=["GET", "POST"])
@login_required
def new_food():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required.")
            return render_template("food_form.html", form=request.form)
        nutrients = form_nutrients()
        conn = get_db()
        serving = request.form.get("serving", "").strip() or "1 serving"
        with conn:
            fid, base = db.upsert_food(conn, name, serving, nutrients, source="manual")
        day, meal = request.form.get("day"), request.form.get("meal")
        if day and meal:
            food = conn.execute("SELECT * FROM foods WHERE id = ?", (fid,)).fetchone()
            eaten = request.form.get("eaten", "").strip()
            amount = matching.amount_for(food, matching.parse_request(f"x {eaten}"))[0] if eaten else base
            db.add_entry(conn, parse_day(day), meal, fid, amount)
            flash(f"Saved {name} and added it to {meal}.")
            return redirect(url_for("day_view", day=day))
        flash(f"Saved {name}.")
        return redirect(url_for("foods", q=name))
    return render_template("food_form.html", form=request.args)


seed_if_empty()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
