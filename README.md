# Nutrelia

*Understand your food. Nourish your future.*

Your MyFitnessPal history, reused. Nutrelia keeps every food you have logged in MFP
(with its nutrition) in a small SQLite database, and lets you build a new day's log by
just typing what you ate:

```
flax seeds 10 tbsp
chapathi 2
egg white, tofu 200 g
```

Set daily targets for calories, carbs, fat, protein and fiber (**Targets**), and each day
shows the target, what you've eaten, and how much is left. Foods are logged under
Breakfast, Lunch, Snacks or Dinner.

Several people can use one Nutrelia. The admin adds them on the **People** page; each person
signs in with their own username and password and has their own log and targets, while
everyone shares the food library. Foods can be changed by the person who added them or
the admin.

Everyone has a **My foods** list. It starts with every food you have logged or added, and
any food you log or add joins it. Each line you type is matched against your own list first.
Only when nothing there matches does Nutrelia show the **public list**, and the food you pick
joins your list. In the public list, the original MFP foods come first, then the Indian
Nutrient Databank, USDA and Open Food Facts. When a food has more than one version, Nutrelia
asks which one you mean. Foods it has never seen can be added by hand.

Foods people add are **private**: only they see them. The admin looks after the public list:
they can add foods to it, edit them, and move any food on or off it from the food's page.

## Fill the public list

The admin's **Foods → Manage public list** page loads:

- **INDB** (Indian Nutrient Databank): download the Excel file from
  [anuvaad.org.in](https://www.anuvaad.org.in/indian-nutrient-databank/) and upload it.
- **USDA FoodData Central**: download *SR Legacy* and/or *Foundation Foods* as CSV from
  [fdc.nal.usda.gov](https://fdc.nal.usda.gov/download-datasets) and upload each .zip.
- **Open Food Facts**: search by name and tick the products to add.

Or from a Bash console (loading a source again updates it, never duplicates):

```bash
python scripts/load_public_foods.py --indb Anuvaad_INDB_2024.11.xlsx
python scripts/load_public_foods.py --usda FoodData_Central_sr_legacy_food_csv_2018-04.zip
python scripts/load_public_foods.py --off-country india   # every product Open Food Facts lists for India
```

Values from these databases are per 100 g. Their household measures come along too, so
`palak paneer 1 bowl`, `rice 1 cup` or `idli 3` (three usual servings) work as well as grams.
Foods can be found by their other names: INDB's *Hot tea (Garam Chai)* matches `garam chai`.
Open Food Facts data is © Open Food Facts contributors under the ODbL; USDA data is public domain.

On **Friends** you choose who can see your diary. People you share with see what you
ate each day (not your targets) and can copy a whole meal into their own day. Sharing
is one way: they only see yours, and you see theirs only if they share back.

## What's inside

| Path | What it does |
| --- | --- |
| `nutrelia/diary_parser.py` | Reads MFP **Printable Diary** PDFs (via `pdftotext -layout`) |
| `nutrelia/db.py` | SQLite schema: `foods` (deduplicated library) and `log_entries` (daily log) |
| `nutrelia/matching.py` | Fuzzy matching of typed foods to the library |
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

On first start Nutrelia creates the admin account `santhosh` (change with
`NUTRELIA_ADMIN_USER` / `NUTRELIA_ADMIN_NAME`) with the password in `NUTRELIA_PASSWORD`.
Set `NUTRELIA_SECRET_KEY` to a long random string. `NUTRELIA_DB` overrides the database path (default `data/nutrelia.db`).

## Add or edit foods

Everything is in the app's **Foods** page:

- **+ New food**: type a name, the serving the nutrition is for (e.g. `100 g`), and the values.
- Tap any food to edit its name, serving or nutrition, or to delete it if it has never been logged.
- **Add from MyFitnessPal PDF**: upload Printable Diary PDFs (Reports → Printable Diary →
  print → Save as PDF). New foods are added to the library; your Nutrelia log is not touched.

## Import more MFP months from the command line

This also loads the days into the Nutrelia log (replacing those days). On
myfitnesspal.com open **Reports → Printable Diary**, choose a date range, and print
it to PDF. Then:

```bash
python scripts/import_diary.py ~/Downloads/MyFitnessPal_Feb_2026.pdf
```

Uses `pdftotext` (poppler) when installed and falls back to `pypdf` otherwise. Re-importing
a day replaces it, so running an import twice is safe.

## Host it on PythonAnywhere (free)

PythonAnywhere's free plan keeps files on disk, so the SQLite database survives restarts.

1. Create a free account at pythonanywhere.com.
2. Open a **Bash console** and run:
   ```bash
   git clone https://github.com/sankumar76/Nutrelia.git nutrelia
   cd nutrelia
   python3.12 -m venv ~/.venvs/nutrelia
   ~/.venvs/nutrelia/bin/pip install -r requirements.txt
   ```
   (For a private repo, clone with a GitHub personal access token.)
3. **Web** tab → *Add a new web app* → *Manual configuration* → Python 3.12.
4. Set **Virtualenv** to `/home/<user>/.venvs/nutrelia`.
5. Edit the **WSGI configuration file** so it contains only:
   ```python
   import os, sys
   sys.path.insert(0, "/home/<user>/nutrelia")
   os.environ["NUTRELIA_PASSWORD"] = "choose-a-password"
   os.environ["NUTRELIA_SECRET_KEY"] = "a-long-random-string"
   from app import app as application
   ```
6. Click **Reload**. The app is at `https://<user>.pythonanywhere.com`.

Free web apps must be renewed from the PythonAnywhere dashboard once every three months.

To update later, in a Bash console:

```bash
cd ~/nutrelia && git pull && ~/.venvs/nutrelia/bin/pip install -r requirements.txt
```

then click **Reload** on the Web tab. Your data lives in `data/nutrelia.db`, which git
never touches, so updates keep everything you've logged.

## Tests

```bash
pip install pytest && pytest -q
```
