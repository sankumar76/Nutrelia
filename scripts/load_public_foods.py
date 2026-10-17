"""Fill Nutrelia's public food list from public databases.

    python scripts/load_public_foods.py --indb Anuvaad_INDB_2024.11.xlsx
    python scripts/load_public_foods.py --usda FoodData_Central_sr_legacy_food_csv_2018-04.zip
    python scripts/load_public_foods.py --off-country india          # fetches from Open Food Facts
    python scripts/load_public_foods.py --off-file en.openfoodfacts.org.products.csv.gz

Running a source again updates the foods it added before instead of adding copies.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from nutrelia import db, sources  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--indb", type=Path, help="INDB .xlsx or .csv")
    ap.add_argument("--usda", type=Path, action="append", default=[], help="FoodData Central CSV .zip (repeatable)")
    ap.add_argument("--off-country", help="fetch Open Food Facts products sold in this country, e.g. india")
    ap.add_argument("--off-limit", type=int, help="stop after this many Open Food Facts products")
    ap.add_argument("--off-file", type=Path, help="Open Food Facts CSV export (.csv or .csv.gz)")
    args = ap.parse_args()
    conn = db.connect()
    jobs = []
    if args.indb:
        jobs.append(("INDB", sources.load_indb(args.indb)))
    for z in args.usda:
        jobs.append((f"USDA {z.name}", sources.load_usda(z)))
    if args.off_file:
        jobs.append(("Open Food Facts file", sources.load_off_csv(args.off_file, args.off_country or "india")))
    elif args.off_country:
        jobs.append(("Open Food Facts", sources.fetch_off_country(args.off_country, args.off_limit)))
    if not jobs:
        ap.error("give at least one of --indb, --usda, --off-country, --off-file")
    for label, foods in jobs:
        stats = sources.save(conn, foods)
        print(f"{label}: {stats['added']} added, {stats['updated']} updated, {stats['skipped']} skipped (no name or calories)")
    print(f"The public list now has {conn.execute('SELECT COUNT(*) FROM foods WHERE is_public = 1').fetchone()[0]} foods.")


if __name__ == "__main__":
    main()
