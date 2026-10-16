"""Match what the user types ("flax seeds 10 tbsp", "2 x chapathi") to foods in the library."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from rapidfuzz import fuzz

from .db import scale, serving_label
from .units import _NUMBER, canonical_unit, convert, count_unit, to_number

AUTO_PICK_SCORE = 90
WEAK_SCORE = 75
MIN_SCORE = 60

_MULT = [
    re.compile(r"^" + _NUMBER + r"\s*[x×*]\s+(?P<f>.+)$", re.I),  # 2 x chapathi
    re.compile(r"^(?P<f>.+?)\s+[x×*]\s*" + _NUMBER + r"$", re.I),  # chapathi x2
    re.compile(r"^(?P<f>.+?)\s+" + _NUMBER + r"\s*[x×*]$", re.I),  # chapathi 2x
]
_TRAILING = re.compile(r"^(?P<f>.+?)\s*,?\s+" + _NUMBER + r"\s*(?P<u>[a-zA-Z]+(?:\s*\(s\))?)?$")  # flax seeds 10tbsp
_LEADING = re.compile(r"^" + _NUMBER + r"\s*(?P<u>[a-zA-Z]+(?:\(s\))?)?\s+(?:of\s+)?(?P<f>.+)$")  # 10 tbsp flax seeds


@dataclass
class Request:
    """What the user asked for: a food name plus an optional amount."""
    query: str
    number: float | None = None
    unit: str | None = None  # a word that followed the number ("tbsp", "large")
    multiplier: bool = False  # "2 x chapathi": that many usual servings


@dataclass
class LineMatch:
    text: str
    request: Request
    options: list[dict] = field(default_factory=list)  # best first; each has amount + totals
    status: str = "none"  # "auto" (one clear match), "choose" (ask), "weak" (only loose guesses), "none"

    @property
    def query(self) -> str:
        return self.request.query


def split_lines(text: str) -> list[str]:
    out = []
    for part in re.split(r"[\n;]+", text):
        # commas separate foods, except "egg white, 3 large" where the number is the amount;
        # "sambar, 2 x chapathi" is still two foods
        items: list[str] = []
        for chunk in part.split(","):
            c = chunk.strip()
            if items and re.match(r"^\d", c) and not re.match(r"^\d+(?:\.\d+)?\s*[x×*]\s", c, re.I):
                items[-1] += ", " + c
            else:
                items.append(c)
        out.extend(items)
    return [s.strip(" -•\t") for s in out if s.strip(" -•\t")]


def parse_request(line: str) -> Request:
    line = re.sub(r"\s+", " ", line.strip())
    for pat in _MULT:
        m = pat.match(line)
        if m:
            return Request(m.group("f").strip(" ,"), to_number(m.group(1) if m.re is _MULT[0] else m.group(2)), multiplier=True)
    for pat in (_TRAILING, _LEADING):
        m = pat.match(line)
        if m:
            num_group = 2 if pat is _TRAILING else 1
            return Request(m.group("f").strip(" ,"), to_number(m.group(num_group)), (m.group("u") or "").lower() or None)
    return Request(line)


def amount_for(food, req: Request) -> tuple[float, bool]:
    """How much of `food` (in its own unit) the request means, and whether the request's unit fit."""
    base = food["base_amount"]
    if req.number is None:
        return base, True
    if req.multiplier:
        return base * req.number, True
    if req.unit:
        canon = canonical_unit(req.unit)
        if canon:
            converted = convert(req.number, canon, food["unit"])
            return (converted, True) if converted is not None else (base, False)
        if count_unit(req.unit) == food["unit"] or food["unit"].startswith(count_unit(req.unit)):
            return req.number, True
    # a bare number: a count ("egg white 4" -> 4 large) for count units, else that many servings
    if food["unit"] in ("g", "kg", "oz", "lb", "tsp", "tbsp", "cup", "ml", "l"):
        return base * req.number, True
    return req.number, True


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s.]", " ", s.lower())).strip()


def _score(query: str, food) -> float:
    q = _norm(query)
    name = _norm(food["name"])
    short = _norm(food["name"].split(" - ", 1)[-1])  # "Onion - Onion": compare without the brand too
    if q in (name, short) or q.rstrip("s") in (name, short):
        return 101  # exact name always wins
    return max(fuzz.WRatio(q, name), fuzz.WRatio(q, short))


def _option(food, req: Request, score: float) -> dict:
    amount, unit_fits = amount_for(food, req)
    return dict(food) | {
        "score": round(min(score, 100)),
        "amount": round(amount, 2),
        "unit_fits": unit_fits,
        "serving": serving_label(food),
        "totals": scale(food, amount),
    }


def match_line(conn: sqlite3.Connection, line: str, foods: list | None = None) -> LineMatch:
    req = parse_request(line)
    foods = foods if foods is not None else conn.execute("SELECT * FROM foods").fetchall()
    scored = [(s, f) for f in foods if (s := _score(req.query, f)) >= MIN_SCORE]
    if not scored and req.unit:
        # the "unit" may really be part of the name ("2 eggs" -> unit "eggs")
        req = Request(f"{req.query} {req.unit}" if _TRAILING.match(line) else f"{req.unit} {req.query}", req.number)
        req.query = req.query.strip()
        scored = [(s, f) for f in foods if (s := _score(req.query, f)) >= MIN_SCORE]
    result = LineMatch(text=line, request=req)
    if not scored:
        return result
    top = max(s for s, _ in scored)
    keep = [(s, f) for s, f in scored if s >= max(MIN_SCORE, top - 8)]
    opts = [_option(f, req, s) for s, f in keep]
    # best name first; foods whose unit fits what was typed before those that don't; then most logged
    opts.sort(key=lambda o: (-o["score"], not o["unit_fits"], -(o["times_logged"] or 0)))
    result.options = opts[:12]
    if top < WEAK_SCORE:
        result.status = "weak"
    elif len(result.options) == 1 and top >= AUTO_PICK_SCORE:
        result.status = "auto"
    else:
        result.status = "choose"
    return result


def match_text(conn: sqlite3.Connection, text: str) -> list[LineMatch]:
    foods = conn.execute("SELECT * FROM foods").fetchall()
    return [match_line(conn, line, foods) for line in split_lines(text)]
