"""Match what the user types ("sambar x2", "2 eggs") to foods in the library."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from rapidfuzz import fuzz

_QTY_PATTERNS = [
    re.compile(r"^(?P<q>\d+(?:\.\d+)?)\s*[x×*]\s*(?P<f>.+)$", re.I),  # 2x sambar
    re.compile(r"^(?P<f>.+?)\s*[x×*]\s*(?P<q>\d+(?:\.\d+)?)$", re.I),  # sambar x2
    re.compile(r"^(?P<f>.+?)\s+(?P<q>\d+(?:\.\d+)?)\s*[x×*]$", re.I),  # sambar 2x
]
AUTO_PICK_SCORE = 90
WEAK_SCORE = 75
MIN_SCORE = 60


@dataclass
class LineMatch:
    text: str
    query: str
    quantity: float
    options: list[dict] = field(default_factory=list)  # best first
    status: str = "none"  # "auto" (one clear match), "choose" (ask the user), "weak", "none"

    @property
    def selected_id(self) -> int | None:
        return self.options[0]["id"] if self.options else None


def split_lines(text: str) -> list[str]:
    parts = re.split(r"[\n;]+", text)
    out = []
    for p in parts:
        # commas separate foods, except "egg white, 3 large" where the number is a serving;
        # "sambar, 2 x chapathi" is still two foods
        items: list[str] = []
        for chunk in p.split(","):
            c = chunk.strip()
            if items and re.match(r"^\d", c) and not re.match(r"^\d+(?:\.\d+)?\s*[x×*]\s", c, re.I):
                items[-1] += ", " + c
            else:
                items.append(c)
        out.extend(items)
    return [s.strip(" -•\t") for s in out if s.strip(" -•\t")]


def parse_quantity(line: str) -> tuple[str, float]:
    for pat in _QTY_PATTERNS:
        m = pat.match(line.strip())
        if m:
            return m.group("f").strip(), float(m.group("q"))
    return line.strip(), 1.0


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s.]", " ", s.lower())).strip()


def _score(query: str, food: sqlite3.Row) -> float:
    q = _norm(query)
    name = _norm(food["name"])
    # "Onion - Onion" style names: also compare against the part after the brand
    short = _norm(food["name"].split(" - ", 1)[-1])
    full = _norm(f"{food['name']} {food['serving']}")
    best = max(
        fuzz.WRatio(q, name),
        fuzz.WRatio(q, short),
        fuzz.token_set_ratio(q, full),
    )
    if q == name or q == short:
        best = 101  # exact name always wins
    return best


def match_line(conn: sqlite3.Connection, line: str, foods: list[sqlite3.Row] | None = None) -> LineMatch:
    query, qty = parse_quantity(line)
    foods = foods if foods is not None else conn.execute("SELECT * FROM foods").fetchall()
    scored = [(_score(query, f), f) for f in foods]
    scored = [s for s in scored if s[0] >= MIN_SCORE]
    result = LineMatch(text=line, query=query, quantity=qty)
    if not scored:
        return result
    top = max(s for s, _ in scored)
    cutoff = max(MIN_SCORE, top - 8)
    keep = [(s, f) for s, f in scored if s >= cutoff]
    keep.sort(key=lambda sf: (-sf[0], -(sf[1]["times_logged"] or 0), sf[1]["last_logged"] or ""))
    result.options = [dict(f) | {"score": round(min(s, 100))} for s, f in keep[:12]]
    if top < WEAK_SCORE:
        result.status = "weak"  # only loose suggestions; the food is probably new
    elif len(result.options) == 1 and top >= AUTO_PICK_SCORE:
        result.status = "auto"
    else:
        result.status = "choose"
    return result


def match_text(conn: sqlite3.Connection, text: str) -> list[LineMatch]:
    foods = conn.execute("SELECT * FROM foods").fetchall()
    return [match_line(conn, line, foods) for line in split_lines(text)]
