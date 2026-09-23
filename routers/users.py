"""Per-user predictions: storage, evaluation, and user-specific projections.

The project had no user model — picks were a single shared JSON blob — so this
module adds the smallest thing that satisfies the requirements on the existing
stack (FastAPI + JSON files on disk):

* every visitor gets a stable user id, taken from the ``X-User-Id`` header (the
  SPA stores one), an ``Authorization: Bearer`` value, ``?user=`` or the
  ``nfl_uid`` cookie, and generated if none is supplied;
* each user's predictions live in their own record in ``users.json``, so User A
  picking Seattle can never touch User B;
* predictions are upserted per (user, game id), which makes saving idempotent;
* verdicts and both standings tables are derived on read from the final ESPN
  game records, so nothing needs migrating when results arrive.

Endpoints:
    GET  /users/me                    identity + this user's predictions
    POST /users/me/predictions        replace this user's predictions
    PUT  /users/me/predictions/{id}   upsert one prediction ("" removes it)
    GET  /predictions/evaluation      per-game verdicts, records, standings
    GET  /standings/official          official NFL table (final games only)
    GET  /standings/projected         official + this user's unresolved picks
"""
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from . import live

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = live.DATA_DIR
USERS_FILE = os.path.join(DATA_DIR, "users.json")
# The AI model's exported picks (the same file the AI Insight panel renders);
# read-only here, so the AI prediction is never rewritten by a result.
PROJECTIONS_FILE = os.environ.get("NFL_PROJECTIONS_FILE") or os.path.join(
    os.path.dirname(BASE_DIR), "frontend", "src", "data", "projections2026.json")

COOKIE = "nfl_uid"
USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")

router = APIRouter(tags=["users"])
_lock = threading.Lock()
_projection_cache = {"mtime": None, "picks": {}}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------- identity ---

def resolve_user(request):
    """A stable id for the caller, generated when they have none yet."""
    bearer = (request.headers.get("authorization") or "").strip()
    if bearer.lower().startswith("bearer "):
        bearer = bearer[7:].strip()
    for raw in ((request.headers.get("x-user-id") or "").strip(),
                bearer,
                (request.query_params.get("user") or "").strip(),
                (request.cookies.get(COOKIE) or "").strip()):
        if raw and USER_ID_RE.match(raw):
            return raw
    return uuid.uuid4().hex


def _with_cookie(payload, uid):
    """Return the payload and make sure the caller keeps the same identity."""
    res = JSONResponse(payload)
    res.set_cookie(COOKIE, uid, max_age=60 * 60 * 24 * 365, samesite="lax")
    return res


# ---------------------------------------------------------------- storage ---

