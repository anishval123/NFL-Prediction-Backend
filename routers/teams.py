"""Team endpoints: roster metadata + team-specific schedules."""
import json
import os

from fastapi import APIRouter, HTTPException

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEAMS_FILE = os.path.join(BASE_DIR, "data", "teams.json")
SCHED_FILE = os.path.join(BASE_DIR, "data", "schedule2026.json")

router = APIRouter(tags=["teams"])


def load_teams():
    with open(TEAMS_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def load_schedule():
    with open(SCHED_FILE, encoding="utf-8") as fh:
        return json.load(fh)


@router.get("/teams")
def get_teams():
    """Return metadata for all 32 NFL teams."""
    return load_teams()


@router.get("/team/{abbr}")
def get_team(abbr: str):
    """Return a single team plus its full 2026 schedule."""
    abbr = abbr.upper()
    teams = load_teams()
    team = next((t for t in teams if t["abbr"] == abbr), None)
    if team is None:
        raise HTTPException(status_code=404, detail="Unknown team: %s" % abbr)

    games = []
    for week in load_schedule():
        for game in week["games"]:
            if abbr in (game["home_abbr"], game["away_abbr"]):
                games.append(game)

    return {"team": team, "games": games}