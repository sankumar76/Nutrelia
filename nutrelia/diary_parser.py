"""Parse MyFitnessPal "Printable Diary" reports.

The report is saved from the browser as a PDF and converted to text with
`pdftotext -layout`. Each day looks like:

    Oct 1, 2025
    FOODS      Calories Carbs Fat Protein Cholest Sodium Sugar Fiber
    Breakfast
    Sambar, 250 gram(s)     128  26g  1g  7g  0mg 200mg  9g  7g
    ...
    TOTALS                 1398 177g ...

Long food names wrap: the name starts on one line, the numbers sit alone on
the next line, and the rest of the name follows on a third line.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

NUTRIENTS = ["calories", "carbs", "fat", "protein", "cholesterol", "sodium", "sugar", "fiber"]
MEALS = {"breakfast", "lunch", "dinner", "snacks"}

_VALUE = r"(--|-?\d[\d,]*(?:\.\d+)?(?:mg|g)?)"
_NUMBERS_RE = re.compile(r"^(?P<prefix>.*?)\s+" + r"\s+".join([_VALUE] * 8) + r"\s*$")
_DATE_RE = re.compile(r"^\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*$")
_GLUED_RE = re.compile(r"(\d(?:mg|g))(?=\d)")
_SKIP_RE = re.compile(r"^\s*(FOODS\s+Calories|English\s*$|\d{1,2}/\d{1,2}/\d{2}, |https?://)")
# Serving is the last ", <number>..." chunk of the entry, e.g. "Sambar, 250 gram(s)".
_SERVING_RE = re.compile(r"^(?P<name>.+?)\s*,\s*(?P<serving>(?:\d|\.\d|½|¼|¾)[^,]*(?:,[^,\d][^,]*)*)$")


@dataclass
class DiaryEntry:
    date: str  # ISO yyyy-mm-dd
    meal: str
    name: str
    serving: str
    calories: float | None
    carbs: float | None
    fat: float | None
    protein: float | None
    cholesterol: float | None
    sodium: float | None
    sugar: float | None
    fiber: float | None

    def nutrients(self) -> dict:
        return {k: getattr(self, k) for k in NUTRIENTS}


def _num(token: str) -> float | None:
    if token == "--":
        return None
    v = float(token.replace(",", "").rstrip("mg").rstrip("g"))
    return int(v) if v.is_integer() else v


def split_serving(full: str) -> tuple[str, str]:
    """'Egg white, 3 large' -> ('Egg white', '3 large')."""
    full = re.sub(r"\s+", " ", full).strip().rstrip(",")
    m = _SERVING_RE.match(full)
    if not m:
        return full, ""
    return m.group("name").strip(), m.group("serving").strip()


def parse_text(text: str) -> list[DiaryEntry]:
    entries: list[DiaryEntry] = []
    date = meal = None
    pending: list[str] = []  # name fragments for a wrapped row
    wrapped_values: list[str] | None = None  # numbers already seen for a wrapped row

    def flush():
        nonlocal pending, wrapped_values
        if wrapped_values is not None and pending:
            _add(" ".join(pending), wrapped_values)
        elif pending and last_row:
            # text after a complete row, not followed by numbers: the row's name continues (pypdf layout)
            full_name, values = last_row[0]
            entries.pop()
            _add(full_name + " " + " ".join(pending), values)
        pending, wrapped_values = [], None

    def _add(full_name: str, values: list[str]):
        name, serving = split_serving(full_name)
        nums = [_num(v) for v in values]
        entries.append(DiaryEntry(date, meal, name, serving, *nums))
        last_row[:] = [(full_name, values)]

    last_row: list = []  # the most recent complete row, in case its name continues on the next line

    for raw in text.splitlines():
        line = _GLUED_RE.sub(r"\1 ", raw.rstrip())  # "11g25g" -> "11g 25g"
        if not line.strip() or _SKIP_RE.match(line):
            continue
        if "Printable Diary for" in line:
            continue
        dm = _DATE_RE.match(line)
        if dm:
            flush()
            last_row.clear()
            date = datetime.strptime(dm.group(1), "%b %d, %Y").date().isoformat()
            meal = None
            continue
        stripped = line.strip()
        if stripped.lower() in MEALS:
            flush()
            last_row.clear()
            meal = stripped.title()
            continue
        m = _NUMBERS_RE.match(line)
        if m:
            prefix = m.group("prefix").strip()
            values = list(m.groups()[1:])
            if prefix.startswith("TOTALS"):
                flush()
                last_row.clear()
                continue
            if prefix:
                flush()
                _add(prefix, values)
            else:
                # numbers alone: the name started on the previous line(s)
                wrapped_values = values
            continue
        # a text-only line: start or continuation of a wrapped name
        if wrapped_values is not None:
            pending.append(stripped)
            flush()
        else:
            pending.append(stripped)
    flush()
    return [e for e in entries if e.date and e.meal]


def pdf_to_text(path: str | Path) -> str:
    """Text of the PDF with its column layout kept: pdftotext if installed, otherwise pypdf."""
    if shutil.which("pdftotext"):
        return subprocess.run(
            ["pdftotext", "-layout", str(path), "-"], check=True, capture_output=True, text=True
        ).stdout
    from pypdf import PdfReader
    return "\n".join(page.extract_text(extraction_mode="layout") for page in PdfReader(str(path)).pages)


def parse_pdf(path: str | Path) -> list[DiaryEntry]:
    return parse_text(pdf_to_text(path))
