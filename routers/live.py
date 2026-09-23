"""Live game status: a background poller that fetches results from the
provider chain (ESPN primary, Wikipedia fallback), persists them, and exposes
/games/live + /games/status.

This module owns the **single source of truth for final results**: everything
that is final is stored here keyed by the NFL game id, /results serves the same
values, and the frontend reads them through one merge path. A game that is
final is never downgraded, so re-polling (or restarting) can never double-count
a game.
"""
import asyncio
import json
import os
import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter

from . import _providers

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Where mutable state lives. Point NFL_DATA_DIR at a persistent volume when you
# deploy, so the stored finals survive restarts and new releases.
DATA_DIR = os.environ.get("NFL_DATA_DIR") or os.path.join(BASE, "data")
LIVE_FILE = os.path.join(DATA_DIR, "live.json")
SCHEDULE_FILE = os.path.join(BASE, "data", "schedule2026.json")

router = APIRouter(tags=["live"])

POLL_SECONDS = int(os.environ.get("LIVE_POLL_SECONDS", "60"))
IDLE_POLL_SECONDS = int(os.environ.get("IDLE_POLL_SECONDS", "900"))
STALE_LIVE_HOURS = 6  # a live row older than this is a leftover, not a live game
_state = {"games": {}, "updated_at": None, "provider": None, "last_error": None}
_lock = threading.Lock()


def _load_schedule_index():
    """All 272 games keyed by game id, for kickoff lookups."""
    try:
        with open(SCHEDULE_FILE, encoding="utf-8") as fh:
            schedule = json.load(fh)
    except OSError:
        return {}
    return {g["id"]: g for week in schedule for g in week.get("games", [])}


_GAMES = _load_schedule_index()


