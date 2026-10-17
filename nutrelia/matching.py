"""Match what the user types ("flax seeds 10 tbsp", "2 x chapathi") to foods in the library."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from .db import food_portions, scale, serving_label
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
    public: bool = False  # options come from the public list, not the person's own foods

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


def _portion_grams(portions: dict, unit: str) -> float | None:
    """Grams in one `unit` from a food's household measures, converting tsp/tbsp/cup/ml between each other."""
    if unit in portions:
        return portions[unit]
    for known, grams in portions.items():
        k = convert(1, unit, known)
        if k is not None:
            return grams * k
    return None


def amount_for(food, req: Request, portions: dict | None = None) -> tuple[float, bool]:
    """How much of `food` (in its own unit) the request means, and whether the request's unit fit.
    `portions` ({unit: grams}) lets "dal 1 bowl" work for a food measured in grams."""
    base = food["base_amount"]
    by_weight = portions and convert(1, "g", food["unit"]) is not None
    if req.number is None:
        return base, True
    if req.multiplier:
        return base * req.number, True
    if req.unit:
        canon = canonical_unit(req.unit)
        if canon:
            converted = convert(req.number, canon, food["unit"])
            if converted is not None:
                return converted, True
        unit = canon or count_unit(req.unit)
        if by_weight and (grams := _portion_grams(portions, unit)):
            return convert(req.number * grams, "g", food["unit"]), True
        if canon:
            return base, False
        if count_unit(req.unit) == food["unit"] or food["unit"].startswith(count_unit(req.unit)):
            return req.number, True
    if by_weight and req.unit is None:
        # "dosa 2": two of the usual household serving
        return convert(req.number * next(iter(portions.values())), "g", food["unit"]), True
    # a bare number: a count ("egg white 4" -> 4 large) for count units, else that many servings
    if food["unit"] in ("g", "kg", "oz", "lb", "tsp", "tbsp", "cup", "ml", "l"):
        return base * req.number, True
    return req.number, True


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s.]", " ", s.lower())).strip()


# the public list in order of preference: the original MFP foods, then the Indian Nutrient
# Databank, USDA FoodData Central, Open Food Facts, and anything else the admin made public
PUBLIC_ORDER = {"mfp": 1, "indb": 2, "usda": 3, "off": 4}


def _blend(q: str, q_words: set, name: str, wratio: float) -> float:
    """WRatio rewards any shared word ("masala chai" ~ "channa masala"), which stops telling foods
    apart in a big library. A name holding every typed word scores high, shorter names higher;
    otherwise whole-name similarity is blended in."""
    words = name.split()
    if q_words <= {w.rstrip("s") for w in words}:
        return max(wratio, 88 + 10 * len(q_words) / max(len(words), 1))
    return (wratio + fuzz.token_sort_ratio(q, name)) / 2


def name_aliases(name: str) -> list[str]:
    """The names a food goes by: "Onion - Onion" also as "Onion"; "Hot tea (Garam Chai)" also as
    "Garam Chai"; "Paneer parantha/paratha" also as "Paneer paratha"."""
    out = [name, name.split(" - ", 1)[-1]]
    main = re.sub(r"\([^)]*\)", " ", name).strip()
    out.append(main)
    for inner in re.findall(r"\(([^)]*)\)", name):
        out += [p for p in re.split(r"[,/]", inner) if p.strip()]
    m = re.match(r"^(.*?)(\w+)/(\w+)(.*)$", main)  # "Dal parantha/paratha": both spellings
    if m:
        out += [m[1] + m[2] + m[4], m[1] + m[3] + m[4]]
    seen, result = set(), []
    for a in map(_norm, out):
        if a and a not in seen:
            seen.add(a)
            result.append(a)
    return result


