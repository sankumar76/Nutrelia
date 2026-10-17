"""A person's health profile and the daily targets it suggests.

Calories: Mifflin-St Jeor resting energy, times an activity factor, adjusted for the goal
(about 0.5 kg a week for losing, a lean surplus for gaining), never below a safe floor.
Macros: protein by body weight (more when losing or gaining), 30% of calories from fat,
carbs for the rest, and 14 g of fiber per 1,000 kcal (the US Dietary Guidelines' figure).
These are starting points; people adjust them on the Targets page.
"""
from __future__ import annotations

from datetime import date

SEXES = {"female": "Female", "male": "Male", "other": "Other / prefer not to say"}
ACTIVITY = {
    "sedentary": ("Sedentary", "desk job, little exercise", 1.2),
    "light": ("Lightly active", "exercise 1-3 days a week", 1.375),
    "moderate": ("Moderately active", "exercise 3-5 days a week", 1.55),
    "active": ("Very active", "hard exercise 6-7 days a week", 1.725),
    "athlete": ("Extra active", "physical job or training twice a day", 1.9),
}
GOALS = {
    "lose": ("Lose weight", -500, 1.6),  # kcal adjustment, protein g per kg
    "maintain": ("Maintain weight", 0, 1.2),
    "gain": ("Gain weight", 300, 1.6),
}
PROFILE_FIELDS = ("sex", "birth_date", "height_cm", "weight_kg", "activity", "goal")


def age_on(birth_date: str | None, today: date | None = None) -> int | None:
    if not birth_date:
        return None
    try:
        b = date.fromisoformat(birth_date)
    except ValueError:
        return None
    today = today or date.today()
    return today.year - b.year - ((today.month, today.day) < (b.month, b.day))


def is_complete(user) -> bool:
    return all(user[f] for f in PROFILE_FIELDS)


def bmi(user) -> float | None:
    if not (user["height_cm"] and user["weight_kg"]):
        return None
    return round(user["weight_kg"] / (user["height_cm"] / 100) ** 2, 1)


def bmi_label(value: float | None) -> str:
    if value is None:
        return ""
    if value < 18.5:
        return "underweight"
    if value < 25:
        return "healthy weight"
    if value < 30:
        return "overweight"
    return "obese"


def suggested_targets(user, today: date | None = None) -> dict | None:
    """Daily calories, carbs, fat, protein and fiber for a complete profile, else None."""
    if not is_complete(user):
        return None
    age = age_on(user["birth_date"], today)
    w, h = user["weight_kg"], user["height_cm"]
    sex_term = {"male": 5, "female": -161}.get(user["sex"], -78)
    resting = 10 * w + 6.25 * h - 5 * age + sex_term
    factor = ACTIVITY.get(user["activity"], ACTIVITY["sedentary"])[2]
    _label, adjust, protein_per_kg = GOALS.get(user["goal"], GOALS["maintain"])
    floor = 1500 if user["sex"] == "male" else 1200
    calories = max(resting * factor + adjust, floor)
    calories = round(calories / 10) * 10
    protein = min(protein_per_kg * w, 0.35 * calories / 4)
    fat = 0.30 * calories / 9
    carbs = (calories - 4 * protein - 9 * fat) / 4
    return {
        "calories": calories,
        "carbs": round(carbs),
        "fat": round(fat),
        "protein": round(protein),
        "fiber": round(14 * calories / 1000),
    }


def read_form(form) -> tuple[dict, list[str]]:
    """Profile values from a submitted form, and what's wrong with them."""
    errors, out = [], {}
    out["sex"] = form.get("sex") if form.get("sex") in SEXES else None
    out["activity"] = form.get("activity") if form.get("activity") in ACTIVITY else None
    out["goal"] = form.get("goal") if form.get("goal") in GOALS else None
    bd = (form.get("birth_date") or "").strip()
    age = age_on(bd)
    if bd and (age is None or not 13 <= age <= 110):
        errors.append("Check your date of birth (Nutrelia is for people 13 and older).")
    out["birth_date"] = bd if age is not None and 13 <= age <= 110 else None
    for key, low, high, label in (("height_cm", 100, 250, "height in cm"), ("weight_kg", 25, 350, "weight in kg")):
        raw = (form.get(key) or "").strip()
        try:
            value = float(raw) if raw else None
        except ValueError:
            value = None
            errors.append(f"Enter your {label} as a number.")
        if value is not None and not low <= value <= high:
            errors.append(f"Check your {label} ({low} to {high}).")
            value = None
        out[key] = value
    return out, errors


def nice_name(name: str) -> str:
    """'priya ramesh' -> 'Priya Ramesh'; leaves the rest of each word alone ('McDonald' stays)."""
    return " ".join(w[:1].upper() + w[1:] for w in name.split())