def _now_naive_utc():
    """Naive UTC 'now', matching _providers.kickoff_utc()."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_final(result):
    """True when a stored row is a finished game (a tie has no winner)."""
    if not result:
        return False
    return result.get("status") == "final" or bool(result.get("winner"))


def _merge_result(old, new):
    """Fold one provider row into what is already stored for that game.

    Finals are terminal for the *result*: a later or staler response can never
    replace a confirmed winner or scoreline, which is what keeps one finished
    game contributing exactly one win and one loss however often we poll. A later
    poll may still carry extra metadata (the ESPN id, the loser, both team
    abbreviations), so any field we do not already have is backfilled from it.
    """
    if not old:
        return new
    if _is_final(old):
        merged = dict(old)
        if _is_final(new):
            for key, value in new.items():
                if merged.get(key) in (None, "") and value not in (None, ""):
                    merged[key] = value
        return merged
    return new


def _unresolved_started(games):
    """Ids of games that have kicked off but still have no final result."""
    now = _now_naive_utc()
    out = []
    for gid, game in _GAMES.items():
        kick = _providers.kickoff_utc(game)
        if kick is None or kick > now:
            continue
        if not _is_final(games.get(gid)):
            out.append(gid)
    return out


def _prune_stale_live(games):
    """Drop live rows for games that kicked off hours ago (server restarts)."""
    now = _now_naive_utc()
    for gid, res in list(games.items()):
        if (res or {}).get("status") != "live":
            continue
        kick = _providers.kickoff_utc(_GAMES.get(gid) or {})
        if kick is not None and (now - kick) > timedelta(hours=STALE_LIVE_HOURS):
            games.pop(gid, None)
    return games


def _next_delay():
    """Poll fast while football is happening, slowly while nothing is on.

    Live games and final-whistles are what need 60s resolution; overnight and
    mid-week there is nothing to learn, so the poller backs off and stays well
    inside provider rate limits.
    """
    now = _now_naive_utc()
    if any((r or {}).get("status") == "live" for r in _state["games"].values()):
        return POLL_SECONDS
    for gid, game in _GAMES.items():
        kick = _providers.kickoff_utc(game)
        if kick is None or _is_final(_state["games"].get(gid)):
            continue
        if timedelta(0) <= kick - now <= timedelta(hours=2):
            return POLL_SECONDS  # kickoff imminent
        if timedelta(0) <= now - kick <= timedelta(hours=6):
            return POLL_SECONDS  # kicked off recently, result due
    return IDLE_POLL_SECONDS


def _now_iso():
    return (datetime.now(timezone.utc)
            .isoformat(timespec="seconds").replace("+00:00", "Z"))


def _persist():
    data = {
        "games": _state["games"],
        "updated_at": _state["updated_at"],
        "provider": _state["provider"],
        "last_error": _state["last_error"],
        "poll_seconds": POLL_SECONDS,
    }
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LIVE_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
    except OSError:
        pass


def _load_cache():
    """Restore persisted state on boot so the API has data before first poll."""
    if _state["updated_at"]:
        return
    try:
        with open(LIVE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("games"):
            with _lock:
                _state["games"] = data.get("games", {})
                _state["updated_at"] = data.get("updated_at")
                _state["provider"] = data.get("provider")
    except OSError:
        pass


def sync_once():
    """Run the provider chain and fold fresh data into the stored results.

    Never raises. The accumulated state (not just this poll's response) is the
    starting point, so a final discovered earlier is never lost when a provider
    later omits that week. Because rows are keyed by game id and finals are
    terminal, repeated polls converge on the same answer instead of stacking up.
    """
    _load_cache()
    with _lock:
        merged = dict(_state["games"])
    restored = bool(merged)  # warm cache: finals survive a restart
    last_error = None
    provider = None

    def absorb(data, name):
        """Merge one provider payload; returns how many rows it offered."""
        count = 0
        for gid, res in (data or {}).items():
            if not isinstance(res, dict):
                continue
            merged[gid] = _merge_result(merged.get(gid), res)
            count += 1
        return count

    if absorb(_baseline_results(), "baseline"):
        # "cache" means what we already had is enough; "baseline" means only the
        # seed file was available, which tells the frontend to warn.
        provider = "cache" if restored else "baseline"

    # Tell the providers which games are already settled (so fallbacks don't
    # re-fetch history) and which stored finals are still missing ESPN metadata
    # (so a single poll can backfill the id/loser into rows written earlier).
    _providers.set_known_ids(
        [gid for gid, res in merged.items() if _is_final(res)],
        enrich_ids=[gid for gid, res in merged.items()
                    if _is_final(res) and not res.get("espn_id")],
    )

    for name, fetcher in _providers.PROVIDERS:
        try:
            data = fetcher()
        except Exception as exc:  # noqa: BLE001
            last_error = "%s: %s" % (name, exc)
            continue
        if absorb(data, name) and provider in (None, "baseline"):
            provider = name
        # Stop once every started game is settled: the remaining (slower)
        # fallbacks have nothing left to add.
        if not _unresolved_started(merged):
            break

    _prune_stale_live(merged)
    with _lock:
        _state["games"] = merged
        _state["provider"] = provider or "none"
        _state["last_error"] = last_error
        _state["updated_at"] = _now_iso()
    _persist()
    return merged


def _baseline_results():
    """Confirmed finals from results2026.json seed the live feed, so the
    endpoint is populated even before the first provider poll succeeds."""
    path = os.path.join(BASE, "data", "results2026.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {k: v for k, v in data.items()
                if (v or {}).get("winner") or (v or {}).get("status") == "final"}
    except OSError:
        return {}


async def poll_loop():
    await asyncio.to_thread(sync_once)  # warm start
    while True:
        # fast while games are live or about to be, slow while nothing is on
        await asyncio.sleep(_next_delay())
        try:
            await asyncio.to_thread(sync_once)
        except Exception:  # noqa: BLE001
            pass


def final_results():
    """The one feed of finalized results (game id -> final result row).

    /results and /games/live both serve from this state, so the User
    Predictions view and the standings can never disagree about a final.
    """
    _load_cache()
    with _lock:
        games = dict(_state["games"])
    return {gid: res for gid, res in games.items() if _is_final(res)}


def all_results():
    """Every stored row (live games and finals) keyed by game id.

    The evaluation endpoint needs live scores as well as finals, and this is the
    same state `/games/live` reports, so there is still one source of truth.
    """
    _load_cache()
    with _lock:
        return dict(_state["games"])


def games_index():
    """Every scheduled game keyed by game id (public accessor for routers)."""
    return _GAMES


# Public alias: routers read the stored rows without the private name.
is_final_result = _is_final


def official_standings():
    """The official NFL W-L-T, built from FINAL games only, each counted once.

    This is the table every visitor must see identically. It never looks at a
    user's picks, a scheduled game or a game still in progress, and because it is
    a pure fold over the stored final rows (keyed by game id) it can be rebuilt
    from scratch at any time — including after a redeploy — without ever
    double-counting a game.
    """
    rec = {}
    for gid, res in final_results().items():
        game = _GAMES.get(gid)
        if not game or not _is_final(res):
            continue
        home, away = game["home_abbr"], game["away_abbr"]
        for abbr in (home, away):
            rec.setdefault(abbr, {"w": 0, "l": 0, "t": 0})

        if res.get("tie"):
            rec[home]["t"] += 1
            rec[away]["t"] += 1
            continue
        winner = res.get("winner")
        if winner not in (home, away):
            continue
        loser = away if winner == home else home
        rec[winner]["w"] += 1
        rec[loser]["l"] += 1

    for r in rec.values():
        played = r["w"] + r["l"] + r["t"]
        r["pct"] = round(r["w"] / played, 4) if played else 0.0
        r["str"] = ("%d-%d-%d" % (r["w"], r["l"], r["t"])) if r["t"] else ("%d-%d" % (r["w"], r["l"]))
    return rec


@router.get("/games/live")
def get_live():
    """Latest live status + final results, refreshed by the poller."""
    _load_cache()
    with _lock:
        games = dict(_state["games"])
        updated_at = _state["updated_at"]
        provider = _state["provider"]
        last_error = _state["last_error"]
    return {
        "games": games,
        "updated_at": updated_at,
        "provider": provider,
        "last_error": last_error,
        "poll_seconds": POLL_SECONDS,
        "next_poll_seconds": _next_delay(),
        "count": len(games),
        "finals": sum(1 for r in games.values() if _is_final(r)),
        "live": sum(1 for r in games.values() if (r or {}).get("status") == "live"),
        "unresolved_started": len(_unresolved_started(games)),
    }


@router.get("/games/finals")
def get_finals():
    """Only the finalized games, served from the same state as /games/live."""
    return final_results()


@router.get("/games/status")
def get_status():
    """Alias for /games/live."""
    return get_live()


__all__ = ["router", "poll_loop", "sync_once", "get_live", "get_status",
           "final_results", "all_results", "official_standings", "games_index",
           "is_final_result", "POLL_SECONDS", "IDLE_POLL_SECONDS", "DATA_DIR",
           "LIVE_FILE"]