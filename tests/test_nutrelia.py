import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nutrelia import db, matching  # noqa: E402
from nutrelia.diary_parser import parse_text, split_serving  # noqa: E402

SAMPLE = """
                                          Printable Diary for someone
                                                                 Oct 1, 2025

      FOODS                                                            Calories Carbs Fat Protein Cholest Sodium Sugar Fiber

      Breakfast
      Sambar, 250 gram(s)                                                    128      26g       1g      7g       0mg 200mg       9g     7g
      Sunflower seeds, unsalted, 1 tbsp, without shell                        52       2g    4g         2g       0mg     0mg     0g     1g
      Hemp Hearts - Hemp Hearts, Raw Shelled Hemp Seeds,
                                                                              57       1g 4g            3g      0mg       1mg    0g     1g
      1 tbsp
      Dinner
      Homemade - Varagu Pongal, 100 g                                       300      66g     9g         8g          --     --     --   9g
      Sambar, 1 cup                                                          130      26g       1g      7g       0mg 200mg       9g     7g
                                                            TOTALS          667 121g 19g             27g 0mg 401mg          18g 25g

                                                                 Oct 2, 2025
      FOODS                                                            Calories Carbs Fat Protein Cholest Sodium Sugar Fiber
      Snacks
      Sambar, 250 gram(s)                                                    128      26g       1g      7g       0mg 200mg       9g     7g
      Egg white, 3 large                                                       51      2g    0g        11g       0mg 164mg       1g    0g
"""


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.import_entries(c, parse_text(SAMPLE))
    return c


def test_parse_rows_and_wrapped_names():
    rows = parse_text(SAMPLE)
    assert len(rows) == 7
    assert sum(r.calories for r in rows if r.date == "2025-10-01") == 667
    hemp = [r for r in rows if r.name.startswith("Hemp")][0]
    assert (hemp.name, hemp.serving, hemp.meal) == ("Hemp Hearts - Hemp Hearts, Raw Shelled Hemp Seeds", "1 tbsp", "Breakfast")
    pongal = [r for r in rows if "Pongal" in r.name][0]
    assert pongal.cholesterol is None and pongal.fiber == 9


def test_split_serving():
    assert split_serving("Egg white, 3 large") == ("Egg white", "3 large")
    assert split_serving("Sunflower seeds, unsalted, 1 tbsp, without shell") == ("Sunflower seeds, unsalted", "1 tbsp, without shell")


def test_identical_rows_are_deduplicated(conn):
    foods = conn.execute("SELECT name, base_amount, unit, times_logged FROM foods WHERE name = 'Sambar' ORDER BY unit").fetchall()
    # 250 g and 1 cup can't be converted into each other, so they stay two entries
    assert [tuple(f) for f in foods] == [("Sambar", 1, "cup", 1), ("Sambar", 250, "g", 2)]
    assert conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 6


def test_resized_rows_become_one_food(tmp_path):
    c = db.connect(str(tmp_path / "f.db"))
    rows = parse_text(SAMPLE.replace("Egg white, 3 large                                                       51      2g    0g        11g       0mg 164mg       1g    0g",
                                     "Flax seeds, 2 tbsp   112  6g  9g  4g  0mg  6mg  0g  6g\n      Flax seeds, 1 tbsp   56  3g  4g  2g  0mg  3mg  0g  3g\n      Flax seeds, 1 tbsp   56  3g  4g  2g  0mg  3mg  0g  3g"))
    db.import_entries(c, rows)
    flax = c.execute("SELECT * FROM foods WHERE name = 'Flax seeds'").fetchall()
    assert len(flax) == 1 and (flax[0]["base_amount"], flax[0]["unit"], flax[0]["calories"]) == (1, "tbsp", 56)
    amounts = sorted(r[0] for r in c.execute("SELECT amount FROM log_entries WHERE food_id = ?", (flax[0]["id"],)))
    assert amounts == [1, 1, 2]


