"""Import MyFitnessPal Printable Diary PDFs into Nutrelia.

    python scripts/import_diary.py MyFitnessPal_Oct_2025.pdf MyFitnessPal_Nov_2025.pdf ...

Writes every parsed row to data/diary_history.csv (the app seeds an empty
database from it) and loads them into the SQLite database. Needs `pdftotext`
(poppler-utils) for PDFs; already-converted .txt files also work.
"""
import argparse
import csv
import sys
from dataclasses import asdict, fields
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nutrelia import db  # noqa: E402
from nutrelia.diary_parser import DiaryEntry, parse_pdf, parse_text  # noqa: E402

HISTORY_CSV = ROOT / "data" / "diary_history.csv"


def load_history(path: Path = HISTORY_CSV) -> list[DiaryEntry]:
    if not path.exists():
        return []
    out = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for k, v in row.items():
                if k not in ("date", "meal", "name", "serving"):
                    row[k] = None if v == "" else float(v)
            out.append(DiaryEntry(**row))
    return out


def save_history(entries: list[DiaryEntry], path: Path = HISTORY_CSV) -> None:
    path.parent.mkdir(exist_ok=True)
    names = [f.name for f in fields(DiaryEntry)]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=names)
        w.writeheader()
        for e in sorted(entries, key=lambda e: e.date):
            w.writerow({k: ("" if v is None else v) for k, v in asdict(e).items()})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--user", help="username whose log the days go into (default: the admin)")
    args = ap.parse_args()

    new: list[DiaryEntry] = []
    for f in args.files:
        rows = parse_text(f.read_text()) if f.suffix == ".txt" else parse_pdf(f)
        print(f"{f.name}: {len(rows)} food rows over {len({r.date for r in rows})} days")
        new.extend(rows)

    # merge with earlier imports; the newest import of a day wins
    new_days = {e.date for e in new}
    history = [e for e in load_history() if e.date not in new_days] + new
    save_history(history)

    conn = db.connect()
    if args.user:
        user = conn.execute("SELECT id FROM users WHERE username = ?", (args.user,)).fetchone()
        if not user:
            sys.exit(f"No user called {args.user}")
    else:
        user = conn.execute("SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1").fetchone()
    stats = db.import_entries(conn, new, user["id"] if user else None)
    print(f"Loaded {stats['entries']} rows for {stats['days']} days; "
          f"{stats['new_foods']} new foods, {stats['total_foods']} unique foods in the library.")


if __name__ == "__main__":
    main()
