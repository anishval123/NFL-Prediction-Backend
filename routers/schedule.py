"""Schedule endpoints: full 2026 schedule + per-week views."""
import json
import os

from fastapi import APIRouter, HTTPException

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(BASE_DIR, "data", "schedule2026.json")

router = APIRouter(tags=["schedule"])


def load_schedule():
    with open(DATA_FILE, encoding="utf-8") as fh:
        return json.load(fh)


@router.get("/schedule")
def get_schedule():
    """Return the full 2026 schedule (array of 18 week objects)."""
    return load_schedule()


@router.get("/schedule/week/{week}")
def get_week(week: int):
    """Return a single week (1-18)."""
    if week < 1 or week > 18:
        raise HTTPException(status_code=404, detail="Week must be 1-18")
    data = load_schedule()
    for item in data:
        if item["week"] == week:
            return item
    raise HTTPException(status_code=404, detail="Week not found")