def _load():
    try:
        with open(USERS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    users = data.get("users") if isinstance(data, dict) else None
    return {
        "users": users if isinstance(users, dict) else {},
        "updated_at": data.get("updated_at") if isinstance(data, dict) else None,
    }


def _save(data):
    """Atomic write, so a crash mid-save can never corrupt every user's picks."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = "%s.%s.tmp" % (USERS_FILE, os.getpid())
    with _lock:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
        os.replace(tmp, USERS_FILE)


def _user_record(data, uid):
    rec = data["users"].get(uid)
    if not isinstance(rec, dict) or not isinstance(rec.get("picks"), dict):
        rec = {"picks": {}, "updated_at": None}
    return rec


def read_picks(uid):
    """This user's predictions: {game_id: winner_abbr}."""
    return dict(_user_record(_load(), uid)["picks"])


def write_picks(uid, picks):
    """Replace one user's predictions, leaving every other user untouched."""
    data = _load()
    rec = _user_record(data, uid)
    rec["picks"] = dict(picks)
    rec["updated_at"] = _now()
    data["users"][uid] = rec
    data["updated_at"] = rec["updated_at"]
    _save(data)
    return rec


# ------------------------------------------------------------ evaluation ----

def ai_picks():
    """{game_id: predicted_winner} from the AI model's exported projections.

    The AI prediction is only ever read here, never written, so the model's pick
    and the actual ESPN result stay separate facts.
    """
    try:
        mtime = os.path.getmtime(PROJECTIONS_FILE)
    except OSError:
        return {}
    if _projection_cache["mtime"] == mtime:
        return _projection_cache["picks"]

    picks = {}
    try:
        with open(PROJECTIONS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        for rows in (data.get("weeks") or {}).values():
            for row in rows or []:
                gid = row.get("game_id")
                winner = row.get("predicted_winner")
                if gid and winner:
                    picks[gid] = winner
    except (OSError, ValueError, AttributeError):
        picks = {}

    _projection_cache.update({"mtime": mtime, "picks": picks})
    return picks


def verdict(result, pick):
    """'correct' | 'incorrect' | 'none' once the game is final, else None.

    No pick is 'none' (the UI shows neither tick nor cross for it), a tie can
    never be a correct pick, and a scheduled or live game is not graded at all.
    """
    if not live.is_final_result(result):
        return None
    if not pick:
        return "none"
    if result.get("tie"):
        return "incorrect"
    return "correct" if result.get("winner") == pick else "incorrect"


def _score_line(result, game):
    hs, aws = result.get("home_score"), result.get("away_score")
    if hs is None or aws is None:
        return None
    home, away = game["home_abbr"], game["away_abbr"]
    if hs >= aws:
        return "%s %d - %s %d" % (home, hs, away, aws)
    return "%s %d - %s %d" % (away, aws, home, hs)


def _tally(record):
    graded = record["correct"] + record["incorrect"]
    record["graded"] = graded
    record["pct"] = round(record["correct"] / graded, 4) if graded else 0.0
    record["str"] = "%d-%d" % (record["correct"], record["incorrect"])
    return record


def projected_standings(uid, official=None):
    """Official standings plus this user's picks for games that are not final.

    The official table is never modified: this is a separate, user-specific view
    that makes the temporary effect of unresolved predictions explicit, and a
    game disappears from it the moment that game is final — from then on the real
    result owns the record.
    """
    official = official if official is not None else live.official_standings()
    games = live.games_index()
    finals = live.final_results()
    picks = read_picks(uid)

    rec = {abbr: dict(row) for abbr, row in official.items()}
    adjustments = []
    for gid, pick in picks.items():
        game = games.get(gid)
        if not game or live.is_final_result(finals.get(gid)):
            continue  # decided on the field: no longer a projection
        home, away = game["home_abbr"], game["away_abbr"]
        if pick not in (home, away):
            continue
        loser = away if pick == home else home
        rec.setdefault(pick, {"w": 0, "l": 0, "t": 0, "pct": 0.0, "str": "0-0"})
        rec.setdefault(loser, {"w": 0, "l": 0, "t": 0, "pct": 0.0, "str": "0-0"})
        rec[pick]["w"] += 1
        rec[loser]["l"] += 1
        adjustments.append({
            "game_id": gid,
            "pick": pick,
            "home_abbr": home,
            "away_abbr": away,
            "status": (finals.get(gid) or {}).get("status") or "scheduled",
            "week": game.get("week"),
            "date": game.get("date"),
        })

    for row in rec.values():
        played = row["w"] + row["l"] + row["t"]
        row["pct"] = round(row["w"] / played, 4) if played else 0.0
        row["str"] = ("%d-%d-%d" % (row["w"], row["l"], row["t"])) if row["t"] else ("%d-%d" % (row["w"], row["l"]))

    return {"official": official, "projected": rec, "adjustments": adjustments}


def evaluate(uid):
    """Everything the UI needs about this user's picks vs the final results."""
    picks = read_picks(uid)
    stored = live.all_results()          # live rows and finals alike
    finals = live.final_results()
    games = live.games_index()
    ai = ai_picks()

    record = {"correct": 0, "incorrect": 0, "none": 0}
    ai_record = {"correct": 0, "incorrect": 0, "none": 0}

    # Every finalized game, plus any game this user has a pick on.
    rows = []
    for gid in sorted(set(finals) | set(picks),
                      key=lambda g: (games.get(g, {}).get("date") or "", g)):
        game = games.get(gid)
        if not game:
            continue
        result = stored.get(gid) or {}
        is_final = live.is_final_result(result)
        user_pick = picks.get(gid) or None
        ai_pick = ai.get(gid) or None
        user_result = verdict(result, user_pick)
        ai_result = verdict(result, ai_pick)

        for bucket, value in ((record, user_result), (ai_record, ai_result)):
            if value in ("correct", "incorrect", "none"):
                bucket[value] += 1

        rows.append({
            "game_id": gid,
            "espn_id": result.get("espn_id"),
            "week": game.get("week"),
            "date": game.get("date"),
            "home_abbr": game["home_abbr"],
            "away_abbr": game["away_abbr"],
            "status": "final" if is_final else (result.get("status") or "scheduled"),
            "home_score": result.get("home_score"),
            "away_score": result.get("away_score"),
            "winner": result.get("winner"),
            "loser": result.get("loser"),
            "tie": bool(result.get("tie")),
            "final_score": _score_line(result, game) if is_final else None,
            # Same facts under the names the UI/spec talks about.
            "actualWinner": result.get("winner"),
            "actualLoser": result.get("loser"),
            "finalScore": _score_line(result, game) if is_final else None,
            "userPrediction": user_pick,
            "predictionResult": user_result,
            "aiPrediction": ai_pick,
            "aiResult": ai_result,
        })

    official = live.official_standings()
    projection = projected_standings(uid, official=official)
    return {
        "user_id": uid,
        "games": rows,
        "picks": picks,
        "record": _tally(record),
        "aiRecord": _tally(ai_record),
        "official": official,
        "projected": projection["projected"],
        "adjustments": projection["adjustments"],
    }


# ------------------------------------------------------------- endpoints ---

@router.get("/users/me")
def get_me(request: Request):
    """This caller's identity plus their stored predictions."""
    uid = resolve_user(request)
    rec = _user_record(_load(), uid)
    return _with_cookie({
        "user_id": uid,
        "picks": rec["picks"],
        "count": len(rec["picks"]),
        "updated_at": rec["updated_at"],
    }, uid)


@router.post("/users/me/predictions")
def save_predictions(request: Request, payload: dict = Body(default={})):
    """Replace this user's predictions (idempotent: same input, same state)."""
    uid = resolve_user(request)
    picks = payload.get("picks") if isinstance(payload, dict) else None
    clean = {}
    if isinstance(picks, dict):
        clean = {str(k): str(v).upper() for k, v in picks.items() if v}
    rec = write_picks(uid, clean)
    return _with_cookie({
        "user_id": uid,
        "ok": True,
        "count": len(rec["picks"]),
        "updated_at": rec["updated_at"],
    }, uid)


@router.put("/users/me/predictions/{game_id}")
def save_one_prediction(request: Request, game_id: str, payload: dict = Body(default={})):
    """Upsert a single prediction; an empty pick removes it."""
    uid = resolve_user(request)
    picks = read_picks(uid)
    pick = (payload or {}).get("pick")
    if pick:
        picks[game_id] = str(pick).upper()
    else:
        picks.pop(game_id, None)
    rec = write_picks(uid, picks)
    return _with_cookie({
        "user_id": uid,
        "ok": True,
        "game_id": game_id,
        "pick": picks.get(game_id),
        "count": len(rec["picks"]),
        "updated_at": rec["updated_at"],
    }, uid)


@router.get("/predictions/evaluation")
def get_evaluation(request: Request):
    """Per-game verdicts for this user, plus records and both standings."""
    uid = resolve_user(request)
    return _with_cookie(evaluate(uid), uid)


@router.get("/standings/official")
def get_official_standings():
    """The official NFL standings: final games only, identical for everyone."""
    return {
        "standings": live.official_standings(),
        "basis": "final games only",
        "finals": len(live.final_results()),
    }


@router.get("/standings/projected")
def get_projected_standings(request: Request):
    """Official standings plus this user's unresolved predictions."""
    uid = resolve_user(request)
    out = projected_standings(uid)
    out["user_id"] = uid
    out["basis"] = "official final results + this user's unresolved predictions"
    return _with_cookie(out, uid)


__all__ = ["router", "evaluate", "projected_standings", "ai_picks", "verdict",
           "read_picks", "write_picks", "resolve_user", "USERS_FILE"]