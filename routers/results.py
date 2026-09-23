"""Results endpoint: the finalized-game feed the frontend reads.

These values are the poller state owned by routers/live.py — the very same rows
/games/live serves — so the User Predictions view, the schedule locks and the
standings all resolve a game from one place. data/results2026.json is only the
seed the poller starts from, never a parallel source of truth.
"""
from fastapi import APIRouter

from . import live

router = APIRouter(tags=["results"])


@router.get("/results")
def get_results():
    """Final results for the games the NFL data source reports as FINAL.

    Keys are game ids (e.g. "W1-SEA-NE"); each value holds the winning
    abbreviation, the final scores and the provider status. Games present here
    are locked and cannot be predicted. Scheduled and still-in-progress games
    are never included.
    """
    return live.final_results()