def test_different_data_stays_separate(tmp_path):
    c = db.connect(str(tmp_path / "b.db"))
    db.upsert_food(c, "Broccoli", "150 g", {"calories": 51, "carbs": 10, "fat": 1, "protein": 4})
    db.upsert_food(c, "Broccoli", "50 g", {"calories": 20, "carbs": 2, "fat": 0, "protein": 2})
    assert c.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 2


def test_reimport_is_idempotent(conn):
    db.import_entries(conn, parse_text(SAMPLE))
    assert conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0] == 7


def test_variants_ask_user(conn):
    m = matching.match_line(conn, "sambar")
    assert m.status == "choose"
    assert m.options[0]["serving"] == "250 g"  # most-logged first


@pytest.mark.parametrize("text, amount, kcal", [
    ("hemp hearts 10 tbsp", 10, 570),
    ("10 tbsp hemp hearts", 10, 570),
    ("hemp hearts 10tbsp", 10, 570),
    ("hemp hearts 3 tsp", 1, 57),
    ("2 x hemp hearts", 2, 114),
    ("hemp hearts", 1, 57),
])
def test_amounts_scale(conn, text, amount, kcal):
    m = matching.match_line(conn, text)
    o = m.options[0]
    assert o["name"].startswith("Hemp") and o["amount"] == amount and round(o["totals"]["calories"]) == kcal


def test_single_match_is_automatic(conn):
    m = matching.match_line(conn, "egg white 6")
    assert m.status == "auto" and m.options[0]["amount"] == 6 and m.options[0]["totals"]["calories"] == 102


def test_unknown_food(conn):
    assert matching.match_line(conn, "pizza").status in ("none", "weak")


def test_split_lines():
    assert matching.split_lines("sambar, 2 x chapathi\negg white; tofu") == ["sambar", "2 x chapathi", "egg white", "tofu"]


def test_app_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("NUTRELIA_DB", str(tmp_path / "app.db"))
    monkeypatch.setenv("NUTRELIA_PASSWORD", "pw1234")
    import importlib
    import app as app_module
    importlib.reload(app_module)
    c = app_module.app.test_client()
    assert c.get("/day/2026-01-05").status_code == 302  # must sign in
    c.post("/login", data={"username": "santhosh", "password": "pw1234"})
    assert b"Sambar" in c.get("/day/2025-10-01").data  # admin owns the imported history
    r = c.post("/day/2026-01-05/match", data={"meal": "Dinner", "foods": "sambar"})
    assert r.status_code == 200 and b"options" in r.data
    import re
    food_id = re.search(rb'name="food_0" value="(\d+)" checked', r.data).group(1).decode()
    r = c.post("/day/2026-01-05/add", data={"meal": "Dinner", "count": "1", "food_0": food_id, f"amount_0_{food_id}": "500"})
    assert r.status_code == 302
    page = c.get("/day/2026-01-05").data
    assert b"256 <small>kcal" in page  # 500 g of sambar at 128 kcal per 250 g


def test_split_lines_keeps_serving_with_name():
    assert matching.split_lines("egg white, 3 large\n2 x tofu") == ["egg white, 3 large", "2 x tofu"]


def test_targets_roundtrip(conn):
    uid = db.create_user(conn, "a", "A", "secret1")
    assert db.get_targets(conn, uid) == {"calories": None, "carbs": None, "fat": None, "protein": None, "fiber": None}
    db.set_targets(conn, uid, {"calories": 1800, "protein": 90})
    assert db.get_targets(conn, uid)["calories"] == 1800 and db.get_targets(conn, uid)["fat"] is None


