"""Live game-status providers for the 2026 NFL pick-'em API.

Each provider's job: return {game_id: result} where result is a dict like
{winner, home_score, away_score, status, detail} — or {} when the provider
cannot produce anything right now. Providers are tried in order by live.py
and must NEVER raise; they degrade quietly on network errors.

* ESPN      — primary; official ESPN scoreboard JSON. Blocked from a few
               datacenter networks, but otherwise the best source.
* Wikipedia — fallback that parses final "Result" columns (W/L + score)
               from the 32 team-season schedule tables. Reachable from most
               networks and updates shortly after games end.
"""
import json
import os
import re
import time
import urllib.error  # noqa: F401
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEDULE_FILE = os.path.join(BASE, "data", "schedule2026.json")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko)"}

# Expanded browser-like headers to reduce blocking from provider endpoints.
HEADERS = {
    "User-Agent": UA["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/",
}

WIKI_API = "https://en.wikipedia.org/w/api.php"
ABBR_ALIAS = {"WSH": "WAS"}

# per-team wikitext cache: {abbr: (fetched_at, wikitext)}
_wiki_cache = {}
WIKI_TTL = 600  # seconds

# Game ids that already have a stored result. live.py pushes these in before
# each poll so fallback providers don't re-fetch games that are already settled.
_known_ids = set()
_enrich_ids = set()


def set_known_ids(ids, enrich_ids=()):
    """Record which games already have a stored result, and which stored finals
    still lack provider metadata that a re-fetch should backfill."""
    global _known_ids, _enrich_ids
    _known_ids = set(ids or ())
    _enrich_ids = set(enrich_ids or ())


def _load_schedule():
    with open(SCHEDULE_FILE, encoding="utf-8") as fh:
        return json.load(fh)


def _all_games():
    return [g for week in _load_schedule() for g in week["games"]]


def _game_index():
    idx = {}
    for g in _all_games():
        idx[(int(g["week"]), g["home_abbr"], g["away_abbr"])] = g["id"]
    return idx


def _season_window():
    """(start, end) covering every scheduled game from the opener through today.

    Fetching the whole played portion of the season (rather than only the week
    containing today) is what lets finals from earlier weeks keep flowing into
    the feed instead of being missed once their week scrolls past.
    """
    today = date.today()
    dates = []
    for g in _all_games():
        try:
            dates.append(date.fromisoformat(g["date"]))
        except (KeyError, ValueError):
            continue
    if not dates:
        return None
    opener = min(dates)
    if today < opener:
        return None  # season hasn't started yet
    played = [d for d in dates if d <= today]
    latest = max(played) if played else opener
    # one day of slack so a late kickoff still falls inside the range
    return opener, min(latest + timedelta(days=1), today + timedelta(days=1))


def _to_int(value):
    """int(value) or None; never raises on provider oddities."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _http_json(url, attempts=2):
    """GET a URL and parse JSON. Returns None on any failure (never raises)."""
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except Exception:  # network/HTTP errors: back off, then give up
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
    return None


def _pair_index():
    """(home, away) -> game id. A pairing never repeats inside one season."""
    return {(g["home_abbr"], g["away_abbr"]): g["id"] for g in _all_games()}


# --------------------------------------------------------------------------
# ESPN provider (primary)
# --------------------------------------------------------------------------

# Scoreboard endpoints serving the same official ESPN JSON. The site API host
# returns 403 from some networks (this one included), so the CDN host — which
# carries the same payload under content.sbData.events — is tried as well.
ESPN_HOSTS = (
    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "https://cdn.espn.com/core/nfl/scoreboard",
)


def _espn_events(payload):
    """Normalise both ESPN scoreboard shapes down to a list of events."""
    if not isinstance(payload, dict):
        return []
    events = payload.get("events")
    if isinstance(events, list) and events:
        return events
    sb = (payload.get("content") or {}).get("sbData") or {}
    events = sb.get("events")
    return events if isinstance(events, list) else []


# ESPN scoreboard is week-oriented: the CDN host only serves one week per
# request (it ignores date ranges), so started weeks are fetched individually
# and cached. Weeks whose games are all settled are immutable, so they are
# cached for good instead of being re-requested on every poll.
_espn_cache = {}          # week -> (fetched_at, events)
ESPN_WEEK_TTL = 60        # seconds to re-use a week that still has games to play
ESPN_SETTLED_TTL = 86400  # finished weeks: re-check once a day at most
ESPN_MAX_WEEKS_PER_POLL = 6  # cap per poll so a cold start stays polite


def _season_year():
    """Season year for provider queries: the year of the season opener."""
    years = []
    for g in _all_games():
        try:
            years.append(date.fromisoformat(g["date"]).year)
        except (KeyError, ValueError):
            continue
    return min(years) if years else date.today().year


def _week_dates(week):
    """(first day, last day) of a schedule week as YYYYMMDD strings."""
    days = []
    for g in _all_games():
        if int(g["week"]) != int(week):
            continue
        try:
            days.append(date.fromisoformat(g["date"]))
        except (KeyError, ValueError):
            continue
    if not days:
        return None
    return min(days).strftime("%Y%m%d"), max(days).strftime("%Y%m%d")


def _weeks_needing_fetch():
    """Started weeks that need a look, newest first.

    A week is worth fetching when it holds a game with no stored result, or a
    stored final that is still missing its ESPN metadata so it can be enriched.
    """
    now = datetime.utcnow()
    by_week = {}
    for g in _all_games():
        kick = _kickoff_utc(g)
        if kick is None or kick > now:
            continue
        by_week.setdefault(int(g["week"]), []).append(g)
    pending = [
        week for week, games in by_week.items()
        if any(g["id"] not in _known_ids or g["id"] in _enrich_ids for g in games)
    ]
    return sorted(pending, reverse=True)


def _week_settled(week):
    """True when every already-kicked-off game in the week has a result."""
    now = datetime.utcnow()
    started = [g for g in _all_games()
               if int(g["week"]) == int(week)
               and (_kickoff_utc(g) or now) <= now]
    return bool(started) and all(g["id"] in _known_ids for g in started)


def _espn_fetch_week(week):
    """Scoreboard events for one week, from the first ESPN host that answers."""
    cached = _espn_cache.get(week)
    ttl = ESPN_SETTLED_TTL if _week_settled(week) else ESPN_WEEK_TTL
    if cached and time.time() - cached[0] < ttl:
        return cached[1]

    events = []
    for host in ESPN_HOSTS:
        if "cdn.espn.com" in host:
            url = "%s?xhr=1&week=%d&year=%d&seasontype=2" % (
                host, int(week), _season_year())
        else:
            span = _week_dates(week)
            if span is None:
                continue
            url = "%s?dates=%s-%s&limit=100" % (host, span[0], span[1])
        events = _espn_events(_http_json(url))
        if events:
            break

    if events:
        _espn_cache[week] = (time.time(), events)
    return events


def _espn_result(ev, by_pair, by_week):
    """One ESPN event -> (game_id, result), or None when it cannot be used.

    Only the provider's own status decides finality: state "post" with
    `completed` set. Kickoff time is never used as a proxy for a final score,
    and `pre` games are dropped so the feed carries only live games and
    confirmed finals.
    """
    comp = (ev.get("competitions") or [{}])[0]
    home = away = None
    scores = {}
    flagged = None
    for c in comp.get("competitors") or []:
        raw = (c.get("team") or {}).get("abbreviation") or ""
        abbr = ABBR_ALIAS.get(raw, raw)
        if not abbr:
            continue
        scores[abbr] = _to_int(c.get("score"))
        if c.get("homeAway") == "home":
            home = abbr
        elif c.get("homeAway") == "away":
            away = abbr
        if c.get("winner"):
            flagged = abbr
    if not home or not away:
        return None

    week = _to_int((ev.get("week") or {}).get("number"))
    gid = by_pair.get((home, away)) or by_week.get((week, home, away))
    if not gid:
        return None

    status = (comp.get("status") or {}).get("type") or {}
    state = status.get("state")
    detail = status.get("shortDetail") or status.get("detail") or ""
    hs, aws = scores.get(home), scores.get(away)

    if state == "post" and status.get("completed", False):
        if hs is None or aws is None:
            return None  # never invent a final without both scores
        tie = hs == aws
        if tie:
            winner = None
        else:
            winner = flagged if flagged in (home, away) else (home if hs > aws else away)
        return gid, {
            "espn_id": str(ev.get("id") or ""),
            "week": week,
            "date": (ev.get("date") or "")[:10],
            "home_abbr": home,
            "away_abbr": away,
            "home_score": hs,
            "away_score": aws,
            "winner": winner,
            "loser": None if tie else (away if winner == home else home),
            "tie": tie,
            "status": "final",
            "detail": detail or "Final",
        }
    if state == "in":
        return gid, {
            "espn_id": str(ev.get("id") or ""),
            "week": week,
            "date": (ev.get("date") or "")[:10],
            "home_abbr": home,
            "away_abbr": away,
            "home_score": hs,
            "away_score": aws,
            "winner": None,
            "loser": None,
            "tie": False,
            "status": "live",
            "detail": detail or "In progress",
        }
    return None  # "pre": scheduled games never enter the results feed


def fetch_espn():
    """ESPN live scores + finals for every started week that still needs one.

    Walks back through the started weeks (newest first) so games finished in
    earlier weeks keep flowing into the feed, not just the current week.
    """
    weeks = _weeks_needing_fetch()[:ESPN_MAX_WEEKS_PER_POLL]
    if not weeks:
        return {}
    by_pair, by_week = _pair_index(), _game_index()
    out = {}
    for week in weeks:
        for ev in _espn_fetch_week(week):
            try:
                parsed = _espn_result(ev, by_pair, by_week)
            except Exception:
                continue
            if parsed:
                out[parsed[0]] = parsed[1]
    return out


# --------------------------------------------------------------------------
# Wikipedia provider (fallback — reachable on this network)
# --------------------------------------------------------------------------

_DASHY = re.compile(r"(?i)\b([WL])\s*(\d{1,3})\s*[–\-—]\s*(\d{1,3})\b")


def _wiki_get(params, attempts=3):
    url = WIKI_API + "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 429 and i < attempts - 1:
                time.sleep(3 * (i + 1))
                continue
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < attempts - 1:
                time.sleep(2 * (i + 1))
                continue
            raise
    raise last or RuntimeError("wiki request failed")


def _team_schedule_wikitext(abbr):
    """Fetch (and cache) a team article's regular-season Schedule wikitext."""
    now = time.time()
    cached = _wiki_cache.get(abbr)
    if cached and now - cached[0] < WIKI_TTL:
        return cached[1]

    title = "2026 %s season" % _TEAM_NAMES.get(abbr, abbr)
    sections = _wiki_get({"action": "parse", "page": title, "prop": "sections",
                          "format": "json", "formatversion": "2"})["parse"]["sections"]
    idx = None
    seen_regular = False
    for s in sections:
        line = (s.get("line", "") or "").strip().lower()
        if line == "regular season":
            seen_regular = True
        elif seen_regular and line in ("schedule", "regular season schedule"):
            idx = s.get("index")
            break
    if idx is None:
        for s in sections:
            if "schedule" in ((s.get("line", "") or "").strip().lower()):
                idx = s.get("index")
                break
    if idx is None:
        raise ValueError("no schedule section: %s" % abbr)
    data = _wiki_get({"action": "parse", "page": title, "section": str(idx),
                      "prop": "wikitext", "format": "json",
                      "formatversion": "2"})
    wt = data["parse"]["wikitext"]
    _wiki_cache[abbr] = (now, wt)
    return wt


def _parse_result_cell(cell):
    """Result cell like 'W 26–24' / 'L 17-24'. Returns
    (team_won, team_score, opp_score) or None."""
    m = _DASHY.search(cell or "")
    if not m:
        return None
    left, right = int(m.group(2)), int(m.group(3))
    letter = m.group(1).upper()
    if letter == "W":
        return True, left, right
    if letter == "L":
        return False, left, right
    return None


def _parse_schedule_results(wt):
    """Schedule table → {week: (team_won, team_score, opp_score)}."""
    out = {}
    wt_nc = re.sub(r"<!--.*?-->", "", wt, flags=re.S)
    for block in re.split(r"(?m)^\|-", wt_nc)[1:]:
        cells = re.findall(r"(?m)^[!|]\s?(.*)$", block)
        if not cells:
            continue
        if cells[0].lstrip().startswith("style=") or "NFLPrimaryStyle" in cells[0]:
            continue
        wm = re.search(r"\d+", cells[0])
        if not wm or "colspan" in cells[0].lower():
            continue
        week = int(wm.group(0))
        if len(cells) < 5:
            continue
        result = _parse_result_cell(cells[3])
        if result:
            out[week] = result
    return out


def _kickoff_utc(game):
    """Approximate kickoff as (naive-UTC) datetime for candidate windowing."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", game.get("date", ""))
    if not m:
        return None
    try:
        d = date.fromisoformat(m.group(1))
    except ValueError:
        return None
    hour, minute = 12, 0
    tm = re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)", game.get("time_et", ""), re.I)
    if tm:
        hour, minute = int(tm.group(1)), int(tm.group(2))
        if tm.group(3).upper() == "PM" and hour != 12:
            hour += 12
        if tm.group(3).upper() == "AM" and hour == 12:
            hour = 0
    elif game.get("time_et") != "TBD":
        return None
    dst = 4 if 4 <= d.month <= 10 else 5  # EDT / EST → UTC
    return datetime(d.year, d.month, d.day, hour, minute) + timedelta(hours=dst)


def fetch_wikipedia():
    """Final scores from team schedule 'Result' columns → {game_id: result}.

    Covers every game that has kicked off this season so earlier weeks can be
    backfilled (ESPN is primary; this is the reachable fallback). Games that
    already have a stored result are skipped, and the candidate set is capped
    so each poll stays small and polite to Wikipedia."""
    games = _all_games()
    now = datetime.utcnow()
    known = _baseline_ids() | _known_ids
    window = _season_window()
    opener = datetime.combine(window[0], datetime.min.time()) if window else now
    candidates = []
    for g in games:
        if g["id"] in known:
            continue
        kick = _kickoff_utc(g)
        if kick is None:
            continue
        # season to date: anything already kicked off is worth asking about
        if opener <= kick <= now:
            candidates.append((kick, g))
    if not candidates:
        return {}

    candidates.sort(key=lambda x: x[0], reverse=True)
    # cap the per-poll workload; ESPN covers the full history anyway
    candidates = [g for _k, g in candidates[:32]]

    teams = sorted({t for g in candidates
                    for t in (g["home_abbr"], g["away_abbr"])})
    results_by_team = {}
    for abbr in teams:
        try:
            wt = _team_schedule_wikitext(abbr)
        except Exception:
            continue
        results_by_team[abbr] = _parse_schedule_results(wt)
        time.sleep(0.25)  # keep Wikipedia happy (shorter pause)

    out = {}
    for g in candidates:
        res = _resolve_from_teams(g, results_by_team)
        if res:
            out[g["id"]] = res
    return out


def _baseline_ids():
    path = os.path.join(BASE, "data", "results2026.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {k for k, v in data.items()
                if (v or {}).get("winner") or (v or {}).get("status") == "final"}
    except OSError:
        return set()


def _resolve_from_teams(g, results_by_team):
    """Combine each side's 'Result' cell into one canonical game result."""
    week = int(g["week"])
    h = results_by_team.get(g["home_abbr"], {}).get(week)
    if h:
        team_won, team_score, opp_score = h
        winner = g["home_abbr"] if team_won else g["away_abbr"]
        home, away = (team_score, opp_score) if team_won else (opp_score, team_score)
        return {"winner": winner, "home_score": home, "away_score": away,
                "status": "final", "detail": "Final"}
    aw = results_by_team.get(g["away_abbr"], {}).get(week)
    if aw:
        team_won, team_score, opp_score = aw
        winner = g["away_abbr"] if team_won else g["home_abbr"]
        home, away = (opp_score, team_score) if team_won else (team_score, opp_score)
        return {"winner": winner, "home_score": home, "away_score": away,
                "status": "final", "detail": "Final"}
    return None


_TEAM_NAMES = {
    "ARI": "Arizona Cardinals", "ATL": "Atlanta Falcons",
    "BAL": "Baltimore Ravens", "BUF": "Buffalo Bills",
    "CAR": "Carolina Panthers", "CHI": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "CLE": "Cleveland Browns",
    "DAL": "Dallas Cowboys", "DEN": "Denver Broncos",
    "DET": "Detroit Lions", "GB": "Green Bay Packers",
    "HOU": "Houston Texans", "IND": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars", "KC": "Kansas City Chiefs",
    "LAC": "Los Angeles Chargers", "LAR": "Los Angeles Rams",
    "LV": "Las Vegas Raiders", "MIA": "Miami Dolphins",
    "MIN": "Minnesota Vikings", "NE": "New England Patriots",
    "NO": "New Orleans Saints", "NYG": "New York Giants",
    "NYJ": "New York Jets", "PHI": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers", "SEA": "Seattle Seahawks",
    "SF": "San Francisco 49ers", "TB": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "WAS": "Washington Commanders",
}

PROVIDERS = [("espn", fetch_espn), ("wikipedia", fetch_wikipedia)]

# Public alias so live.py can date-sweep stale live rows without the private name.
kickoff_utc = _kickoff_utc

__all__ = ["PROVIDERS", "ESPN_HOSTS", "fetch_espn", "fetch_wikipedia",
           "kickoff_utc", "set_known_ids"]