class FoodIndex:
    """Foods with their names normalised once, so each typed line is scored against all of them quickly."""

    def __init__(self, foods: list, portions: dict | None = None):
        self.foods = foods
        self.portions = portions or {}
        self.aliases, self.owner = [], []
        for i, f in enumerate(foods):
            for a in name_aliases(f["name"]):
                self.aliases.append(a)
                self.owner.append(i)

    def scored(self, query: str) -> list[tuple[float, object]]:
        q = _norm(query)
        if not q:
            return []
        best: dict[int, float] = {}
        q_words = {w.rstrip("s") for w in q.split()}
        for alias, score, j in process.extract(q, self.aliases, scorer=fuzz.WRatio, score_cutoff=MIN_SCORE,
                                               limit=None):
            i = self.owner[j]
            score = 101 if q in (alias, alias.rstrip("s")) or q.rstrip("s") == alias else _blend(q, q_words, alias, score)
            if score >= MIN_SCORE:
                best[i] = max(best.get(i, 0), score)  # exact name always wins (101)
        return [(score, self.foods[i]) for i, score in best.items()]


def food_tier(food, user_id: int | None) -> int:
    """0: your own food list, then the public list in PUBLIC_ORDER."""
    if _in_mine(food):
        return 0
    return PUBLIC_ORDER.get(food["source"], 5)


def _in_mine(food) -> bool:
    return "in_mine" in food.keys() and bool(food["in_mine"])


def _option(food, req: Request, score: float, user_id: int | None, portions: dict | None = None) -> dict:
    amount, unit_fits = amount_for(food, req, portions)
    return dict(food) | {
        "tier": food_tier(food, user_id),
        "mine": _in_mine(food),
        "score": round(min(score, 100)),
        "amount": round(amount, 2),
        "unit_fits": unit_fits,
        "serving": serving_label(food),
        "portions": portions or {},
        "totals": scale(food, amount),
    }


def match_line(conn: sqlite3.Connection, line: str, foods: list | FoodIndex | None = None,
               user_id: int | None = None) -> LineMatch:
    req = parse_request(line)
    foods = foods if foods is not None else _foods(conn, user_id)
    index = foods if isinstance(foods, FoodIndex) else FoodIndex(foods, food_portions(conn))
    scored = index.scored(req.query)
    if not scored and req.unit:
        # the "unit" may really be part of the name ("2 eggs" -> unit "eggs")
        req = Request(f"{req.query} {req.unit}" if _TRAILING.match(line) else f"{req.unit} {req.query}", req.number)
        req.query = req.query.strip()
        scored = index.scored(req.query)
    result = LineMatch(text=line, request=req)
    if not scored:
        return result
    top = max(s for s, _ in scored)
    keep = [(s, f) for s, f in scored if s >= max(MIN_SCORE, top - 8)]
    opts = [_option(f, req, s, user_id, index.portions.get(f["id"])) for s, f in keep]
    # Your own foods answer first. Only when none of them is among the best matches does the
    # public list come in, and then you always choose.
    mine = [o for o in opts if o["mine"]]
    result.public = not mine
    opts = mine or opts
    # names that match about equally well (within 5 points) go by source order; a clearly better name wins
    opts.sort(key=lambda o: (-(o["score"] // 5), o["tier"], -o["score"], not o["unit_fits"], -(o["times_logged"] or 0)))
    result.options = opts[:12]
    best = max(o["score"] for o in opts)
    if best < WEAK_SCORE:
        result.status = "weak"
    elif len(result.options) == 1 and best >= AUTO_PICK_SCORE and not result.public:
        result.status = "auto"
    else:
        result.status = "choose"
    return result


def _foods(conn: sqlite3.Connection, user_id: int | None) -> list:
    """Foods this person can match: their own list and the public list (everything when there's no user)."""
    return conn.execute(
        """SELECT f.*, u.name AS creator_name, (:uid IS NULL OR m.user_id IS NOT NULL) AS in_mine
           FROM foods f LEFT JOIN users u ON u.id = f.created_by
           LEFT JOIN user_foods m ON m.food_id = f.id AND m.user_id = :uid
           WHERE :uid IS NULL OR m.user_id IS NOT NULL OR f.is_public = 1 OR f.created_by = :uid""",
        {"uid": user_id},
    ).fetchall()


def match_text(conn: sqlite3.Connection, text: str, user_id: int | None = None) -> list[LineMatch]:
    foods = FoodIndex(_foods(conn, user_id), food_portions(conn))
    return [match_line(conn, line, foods, user_id) for line in split_lines(text)]
