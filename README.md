# BiteLedger

Your MyFitnessPal history, reused. BiteLedger keeps every food you have logged in MFP
(with its nutrition) in a small SQLite database, and lets you build a new day's log by
just typing what you ate:

```
flax seeds 10 tbsp
chapathi 2
egg white, tofu 200 g
```

Each line is matched against your own food history. When a food has more than one
version (for example *Sambar, 250 g* and *Sambar, 1 cup*), BiteLedger asks which one
you mean, with the one you've logged most often preselected. Foods it has never seen
can be added by hand.

## What's inside

| Path | What it does |
| --- | --- |
| `biteledger/diary_parser.py` | Reads MFP **Printable Diary** PDFs (via `pdftotext -layout`) |
| `biteledger/db.py` | SQLite schema: `foods` (deduplicated library) and `log_entries` (daily log) |
| `biteledger/matching.py` | Fuzzy matching of typed foods to the library |
| `app.py` | Flask web app (mobile friendly) |
| `scripts/import_diary.py` | Imports PDFs into the database and `data/diary_history.csv` |
| `data/diary_history.csv` | Every food row from the imported diaries; an empty database is seeded from it |

Each food is stored once, with its nutrition for one base serving. MFP rows that are
just different sizes of the same food (*Flax seeds, 1 tbsp* and *Flax seeds, 2 tbsp*) are
merged, and the log records how much you ate, so `flax seeds 10 tbsp`, `200 g tofu`,
`egg white 4` or `2 x chapathi` (two usual servings) are all scaled for you. Grams,
ounces, pounds, teaspoons, tablespoons and cups convert into each other.
Rows with the same name whose nutrition doesn't scale (different brands or recipes), or
whose units can't be converted (*250 g* vs *1 cup*), stay separate, and the app asks
which one you mean.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000
```

Set `BITELEDGER_PASSWORD` to require a password, and `BITELEDGER_SECRET_KEY` to a long
random string. `BITELEDGER_DB` overrides the database path (default `data/biteledger.db`).

## Import more MFP months

On myfitnesspal.com open **Reports → Printable Diary**, choose a date range, and print
it to PDF. Then:

```bash
python scripts/import_diary.py ~/Downloads/MyFitnessPal_Feb_2026.pdf
```

Needs `pdftotext` (`apt install poppler-utils` / `brew install poppler`). Re-importing
a day replaces it, so running an import twice is safe.

## Host it on PythonAnywhere (free)

PythonAnywhere's free plan keeps files on disk, so the SQLite database survives restarts.

1. Create a free account at pythonanywhere.com.
2. Open a **Bash console** and run:
   ```bash
   git clone https://github.com/<you>/biteledger.git
   cd biteledger
   python3.11 -m venv ~/.venvs/biteledger
   ~/.venvs/biteledger/bin/pip install -r requirements.txt
   ```
   (For a private repo, clone with a GitHub personal access token.)
3. **Web** tab → *Add a new web app* → *Manual configuration* → Python 3.11.
4. Set **Virtualenv** to `/home/<user>/.venvs/biteledger`.
5. Edit the **WSGI configuration file** so it contains only:
   ```python
   import os, sys
   sys.path.insert(0, "/home/<user>/biteledger")
   os.environ["BITELEDGER_PASSWORD"] = "choose-a-password"
   os.environ["BITELEDGER_SECRET_KEY"] = "a-long-random-string"
   from app import app as application
   ```
6. Click **Reload**. The app is at `https://<user>.pythonanywhere.com`.

Free web apps must be renewed from the PythonAnywhere dashboard once every three months.

To update later: `cd ~/biteledger && git pull`, then **Reload**.

## Tests

```bash
pip install pytest && pytest -q
```