def test_first_admin_adopts_existing_log_and_targets(conn):
    conn.execute("INSERT INTO settings (key, value) VALUES ('target_calories', '1600')")
    admin = db.create_user(conn, "santhosh", "Santhosh", "secret1", is_admin=True)
    other = db.create_user(conn, "priya", "Priya", "secret2")
    assert conn.execute("SELECT COUNT(*) FROM log_entries WHERE user_id IS NOT ?", (admin,)).fetchone()[0] == 0
    assert db.get_targets(conn, admin)["calories"] == 1600
    assert db.get_targets(conn, other)["calories"] is None
    assert db.day_entries(conn, other, "2025-10-01") == []
    assert len(db.day_entries(conn, admin, "2025-10-01")) == 5
    assert db.check_login(conn, "Priya", "secret2")["id"] == other
    assert db.check_login(conn, "priya", "wrong") is None


def test_old_database_is_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE foods (id INTEGER PRIMARY KEY, name TEXT NOT NULL, base_amount REAL NOT NULL DEFAULT 1,
            unit TEXT NOT NULL DEFAULT 'serving', calories REAL, carbs REAL, fat REAL, protein REAL, cholesterol REAL,
            sodium REAL, sugar REAL, fiber REAL, times_logged INTEGER NOT NULL DEFAULT 0, last_logged TEXT,
            source TEXT NOT NULL DEFAULT 'mfp');
        CREATE TABLE log_entries (id INTEGER PRIMARY KEY, day TEXT NOT NULL, meal TEXT NOT NULL,
            food_id INTEGER NOT NULL REFERENCES foods(id), amount REAL NOT NULL, position INTEGER NOT NULL DEFAULT 0);
        INSERT INTO foods (name, calories) VALUES ('Egg', 72);
        INSERT INTO log_entries (day, meal, food_id, amount) VALUES ('2026-10-16', 'Breakfast', 1, 2);
    """)
    old.commit()
    old.close()
    c = db.connect(str(path))
    uid = db.create_user(c, "santhosh", "Santhosh", "secret1", is_admin=True)
    assert [r["amount"] for r in db.day_entries(c, uid, "2026-10-16")] == [2]


def test_edit_and_delete_food(conn):
    fid, _ = db.upsert_food(conn, "Pizza slice", "1 slice", {"calories": 285})
    db.update_food(conn, fid, "Pizza", 2, "slice", {"calories": 560, "protein": 24})
    row = conn.execute("SELECT * FROM foods WHERE id = ?", (fid,)).fetchone()
    assert (row["name"], row["base_amount"], row["calories"]) == ("Pizza", 2, 560)
    assert db.delete_food(conn, fid)
    logged = conn.execute("SELECT food_id FROM log_entries LIMIT 1").fetchone()[0]
    assert not db.delete_food(conn, logged)


def test_add_foods_only_leaves_log_alone(conn):
    before = conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0]
    stats = db.add_foods_only(conn, parse_text(SAMPLE.replace("Egg white", "Quinoa")))
    assert stats["new_foods"] == 1
    assert conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0] == before


def test_pypdf_style_continuation_lines():
    text = """
                                 Oct 2, 2025
    Breakfast
    Hemp Hearts - Hemp Hearts, Raw Shelled Hemp Seeds, 1    57     1g  4g      3g    0mg      1mg     0g     1g
    tbsp

    Peanuts, 50 gram(s)                     294     11g25g      12g     0mg     3mg     2g    4g
