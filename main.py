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

app.add_middleware(
    CORSMiddleware,
    # Set NFL_CORS_ORIGINS to your deployed frontend origin(s), comma separated,
    # e.g. NFL_CORS_ORIGINS=https://picks.example.com
    allow_origins=[o.strip() for o in os.environ.get(
        "NFL_CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    ).split(",") if o.strip()],
    # Any localhost port during development, so `vite --port 5174` just works.
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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