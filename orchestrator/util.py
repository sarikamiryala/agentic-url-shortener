"""Small shared helpers: hashing, ids, glob matching."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from functools import lru_cache
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def digest(obj: Any) -> str:
    """Stable content hash of any JSON-able object."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short(d: str | None, n: int = 10) -> str:
    return (d or "")[:n]


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@lru_cache(maxsize=256)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob (`*`, `?`, `**`) into an anchored regex."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def glob_match(path: str, pattern: str) -> bool:
    return bool(glob_to_regex(pattern).match(path))


def matches_any(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    return any(glob_match(path, p) for p in patterns)
