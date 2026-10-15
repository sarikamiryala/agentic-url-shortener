"""Combined deployment entrypoint: `uvicorn server:app`.

    /                  landing page
    /orchestrator/     agentic orchestrator dashboard and API
    /api/links, /{code}, /healthz   the URL shortener
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse

from orchestrator.api import create_app as create_orchestrator
from shortener.api import create_app as create_shortener
from shortener.config import Settings

LANDING = Path(__file__).parent / "orchestrator" / "static" / "landing.html"


def create_app(data_dir: str | None = None) -> FastAPI:
    data = Path(data_dir or os.getenv("DATA_DIR", "data"))
    data.mkdir(parents=True, exist_ok=True)
    settings = Settings.from_env()
    if "SHORTENER_DB" not in os.environ:
        settings = Settings(**{**settings.__dict__, "db_path": str(data / "shortener.db")})
    shortener = create_shortener(settings)
    orch = create_orchestrator(data_dir=str(data / "orchestrator"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with orch.router.lifespan_context(orch):  # recover interrupted runs, close the engine on exit
            yield

    app = FastAPI(title="Agentic Software Engineering System", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def landing() -> HTMLResponse:
        return HTMLResponse(LANDING.read_text(encoding="utf-8"))

    @app.get("/orchestrator", include_in_schema=False)
    def orchestrator_slash():
        return RedirectResponse("/orchestrator/", status_code=307)

    # Mounts are matched in order, so the orchestrator wins over the shortener's /{code} catch-all.
    app.mount("/orchestrator", orch)
    app.mount("/", shortener)
    return app


app = create_app()