"""
    rows = parse_text(text)
    assert [(r.name, r.serving) for r in rows] == [
        ("Hemp Hearts - Hemp Hearts, Raw Shelled Hemp Seeds", "1 tbsp"), ("Peanuts", "50 gram(s)")]
    assert rows[1].fat == 25


def test_people_have_separate_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("NUTRELIA_DB", str(tmp_path / "app.db"))
    monkeypatch.setenv("NUTRELIA_PASSWORD", "pw1234")
    import importlib
    import app as app_module
    importlib.reload(app_module)
    admin = app_module.app.test_client()
    admin.post("/login", data={"username": "santhosh", "password": "pw1234"})
    admin.post("/people", data={"action": "add", "name": "Priya", "username": "priya", "password": "pw5678"})
    admin.post("/targets", data={"calories": "1800"})
    priya = app_module.app.test_client()
    priya.post("/login", data={"username": "priya", "password": "pw5678"})
    page = priya.get("/day/2025-10-01").data
    assert b"Sambar" not in page and b"Set targets" in page
    assert priya.get("/people").status_code == 403
    entry = app_module.db.connect().execute("SELECT id FROM log_entries LIMIT 1").fetchone()[0]
    assert priya.post(f"/entry/{entry}/update", data={"delete": "1"}).status_code == 404


def test_my_foods_answer_first_then_the_public_list(conn):
    me = db.create_user(conn, "Me", "me", "pw1234")
    other = db.create_user(conn, "Other", "other", "pw1234")
    n = {"calories": 100, "carbs": 1, "fat": 1, "protein": 1}
    db.upsert_food(conn, "Egg white", "100 g", n, source="manual", created_by=other)
    db.save_public_food(conn, "usda", "171287", "Egg white, raw", 100, "g", n)
    # nothing of mine matches: the public list (original MFP foods before USDA), and I choose.
    # Other people's foods are private, so theirs never shows up.
    m = matching.match_line(conn, "egg white", user_id=me)
    assert m.public and m.status == "choose"
    assert [o["source"] for o in m.options] == ["mfp", "usda"]
    # once a food is mine it answers alone, and a single clear match is picked automatically
    db.upsert_food(conn, "Egg white", "1 cup", n, source="manual", created_by=me)
    m = matching.match_line(conn, "egg white", user_id=me)
    assert not m.public and m.status == "auto" and [o["created_by"] for o in m.options] == [me]
    # removing it from my list sends me back to the public list; logging adds a food to the list
    mine = m.options[0]["id"]
    db.remove_from_my_foods(conn, me, mine)
    assert matching.match_line(conn, "egg white", user_id=me).public
    original = conn.execute("SELECT id FROM foods WHERE name = 'Egg white' AND created_by IS NULL").fetchone()[0]
    db.add_entry(conn, me, "2026-02-01", "Lunch", original, 3)
    assert db.my_food_ids(conn, me) == {original}


def test_existing_people_start_with_what_they_logged(tmp_path):
    c = db.connect(str(tmp_path / "t.db"))
    db.import_entries(c, parse_text(SAMPLE))
    uid = db.create_user(c, "santhosh", "Santhosh", "pw1234", is_admin=True)
    c.execute("DELETE FROM user_foods")
    c.execute("DELETE FROM settings WHERE key = 'my_foods_seeded'")
    c.commit()
    c = db.connect(str(tmp_path / "t.db"))  # an older database is upgraded on open
    logged = {r[0] for r in c.execute("SELECT DISTINCT food_id FROM log_entries")}
    assert db.my_food_ids(c, uid) == logged


def test_sharing_a_diary(tmp_path, monkeypatch):
    monkeypatch.setenv("NUTRELIA_DB", str(tmp_path / "app.db"))
    monkeypatch.setenv("NUTRELIA_PASSWORD", "pw1234")
    import importlib
    import app as app_module
    importlib.reload(app_module)
    admin = app_module.app.test_client()
    admin.post("/login", data={"username": "santhosh", "password": "pw1234"})
    admin.post("/people", data={"action": "add", "name": "Priya", "username": "priya", "password": "pw5678"})
    priya = app_module.app.test_client()
    priya.post("/login", data={"username": "priya", "password": "pw5678"})
    conn = app_module.db.connect()
    sid = conn.execute("SELECT id FROM users WHERE username='santhosh'").fetchone()[0]
    pid = conn.execute("SELECT id FROM users WHERE username='priya'").fetchone()[0]
    assert priya.get(f"/friends/{sid}/day/2025-10-01").status_code == 404  # not shared yet
    admin.post("/friends", data={"viewer": [str(pid)]})
    assert b"Santhosh" in priya.get("/friends").data
    assert b"Sambar" in priya.get(f"/friends/{sid}/day/2025-10-01").data
    assert admin.get(f"/friends/{pid}/day/2025-10-01").status_code == 404  # sharing is one way
    meal = conn.execute("SELECT meal FROM log_entries WHERE user_id=? AND day='2025-10-01' LIMIT 1", (sid,)).fetchone()[0]
    r = priya.post(f"/friends/{sid}/day/2025-10-01/copy", data={"meal": meal, "to": "2026-02-01"})
    assert r.status_code == 302
    assert conn.execute("SELECT COUNT(*) FROM log_entries WHERE user_id=? AND day='2026-02-01'", (pid,)).fetchone()[0] > 0
    admin.post("/friends", data={})  # stop sharing
    assert priya.get(f"/friends/{sid}/day/2025-10-01").status_code == 404


def _usda_zip(path):
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("FoodData_Central_sr_legacy_food_csv/nutrient.csv",
                   'id,name,unit_name,nutrient_nbr,rank\n1003,Protein,G,203,600\n1004,Total lipid (fat),G,204,800\n'
                   '1005,"Carbohydrate, by difference",G,205,1110\n1008,Energy,KCAL,208,300\n1079,"Fiber, total dietary",G,291,1200\n'
                   '1093,"Sodium, Na",MG,307,5800\n2047,Energy (Atwater General Factors),KCAL,957,280\n')
        z.writestr("FoodData_Central_sr_legacy_food_csv/food.csv",
                   'fdc_id,data_type,description,food_category_id,publication_date\n'
                   '168878,sr_legacy_food,"Rice, white, long-grain, regular, cooked",20,2019-04-01\n'
                   '2346384,foundation_food,"Lentils, dry",16,2022-04-28\n'
                   '999,sub_sample_food,"Lentils sample 1",16,2022-04-28\n')
        z.writestr("FoodData_Central_sr_legacy_food_csv/food_nutrient.csv",
                   'id,fdc_id,nutrient_id,amount\n1,168878,1008,130\n2,168878,1005,28.2\n3,168878,1003,2.69\n'
                   '4,168878,1004,0.28\n5,168878,1093,1\n6,2346384,2047,357\n7,2346384,1003,23.6\n8,999,1008,1\n')


def test_public_databases_load_and_reload(conn, tmp_path):
    from nutrelia import sources
    indb = tmp_path / "indb.csv"
    indb.write_text("food_code,food_name,primarysource,energy_kj,energy_kcal,carb_g,protein_g,fat_g,freesugar_g,"
                    "fibre_g,cholesterol_mg,sodium_mg,unit_serving_energy_kcal\n"
                    "ASC001,Hot tea (Garam Chai),asc_manual,67.1,16.1,2.6,0.4,0.5,2.5,0,1.4,3.8,30\n"
                    "ASC171,Masala dosa,asc_manual,700,167.3,24.1,3.9,6.2,0.6,2.1,0,250,400\n")
    usda = tmp_path / "usda.zip"
    _usda_zip(usda)
    assert sources.save(conn, sources.load_indb(indb)) == {"added": 2, "updated": 0, "skipped": 0}
    assert sources.save(conn, sources.load_usda(usda)) == {"added": 2, "updated": 0, "skipped": 0}
    assert sources.save(conn, sources.load_indb(indb))["updated"] == 2  # loading again updates, no copies
    dosa = conn.execute("SELECT * FROM foods WHERE source = 'indb' AND source_ref = 'ASC171'").fetchone()
    assert (dosa["name"], dosa["calories"], dosa["fiber"], dosa["sodium"], dosa["base_amount"], dosa["unit"]) == \
        ("Masala dosa", 167.3, 2.1, 250, 100, "g")
    rice = conn.execute("SELECT * FROM foods WHERE source = 'usda' AND source_ref = '168878'").fetchone()
    assert (rice["calories"], rice["carbs"], rice["is_public"]) == (130, 28.2, 1)
    lentils = conn.execute("SELECT * FROM foods WHERE source = 'usda' AND source_ref = '2346384'").fetchone()
    assert lentils["calories"] == 357  # Atwater energy when 208 is missing
    m = matching.match_line(conn, "masala dosa 250 g")
    assert m.options[0]["name"] == "Masala dosa" and round(m.options[0]["totals"]["calories"]) == 418


def test_open_food_facts_product():
    from nutrelia.sources import _off_food
    f = _off_food({"code": "8904004400267", "product_name": "Aloo Bhujia", "brands": "Haldiram's",
                   "nutriments": {"energy-kcal_100g": 583, "carbohydrates_100g": 41, "fat_100g": 41.6,
                                  "proteins_100g": 11.2, "sodium_100g": 0.9}})
    assert f.name == "Haldiram's - Aloo Bhujia" and f.nutrients["sodium"] == 900 and f.nutrients["calories"] == 583
    assert _off_food({"code": "1", "product_name": ""}) is None


def test_peoples_foods_are_private(conn):
    me = db.create_user(conn, "Me", "me", "pw1234")
    other = db.create_user(conn, "Other", "other", "pw1234")
    n = {"calories": 210, "carbs": 30, "fat": 8, "protein": 4}
    fid, _ = db.upsert_food(conn, "Grandma's laddu", "1 piece", n, source="manual", created_by=other)
    assert not matching.match_line(conn, "grandma's laddu", user_id=me).options
    assert matching.match_line(conn, "grandma's laddu", user_id=other).options[0]["id"] == fid
    # the same food typed by someone else becomes their own copy, not a shared one
    mine, _ = db.upsert_food(conn, "Grandma's laddu", "1 piece", n, source="manual", created_by=me)
    assert mine != fid
    db.set_public(conn, fid, True)  # the admin can put it on the public list
    assert fid in [o["id"] for o in matching.match_line(conn, "grandma's laddu", user_id=db.create_user(
        conn, "Third", "third", "pw1234")).options]


def test_private_food_pages_and_admin_public_list(tmp_path, monkeypatch):
    monkeypatch.setenv("NUTRELIA_DB", str(tmp_path / "app.db"))
    monkeypatch.setenv("NUTRELIA_PASSWORD", "pw1234")
    import importlib
    import app as app_module
    importlib.reload(app_module)
    admin = app_module.app.test_client()
    admin.post("/login", data={"username": "santhosh", "password": "pw1234"})
    admin.post("/people", data={"action": "add", "name": "Priya", "username": "priya", "password": "pw5678"})
    priya = app_module.app.test_client()
    priya.post("/login", data={"username": "priya", "password": "pw5678"})
    priya.post("/foods/new", data={"name": "Priya's poha", "serving": "1 bowl", "calories": "250", "public": "1"})
    conn = app_module.db.connect()
    food = conn.execute("SELECT * FROM foods WHERE name = ?", ("Priya's poha",)).fetchone()
    assert food["is_public"] == 0  # only the admin can make foods public
    assert admin.get(f"/foods/{food['id']}").status_code == 200  # the admin sees everything
    admin.post("/people", data={"action": "add", "name": "Ravi", "username": "ravi", "password": "pw5678"})
    ravi = app_module.app.test_client()
    ravi.post("/login", data={"username": "ravi", "password": "pw5678"})
    assert ravi.get(f"/foods/{food['id']}").status_code == 404
    assert priya.get("/public-foods").status_code == 403
    admin.post(f"/foods/{food['id']}", data={"public": "1"})
    assert ravi.get(f"/foods/{food['id']}").status_code == 200
    assert priya.post(f"/foods/{food['id']}", data={"name": "x", "serving": "1 bowl", "calories": "1"}).status_code == 403
