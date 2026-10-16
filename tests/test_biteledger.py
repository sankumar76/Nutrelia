import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from biteledger import db, matching  # noqa: E402
from biteledger.diary_parser import parse_text, split_serving  # noqa: E402

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
    foods = conn.execute("SELECT name, serving, times_logged FROM foods WHERE name = 'Sambar' ORDER BY serving").fetchall()
    assert [tuple(f) for f in foods] == [("Sambar", "1 cup", 1), ("Sambar", "250 gram(s)", 2)]
    assert conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 6


def test_reimport_is_idempotent(conn):
    db.import_entries(conn, parse_text(SAMPLE))
    assert conn.execute("SELECT COUNT(*) FROM foods").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0] == 7


def test_variants_ask_user(conn):
    m = matching.match_line(conn, "sambar")
    assert m.status == "choose"
    assert m.options[0]["serving"] == "250 gram(s)"  # most-logged first


def test_single_match_is_automatic(conn):
    m = matching.match_line(conn, "egg white x2")
    assert m.status == "auto" and m.quantity == 2 and m.options[0]["name"] == "Egg white"


def test_unknown_food(conn):
    assert matching.match_line(conn, "pizza").status in ("none", "weak")


def test_split_lines():
    assert matching.split_lines("sambar, 2 x chapathi\negg white; tofu") == ["sambar", "2 x chapathi", "egg white", "tofu"]


def test_app_flow(tmp_path, monkeypatch):
    monkeypatch.setenv("BITELEDGER_DB", str(tmp_path / "app.db"))
    import importlib
    import app as app_module
    importlib.reload(app_module)
    c = app_module.app.test_client()
    r = c.post("/day/2026-01-05/match", data={"meal": "Dinner", "foods": "sambar"})
    assert r.status_code == 200 and b"pick one" in r.data


def test_split_lines_keeps_serving_with_name():
    assert matching.split_lines("egg white, 3 large\n2 x tofu") == ["egg white, 3 large", "2 x tofu"]
