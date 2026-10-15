"""Business logic: create, resolve, stats."""
from __future__ import annotations

import secrets
import string
import time
from dataclasses import dataclass
from typing import Callable

from .config import Settings
from .storage import DuplicateCode, Link, Storage
from .validation import validate_alias, validate_url

ALPHABET = string.ascii_letters + string.digits
MAX_CODE_ATTEMPTS = 5
MAX_EXPIRY_DAYS = 365
SECONDS_PER_DAY = 86_400


class LinkNotFound(Exception):
    pass


class LinkExpired(Exception):
    pass


class AliasTaken(Exception):
    pass


class InvalidExpiry(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    expires_at: float | None = None
    expired: bool = False


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

    @staticmethod
    def _expiry_seconds(expires_in_days: int | None) -> int | None:
        if expires_in_days is None:
            return None
        if isinstance(expires_in_days, bool) or not isinstance(expires_in_days, int):
            raise InvalidExpiry("expires_in_days must be an integer")
        if not 1 <= expires_in_days <= MAX_EXPIRY_DAYS:
            raise InvalidExpiry(f"expires_in_days must be between 1 and {MAX_EXPIRY_DAYS}")
        return expires_in_days * SECONDS_PER_DAY

    def create(
        self,
        url: str,
        alias: str | None = None,
        expires_in_days: int | None = None,
    ) -> Link:
        valid = validate_url(url, self._settings.max_url_length)
        ttl = self._expiry_seconds(expires_in_days)
        now = self._clock()
        expires_at = None if ttl is None else now + ttl
        if alias is not None:
            code = validate_alias(alias)
            try:
                return self._storage.insert_link(code, valid, now, expires_at)
            except DuplicateCode as exc:
                raise AliasTaken(code) from exc
        for _ in range(MAX_CODE_ATTEMPTS):
            try:
                return self._storage.insert_link(self._new_code(), valid, now, expires_at)
            except DuplicateCode:
                continue
        raise CodeGenerationError("could not allocate a unique short code")

    def _is_expired(self, link: Link) -> bool:
        return link.expires_at is not None and self._clock() >= link.expires_at

    def resolve(self, code: str, referrer: str | None = None, user_agent: str | None = None) -> str:
        link = self._storage.get_link(code)
        if link is None:
            raise LinkNotFound(code)
        if self._is_expired(link):
            raise LinkExpired(code)
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
            expires_at=link.expires_at,
            expired=self._is_expired(link),
        )
