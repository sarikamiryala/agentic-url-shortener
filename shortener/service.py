"""Business logic: create, resolve, stats."""
from __future__ import annotations

import secrets
import string
import time
from dataclasses import dataclass
from typing import Callable

from .config import Settings
from .storage import DuplicateCode, Link, Storage
from .validation import validate_url

ALPHABET = string.ascii_letters + string.digits
MAX_CODE_ATTEMPTS = 5


class LinkNotFound(Exception):
    pass


class CodeGenerationError(Exception):
    pass


@dataclass(frozen=True)
class LinkStats:
    code: str
    url: str
    created_at: float
    clicks: int
    last_clicked_at: float | None
    top_referrers: list


class ShortenerService:
    def __init__(
        self,
        storage: Storage,
        settings: Settings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._storage = storage
        self._settings = settings
        self._clock = clock

    def _new_code(self) -> str:
        return "".join(secrets.choice(ALPHABET) for _ in range(self._settings.code_length))

    def create(self, url: str) -> Link:
        valid = validate_url(url, self._settings.max_url_length)
        for _ in range(MAX_CODE_ATTEMPTS):
            try:
                return self._storage.insert_link(self._new_code(), valid, self._clock())
            except DuplicateCode:
                continue
        raise CodeGenerationError("could not allocate a unique short code")

    def resolve(self, code: str, referrer: str | None = None, user_agent: str | None = None) -> str:
        link = self._storage.get_link(code)
        if link is None:
            raise LinkNotFound(code)
        self._storage.record_click(code, referrer, user_agent, self._clock())
        return link.url

    def stats(self, code: str) -> LinkStats:
        link = self._storage.get_link(code)
        if link is None:
            raise LinkNotFound(code)
        extra = self._storage.click_stats(code)
        return LinkStats(
            code=link.code,
            url=link.url,
            created_at=link.created_at,
            clicks=link.click_count,
            last_clicked_at=extra["last_clicked_at"],
            top_referrers=extra["top_referrers"],
        )
