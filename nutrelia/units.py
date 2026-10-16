"""Serving sizes: split "2 tbsp" into an amount and a unit, and convert between units."""
from __future__ import annotations

import re
from dataclasses import dataclass

# unit -> (dimension, size in the dimension's base unit)
_MASS = {"g": 1.0, "kg": 1000.0, "oz": 28.3495, "lb": 453.592}
_VOLUME = {"tsp": 1.0, "tbsp": 3.0, "cup": 48.0, "ml": 0.202884, "l": 202.884}
_ALIASES = {
    "g": "g", "gm": "g", "gms": "g", "gr": "g", "gram": "g", "grams": "g", "gram(s)": "g", "grm": "g",
    "kg": "kg", "kgs": "kg", "kilogram": "kg", "kilograms": "kg",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "tbsp": "tbsp", "tbs": "tbsp", "tb": "tbsp", "tbl": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "cup": "cup", "cups": "cup",
    "ml": "ml", "milliliter": "ml", "milliliters": "ml", "l": "l", "liter": "l", "litre": "l",
}
_NUMBER = r"(\d+\s+\d+/\d+|\d+/\d+|\d*\.\d+|\d+)"


@dataclass(frozen=True)
class Serving:
    amount: float
    unit: str  # canonical unit ("g", "tbsp") or a lower-cased count word ("large", "piece")

    @property
    def dimension(self) -> str:
        return dimension(self.unit)

    def label(self) -> str:
        return f"{fmt_amount(self.amount)} {self.unit}".strip()


def to_number(text: str) -> float:
    text = text.strip()
    if " " in text:  # "1 1/2"
        whole, frac = text.split()
        return float(whole) + to_number(frac)
    if "/" in text:
        a, b = text.split("/")
        return float(a) / float(b)
    return float(text)


def fmt_amount(x: float) -> str:
    x = round(x, 2)
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def canonical_unit(word: str) -> str | None:
    """Return the canonical mass/volume unit for a word, or None if it isn't one."""
    return _ALIASES.get(word.strip().lower().rstrip("."))


def count_unit(word: str) -> str:
    w = re.sub(r"\s+", " ", word.strip().lower())
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"  # berries -> berry
    if w in ("pieces", "scoops", "slices", "eggs", "units", "cloves", "nuts", "almonds", "leaves"):
        return {"leaves": "leaf"}.get(w, w[:-1])
    return w


def dimension(unit: str) -> str:
    if unit in _MASS:
        return "mass"
    if unit in _VOLUME:
        return "volume"
    return "count:" + unit


def convert(amount: float, from_unit: str, to_unit: str) -> float | None:
    """Convert an amount between units of the same dimension; None if impossible."""
    if from_unit == to_unit:
        return amount
    for table in (_MASS, _VOLUME):
        if from_unit in table and to_unit in table:
            return amount * table[from_unit] / table[to_unit]
    return None


def parse_serving(text: str) -> Serving:
    """'2 tbsp' -> (2, tbsp); '250 gram(s)' -> (250, g); '2 100 g' -> (200, g); '1 onion (medium)' -> (1, onion (medium))."""
    text = re.sub(r"\s+", " ", text.strip())
    m = re.match(_NUMBER + r"\s*(.*)$", text)
    if not m:
        return Serving(1.0, count_unit(text) or "serving")
    amount, rest = to_number(m.group(1)), m.group(2).strip()
    # "2 100 g": two servings of 100 g
    inner = re.match(_NUMBER + r"\s*([a-zA-Z]+(?:\(s\))?)(.*)$", rest)
    if inner and canonical_unit(inner.group(2)):
        return Serving(amount * to_number(inner.group(1)), canonical_unit(inner.group(2)))
    # "g, raw", "grams (3 oz.)", "oz (84g)", "tbsp, without shell": a known unit followed by a note
    head = re.match(r"([a-zA-Z]+(?:\(s\))?)(.*)$", rest)
    if head and canonical_unit(head.group(1)):
        return Serving(amount, canonical_unit(head.group(1)))
    return Serving(amount, count_unit(rest) or "serving")
