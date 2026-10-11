"""
2026 NFL Predictions API.

Minimal JSON-backed FastAPI backend that serves the team roster and the full
(official, already-released) 2026 schedule, plus simple pick persistence and a
live game-status poller (ESPN + Wikipedia providers) exposed at /games/live.

Run with:
    uvicorn main:app --reload --port 8000
"""
import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import live, picks, results, schedule, teams, users

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Start the live-result poller (first sync runs immediately in a thread)
    task = asyncio.create_task(live.poll_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="2026 NFL Predictions API",
    description="Schedule + team data for the 2026 NFL season, "
                "with JSON pick storage and live game-status updates. "
                "Data lives in backend/data/*.json.",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS. Two client origins matter: local development on any localhost port, and
# the deployed Vercel site. Vercel serves production from a project domain and
# previews from <project>-<hash>.vercel.app, so the shipped defaults cover exact
# production origins plus this project's own preview hostnames, without opening
# the API to every site on the internet.
#
# Env knobs:
#   NFL_CORS_ORIGINS       comma separated exact origins. REPLACES the default
#                          list, so add your own domain here if you use one.
#   NFL_CORS_ORIGIN_REGEX  EXTRA pattern, added to the built-in ones. It is
#                          additive on purpose: a stale or blank value in a host's
#                          environment can never lock the deployed site out, which
#                          is exactly how the Vercel site ended up unable to read
#                          any results.
DEFAULT_CORS_ORIGINS = "https://nfl-prediction-frontend.vercel.app," \
                       "https://nfl-predictions.vercel.app," \
                       "http://localhost:5173,http://127.0.0.1:5173"

DEFAULT_CORS_ORIGIN_REGEX = (
    # any localhost / loopback port, for `npm run dev`
    r"http://(localhost|127\.0\.0\.1)(:\d+)?"
    # this project's Vercel production domain and preview deployments
    r"|https://nfl-prediction[a-z0-9-]*\.vercel\.app"
    r"|https://nfl-predictions[a-z0-9-]*\.vercel\.app"
)

_extra_origin_regex = os.environ.get("NFL_CORS_ORIGIN_REGEX", "").strip()
CORS_ORIGIN_REGEX = (
    f"(?:{DEFAULT_CORS_ORIGIN_REGEX})|(?:{_extra_origin_regex})"
    if _extra_origin_regex else DEFAULT_CORS_ORIGIN_REGEX
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.environ.get(
        "NFL_CORS_ORIGINS",
        DEFAULT_CORS_ORIGINS,
    ).split(",") if o.strip()],
    allow_origin_regex=CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    # The SPA sends Content-Type, Cache-Control and X-User-Id, so any header must
    # be allowed or the browser's preflight fails.
    allow_headers=["*"],
    # Cache the preflight for 10 minutes so polling does not send an OPTIONS
    # request on every tick.
    max_age=600,
)

app.include_router(schedule.router)
app.include_router(teams.router)
app.include_router(results.router)
app.include_router(picks.router)
app.include_router(users.router)
app.include_router(live.router)


@app.get("/", tags=["meta"])
def root():
    return {
        "name": "2026 NFL Predictions API",
        "version": "1.0.0",
        "endpoints": {
            "GET /schedule": "full 2026 schedule (18 weeks, 272 games)",
            "GET /schedule/week/{week}": "one week of games",
            "GET /teams": "all 32 teams with metadata",
            "GET /team/{abbr}": "team metadata + its 17-game schedule",
            "GET /results": "final results only (same feed /games/live serves)",
            "GET /games/live": "live status + results (auto-refreshed)",
            "GET /games/finals": "finalized games only",
            "GET /standings/official": "official NFL table (final games only)",
            "GET /standings/projected": "official + this user's unresolved picks",
            "GET /predictions/evaluation": "per-game verdicts, records, both tables",
            "GET /users/me": "caller identity + this user's stored predictions",
            "POST /users/me/predictions": "replace this user's predictions",
            "PUT /users/me/predictions/{game_id}": "upsert one prediction",
            "GET /games/status": "alias for /games/live",
            "POST /save-picks": "persist {picks: {game_id: winner_abbr}}",
            "GET /load-picks": "read stored picks",
        },
        "data_dir": os.path.join(BASE_DIR, "data"),
    }