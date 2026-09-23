"""Pick persistence: JSON file storage behind /save-picks + /load-picks."""
import json
import os
import threading
from typing import Dict
from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PICKS_FILE = os.path.join(BASE_DIR, "data", "picks.json")

router = APIRouter(tags=["picks"])
_lock = threading.Lock()


class PickPayload(BaseModel):
    picks: Dict[str, str] = {}


def _read():
    if not os.path.exists(PICKS_FILE):
        return {"picks": {}, "updated_at": None}
    with open(PICKS_FILE, encoding="utf-8") as fh:
        return json.load(fh)


@router.post("/save-picks")
def save_picks(payload: PickPayload):
    """Placeholder storage for a user's picks: writes backend/data/picks.json."""
    record = {
        "picks": payload.picks,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    with _lock:
        with open(PICKS_FILE, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
    return {"ok": True, "count": len(payload.picks)}


@router.get("/load-picks")
def load_picks():
    """Read previously saved picks (or an empty record)."""
    return _read()