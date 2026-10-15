"""Knowledge the agents work from: feature definitions, API deltas, task templates
and the reference implementations ("packs") the implementation agent materialises.

This is the deterministic stand-in for an LLM. The pack files under `catalog/` are
real, tested code written ahead of time; agents select, parameterise and apply them
under the orchestrator's gates. Swapping in an LLM-backed agent means replacing the
`impl_steps` / `test_files` calls, not the orchestration.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FEATURE_ORDER = [
    "shorten", "redirect", "analytics", "validation", "rate_limit", "custom_alias", "expiry", "link_safety",
]
CORE_FEATURES = {"shorten", "redirect", "analytics", "validation", "rate_limit"}

FEATURES: dict[str, dict] = {
    "shorten": {
        "title": "Create short links",
        "fr": "Users can create a short link for a long URL via POST /api/links.",
        "acceptance": [
            "A valid http(s) URL returns 201 with code, short_url and url",
            "Codes are unique, 7 characters, base62",
            "A malformed or non-http(s) URL returns 422 with a reason",
        ],
    },
    "redirect": {
        "title": "Redirect",
        "fr": "Anyone can open GET /{code} and be redirected to the original URL.",
        "acceptance": [
            "Known code returns 307 with a Location header",
            "Unknown code returns 404",
        ],
    },
    "analytics": {
        "title": "Click analytics",
        "fr": "Link owners can read click analytics via GET /api/links/{code}/stats.",
        "acceptance": [
            "Every redirect records one click",
            "Stats expose total clicks, last click time and top referrers",
            "Unknown code returns 404",
        ],
    },
    "validation": {
        "title": "Input validation",
        "fr": "Destination URLs are validated before a link is stored.",
        "acceptance": [
            "Only http and https schemes; max 2048 characters",
            "No embedded credentials; no localhost or private-network hosts",
        ],
    },
    "rate_limit": {
        "title": "Rate limiting",
        "fr": "Link creation is rate limited per client.",
        "acceptance": [
            "Exceeding the limit returns 429 with a Retry-After header",
            "Redirects are not rate limited",
        ],
    },
    "custom_alias": {
        "title": "Custom aliases",
        "fr": "Users can choose their own short code (alias) when creating a link.",
        "acceptance": [
            "alias is optional; 3-32 characters from [A-Za-z0-9_-]",
            "An alias already in use returns 409; the original link is unchanged",
            "Aliases that shadow service routes (api, healthz, orchestrator, ...) are rejected with 422",
        ],
    },
    "expiry": {
        "title": "Link expiry",
        "fr": "Users can make a link expire after a number of days.",
        "acceptance": [
            "expires_in_days is optional; integer 1-365; omitted means the link never expires",
            "An expired link returns 410 and no longer records clicks",
            "Links created before this change keep working and never expire",
        ],
    },
    "link_safety": {
        "title": "Safer links",
        "fr": "Links whose destination is on a blocklist are refused.",
        "acceptance": [
            "A blocklisted destination is rejected at creation with 422",
            "The blocklist is configurable without a code change",
        ],
    },
}

# ---- API surface ------------------------------------------------------------------------
BASE_ENDPOINTS: dict[str, list[dict]] = {
    "shorten": [
        {
            "method": "POST", "path": "/api/links", "summary": "Create a short link",
            "request": {"url": "string, required: http(s) destination"},
            "responses": {"201": "Created: {code, short_url, url, created_at}", "422": "Invalid URL (reason in detail)"},
        }
    ],
    "redirect": [
        {
            "method": "GET", "path": "/{code}", "summary": "Redirect to the original URL",
            "request": {}, "responses": {"307": "Temporary redirect", "404": "Unknown code"},
        }
    ],
    "analytics": [
        {
            "method": "GET", "path": "/api/links/{code}/stats", "summary": "Click analytics for a link",
            "request": {}, "responses": {"200": "Stats: clicks, last_clicked_at, top_referrers", "404": "Unknown code"},
        }
    ],
}
HEALTH = {
    "method": "GET", "path": "/healthz", "summary": "Liveness probe",
    "request": {}, "responses": {"200": "{status: ok}"},
}

DELTAS: dict[str, list[tuple[str, str, dict]]] = {
    "rate_limit": [("POST", "/api/links", {"responses": {"429": "Rate limit exceeded (Retry-After header)"}})],
    "custom_alias": [
        (
            "POST", "/api/links",
            {
                "request": {"alias": "string, optional: 3-32 chars [A-Za-z0-9_-], not a reserved word"},
                "responses": {"409": "Alias already in use"},
            },
        )
    ],
    "expiry": [
        ("POST", "/api/links", {"request": {"expires_in_days": "integer, optional: 1-365"}}),
        ("GET", "/{code}", {"responses": {"410": "Link expired"}}),
    ],
    "link_safety": [("POST", "/api/links", {"responses": {"422": "Invalid URL, or destination is blocklisted"}})],
}

ROUTE_FEATURE = {
    ("POST", "/api/links"): "shorten",
    ("GET", "/{code}"): "redirect",
    ("GET", "/api/links/{code}/stats"): "analytics",
}


def compose_contract(features: set[str], safety: dict | None = None) -> dict:
    """Merge base endpoints and per-feature deltas into the full API surface."""
    endpoints: dict[tuple[str, str], dict] = {}
    for f in FEATURE_ORDER:
        if f in features:
            for ep in BASE_ENDPOINTS.get(f, []):
                endpoints[(ep["method"], ep["path"])] = copy.deepcopy(ep)
    endpoints[(HEALTH["method"], HEALTH["path"])] = copy.deepcopy(HEALTH)
    for f in FEATURE_ORDER:
        if f not in features:
            continue
        for method, path, delta in DELTAS.get(f, []):
            ep = endpoints.get((method, path))
            if ep is None:
                continue
            ep["request"].update(delta.get("request", {}))
            ep["responses"].update(delta.get("responses", {}))
    if safety and safety.get("enforce") == "create_and_redirect" and ("GET", "/{code}") in endpoints:
        endpoints[("GET", "/{code}")]["responses"]["403"] = "Destination blocklisted (checked at redirect time)"
    ordered = sorted(endpoints.values(), key=lambda e: (e["path"], e["method"]))
    return {"endpoints": ordered}


# ---- task templates ----------------------------------------------------------------------
def _t(id: str, title: str, stage: str, module: str, deps: tuple[str, ...] = ()) -> dict:
    return {"id": id, "title": title, "stage": stage, "module": module, "depends_on": list(deps)}


def pack_tasks(pack: str, spec: dict | None = None) -> list[dict]:
    spec = spec or {}
    if pack == "core_service":
        return [
            _t("T1", "Persistence layer: SQLite schema, parameterised queries, atomic click recording", "implementation", "shortener/storage.py"),
            _t("T2", "URL validation: scheme, length, credentials, private hosts", "implementation", "shortener/validation.py"),
            _t("T3", "Settings loaded from environment", "implementation", "shortener/config.py"),
            _t("T4", "Sliding-window rate limiter", "implementation", "shortener/ratelimit.py"),
            _t("T5", "Service: collision-safe code allocation, resolve, stats", "implementation", "shortener/service.py", ("T1", "T2", "T3")),
            _t("T6", "HTTP API: create, redirect, stats, health", "implementation", "shortener/api.py", ("T4", "T5")),
            _t("T7", "Tests mapped to features (unit and API)", "tests", "tests/"),
            _t("T8", "API reference and README generated from the contract", "docs", "docs/"),
        ]
    if pack == "aliases_expiry":
        return [
            _t("T1", "Schema: add nullable expires_at to links, with an in-place migration for existing databases", "implementation", "shortener/storage.py"),
            _t("T2", "Alias validation: format, length, reserved route names", "implementation", "shortener/validation.py"),
            _t("T3", "Service: alias-aware create, expiry-aware resolve and stats", "implementation", "shortener/service.py", ("T1", "T2")),
            _t("T4", "API: alias and expires_in_days fields; 409 and 410 responses", "implementation", "shortener/api.py", ("T3",)),
            _t("T5", "Tests for aliases and expiry (contract-level)", "tests", "tests/"),
            _t("T6", "Regenerate API reference", "docs", "docs/"),
        ]
    if pack == "safer_links":
        tasks = [
            _t("T1", "Blocklist matcher (built-in demo entries, env/file extensions)", "implementation", "shortener/safety.py"),
            _t("T2", "Settings: blocklist entries and file path", "implementation", "shortener/config.py"),
            _t("T3", "Service: refuse blocklisted destinations at creation", "implementation", "shortener/service.py", ("T1", "T2")),
        ]
        if spec.get("enforce") == "create_and_redirect":
            tasks.append(_t("T4", "Service: re-check destination at redirect time", "implementation", "shortener/service.py", ("T3",)))
        tasks.append(_t("T5", "API: map blocked destinations to 422 (create)" + (" and 403 (redirect)" if spec.get("enforce") == "create_and_redirect" else ""), "implementation", "shortener/api.py", ("T3",)))
        tasks.append(_t("T6", "Tests for blocklist behaviour", "tests", "tests/"))
        tasks.append(_t("T7", "Regenerate API reference", "docs", "docs/"))
        return tasks
    return []


def select_pack(features: set[str], mode: str) -> str | None:
    f = set(features)
    if mode == "greenfield" and f and f <= CORE_FEATURES and "shorten" in f:
        return "core_service"
    if mode == "brownfield":
        if f == {"custom_alias", "expiry"}:
            return "aliases_expiry"
        if f == {"link_safety"}:
            return "safer_links"
    return None


# ---- reference implementations -----------------------------------------------------------
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _subst_markers(text: str, markers: dict[str, str]) -> str:
    """Replace `# @@NAME@@` marker lines with an (indented) block, or drop the line."""
    for name, block in markers.items():
        pat = re.compile(rf"^(?P<indent>[ \t]*)# @@{name}@@\n", re.M)

        def repl(m: re.Match, block: str = block) -> str:
            if not block:
                return ""
            ind = m.group("indent")
            return "".join(f"{ind}{line}\n" if line.strip() else "\n" for line in block.splitlines())

        text = pat.sub(repl, text)
    return text


def impl_steps(pack: str, strategy: str, spec: dict, repo_root: Path = REPO_ROOT) -> list[dict]:
    """Ordered implementation steps: [{task, files: {path: content}}]."""
    if pack == "core_service":
        src = repo_root / "shortener"

        def f(name: str) -> dict[str, str]:
            return {f"shortener/{name}": _read(src / name)}

        return [
            {"task": "T1", "files": f("storage.py")},
            {"task": "T2", "files": f("validation.py")},
            {"task": "T3", "files": f("config.py")},
            {"task": "T4", "files": f("ratelimit.py")},
            {"task": "T5", "files": f("service.py")},
            {"task": "T6", "files": {**f("api.py"), **f("main.py"), **f("__init__.py")}},
        ]
    if pack == "aliases_expiry":
        base = repo_root / "catalog" / "aliases_expiry"
        storage = "storage_migration_aware.py" if strategy == "migration_aware" else "storage_direct.py"
        return [
            {"task": "T1", "files": {"shortener/storage.py": _read(base / storage)}},
            {"task": "T2", "files": {"shortener/validation.py": _read(base / "validation.py")}},
            {"task": "T3", "files": {"shortener/service.py": _read(base / "service.py")}},
            {"task": "T4", "files": {"shortener/api.py": _read(base / "api.py")}},
        ]
    if pack == "safer_links":
        base = repo_root / "catalog" / "safer_links"
        redirect = spec.get("enforce") == "create_and_redirect"
        match = spec.get("match", "host_and_subdomains")
        service_block = (
            "if self._blocklist.is_blocked(link.url):\n"
            "    raise BlockedDestination(\"destination is on the blocklist\")"
            if redirect
            else ""
        )
        api_block = (
            "except BlockedDestination as exc:\n"
            "    raise HTTPException(status_code=403, detail=\"destination is blocked\") from exc"
            if redirect
            else ""
        )
        safety = _read(base / "safety.py").replace("@@MATCH_MODE@@", match)
        service = _subst_markers(_read(base / "service.py"), {"REDIRECT_CHECK": service_block})
        api = _subst_markers(_read(base / "api.py"), {"REDIRECT_CHECK": api_block})
        return [
            {"task": "T1", "files": {"shortener/safety.py": safety}},
            {"task": "T2", "files": {"shortener/config.py": _read(base / "config.py")}},
            {"task": "T3", "files": {"shortener/service.py": service}},
            {"task": "T5", "files": {"shortener/api.py": api}},
        ]
    raise KeyError(pack)


def authored_tests(pack: str, spec: dict, repo_root: Path = REPO_ROOT) -> dict[str, str]:
    if pack == "core_service":
        d = repo_root / "tests" / "product"
        return {f"tests/{p.name}": _read(p) for p in sorted(d.glob("*.py"))}
    if pack == "aliases_expiry":
        p = repo_root / "catalog" / "aliases_expiry" / "tests" / "test_aliases_expiry.py"
        return {"tests/test_aliases_expiry.py": _read(p)}
    if pack == "safer_links":
        base = repo_root / "catalog" / "safer_links" / "tests"
        match = spec.get("match", "host_and_subdomains")
        files = {"tests/test_safer_links.py": _read(base / "test_safer_links.py").replace("@@MATCH_MODE@@", match)}
        if spec.get("enforce") == "create_and_redirect":
            files["tests/test_safer_links_redirect.py"] = _read(base / "test_safer_links_redirect.py")
        return files
    raise KeyError(pack)


# ---- risk templates ----------------------------------------------------------------------
def risks_for(features: set[str], tags: set[str], safety: dict | None = None) -> list[dict]:
    r: list[dict] = []

    def add(risk: str, impact: str, mitigation: str, validated_by: str, note: str = "") -> None:
        r.append(
            {
                "id": f"R-{len(r) + 1}",
                "risk": risk,
                "impact": impact,
                "mitigation": mitigation,
                "validated_by": validated_by,
                "note": note,
            }
        )

    if "new_system" in tags:
        add("Link creation can be abused to generate unbounded data", "medium", "Per-client rate limit on POST /api/links", "verification")
        add("SQL injection through short codes or URLs", "high", "Parameterised queries only; static scan rejects string-built SQL", "security_review")
        add("Public shortener can be used to hide phishing destinations", "high", "Not solved here; link-safety work is the natural follow-up", "accepted",
            "Known limitation of any open shortener; needs a blocklist/reputation policy decision")
        add("SQLite single-writer and in-memory rate limiter limit scale and reset on restart", "medium", "Single-node by design; swap storage/limiter behind existing interfaces to scale", "accepted",
            "Prototype scope")
    if "schema_change" in tags:
        add("Existing databases lack the new column; new code could fail at startup or first query", "high",
            "In-place, idempotent migration; verified by opening a database created by the previous version", "migration_check")
    if "public_api_change" in tags and "new_system" not in tags:
        add("Existing clients break because response shapes or status codes change", "high",
            "Only additive request/response fields; the full existing test suite must still pass", "verification")
    if "custom_alias" in features:
        add("Alias shadows a service route such as /api or /healthz", "high", "Reserved-word list checked case-insensitively", "verification")
    if "expiry" in features:
        add("Expiry compared against the wrong clock or unit", "medium", "Epoch seconds from one injectable clock; boundary tests", "verification")
    if "link_safety" in features:
        add("Blocklist false positives block legitimate destinations", "medium", "List is configurable; matching mode chosen by a human", "accepted",
            "Operator owns list quality")
        add("A static list cannot detect newly registered malicious domains", "medium", "Out of scope; documented limitation", "accepted",
            "Needs an external reputation service")
        if (safety or {}).get("enforce") != "create_and_redirect":
            add("Links created before a domain was blocklisted keep redirecting", "medium", "Not mitigated when enforcement is creation-only", "accepted",
                "Human chose creation-only enforcement; revisit if abuse is observed")
    add("Generated code runs only in a subprocess with a timeout; this is not a security sandbox", "medium",
        "Run the orchestrator in a disposable container for untrusted agents", "accepted", "Prototype scope")
    return r
