"""HTTP API for the URL shortener."""
from __future__ import annotations

import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from .config import Settings
from .ratelimit import RateLimiter
from .service import (
    AliasTaken,
    CodeGenerationError,
    InvalidExpiry,
    LinkExpired,
    LinkNotFound,
    ShortenerService,
)
from .storage import Storage
from .validation import InvalidAlias, InvalidURL

_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class CreateLinkRequest(BaseModel):
    url: str
    alias: str | None = None
    expires_in_days: int | None = None


class LinkResponse(BaseModel):
    code: str
    short_url: str
    url: str
    created_at: float
    expires_at: float | None = None


class ReferrerCount(BaseModel):
    referrer: str
    clicks: int


class StatsResponse(BaseModel):
    code: str
    url: str
    created_at: float
    clicks: int
    last_clicked_at: float | None
    top_referrers: list[ReferrerCount]
    expires_at: float | None = None
    expired: bool = False


def _client_key(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    storage = Storage(settings.db_path)
    service = ShortenerService(storage, settings)
    limiter = RateLimiter(settings.rate_limit_per_minute, 60.0)

    app = FastAPI(title="URL Shortener", version="1.1.0")
    app.state.service = service
    app.state.storage = storage
    app.state.settings = settings

    @app.post("/api/links", status_code=201, response_model=LinkResponse)
    def create_link(body: CreateLinkRequest, request: Request) -> LinkResponse:
        allowed, retry_after = limiter.check(_client_key(request, settings))
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={"Retry-After": str(retry_after)},
            )
        try:
            link = service.create(body.url, alias=body.alias, expires_in_days=body.expires_in_days)
        except (InvalidURL, InvalidAlias, InvalidExpiry) as exc:
            raise HTTPException(status_code=422, detail=exc.reason) from exc
        except AliasTaken as exc:
            raise HTTPException(status_code=409, detail="alias already in use") from exc
        except CodeGenerationError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return LinkResponse(
            code=link.code,
            short_url=f"{settings.base_url}/{link.code}",
            url=link.url,
            created_at=link.created_at,
            expires_at=link.expires_at,
        )

    @app.get("/api/links/{code}/stats", response_model=StatsResponse)
    def link_stats(code: str) -> StatsResponse:
        if not _CODE_RE.match(code):
            raise HTTPException(status_code=404, detail="unknown code")
        try:
            stats = service.stats(code)
        except LinkNotFound as exc:
            raise HTTPException(status_code=404, detail="unknown code") from exc
        return StatsResponse(
            code=stats.code,
            url=stats.url,
            created_at=stats.created_at,
            clicks=stats.clicks,
            last_clicked_at=stats.last_clicked_at,
            top_referrers=[ReferrerCount(**r) for r in stats.top_referrers],
            expires_at=stats.expires_at,
            expired=stats.expired,
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    # Keep the catch-all redirect last so fixed routes win.
    @app.get("/{code}")
    def redirect(code: str, request: Request) -> RedirectResponse:
        if not _CODE_RE.match(code):
            raise HTTPException(status_code=404, detail="unknown code")
        try:
            target = service.resolve(
                code,
                referrer=request.headers.get("referer"),
                user_agent=request.headers.get("user-agent"),
            )
        except LinkNotFound as exc:
            raise HTTPException(status_code=404, detail="unknown code") from exc
        except LinkExpired as exc:
            raise HTTPException(status_code=410, detail="link has expired") from exc
        # 307 (not 301): browsers must come back to us on every click so analytics stay accurate.
        return RedirectResponse(target, status_code=307)

    return app
